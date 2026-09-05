<?php
// Chunked signed-GET media transport for Vesta Bot Bridge.
// Loaded as a companion plugin/module. Avoids outbound HTTP downloads and POST bodies.

if (!defined('ABSPATH')) {
    exit;
}

function vbb_chunk_tmp_dir() {
    $uploads = wp_upload_dir();
    $dir = trailingslashit($uploads['basedir']) . 'vesta-bot-bridge-tmp';
    if (!is_dir($dir)) {
        wp_mkdir_p($dir);
    }
    return $dir;
}


function vbb_cleanup_stale_chunks() {
    $dir = vbb_chunk_tmp_dir();
    $cutoff = time() - (2 * HOUR_IN_SECONDS);
    foreach ((array) glob(trailingslashit($dir) . '*.part') as $path) {
        if (is_file($path) && @filemtime($path) < $cutoff) {
            @unlink($path);
        }
    }
}

function vbb_chunk_id($payload) {
    $id = isset($payload['upload_id']) ? strtolower((string) $payload['upload_id']) : '';
    if (!preg_match('/^[a-f0-9]{32}$/', $id)) {
        vbb_fail('Invalid upload id.', 400);
    }
    return $id;
}

function vbb_chunk_path($id) {
    return trailingslashit(vbb_chunk_tmp_dir()) . $id . '.part';
}

function vbb_chunk_meta_key($id) {
    return 'vbb_upload_' . $id;
}

function vbb_chunk_result_key($id) {
    return 'vbb_upload_result_' . $id;
}

function vbb_chunk_decode($value) {
    $value = strtr((string) $value, '-_', '+/');
    $pad = strlen($value) % 4;
    if ($pad) {
        $value .= str_repeat('=', 4 - $pad);
    }
    return base64_decode($value, true);
}

function vbb_media_begin($payload) {
    vbb_cleanup_stale_chunks();
    $id = vbb_chunk_id($payload);
    $filename = sanitize_file_name(isset($payload['filename']) ? $payload['filename'] : 'vesta-product.jpg');
    $size = isset($payload['size']) ? intval($payload['size']) : 0;
    $sha256 = isset($payload['sha256']) ? strtolower((string) $payload['sha256']) : '';

    if ($size <= 0 || $size > 10 * 1024 * 1024) {
        vbb_fail('Invalid media size.', 400);
    }
    if (!preg_match('/^[a-f0-9]{64}$/', $sha256)) {
        vbb_fail('Invalid media hash.', 400);
    }

    $existing = get_transient(vbb_chunk_result_key($id));
    if (is_array($existing) && !empty($existing['id'])) {
        return array('upload_id' => $id, 'already_finished' => true, 'result' => $existing);
    }

    $path = vbb_chunk_path($id);
    $fh = @fopen($path, 'c+b');
    if (!$fh) {
        throw new Exception('Could not initialize media upload.');
    }
    if (!flock($fh, LOCK_EX)) {
        fclose($fh);
        throw new Exception('Could not lock media upload.');
    }
    ftruncate($fh, 0);
    fflush($fh);
    flock($fh, LOCK_UN);
    fclose($fh);

    set_transient(vbb_chunk_meta_key($id), array(
        'filename' => $filename,
        'size' => $size,
        'sha256' => $sha256,
    ), HOUR_IN_SECONDS);

    return array('upload_id' => $id, 'ready' => true);
}

function vbb_media_chunk($payload) {
    $id = vbb_chunk_id($payload);
    $meta = get_transient(vbb_chunk_meta_key($id));
    if (!is_array($meta)) {
        @unlink(vbb_chunk_path($id));
        vbb_fail('Upload session expired.', 410);
    }

    $offset = isset($payload['offset']) ? intval($payload['offset']) : -1;
    $encoded = isset($payload['data']) ? (string) $payload['data'] : '';
    $bytes = vbb_chunk_decode($encoded);
    if ($offset < 0 || $bytes === false || $bytes === '') {
        vbb_fail('Invalid media chunk.', 400);
    }
    if ($offset + strlen($bytes) > intval($meta['size'])) {
        vbb_fail('Media chunk exceeds declared size.', 400);
    }

    $path = vbb_chunk_path($id);
    $fh = @fopen($path, 'c+b');
    if (!$fh) {
        throw new Exception('Could not open media upload.');
    }
    if (!flock($fh, LOCK_EX)) {
        fclose($fh);
        throw new Exception('Could not lock media upload.');
    }
    if (fseek($fh, $offset) !== 0) {
        flock($fh, LOCK_UN);
        fclose($fh);
        throw new Exception('Could not seek media upload.');
    }
    $written = fwrite($fh, $bytes);
    fflush($fh);
    flock($fh, LOCK_UN);
    fclose($fh);

    if ($written !== strlen($bytes)) {
        throw new Exception('Could not write complete media chunk.');
    }

    return array('upload_id' => $id, 'offset' => $offset, 'written' => $written);
}

function vbb_media_finish($payload) {
    $id = vbb_chunk_id($payload);

    $existing = get_transient(vbb_chunk_result_key($id));
    if (is_array($existing) && !empty($existing['id'])) {
        return $existing;
    }

    $meta = get_transient(vbb_chunk_meta_key($id));
    if (!is_array($meta)) {
        @unlink(vbb_chunk_path($id));
        vbb_fail('Upload session expired.', 410);
    }

    $path = vbb_chunk_path($id);
    if (!is_file($path)) {
        vbb_fail('Uploaded media file not found.', 404);
    }

    clearstatcache(true, $path);
    if (filesize($path) !== intval($meta['size'])) {
        vbb_fail('Uploaded media size mismatch.', 409);
    }
    $actual_hash = hash_file('sha256', $path);
    if (!hash_equals((string) $meta['sha256'], (string) $actual_hash)) {
        @unlink($path);
        delete_transient(vbb_chunk_meta_key($id));
        vbb_fail('Uploaded media checksum mismatch.', 409);
    }

    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/media.php';
    require_once ABSPATH . 'wp-admin/includes/image.php';

    $file = array(
        'name' => (string) $meta['filename'],
        'tmp_name' => $path,
    );
    $attachment_id = media_handle_sideload($file, 0);
    if (is_wp_error($attachment_id)) {
        @unlink($path);
        throw new Exception($attachment_id->get_error_message());
    }

    delete_transient(vbb_chunk_meta_key($id));
    $result = array(
        'id' => (int) $attachment_id,
        'source_url' => (string) wp_get_attachment_url($attachment_id),
    );
    set_transient(vbb_chunk_result_key($id), $result, HOUR_IN_SECONDS);
    return $result;
}

// Handle chunk-only operations before the main v2 dispatcher. Check the raw op first;
// otherwise merely inspecting a normal signed request would consume its nonce.
function vbb_handle_chunk_request() {
    if (!function_exists('vbb_v2_request') || !vbb_v2_request()) {
        return;
    }

    $raw_op = isset($_GET['o']) ? sanitize_key(wp_unslash($_GET['o'])) : '';
    if (!in_array($raw_op, array('media_begin', 'media_chunk', 'media_finish'), true)) {
        return;
    }

    list($op, $payload) = vbb_v2_authorize();

    try {
        if ($op === 'media_begin') {
            $result = vbb_media_begin($payload);
        } elseif ($op === 'media_chunk') {
            $result = vbb_media_chunk($payload);
        } else {
            $result = vbb_media_finish($payload);
        }
        vbb_no_cache();
        wp_send_json_success($result);
    } catch (Throwable $e) {
        vbb_fail($e->getMessage(), 500);
    }
    exit;
}

// The main bridge dispatcher runs on plugins_loaded at PHP_INT_MAX, so media
// operations must be claimed one priority earlier or they fall through as
// "Unknown operation" before template_redirect is reached.
add_action('plugins_loaded', 'vbb_handle_chunk_request', PHP_INT_MAX - 1);
add_action('template_redirect', 'vbb_handle_chunk_request', -1);
