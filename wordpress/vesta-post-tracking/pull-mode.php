<?php
// Pull-mode transport for Vesta Smart Post Tracking 2.3.2+.
// WordPress initiates the connection to the bot. This avoids the unreliable
// bot -> Iran-host/WAF path entirely for website tracking imports.

if (!defined('ABSPATH')) {
    exit;
}

function vpt_pull_base_url() {
    return 'https://ordersvesta.smarbiz.sbs';
}

function vpt_pull_b64url($data) {
    return rtrim(strtr(base64_encode($data), '+/', '-_'), '=');
}

function vpt_pull_direct_get($url, $timeout = 12) {
    // Raw cURL is preferred so WordPress HTTP filters/boosters cannot rewrite or
    // blacklist the bot endpoint. WP HTTP API remains a fallback only.
    if (function_exists('curl_init')) {
        $ch = curl_init($url);
        curl_setopt_array($ch, array(
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_CONNECTTIMEOUT => min(6, (int) $timeout),
            CURLOPT_TIMEOUT => (int) $timeout,
            CURLOPT_SSL_VERIFYPEER => true,
            CURLOPT_SSL_VERIFYHOST => 2,
            CURLOPT_HTTPHEADER => array(
                'Accept: application/json',
                'Cache-Control: no-cache',
                'User-Agent: VestaPostTrackingPull/2.3.2',
            ),
        ));
        $body = curl_exec($ch);
        $error = curl_error($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $remote = (string) curl_getinfo($ch, CURLINFO_PRIMARY_IP);
        curl_close($ch);
        if ($body === false || $error) {
            throw new Exception('Bot pull cURL: ' . ($error ?: 'unknown error'));
        }
        if ($code < 200 || $code >= 300) {
            throw new Exception('Bot pull HTTP ' . $code . ($remote ? ' via ' . $remote : '') . ': ' . substr((string) $body, 0, 300));
        }
        return (string) $body;
    }

    $response = wp_remote_get($url, array(
        'timeout' => (int) $timeout,
        'redirection' => 2,
        'headers' => array('Accept' => 'application/json'),
    ));
    if (is_wp_error($response)) {
        throw new Exception('Bot pull WP HTTP: ' . $response->get_error_message());
    }
    $code = (int) wp_remote_retrieve_response_code($response);
    $body = (string) wp_remote_retrieve_body($response);
    if ($code < 200 || $code >= 300) {
        throw new Exception('Bot pull HTTP ' . $code . ': ' . substr($body, 0, 300));
    }
    return $body;
}

function vpt_pull_token() {
    return function_exists('vbb_get_token') ? (string) vbb_get_token() : '';
}

function vpt_pull_health_check() {
    $raw = vpt_pull_direct_get(vpt_pull_base_url() . '/health', 8);
    $decoded = json_decode($raw, true);
    if (!is_array($decoded) || ($decoded['status'] ?? '') !== 'ok') {
        throw new Exception('Bot health response is invalid: ' . substr($raw, 0, 250));
    }
    return $decoded;
}

function vpt_pull_fetch_job() {
    $token = vpt_pull_token();
    if ($token === '') {
        throw new Exception('Vesta Bot Bridge token is unavailable in WordPress.');
    }
    $t = (string) time();
    $n = bin2hex(random_bytes(16));
    $s = hash_hmac('sha256', 'pull|' . $t . '|' . $n, $token);
    $url = add_query_arg(array('t' => $t, 'n' => $n, 's' => $s), vpt_pull_base_url() . '/tracking-pull');
    $raw = vpt_pull_direct_get($url, 12);
    $decoded = json_decode($raw, true);
    if (!is_array($decoded) || empty($decoded['success'])) {
        throw new Exception('Invalid bot pull response: ' . substr($raw, 0, 300));
    }
    return isset($decoded['job']) && is_array($decoded['job']) ? $decoded['job'] : null;
}

function vpt_pull_ack($job_id, $result) {
    $token = vpt_pull_token();
    if ($token === '') {
        throw new Exception('Vesta Bot Bridge token is unavailable in WordPress.');
    }
    $raw = wp_json_encode($result, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    $packed = vpt_pull_b64url(gzcompress($raw, 9));
    $t = (string) time();
    $n = bin2hex(random_bytes(16));
    $message = 'ack|' . $t . '|' . $n . '|' . $job_id . '|' . $packed;
    $s = hash_hmac('sha256', $message, $token);
    $url = add_query_arg(array(
        't' => $t,
        'n' => $n,
        'job' => $job_id,
        'd' => $packed,
        's' => $s,
    ), vpt_pull_base_url() . '/tracking-ack');
    $ack_raw = vpt_pull_direct_get($url, 12);
    $decoded = json_decode($ack_raw, true);
    if (!is_array($decoded) || empty($decoded['success'])) {
        throw new Exception('Bot acknowledgement failed: ' . substr($ack_raw, 0, 300));
    }
    return true;
}

function vpt_pull_run() {
    if (get_transient('vpt_pull_lock')) {
        return array('ok' => false, 'state' => 'locked', 'message' => 'Pull is already running.');
    }
    set_transient('vpt_pull_lock', 1, 50);
    update_option('vpt_pull_last_attempt', current_time('mysql'), false);

    try {
        $job = vpt_pull_fetch_job();
        if (!$job) {
            delete_option('vpt_pull_last_error');
            update_option('vpt_pull_last_state', 'queue-empty', false);
            return array('ok' => true, 'state' => 'queue-empty', 'message' => 'Bot queue is reachable but currently empty.');
        }

        $job_id = isset($job['id']) ? strtolower((string) $job['id']) : '';
        $filename = sanitize_file_name(isset($job['filename']) ? $job['filename'] : 'tracking.xlsx');
        $rows = isset($job['rows']) && is_array($job['rows']) ? $job['rows'] : array();
        if (!preg_match('/^[a-f0-9]{32}$/', $job_id) || count($rows) < 2) {
            throw new Exception('Invalid tracking job received from bot.');
        }

        // If import succeeded but ACK failed previously, never import twice.
        $cache_key = 'vpt_pull_done_' . md5($job_id);
        $result = get_transient($cache_key);
        if (!is_array($result)) {
            $stats = Vesta_Smart_Post_Tracking::import_rows($rows, $filename, true);
            $status = function_exists('vpt_bot_status_data') ? vpt_bot_status_data() : array();
            $result = array_merge(array(
                'filename' => $filename,
                'rows' => max(0, count($rows) - 1),
            ), is_array($stats) ? $stats : array(), is_array($status) ? $status : array());
            set_transient($cache_key, $result, 12 * HOUR_IN_SECONDS);
        }

        vpt_pull_ack($job_id, $result);
        delete_transient($cache_key);
        delete_option('vpt_pull_last_error');
        update_option('vpt_pull_last_state', 'success', false);
        update_option('vpt_pull_last_success', wp_json_encode(array(
            'time' => current_time('mysql'),
            'job' => $job_id,
            'result' => $result,
        ), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES), false);

        return array(
            'ok' => true,
            'state' => 'success',
            'job' => $job_id,
            'result' => $result,
            'message' => 'Tracking file imported and acknowledged successfully.',
        );
    } catch (Throwable $e) {
        $message = current_time('mysql') . ' | ' . $e->getMessage();
        update_option('vpt_pull_last_error', $message, false);
        update_option('vpt_pull_last_state', 'error', false);
        return array('ok' => false, 'state' => 'error', 'message' => $e->getMessage());
    } finally {
        delete_transient('vpt_pull_lock');
    }
}

add_filter('cron_schedules', function ($schedules) {
    if (!isset($schedules['vpt_every_minute'])) {
        $schedules['vpt_every_minute'] = array(
            'interval' => 60,
            'display' => 'Vesta every minute',
        );
    }
    return $schedules;
});

add_action('vpt_bot_pull_cron', 'vpt_pull_run');

add_action('init', function () {
    if (!wp_next_scheduled('vpt_bot_pull_cron')) {
        wp_schedule_event(time(), 'vpt_every_minute', 'vpt_bot_pull_cron');
    }
    if (!get_transient('vpt_pull_cron_spawn_guard')) {
        set_transient('vpt_pull_cron_spawn_guard', 1, 30);
        if (function_exists('spawn_cron') && (!defined('DISABLE_WP_CRON') || !DISABLE_WP_CRON)) {
            spawn_cron(time());
        }
    }
}, 20);

// Admin visits are a second trigger independent of WP-Cron.
add_action('admin_init', function () {
    if (wp_doing_ajax() || !current_user_can('manage_woocommerce')) {
        return;
    }
    if (get_transient('vpt_pull_admin_guard')) {
        return;
    }
    set_transient('vpt_pull_admin_guard', 1, 30);
    vpt_pull_run();
}, 20);

// -----------------------------------------------------------------------------
// Manual diagnostic / force-pull screen.
// This turns the next failure into an exact actionable error instead of guessing.
// -----------------------------------------------------------------------------
function vpt_pull_diag_result_key() {
    return 'vpt_pull_diag_' . get_current_user_id();
}

add_action('admin_menu', function () {
    add_submenu_page(
        'woocommerce',
        'Vesta Tracking Pull',
        'Vesta Tracking Pull',
        'manage_woocommerce',
        'vesta-tracking-pull',
        'vpt_pull_diagnostics_page'
    );
});

add_action('admin_post_vpt_pull_now', function () {
    if (!current_user_can('manage_woocommerce')) {
        wp_die('Access denied.');
    }
    check_admin_referer('vpt_pull_now');

    // Manual action must never be hidden by a stale transient lock.
    delete_transient('vpt_pull_lock');
    $diag = array(
        'time' => current_time('mysql'),
        'health' => null,
        'pull' => null,
        'error' => null,
    );
    try {
        $diag['health'] = vpt_pull_health_check();
        $diag['pull'] = vpt_pull_run();
    } catch (Throwable $e) {
        $diag['error'] = $e->getMessage();
        update_option('vpt_pull_last_error', current_time('mysql') . ' | manual diagnostic | ' . $e->getMessage(), false);
    }
    set_transient(vpt_pull_diag_result_key(), $diag, 10 * MINUTE_IN_SECONDS);
    wp_safe_redirect(admin_url('admin.php?page=vesta-tracking-pull'));
    exit;
});

function vpt_pull_diagnostics_page() {
    if (!current_user_can('manage_woocommerce')) {
        wp_die('Access denied.');
    }
    $diag = get_transient(vpt_pull_diag_result_key());
    if ($diag) {
        delete_transient(vpt_pull_diag_result_key());
    }
    $token = vpt_pull_token();
    $last_attempt = get_option('vpt_pull_last_attempt', 'هرگز');
    $last_state = get_option('vpt_pull_last_state', 'نامشخص');
    $last_error = get_option('vpt_pull_last_error', '');
    $last_success = get_option('vpt_pull_last_success', '');
    $next = wp_next_scheduled('vpt_bot_pull_cron');
    ?>
    <div class="wrap">
        <h1>Vesta Tracking Pull</h1>
        <p><strong>نسخه مسیر Pull: 2.3.2</strong></p>
        <table class="widefat striped" style="max-width:980px">
            <tbody>
                <tr><td style="width:220px"><strong>Bot endpoint</strong></td><td><code><?php echo esc_html(vpt_pull_base_url()); ?></code></td></tr>
                <tr><td><strong>Bridge token</strong></td><td><?php echo $token !== '' ? '✅ موجود (hash: <code>' . esc_html(substr(hash('sha256', $token), 0, 10)) . '</code>)' : '❌ پیدا نشد'; ?></td></tr>
                <tr><td><strong>آخرین تلاش</strong></td><td><?php echo esc_html($last_attempt); ?></td></tr>
                <tr><td><strong>آخرین وضعیت</strong></td><td><?php echo esc_html($last_state); ?></td></tr>
                <tr><td><strong>اجرای بعدی Cron</strong></td><td><?php echo $next ? esc_html(wp_date('Y-m-d H:i:s', $next)) : '❌ زمان‌بندی نشده'; ?></td></tr>
                <tr><td><strong>آخرین خطا</strong></td><td><code style="white-space:pre-wrap"><?php echo esc_html($last_error ?: '—'); ?></code></td></tr>
            </tbody>
        </table>

        <p style="margin-top:18px">این دکمه اول اتصال مستقیم هاست سایت به ربات را تست می‌کند، سپس همان لحظه فایل صف را دریافت و Import می‌کند.</p>
        <form method="post" action="<?php echo esc_url(admin_url('admin-post.php')); ?>">
            <input type="hidden" name="action" value="vpt_pull_now" />
            <?php wp_nonce_field('vpt_pull_now'); ?>
            <button class="button button-primary button-hero">🔄 تست اتصال و دریافت فایل الان</button>
        </form>

        <?php if (is_array($diag)): ?>
            <h2>نتیجه تست دستی</h2>
            <?php if (!empty($diag['error'])): ?>
                <div class="notice notice-error inline"><p><strong>خطا:</strong> <code><?php echo esc_html($diag['error']); ?></code></p></div>
            <?php else: ?>
                <div class="notice notice-success inline"><p>✅ Health ربات از همین هاست دریافت شد.</p></div>
                <pre style="background:#fff;border:1px solid #ccd0d4;padding:12px;max-width:980px;white-space:pre-wrap"><?php echo esc_html(wp_json_encode($diag, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES)); ?></pre>
            <?php endif; ?>
        <?php endif; ?>

        <?php if ($last_success): ?>
            <h2>آخرین موفقیت</h2>
            <pre style="background:#fff;border:1px solid #ccd0d4;padding:12px;max-width:980px;white-space:pre-wrap"><?php echo esc_html($last_success); ?></pre>
        <?php endif; ?>
    </div>
    <?php
}
