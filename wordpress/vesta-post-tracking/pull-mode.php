<?php
// Pull-mode transport for Vesta Smart Post Tracking 2.3.4+.
// WordPress initiates the connection to the bot. Imports are processed as a
// resumable step machine: one small row batch per request, so wp-admin never
// waits for the whole spreadsheet and low-worker hosts stay responsive.

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
                'User-Agent: VestaPostTrackingPull/2.3.4',
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

function vpt_pull_state_get() {
    $raw = get_option('vpt_pull_active_job', '');
    $state = json_decode((string) $raw, true);
    return is_array($state) ? $state : null;
}

function vpt_pull_state_set($state) {
    update_option('vpt_pull_active_job', wp_json_encode($state, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES), false);
}

function vpt_pull_state_clear() {
    delete_option('vpt_pull_active_job');
}

function vpt_pull_begin() {
    $existing = vpt_pull_state_get();
    if (is_array($existing) && !empty($existing['job_id'])) {
        $total = (int) ($existing['total'] ?? 0);
        $cursor = (int) ($existing['cursor'] ?? 0);
        vpt_pull_progress('importing', max(5, min(90, 10 + (int) floor(80 * $cursor / max(1, $total)))), 'ادامه پردازش فایل قبلی…', $cursor, $total);
        return array('ok' => true, 'state' => 'resumed', 'active' => true, 'job' => $existing['job_id']);
    }

    if (get_transient('vpt_pull_begin_lock')) {
        return array('ok' => true, 'state' => 'starting', 'active' => false);
    }
    set_transient('vpt_pull_begin_lock', 1, 15);
    update_option('vpt_pull_last_attempt', current_time('mysql'), false);
    update_option('vpt_pull_last_state', 'starting', false);
    vpt_pull_progress('connecting', 3, 'در حال دریافت فایل از صف ربات…');

    try {
        $job = vpt_pull_fetch_job();
        if (!$job) {
            delete_option('vpt_pull_last_error');
            update_option('vpt_pull_last_state', 'queue-empty', false);
            vpt_pull_progress('queue-empty', 100, 'صف ربات خالی است.');
            return array('ok' => true, 'state' => 'queue-empty', 'active' => false);
        }

        $job_id = isset($job['id']) ? strtolower((string) $job['id']) : '';
        $filename = sanitize_file_name(isset($job['filename']) ? $job['filename'] : 'tracking.xlsx');
        $rows = isset($job['rows']) && is_array($job['rows']) ? $job['rows'] : array();
        if (!preg_match('/^[a-f0-9]{32}$/', $job_id) || count($rows) < 2) {
            throw new Exception('Invalid tracking job received from bot.');
        }

        $header = array_shift($rows);
        $state = array(
            'job_id' => $job_id,
            'filename' => $filename,
            'header' => $header,
            'rows' => array_values($rows),
            'cursor' => 0,
            'total' => count($rows),
            'totals' => array('inserted' => 0, 'updated' => 0, 'skipped' => 0, 'linked' => 0, 'unlinked' => 0),
            'started_at' => current_time('mysql'),
        );
        vpt_pull_state_set($state);
        delete_option('vpt_pull_last_error');
        update_option('vpt_pull_last_state', 'running', false);
        vpt_pull_progress('received', 8, 'فایل دریافت شد؛ آماده ثبت مرحله‌ای.', 0, count($rows));
        return array('ok' => true, 'state' => 'running', 'active' => true, 'job' => $job_id, 'total' => count($rows));
    } catch (Throwable $e) {
        update_option('vpt_pull_last_error', current_time('mysql') . ' | ' . $e->getMessage(), false);
        update_option('vpt_pull_last_state', 'error', false);
        vpt_pull_progress('error', 100, $e->getMessage());
        return array('ok' => false, 'state' => 'error', 'message' => $e->getMessage(), 'active' => false);
    } finally {
        delete_transient('vpt_pull_begin_lock');
    }
}

function vpt_pull_step($batch_size = 25) {
    if (get_transient('vpt_pull_step_lock')) {
        return array('ok' => true, 'state' => 'busy', 'progress' => vpt_pull_progress_data(), 'active' => true);
    }
    set_transient('vpt_pull_step_lock', 1, 25);
    @set_time_limit(40);

    try {
        $state = vpt_pull_state_get();
        if (!is_array($state) || empty($state['job_id'])) {
            return array('ok' => true, 'state' => 'idle', 'active' => false, 'progress' => vpt_pull_progress_data());
        }

        $cursor = max(0, (int) ($state['cursor'] ?? 0));
        $total = max(0, (int) ($state['total'] ?? count($state['rows'] ?? array())));
        $rows = isset($state['rows']) && is_array($state['rows']) ? $state['rows'] : array();
        $header = isset($state['header']) && is_array($state['header']) ? $state['header'] : array();
        $filename = sanitize_file_name((string) ($state['filename'] ?? 'tracking.xlsx'));
        $job_id = strtolower((string) $state['job_id']);

        if ($cursor < $total) {
            $slice = array_slice($rows, $cursor, max(1, (int) $batch_size));
            if (!$slice) {
                throw new Exception('Tracking batch cursor is invalid.');
            }
            $batch = array_merge(array($header), $slice);
            $stats = Vesta_Smart_Post_Tracking::import_rows($batch, $filename, true);
            if (!is_array($stats)) {
                $stats = array();
            }
            $totals = isset($state['totals']) && is_array($state['totals']) ? $state['totals'] : array();
            foreach (array('inserted', 'updated', 'skipped', 'linked', 'unlinked') as $key) {
                $totals[$key] = (int) ($totals[$key] ?? 0) + (int) ($stats[$key] ?? 0);
            }
            $cursor += count($slice);
            $state['cursor'] = $cursor;
            $state['totals'] = $totals;
            vpt_pull_state_set($state);
            $pct = 10 + (int) floor(80 * $cursor / max(1, $total));
            $progress = vpt_pull_progress('importing', min(90, $pct), 'در حال ثبت و تطبیق کدهای رهگیری…', $cursor, $total);
            return array('ok' => true, 'state' => 'importing', 'active' => true, 'progress' => $progress);
        }

        $status = function_exists('vpt_bot_status_data') ? vpt_bot_status_data() : array();
        $result = array_merge(array(
            'filename' => $filename,
            'rows' => $total,
        ), isset($state['totals']) && is_array($state['totals']) ? $state['totals'] : array(), is_array($status) ? $status : array());

        vpt_pull_progress('acknowledging', 95, 'ثبت انجام شد؛ در حال تأیید نتیجه به ربات…', $total, $total);
        vpt_pull_ack($job_id, $result);
        update_option('vpt_pull_last_success', wp_json_encode(array(
            'time' => current_time('mysql'),
            'job' => $job_id,
            'result' => $result,
        ), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES), false);
        delete_option('vpt_pull_last_error');
        update_option('vpt_pull_last_state', 'success', false);
        vpt_pull_state_clear();
        $progress = vpt_pull_progress('success', 100, 'ثبت فایل با موفقیت کامل شد.', $total, $total);
        return array('ok' => true, 'state' => 'success', 'active' => false, 'result' => $result, 'progress' => $progress);
    } catch (Throwable $e) {
        update_option('vpt_pull_last_error', current_time('mysql') . ' | ' . $e->getMessage(), false);
        update_option('vpt_pull_last_state', 'error', false);
        $progress = vpt_pull_progress('error', 100, $e->getMessage());
        return array('ok' => false, 'state' => 'error', 'active' => (bool) vpt_pull_state_get(), 'message' => $e->getMessage(), 'progress' => $progress);
    } finally {
        delete_transient('vpt_pull_step_lock');
    }
}

function vpt_pull_cron_tick() {
    $state = vpt_pull_state_get();
    if (!$state) {
        $begin = vpt_pull_begin();
        if (empty($begin['active'])) {
            return;
        }
    }
    vpt_pull_step(25);
    if (vpt_pull_state_get()) {
        vpt_pull_step(25);
    }
}

add_filter('cron_schedules', function ($schedules) {
    if (!isset($schedules['vpt_every_minute'])) {
        $schedules['vpt_every_minute'] = array('interval' => 60, 'display' => 'Vesta every minute');
    }
    return $schedules;
});

add_action('vpt_bot_pull_cron', 'vpt_pull_cron_tick');
add_action('init', function () {
    if (!wp_next_scheduled('vpt_bot_pull_cron')) {
        wp_schedule_event(time() + 5, 'vpt_every_minute', 'vpt_bot_pull_cron');
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
    @set_time_limit(30);
    $result = vpt_pull_begin();
    wp_send_json_success(array(
        'begin' => $result,
        'progress' => vpt_pull_progress_data(),
        'active' => (bool) vpt_pull_state_get(),
    ));
});

add_action('wp_ajax_vpt_pull_step', function () {
    if (!current_user_can('manage_woocommerce')) {
        wp_send_json_error(array('message' => 'Access denied.'), 403);
    }
    check_ajax_referer('vpt_pull_ajax', 'nonce');
    $result = vpt_pull_step(25);
    wp_send_json_success($result);
});

add_action('wp_ajax_vpt_pull_status', function () {
    if (!current_user_can('manage_woocommerce')) {
        wp_send_json_error(array('message' => 'Access denied.'), 403);
    }
    check_ajax_referer('vpt_pull_ajax', 'nonce');
    $last_success = json_decode((string) get_option('vpt_pull_last_success', ''), true);
    wp_send_json_success(array(
        'progress' => vpt_pull_progress_data(),
        'last_attempt' => get_option('vpt_pull_last_attempt', 'هرگز'),
        'last_state' => get_option('vpt_pull_last_state', 'نامشخص'),
        'last_error' => get_option('vpt_pull_last_error', ''),
        'last_success' => is_array($last_success) ? $last_success : null,
        'active' => (bool) vpt_pull_state_get(),
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
    $initial = vpt_pull_progress_data();
    ?>
    <div class="wrap">
        <h1>Vesta Tracking Pull</h1>
        <p><strong>نسخه مسیر Pull: 2.3.4</strong></p>
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

        <p style="margin-top:18px">هر درخواست فقط یک بخش کوچک از فایل را پردازش می‌کند. بنابراین صفحه و PHP worker درگیر یک درخواست طولانی نمی‌شوند.</p>
        <button id="vpt-pull-now" class="button button-primary button-hero">🔄 دریافت / ادامه ثبت فایل</button>

        <div id="vpt-progress-wrap" style="max-width:980px;margin-top:18px;background:#fff;border:1px solid #ccd0d4;border-radius:8px;padding:14px">
            <div style="display:flex;justify-content:space-between;gap:12px"><strong id="vpt-progress-message"><?php echo esc_html($initial['message'] ?? 'آماده'); ?></strong><span id="vpt-progress-percent"><?php echo (int) ($initial['percent'] ?? 0); ?>%</span></div>
            <div style="height:14px;background:#e5e7eb;border-radius:999px;margin-top:10px;overflow:hidden"><div id="vpt-progress-bar" style="height:100%;width:<?php echo (int) ($initial['percent'] ?? 0); ?>%;background:#2271b1;transition:width .25s"></div></div>
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
        let running = false;

        function apply(data) {
            if (!data) return;
            const p = data.progress || data;
            if (p && typeof p === 'object') {
                const percent = Math.max(0, Math.min(100, parseInt(p.percent || 0, 10)));
                msg.textContent = p.message || p.stage || 'در حال پردازش…';
                pct.textContent = percent + '%';
                bar.style.width = percent + '%';
                count.textContent = p.total ? ((p.processed || 0) + ' / ' + p.total + ' ردیف') : '';
            }
            if (data.last_attempt) document.getElementById('vpt-last-attempt').textContent = data.last_attempt;
            if (data.last_state) document.getElementById('vpt-last-state').textContent = data.last_state;
            if (typeof data.last_error !== 'undefined') document.getElementById('vpt-last-error').textContent = data.last_error || '—';
        }

        async function post(action) {
            const body = new URLSearchParams({action, nonce});
            const r = await fetch(ajaxUrl, {method:'POST', credentials:'same-origin', body});
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const j = await r.json();
            if (!j || !j.success) throw new Error((j && j.data && j.data.message) || 'خطای نامشخص');
            return j.data || {};
        }

        async function refreshStatus() {
            try { const d = await post('vpt_pull_status'); apply(d); return d; } catch(e) { return null; }
        }

        async function runSteps() {
            if (running) return;
            running = true;
            btn.disabled = true;
            result.style.display = 'none';
            try {
                let start = await post('vpt_pull_start');
                apply(start);
                if (!start.active) {
                    await refreshStatus();
                    return;
                }

                while (true) {
                    await new Promise(resolve => setTimeout(resolve, 350));
                    const step = await post('vpt_pull_step');
                    apply(step);
                    if (step.state === 'success') {
                        result.style.display = 'block';
                        result.textContent = '✅ ثبت فایل کامل شد.\n\n' + JSON.stringify(step.result || {}, null, 2);
                        break;
                    }
                    if (step.state === 'error') {
                        throw new Error(step.message || 'خطا در پردازش');
                    }
                    if (!step.active && step.state !== 'busy') {
                        break;
                    }
                }
                await refreshStatus();
            } catch (e) {
                result.style.display = 'block';
                result.textContent = '❌ ' + e.message + '\n\nبرای ادامه دوباره دکمه را بزنید؛ cursor ذخیره شده و از اول شروع نمی‌شود.';
                await refreshStatus();
            } finally {
                running = false;
                btn.disabled = false;
            }
        }

        btn.addEventListener('click', runSteps);
        refreshStatus().then(d => {
            if (d && d.active) runSteps();
        });
    })();
    </script>
    <?php
}
