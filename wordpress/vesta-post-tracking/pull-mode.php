<?php
// Pull-mode transport for Vesta Smart Post Tracking 2.3.3+.
// WordPress initiates the connection to the bot. Manual imports run via AJAX so
// wp-admin never navigates into a long blocking request.

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
                'User-Agent: VestaPostTrackingPull/2.3.3',
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

function vpt_pull_progress($stage, $percent, $message = '', $processed = 0, $total = 0) {
    $data = array(
        'stage' => (string) $stage,
        'percent' => max(0, min(100, (int) $percent)),
        'message' => (string) $message,
        'processed' => (int) $processed,
        'total' => (int) $total,
        'time' => current_time('mysql'),
    );
    update_option('vpt_pull_progress', wp_json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES), false);
    return $data;
}

function vpt_pull_progress_data() {
    $raw = get_option('vpt_pull_progress', '');
    $data = json_decode((string) $raw, true);
    return is_array($data) ? $data : array(
        'stage' => 'idle', 'percent' => 0, 'message' => 'آماده', 'processed' => 0, 'total' => 0,
    );
}

function vpt_pull_import_rows_chunked($rows, $filename) {
    if (!is_array($rows) || count($rows) < 2) {
        throw new Exception('Tracking job has no usable rows.');
    }

    $header = array_shift($rows);
    $total_rows = count($rows);
    $chunks = array_chunk($rows, 40);
    $totals = array('inserted' => 0, 'updated' => 0, 'skipped' => 0, 'linked' => 0, 'unlinked' => 0);
    $processed = 0;

    foreach ($chunks as $index => $chunk) {
        $batch = array_merge(array($header), $chunk);
        $stats = Vesta_Smart_Post_Tracking::import_rows($batch, $filename, true);
        if (!is_array($stats)) {
            $stats = array();
        }
        foreach (array_keys($totals) as $key) {
            $totals[$key] += (int) ($stats[$key] ?? 0);
        }
        $processed += count($chunk);
        $pct = 15 + (int) floor(75 * $processed / max(1, $total_rows));
        vpt_pull_progress(
            'importing',
            $pct,
            'در حال ثبت و تطبیق کدهای رهگیری…',
            $processed,
            $total_rows
        );
        if ($index + 1 < count($chunks)) {
            usleep(80000);
        }
    }

    return $totals;
}

function vpt_pull_run() {
    if (get_transient('vpt_pull_lock')) {
        return array('ok' => false, 'state' => 'locked', 'message' => 'Pull is already running.');
    }
    set_transient('vpt_pull_lock', 1, 180);
    update_option('vpt_pull_last_attempt', current_time('mysql'), false);
    update_option('vpt_pull_last_state', 'running', false);
    vpt_pull_progress('connecting', 3, 'در حال دریافت فایل از صف ربات…');

    try {
        $job = vpt_pull_fetch_job();
        if (!$job) {
            delete_option('vpt_pull_last_error');
            update_option('vpt_pull_last_state', 'queue-empty', false);
            vpt_pull_progress('queue-empty', 100, 'صف ربات خالی است.');
            return array('ok' => true, 'state' => 'queue-empty', 'message' => 'Bot queue is reachable but currently empty.');
        }

        $job_id = isset($job['id']) ? strtolower((string) $job['id']) : '';
        $filename = sanitize_file_name(isset($job['filename']) ? $job['filename'] : 'tracking.xlsx');
        $rows = isset($job['rows']) && is_array($job['rows']) ? $job['rows'] : array();
        if (!preg_match('/^[a-f0-9]{32}$/', $job_id) || count($rows) < 2) {
            throw new Exception('Invalid tracking job received from bot.');
        }

        $row_count = max(0, count($rows) - 1);
        vpt_pull_progress('received', 10, 'فایل از ربات دریافت شد؛ Import شروع شد.', 0, $row_count);

        $cache_key = 'vpt_pull_done_' . md5($job_id);
        $result = get_transient($cache_key);
        if (!is_array($result)) {
            $stats = vpt_pull_import_rows_chunked($rows, $filename);
            $status = function_exists('vpt_bot_status_data') ? vpt_bot_status_data() : array();
            $result = array_merge(array(
                'filename' => $filename,
                'rows' => $row_count,
            ), is_array($stats) ? $stats : array(), is_array($status) ? $status : array());
            set_transient($cache_key, $result, 12 * HOUR_IN_SECONDS);
        }

        vpt_pull_progress('acknowledging', 95, 'ثبت انجام شد؛ در حال تأیید نتیجه به ربات…', $row_count, $row_count);
        vpt_pull_ack($job_id, $result);
        delete_transient($cache_key);
        delete_option('vpt_pull_last_error');
        update_option('vpt_pull_last_state', 'success', false);
        update_option('vpt_pull_last_success', wp_json_encode(array(
            'time' => current_time('mysql'),
            'job' => $job_id,
            'result' => $result,
        ), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES), false);
        vpt_pull_progress('success', 100, 'ثبت فایل با موفقیت کامل شد.', $row_count, $row_count);

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
        vpt_pull_progress('error', 100, $e->getMessage());
        return array('ok' => false, 'state' => 'error', 'message' => $e->getMessage());
    } finally {
        delete_transient('vpt_pull_lock');
    }
}

add_filter('cron_schedules', function ($schedules) {
    if (!isset($schedules['vpt_every_minute'])) {
        $schedules['vpt_every_minute'] = array('interval' => 60, 'display' => 'Vesta every minute');
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

add_action('wp_ajax_vpt_pull_start', function () {
    if (!current_user_can('manage_woocommerce')) {
        wp_send_json_error(array('message' => 'Access denied.'), 403);
    }
    check_ajax_referer('vpt_pull_ajax', 'nonce');
    ignore_user_abort(true);
    @set_time_limit(240);

    if (get_transient('vpt_pull_lock')) {
        wp_send_json_success(array(
            'state' => 'locked',
            'message' => 'یک پردازش دیگر در حال اجراست. وضعیت را دنبال کنید.',
            'progress' => vpt_pull_progress_data(),
        ));
    }

    $diag = array('time' => current_time('mysql'), 'health' => null, 'pull' => null, 'error' => null);
    try {
        $diag['health'] = vpt_pull_health_check();
        $diag['pull'] = vpt_pull_run();
    } catch (Throwable $e) {
        $diag['error'] = $e->getMessage();
        update_option('vpt_pull_last_error', current_time('mysql') . ' | manual ajax | ' . $e->getMessage(), false);
        vpt_pull_progress('error', 100, $e->getMessage());
    }
    update_option('vpt_pull_last_diag', wp_json_encode($diag, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES), false);
    wp_send_json_success(array('diag' => $diag, 'progress' => vpt_pull_progress_data()));
});

add_action('wp_ajax_vpt_pull_status', function () {
    if (!current_user_can('manage_woocommerce')) {
        wp_send_json_error(array('message' => 'Access denied.'), 403);
    }
    check_ajax_referer('vpt_pull_ajax', 'nonce');
    $last_diag = json_decode((string) get_option('vpt_pull_last_diag', ''), true);
    $last_success = json_decode((string) get_option('vpt_pull_last_success', ''), true);
    wp_send_json_success(array(
        'progress' => vpt_pull_progress_data(),
        'last_attempt' => get_option('vpt_pull_last_attempt', 'هرگز'),
        'last_state' => get_option('vpt_pull_last_state', 'نامشخص'),
        'last_error' => get_option('vpt_pull_last_error', ''),
        'last_diag' => is_array($last_diag) ? $last_diag : null,
        'last_success' => is_array($last_success) ? $last_success : null,
        'locked' => (bool) get_transient('vpt_pull_lock'),
    ));
});

function vpt_pull_diagnostics_page() {
    if (!current_user_can('manage_woocommerce')) {
        wp_die('Access denied.');
    }
    $token = vpt_pull_token();
    $last_attempt = get_option('vpt_pull_last_attempt', 'هرگز');
    $last_state = get_option('vpt_pull_last_state', 'نامشخص');
    $last_error = get_option('vpt_pull_last_error', '');
    $last_success = get_option('vpt_pull_last_success', '');
    $next = wp_next_scheduled('vpt_bot_pull_cron');
    $nonce = wp_create_nonce('vpt_pull_ajax');
    ?>
    <div class="wrap">
        <h1>Vesta Tracking Pull</h1>
        <p><strong>نسخه مسیر Pull: 2.3.3</strong></p>
        <table class="widefat striped" style="max-width:980px">
            <tbody>
                <tr><td style="width:220px"><strong>Bot endpoint</strong></td><td><code><?php echo esc_html(vpt_pull_base_url()); ?></code></td></tr>
                <tr><td><strong>Bridge token</strong></td><td><?php echo $token !== '' ? '✅ موجود (hash: <code>' . esc_html(substr(hash('sha256', $token), 0, 10)) . '</code>)' : '❌ پیدا نشد'; ?></td></tr>
                <tr><td><strong>آخرین تلاش</strong></td><td id="vpt-last-attempt"><?php echo esc_html($last_attempt); ?></td></tr>
                <tr><td><strong>آخرین وضعیت</strong></td><td id="vpt-last-state"><?php echo esc_html($last_state); ?></td></tr>
                <tr><td><strong>اجرای بعدی Cron</strong></td><td><?php echo $next ? esc_html(wp_date('Y-m-d H:i:s', $next)) : '❌ زمان‌بندی نشده'; ?></td></tr>
                <tr><td><strong>آخرین خطا</strong></td><td><code id="vpt-last-error" style="white-space:pre-wrap"><?php echo esc_html($last_error ?: '—'); ?></code></td></tr>
            </tbody>
        </table>

        <p style="margin-top:18px">این دکمه صفحه را ترک نمی‌کند. دریافت، Import و تطبیق در درخواست جدا اجرا می‌شود و Progress همینجا به‌روزرسانی می‌شود.</p>
        <button id="vpt-pull-now" class="button button-primary button-hero">🔄 دریافت و ثبت فایل الان</button>

        <div id="vpt-progress-wrap" style="max-width:980px;margin-top:18px;background:#fff;border:1px solid #ccd0d4;border-radius:8px;padding:14px">
            <div style="display:flex;justify-content:space-between;gap:12px"><strong id="vpt-progress-message">آماده</strong><span id="vpt-progress-percent">0%</span></div>
            <div style="height:14px;background:#e5e7eb;border-radius:999px;margin-top:10px;overflow:hidden"><div id="vpt-progress-bar" style="height:100%;width:0%;background:#2271b1;transition:width .25s"></div></div>
            <div id="vpt-progress-count" style="margin-top:8px;color:#646970"></div>
        </div>

        <pre id="vpt-result" style="display:none;background:#fff;border:1px solid #ccd0d4;padding:12px;max-width:980px;white-space:pre-wrap;margin-top:16px"></pre>

        <?php if ($last_success): ?>
            <h2>آخرین موفقیت</h2>
            <pre style="background:#fff;border:1px solid #ccd0d4;padding:12px;max-width:980px;white-space:pre-wrap"><?php echo esc_html($last_success); ?></pre>
        <?php endif; ?>
    </div>
    <script>
    (function(){
        const ajaxUrl = <?php echo wp_json_encode(admin_url('admin-ajax.php')); ?>;
        const nonce = <?php echo wp_json_encode($nonce); ?>;
        const btn = document.getElementById('vpt-pull-now');
        const msg = document.getElementById('vpt-progress-message');
        const pct = document.getElementById('vpt-progress-percent');
        const bar = document.getElementById('vpt-progress-bar');
        const count = document.getElementById('vpt-progress-count');
        const result = document.getElementById('vpt-result');
        let timer = null;

        function apply(data) {
            if (!data) return;
            const p = data.progress || data;
            const percent = Math.max(0, Math.min(100, parseInt(p.percent || 0, 10)));
            msg.textContent = p.message || p.stage || 'در حال پردازش…';
            pct.textContent = percent + '%';
            bar.style.width = percent + '%';
            count.textContent = p.total ? ((p.processed || 0) + ' / ' + p.total + ' ردیف') : '';
            if (data.last_attempt) document.getElementById('vpt-last-attempt').textContent = data.last_attempt;
            if (data.last_state) document.getElementById('vpt-last-state').textContent = data.last_state;
            if (typeof data.last_error !== 'undefined') document.getElementById('vpt-last-error').textContent = data.last_error || '—';
            if (['success','error','queue-empty'].includes(p.stage)) {
                btn.disabled = false;
                if (timer) { clearInterval(timer); timer = null; }
            }
        }

        async function status() {
            try {
                const body = new URLSearchParams({action:'vpt_pull_status', nonce});
                const r = await fetch(ajaxUrl, {method:'POST', credentials:'same-origin', body});
                const j = await r.json();
                if (j && j.success) apply(j.data);
            } catch(e) {}
        }

        btn.addEventListener('click', function(){
            btn.disabled = true;
            result.style.display = 'none';
            msg.textContent = 'در حال شروع…';
            pct.textContent = '1%'; bar.style.width = '1%';
            if (timer) clearInterval(timer);
            timer = setInterval(status, 2000);

            const body = new URLSearchParams({action:'vpt_pull_start', nonce});
            fetch(ajaxUrl, {method:'POST', credentials:'same-origin', body})
                .then(r => r.json())
                .then(j => {
                    if (j && j.success) {
                        if (j.data && j.data.progress) apply(j.data.progress);
                        result.style.display = 'block';
                        result.textContent = JSON.stringify(j.data || {}, null, 2);
                    } else {
                        throw new Error((j && j.data && j.data.message) || 'خطای نامشخص');
                    }
                })
                .catch(e => {
                    result.style.display = 'block';
                    result.textContent = 'خطا: ' + e.message;
                })
                .finally(() => status());
        });

        status();
    })();
    </script>
    <?php
}
