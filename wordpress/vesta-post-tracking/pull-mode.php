<?php
// Pull-mode transport for Vesta Smart Post Tracking 2.3.5+.
// WordPress pulls queued tracking jobs from the bot. Import is resumable and
// processed in tiny batches through a lightweight front endpoint (not admin-ajax).

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
                'User-Agent: VestaPostTrackingPull/2.3.5',
            ),
        ));
        $body = curl_exec($ch);
        $error = curl_error($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        if ($body === false || $error) {
            throw new Exception('Bot pull cURL: ' . ($error ?: 'unknown error'));
        }
        if ($code < 200 || $code >= 300) {
            throw new Exception('Bot pull HTTP ' . $code . ': ' . substr((string) $body, 0, 300));
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
    $url = add_query_arg(array('t' => $t, 'n' => $n, 'job' => $job_id, 'd' => $packed, 's' => $s), vpt_pull_base_url() . '/tracking-ack');
    $decoded = json_decode(vpt_pull_direct_get($url, 12), true);
    if (!is_array($decoded) || empty($decoded['success'])) {
        throw new Exception('Bot acknowledgement failed.');
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
    $data = json_decode((string) get_option('vpt_pull_progress', ''), true);
    return is_array($data) ? $data : array('stage' => 'idle', 'percent' => 0, 'message' => 'آماده', 'processed' => 0, 'total' => 0);
}

function vpt_pull_state_get() {
    $state = json_decode((string) get_option('vpt_pull_active_job', ''), true);
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
        $total = max(1, (int) ($existing['total'] ?? 0));
        $cursor = max(0, (int) ($existing['cursor'] ?? 0));
        vpt_pull_progress('importing', max(8, min(92, 8 + (int) floor(84 * $cursor / $total))), 'ادامه پردازش فایل قبلی…', $cursor, $total);
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
        $job_id = strtolower((string) ($job['id'] ?? ''));
        $filename = sanitize_file_name((string) ($job['filename'] ?? 'tracking.xlsx'));
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
        vpt_pull_progress('received', 8, 'فایل دریافت شد؛ آماده ثبت سریع و مرحله‌ای.', 0, count($rows));
        return array('ok' => true, 'state' => 'running', 'active' => true, 'job' => $job_id, 'total' => count($rows));
    } catch (Throwable $e) {
        update_option('vpt_pull_last_error', current_time('mysql') . ' | ' . $e->getMessage(), false);
        update_option('vpt_pull_last_state', 'error', false);
        vpt_pull_progress('error', 100, $e->getMessage());
        return array('ok' => false, 'state' => 'error', 'active' => false, 'message' => $e->getMessage());
    } finally {
        delete_transient('vpt_pull_begin_lock');
    }
}

function vpt_pull_fast_match($payload) {
    $direct = Vesta_Smart_Post_Tracking::match_order($payload, false);
    if (!empty($direct['order_id']) && !empty($direct['order'])) {
        return $direct;
    }

    $name = Vesta_Smart_Post_Tracking::normalize_text($payload['customer_name'] ?? '');
    if ($name === '') {
        return Vesta_Smart_Post_Tracking::match_failure('اطلاعات کافی برای تطبیق سریع سفارش وجود ندارد.');
    }

    $ids = Vesta_Smart_Post_Tracking::order_ids_by_exact_name_sql($name, 20);
    if (!$ids) {
        return Vesta_Smart_Post_Tracking::match_failure('سفارشی با نام گیرنده پیدا نشد.');
    }

    $destination = Vesta_Smart_Post_Tracking::normalize_text($payload['destination'] ?? '');
    $address = Vesta_Smart_Post_Tracking::normalize_text($payload['address'] ?? '');
    $best = null;
    $best_score = -1;
    $exact = array();
    foreach ($ids as $id) {
        $order = function_exists('wc_get_order') ? wc_get_order(absint($id)) : null;
        if (!$order) continue;
        if (Vesta_Smart_Post_Tracking::order_has_exact_name($order, $name)) $exact[] = $order;
        $score = Vesta_Smart_Post_Tracking::score_order_match($order, $name, $destination, $address);
        if ($score > $best_score) {
            $best_score = $score;
            $best = $order;
        }
    }

    if (count($exact) === 1) {
        return array('order_id' => $exact[0]->get_id(), 'order' => $exact[0], 'method' => 'fast_unique_name', 'reason' => 'با نام یکتای گیرنده وصل شد.', 'debug' => array('score' => $best_score));
    }
    if ($best && $best_score >= 72) {
        return array('order_id' => $best->get_id(), 'order' => $best, 'method' => 'fast_name_address_' . $best_score, 'reason' => 'با نام/شهر/آدرس وصل شد.', 'debug' => array('score' => $best_score));
    }
    if (count($exact) > 1) {
        return Vesta_Smart_Post_Tracking::match_failure('چند سفارش با نام مشابه پیدا شد؛ اتصال خودکار انجام نشد.');
    }
    return Vesta_Smart_Post_Tracking::match_failure('کاندیدای مطمئن برای اتصال پیدا نشد.');
}

function vpt_pull_import_fast_batch($rows, $filename) {
    $stats = array('inserted' => 0, 'updated' => 0, 'skipped' => 0, 'linked' => 0, 'unlinked' => 0);
    if (!is_array($rows) || count($rows) < 2) return $stats;
    $headers = array_map(array('Vesta_Smart_Post_Tracking', 'normalize_header'), $rows[0]);
    global $wpdb;
    $table = Vesta_Smart_Post_Tracking::table_name();

    for ($i = 1; $i < count($rows); $i++) {
        $row = $rows[$i];
        if (Vesta_Smart_Post_Tracking::row_is_empty($row)) continue;
        $assoc = Vesta_Smart_Post_Tracking::map_row($headers, $row);
        $payload = Vesta_Smart_Post_Tracking::payload_from_assoc($assoc);
        if (empty($payload['tracking_code'])) {
            $stats['skipped']++;
            continue;
        }

        $existing_order_id = (int) $wpdb->get_var($wpdb->prepare("SELECT order_id FROM {$table} WHERE tracking_code=%s LIMIT 1", $payload['tracking_code']));
        if ($existing_order_id && function_exists('wc_get_order')) {
            $existing_order = wc_get_order($existing_order_id);
            if ($existing_order) {
                $payload['order_id'] = $existing_order_id;
                $payload['match_method'] = 'existing_tracking';
                $payload['match_status'] = 'linked';
                $payload['match_reason'] = 'اتصال قبلی این کد رهگیری حفظ شد.';
                Vesta_Smart_Post_Tracking::enrich_from_order($payload, $existing_order);
            }
        }

        if (empty($payload['order_id'])) {
            $match = vpt_pull_fast_match($payload);
            if (!empty($match['order_id']) && !empty($match['order'])) {
                Vesta_Smart_Post_Tracking::apply_match_success($payload, $match);
                Vesta_Smart_Post_Tracking::enrich_from_order($payload, $match['order']);
                Vesta_Smart_Post_Tracking::sync_order_tracking($match['order'], $payload['tracking_code']);
            } else {
                Vesta_Smart_Post_Tracking::apply_match_failure($payload, $match);
            }
        }

        $status = Vesta_Smart_Post_Tracking::upsert_record($payload, $filename);
        if ($status === 'inserted') $stats['inserted']++;
        elseif ($status === 'updated') $stats['updated']++;
        if (!empty($payload['order_id'])) $stats['linked']++;
        else $stats['unlinked']++;
    }
    return $stats;
}

function vpt_pull_step($batch_size = 8) {
    if (get_transient('vpt_pull_step_lock')) {
        return array('ok' => true, 'state' => 'busy', 'active' => true, 'progress' => vpt_pull_progress_data());
    }
    set_transient('vpt_pull_step_lock', 1, 20);
    @set_time_limit(25);

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
            $stats = vpt_pull_import_fast_batch(array_merge(array($header), $slice), $filename);
            $totals = isset($state['totals']) && is_array($state['totals']) ? $state['totals'] : array();
            foreach (array('inserted', 'updated', 'skipped', 'linked', 'unlinked') as $key) {
                $totals[$key] = (int) ($totals[$key] ?? 0) + (int) ($stats[$key] ?? 0);
            }
            $cursor += count($slice);
            $state['cursor'] = $cursor;
            $state['totals'] = $totals;
            vpt_pull_state_set($state);
            update_option('vpt_pull_last_state', 'running', false);
            $pct = 8 + (int) floor(84 * $cursor / max(1, $total));
            $progress = vpt_pull_progress('importing', min(92, $pct), 'در حال ثبت و تطبیق سریع رهگیری‌ها…', $cursor, $total);
            return array('ok' => true, 'state' => 'importing', 'active' => true, 'progress' => $progress);
        }

        $status = function_exists('vpt_bot_status_data') ? vpt_bot_status_data() : array();
        $result = array_merge(array('filename' => $filename, 'rows' => $total), $state['totals'] ?? array(), is_array($status) ? $status : array());
        vpt_pull_progress('acknowledging', 96, 'ثبت انجام شد؛ در حال تأیید نتیجه به ربات…', $total, $total);
        vpt_pull_ack($job_id, $result);
        update_option('vpt_pull_last_success', wp_json_encode(array('time' => current_time('mysql'), 'job' => $job_id, 'result' => $result), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES), false);
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

function vpt_pull_status_payload() {
    $last_success = json_decode((string) get_option('vpt_pull_last_success', ''), true);
    return array(
        'progress' => vpt_pull_progress_data(),
        'last_attempt' => get_option('vpt_pull_last_attempt', 'هرگز'),
        'last_state' => get_option('vpt_pull_last_state', 'نامشخص'),
        'last_error' => get_option('vpt_pull_last_error', ''),
        'last_success' => is_array($last_success) ? $last_success : null,
        'active' => (bool) vpt_pull_state_get(),
    );
}

add_action('template_redirect', function () {
    if (!isset($_REQUEST['vpt_pull_api'])) return;
    nocache_headers();
    if (!is_user_logged_in() || !current_user_can('manage_woocommerce')) {
        wp_send_json_error(array('message' => 'Access denied.'), 403);
    }
    $nonce = isset($_REQUEST['nonce']) ? (string) wp_unslash($_REQUEST['nonce']) : '';
    if (!wp_verify_nonce($nonce, 'vpt_pull_api')) {
        wp_send_json_error(array('message' => 'Invalid nonce.'), 403);
    }
    $op = isset($_REQUEST['op']) ? sanitize_key(wp_unslash($_REQUEST['op'])) : 'status';
    if ($op === 'start') {
        $result = vpt_pull_begin();
        wp_send_json_success(array('begin' => $result, 'progress' => vpt_pull_progress_data(), 'active' => (bool) vpt_pull_state_get()));
    }
    if ($op === 'step') wp_send_json_success(vpt_pull_step(8));
    if ($op === 'status') wp_send_json_success(vpt_pull_status_payload());
    wp_send_json_error(array('message' => 'Unknown operation.'), 400);
}, -100);

function vpt_pull_cron_tick() {
    if (!vpt_pull_state_get()) {
        $begin = vpt_pull_begin();
        if (empty($begin['active'])) return;
    }
    vpt_pull_step(5);
}

add_filter('cron_schedules', function ($schedules) {
    if (!isset($schedules['vpt_every_minute'])) $schedules['vpt_every_minute'] = array('interval' => 60, 'display' => 'Vesta every minute');
    return $schedules;
});
add_action('vpt_bot_pull_cron', 'vpt_pull_cron_tick');
add_action('init', function () {
    if (!wp_next_scheduled('vpt_bot_pull_cron')) wp_schedule_event(time() + 5, 'vpt_every_minute', 'vpt_bot_pull_cron');
}, 20);

add_action('admin_menu', function () {
    add_submenu_page('woocommerce', 'Vesta Tracking Pull', 'Vesta Tracking Pull', 'manage_woocommerce', 'vesta-tracking-pull', 'vpt_pull_diagnostics_page');
});

function vpt_pull_diagnostics_page() {
    if (!current_user_can('manage_woocommerce')) wp_die('Access denied.');
    $token = vpt_pull_token();
    $last_attempt = get_option('vpt_pull_last_attempt', 'هرگز');
    $last_state = get_option('vpt_pull_last_state', 'نامشخص');
    $last_error = get_option('vpt_pull_last_error', '');
    $last_success = get_option('vpt_pull_last_success', '');
    $next = wp_next_scheduled('vpt_bot_pull_cron');
    $nonce = wp_create_nonce('vpt_pull_api');
    $initial = vpt_pull_progress_data();
    $api = add_query_arg('vpt_pull_api', '1', home_url('/'));
    ?>
    <div class="wrap">
        <h1>Vesta Tracking Pull</h1>
        <p><strong>نسخه مسیر Pull: 2.3.5</strong></p>
        <table class="widefat striped" style="max-width:980px"><tbody>
            <tr><td style="width:220px"><strong>Bot endpoint</strong></td><td><code><?php echo esc_html(vpt_pull_base_url()); ?></code></td></tr>
            <tr><td><strong>Bridge token</strong></td><td><?php echo $token !== '' ? '✅ موجود (hash: <code>' . esc_html(substr(hash('sha256', $token), 0, 10)) . '</code>)' : '❌ پیدا نشد'; ?></td></tr>
            <tr><td><strong>آخرین تلاش</strong></td><td id="vpt-last-attempt"><?php echo esc_html($last_attempt); ?></td></tr>
            <tr><td><strong>آخرین وضعیت</strong></td><td id="vpt-last-state"><?php echo esc_html($last_state); ?></td></tr>
            <tr><td><strong>اجرای بعدی Cron</strong></td><td><?php echo $next ? esc_html(wp_date('Y-m-d H:i:s', $next)) : '❌ زمان‌بندی نشده'; ?></td></tr>
            <tr><td><strong>آخرین خطا</strong></td><td><code id="vpt-last-error"><?php echo esc_html($last_error ?: '—'); ?></code></td></tr>
        </tbody></table>
        <p style="margin-top:18px">این نسخه admin-ajax را کنار گذاشته و هر درخواست فقط ۸ ردیف را با تطبیق سریع پردازش می‌کند.</p>
        <button id="vpt-pull-now" class="button button-primary button-hero">🔄 دریافت / ادامه ثبت فایل</button>
        <div style="max-width:980px;margin-top:18px;background:#fff;border:1px solid #ccd0d4;border-radius:8px;padding:14px">
            <div style="display:flex;justify-content:space-between"><strong id="vpt-progress-message"><?php echo esc_html($initial['message'] ?? 'آماده'); ?></strong><span id="vpt-progress-percent"><?php echo (int) ($initial['percent'] ?? 0); ?>%</span></div>
            <div style="height:14px;background:#e5e7eb;border-radius:999px;margin-top:10px;overflow:hidden"><div id="vpt-progress-bar" style="height:100%;width:<?php echo (int) ($initial['percent'] ?? 0); ?>%;background:#2271b1;transition:width .25s"></div></div>
            <div id="vpt-progress-count" style="margin-top:8px;color:#646970"></div>
        </div>
        <pre id="vpt-result" style="display:none;background:#fff;border:1px solid #ccd0d4;padding:12px;max-width:980px;white-space:pre-wrap;margin-top:16px"></pre>
        <?php if ($last_success): ?><h2>آخرین موفقیت</h2><pre style="background:#fff;border:1px solid #ccd0d4;padding:12px;max-width:980px;white-space:pre-wrap"><?php echo esc_html($last_success); ?></pre><?php endif; ?>
    </div>
    <script>
    (function(){
        const apiUrl = <?php echo wp_json_encode($api); ?>;
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
                const n = Math.max(0, Math.min(100, parseInt(p.percent || 0, 10)));
                msg.textContent = p.message || p.stage || 'در حال پردازش…';
                pct.textContent = n + '%'; bar.style.width = n + '%';
                count.textContent = p.total ? ((p.processed || 0) + ' / ' + p.total + ' ردیف') : '';
            }
            if (data.last_attempt) document.getElementById('vpt-last-attempt').textContent = data.last_attempt;
            if (data.last_state) document.getElementById('vpt-last-state').textContent = data.last_state;
            if (typeof data.last_error !== 'undefined') document.getElementById('vpt-last-error').textContent = data.last_error || '—';
        }

        async function call(op) {
            const body = new URLSearchParams({vpt_pull_api:'1', op, nonce});
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 20000);
            try {
                let r;
                try {
                    r = await fetch(apiUrl, {method:'POST', credentials:'same-origin', cache:'no-store', body, signal:controller.signal});
                } catch (firstError) {
                    const u = apiUrl + (apiUrl.includes('?') ? '&' : '?') + new URLSearchParams({op, nonce, _ts:String(Date.now())});
                    r = await fetch(u, {method:'GET', credentials:'same-origin', cache:'no-store', signal:controller.signal});
                }
                const text = await r.text();
                if (!r.ok) throw new Error('HTTP ' + r.status + ': ' + text.slice(0,160));
                let j; try { j = JSON.parse(text); } catch(e) { throw new Error('پاسخ JSON نبود: ' + text.slice(0,160)); }
                if (!j || !j.success) throw new Error((j && j.data && j.data.message) || 'خطای نامشخص');
                return j.data || {};
            } finally { clearTimeout(timer); }
        }

        async function status(){ try { const d = await call('status'); apply(d); return d; } catch(e) { return null; } }

        async function run(){
            if (running) return;
            running = true; btn.disabled = true; result.style.display='none';
            try {
                const start = await call('start'); apply(start);
                if (!start.active) { await status(); return; }
                while (true) {
                    await new Promise(r => setTimeout(r, 250));
                    const step = await call('step'); apply(step);
                    if (step.state === 'success') {
                        result.style.display='block'; result.textContent='✅ ثبت فایل کامل شد.\n\n'+JSON.stringify(step.result||{},null,2); break;
                    }
                    if (step.state === 'error') throw new Error(step.message || 'خطا در پردازش');
                    if (!step.active && step.state !== 'busy') break;
                }
                await status();
            } catch(e) {
                result.style.display='block';
                result.textContent='❌ '+(e.name==='AbortError'?'درخواست بیشتر از ۲۰ ثانیه طول کشید.':e.message)+'\n\nCursor ذخیره است؛ دوباره دکمه را بزنید تا از همانجا ادامه دهد.';
                await status();
            } finally { running=false; btn.disabled=false; }
        }
        btn.addEventListener('click', run);
        status().then(d => { if (d && d.active) run(); });
    })();
    </script>
    <?php
}
