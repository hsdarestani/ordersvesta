<?php
// Pull-mode transport for Vesta Smart Post Tracking 2.3+.
// The WordPress site initiates the connection to the bot. This completely avoids
// the unreliable bot -> Iran-host/WAF path used by the legacy signed-GET importer.

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
    // Use raw cURL first so WordPress HTTP filters/boosters cannot blacklist or
    // rewrite this fast bot endpoint. The WP HTTP API is only a fallback.
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
                'User-Agent: VestaPostTrackingPull/2.3',
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
            throw new Exception('Bot pull HTTP ' . $code . ': ' . substr((string) $body, 0, 250));
        }
        return (string) $body;
    }

    $response = wp_remote_get($url, array(
        'timeout' => (int) $timeout,
        'redirection' => 2,
        'headers' => array('Accept' => 'application/json'),
    ));
    if (is_wp_error($response)) {
        throw new Exception($response->get_error_message());
    }
    $code = (int) wp_remote_retrieve_response_code($response);
    $body = (string) wp_remote_retrieve_body($response);
    if ($code < 200 || $code >= 300) {
        throw new Exception('Bot pull HTTP ' . $code . ': ' . substr($body, 0, 250));
    }
    return $body;
}

function vpt_pull_token() {
    return function_exists('vbb_get_token') ? (string) vbb_get_token() : '';
}

function vpt_pull_fetch_job() {
    $token = vpt_pull_token();
    if ($token === '') {
        throw new Exception('Vesta Bot Bridge token is unavailable.');
    }
    $t = (string) time();
    $n = bin2hex(random_bytes(16));
    $s = hash_hmac('sha256', 'pull|' . $t . '|' . $n, $token);
    $url = add_query_arg(array('t' => $t, 'n' => $n, 's' => $s), vpt_pull_base_url() . '/tracking-pull');
    $decoded = json_decode(vpt_pull_direct_get($url, 12), true);
    if (!is_array($decoded) || empty($decoded['success'])) {
        throw new Exception('Invalid bot pull response.');
    }
    return isset($decoded['job']) && is_array($decoded['job']) ? $decoded['job'] : null;
}

function vpt_pull_ack($job_id, $result) {
    $token = vpt_pull_token();
    if ($token === '') {
        throw new Exception('Vesta Bot Bridge token is unavailable.');
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
    $decoded = json_decode(vpt_pull_direct_get($url, 12), true);
    if (!is_array($decoded) || empty($decoded['success'])) {
        throw new Exception('Bot acknowledgement failed.');
    }
    return true;
}

function vpt_pull_run() {
    if (get_transient('vpt_pull_lock')) {
        return;
    }
    set_transient('vpt_pull_lock', 1, 50);
    try {
        $job = vpt_pull_fetch_job();
        if (!$job) {
            delete_option('vpt_pull_last_error');
            return;
        }
        $job_id = isset($job['id']) ? strtolower((string) $job['id']) : '';
        $filename = sanitize_file_name(isset($job['filename']) ? $job['filename'] : 'tracking.xlsx');
        $rows = isset($job['rows']) && is_array($job['rows']) ? $job['rows'] : array();
        if (!preg_match('/^[a-f0-9]{32}$/', $job_id) || count($rows) < 2) {
            throw new Exception('Invalid tracking job received from bot.');
        }

        // If the import succeeded but ACK failed previously, do not import again.
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
        update_option('vpt_pull_last_success', wp_json_encode(array(
            'time' => current_time('mysql'),
            'job' => $job_id,
            'result' => $result,
        ), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES), false);
    } catch (Throwable $e) {
        update_option('vpt_pull_last_error', current_time('mysql') . ' | ' . $e->getMessage(), false);
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
        wp_schedule_event(time() + 5, 'vpt_every_minute', 'vpt_bot_pull_cron');
    }
});
