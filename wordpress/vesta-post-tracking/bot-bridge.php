<?php
// -----------------------------------------------------------------------------
// Vesta Telegram Bot integration
// -----------------------------------------------------------------------------
// Reuses the HMAC authentication from the separate Vesta Bot Bridge plugin.
// Legacy binary chunk upload remains supported, but the preferred bot transport
// sends compact spreadsheet rows in a few idempotent signed batches. This avoids
// dozens of WAF-sensitive file-chunk requests.

function vpt_bot_tmp_dir() {
    $uploads = wp_upload_dir();
    $dir = trailingslashit($uploads['basedir']) . 'vesta-post-tracking-bot-tmp';
    if (!is_dir($dir)) {
        wp_mkdir_p($dir);
    }
    return $dir;
}

function vpt_bot_upload_id($payload) {
    $id = isset($payload['upload_id']) ? strtolower((string) $payload['upload_id']) : '';
    if (!preg_match('/^[a-f0-9]{32}$/', $id)) {
        vbb_fail('Invalid tracking upload id.', 400);
    }
    return $id;
}

function vpt_bot_path($id) {
    return trailingslashit(vpt_bot_tmp_dir()) . $id . '.part';
}

function vpt_bot_meta_key($id) {
    return 'vpt_bot_upload_' . $id;
}

function vpt_bot_result_key($id) {
    return 'vpt_bot_result_' . $id;
}

function vpt_bot_rows_result_key($import_id, $batch_index) {
    return 'vpt_rows_' . $import_id . '_' . intval($batch_index);
}

function vpt_bot_b64decode($value) {
    $value = strtr((string) $value, '-_', '+/');
    $pad = strlen($value) % 4;
    if ($pad) {
        $value .= str_repeat('=', 4 - $pad);
    }
    return base64_decode($value, true);
}

function vpt_bot_status_data() {
    global $wpdb;
    $table = Vesta_Smart_Post_Tracking::table_name();
    $exists = $wpdb->get_var($wpdb->prepare('SHOW TABLES LIKE %s', $table));
    if ($exists !== $table) {
        Vesta_Smart_Post_Tracking::create_tables();
    }
    $total = (int) $wpdb->get_var("SELECT COUNT(*) FROM {$table}");
    $linked = (int) $wpdb->get_var("SELECT COUNT(*) FROM {$table} WHERE order_id IS NOT NULL AND order_id > 0");
    $unlinked = max(0, $total - $linked);
    return array(
        'plugin_version' => Vesta_Smart_Post_Tracking::VERSION,
        'total' => $total,
        'linked' => $linked,
        'unlinked' => $unlinked,
    );
}

function vpt_bot_begin($payload) {
    $id = vpt_bot_upload_id($payload);
    $existing = get_transient(vpt_bot_result_key($id));
    if (is_array($existing)) {
        return array('upload_id' => $id, 'already_finished' => true, 'result' => $existing);
    }

    $filename = sanitize_file_name(isset($payload['filename']) ? $payload['filename'] : 'tracking.xlsx');
    $ext = strtolower(pathinfo($filename, PATHINFO_EXTENSION));
    $size = isset($payload['size']) ? intval($payload['size']) : 0;
    $sha256 = isset($payload['sha256']) ? strtolower((string) $payload['sha256']) : '';
    if (!in_array($ext, array('xlsx', 'xlsm', 'csv'), true)) {
        vbb_fail('Only XLSX, XLSM or CSV tracking files are supported.', 400);
    }
    if ($size <= 0 || $size > 20 * 1024 * 1024) {
        vbb_fail('Invalid tracking file size.', 400);
    }
    if (!preg_match('/^[a-f0-9]{64}$/', $sha256)) {
        vbb_fail('Invalid tracking file hash.', 400);
    }

    $path = vpt_bot_path($id);
    $fh = @fopen($path, 'c+b');
    if (!$fh) {
        throw new Exception('Could not initialize tracking upload.');
    }
    if (!flock($fh, LOCK_EX)) {
        fclose($fh);
        throw new Exception('Could not lock tracking upload.');
    }
    ftruncate($fh, 0);
    fflush($fh);
    flock($fh, LOCK_UN);
    fclose($fh);

    $opts = Vesta_Smart_Post_Tracking::options();
    $auto_match = array_key_exists('auto_match', $payload)
        ? !empty($payload['auto_match'])
        : (($opts['auto_match_on_import'] ?? 'yes') === 'yes');

    set_transient(vpt_bot_meta_key($id), array(
        'filename' => $filename,
        'ext' => $ext,
        'size' => $size,
        'sha256' => $sha256,
        'auto_match' => $auto_match,
    ), 2 * HOUR_IN_SECONDS);

    return array('upload_id' => $id, 'ready' => true, 'auto_match' => $auto_match);
}

function vpt_bot_chunk($payload) {
    $id = vpt_bot_upload_id($payload);
    $meta = get_transient(vpt_bot_meta_key($id));
    if (!is_array($meta)) {
        vbb_fail('Tracking upload session expired.', 410);
    }
    $offset = isset($payload['offset']) ? intval($payload['offset']) : -1;
    $bytes = vpt_bot_b64decode(isset($payload['data']) ? (string) $payload['data'] : '');
    if ($offset < 0 || $bytes === false || $bytes === '') {
        vbb_fail('Invalid tracking file chunk.', 400);
    }
    if ($offset + strlen($bytes) > intval($meta['size'])) {
        vbb_fail('Tracking file chunk exceeds declared size.', 400);
    }

    $path = vpt_bot_path($id);
    $fh = @fopen($path, 'c+b');
    if (!$fh) {
        throw new Exception('Could not open tracking upload.');
    }
    if (!flock($fh, LOCK_EX)) {
        fclose($fh);
        throw new Exception('Could not lock tracking upload.');
    }
    if (fseek($fh, $offset) !== 0) {
        flock($fh, LOCK_UN);
        fclose($fh);
        throw new Exception('Could not seek tracking upload.');
    }
    $written = fwrite($fh, $bytes);
    fflush($fh);
    flock($fh, LOCK_UN);
    fclose($fh);
    if ($written !== strlen($bytes)) {
        throw new Exception('Could not write complete tracking chunk.');
    }
    return array('upload_id' => $id, 'offset' => $offset, 'written' => $written);
}

function vpt_bot_finish($payload) {
    $id = vpt_bot_upload_id($payload);
    $existing = get_transient(vpt_bot_result_key($id));
    if (is_array($existing)) {
        return $existing;
    }
    $meta = get_transient(vpt_bot_meta_key($id));
    if (!is_array($meta)) {
        vbb_fail('Tracking upload session expired.', 410);
    }
    $path = vpt_bot_path($id);
    if (!is_file($path)) {
        vbb_fail('Uploaded tracking file not found.', 404);
    }
    clearstatcache(true, $path);
    if (filesize($path) !== intval($meta['size'])) {
        vbb_fail('Uploaded tracking file size mismatch.', 409);
    }
    if (!hash_equals((string) $meta['sha256'], (string) hash_file('sha256', $path))) {
        @unlink($path);
        delete_transient(vpt_bot_meta_key($id));
        vbb_fail('Uploaded tracking file checksum mismatch.', 409);
    }

    if ($meta['ext'] === 'csv') {
        $rows = Vesta_Smart_Post_Tracking::parse_csv($path);
    } else {
        $rows = Vesta_Smart_Post_Tracking::parse_xlsx($path);
    }
    if (!is_array($rows) || count($rows) < 2) {
        @unlink($path);
        delete_transient(vpt_bot_meta_key($id));
        vbb_fail('No usable rows were found in the tracking spreadsheet.', 422);
    }

    $stats = Vesta_Smart_Post_Tracking::import_rows(
        $rows,
        (string) $meta['filename'],
        !empty($meta['auto_match'])
    );
    $status = vpt_bot_status_data();
    $result = array_merge(array(
        'filename' => (string) $meta['filename'],
        'rows' => max(0, count($rows) - 1),
        'auto_match' => !empty($meta['auto_match']),
    ), $stats, $status);

    @unlink($path);
    delete_transient(vpt_bot_meta_key($id));
    set_transient(vpt_bot_result_key($id), $result, HOUR_IN_SECONDS);
    return $result;
}

function vpt_bot_import_rows_batch($payload) {
    $import_id = isset($payload['import_id']) ? strtolower((string) $payload['import_id']) : '';
    $batch_index = isset($payload['batch_index']) ? intval($payload['batch_index']) : -1;
    $filename = sanitize_file_name(isset($payload['filename']) ? $payload['filename'] : 'tracking.xlsx');
    $rows = isset($payload['rows']) && is_array($payload['rows']) ? $payload['rows'] : array();
    if (!preg_match('/^[a-f0-9]{32}$/', $import_id) || $batch_index < 0) {
        vbb_fail('Invalid tracking row-batch id.', 400);
    }
    if (count($rows) < 2 || count($rows) > 101) {
        vbb_fail('Tracking row batch must contain a header and 1..100 rows.', 400);
    }

    $cache_key = vpt_bot_rows_result_key($import_id, $batch_index);
    $existing = get_transient($cache_key);
    if (is_array($existing)) {
        return $existing;
    }

    // Normalize every cell to a scalar string. The main plugin still owns all
    // header mapping, Woo matching and DB upsert logic.
    $clean_rows = array();
    foreach ($rows as $row) {
        if (!is_array($row)) {
            vbb_fail('Invalid tracking row batch.', 400);
        }
        $clean = array();
        foreach ($row as $cell) {
            if (is_scalar($cell) || $cell === null) {
                $clean[] = trim((string) $cell);
            } else {
                $clean[] = '';
            }
        }
        $clean_rows[] = $clean;
    }

    $opts = Vesta_Smart_Post_Tracking::options();
    $auto_match = array_key_exists('auto_match', $payload)
        ? !empty($payload['auto_match'])
        : (($opts['auto_match_on_import'] ?? 'yes') === 'yes');

    $stats = Vesta_Smart_Post_Tracking::import_rows($clean_rows, $filename, $auto_match);
    $status = vpt_bot_status_data();
    $result = array_merge(array(
        'filename' => $filename,
        'rows' => max(0, count($clean_rows) - 1),
        'batch_index' => $batch_index,
        'auto_match' => $auto_match,
    ), $stats, $status);

    // Makes a timeout/retry safe: the same batch will not be imported twice and
    // the bot receives the original statistics when it retries.
    set_transient($cache_key, $result, 2 * HOUR_IN_SECONDS);
    return $result;
}

function vpt_bot_bridge_route() {
    if (!function_exists('vbb_v2_request') || !function_exists('vbb_v2_authorize') || !vbb_v2_request()) {
        return;
    }
    $raw_op = isset($_GET['o']) ? sanitize_key(wp_unslash($_GET['o'])) : '';
    $ops = array('vpt_status', 'vpt_import_begin', 'vpt_import_chunk', 'vpt_import_finish', 'vpt_import_rows_batch');
    if (!in_array($raw_op, $ops, true)) {
        return;
    }

    list($op, $payload) = vbb_v2_authorize();
    try {
        if ($op === 'vpt_status') {
            $result = vpt_bot_status_data();
        } elseif ($op === 'vpt_import_begin') {
            $result = vpt_bot_begin($payload);
        } elseif ($op === 'vpt_import_chunk') {
            $result = vpt_bot_chunk($payload);
        } elseif ($op === 'vpt_import_rows_batch') {
            $result = vpt_bot_import_rows_batch($payload);
        } else {
            $result = vpt_bot_finish($payload);
        }
        vbb_no_cache();
        wp_send_json_success($result);
    } catch (Throwable $e) {
        vbb_fail($e->getMessage(), 500);
    }
    exit;
}
add_action('template_redirect', 'vpt_bot_bridge_route', -20);
