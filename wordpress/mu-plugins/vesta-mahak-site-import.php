<?php
/**
 * Plugin Name: Vesta Mahak Existing Products to WooCommerce
 * Description: Signed, read-only search of existing Mahak products and idempotent WooCommerce draft import.
 * Version: 0.1.0
 *
 * Never creates/updates a Mahak product and never imports accounting quantities or prices.
 * Requires vesta-mahak-product-bridge.php and the signed Vesta Bot Bridge.
 */
if (!defined('ABSPATH')) { exit; }

function vms_normalize($value) {
    $value = str_replace(
        array('ي', 'ك', '‌', '۰','۱','۲','۳','۴','۵','۶','۷','۸','۹','٠','١','٢','٣','٤','٥','٦','٧','٨','٩'),
        array('ی', 'ک', ' ', '0','1','2','3','4','5','6','7','8','9','0','1','2','3','4','5','6','7','8','9'),
        (string) $value
    );
    return trim(preg_replace('/\s+/u', ' ', $value));
}

function vms_product_details($catalog, $product) {
    $found = array();
    foreach ((array) ($catalog['ProductDetails'] ?? array()) as $item) {
        $detail = (array) $item;
        if (!empty($detail['Deleted'])) { continue; }
        if (
            (string) ($detail['ProductId'] ?? '') === (string) ($product['ProductId'] ?? '')
            || (string) ($detail['ProductCode'] ?? '') === (string) ($product['ProductCode'] ?? '')
        ) {
            $found[] = $detail;
        }
    }
    return $found;
}

function vms_existing_woo($code, $product_id) {
    if (!function_exists('wc_get_product_id_by_sku')) {
        throw new RuntimeException('ووکامرس فعال نیست.');
    }
    $by_sku = (int) wc_get_product_id_by_sku((string) $code);
    if ($by_sku) { return $by_sku; }
    if ($product_id && function_exists('get_posts')) {
        $matches = get_posts(array(
            'post_type' => 'product',
            'post_status' => array('publish', 'private', 'draft', 'pending', 'future'),
            'posts_per_page' => 1,
            'fields' => 'ids',
            'meta_key' => '_vesta_mahak_product_id',
            'meta_value' => (string) $product_id,
            'suppress_filters' => true,
        ));
        if ($matches) { return (int) $matches[0]; }
    }
    return 0;
}

function vms_search($payload) {
    $term = vms_normalize($payload['query'] ?? '');
    if (mb_strlen($term) < 2 || mb_strlen($term) > 100) {
        throw new InvalidArgumentException('حداقل دو و حداکثر صد حرف از نام یا بارکد را بفرستید.');
    }
    $catalog = vmb_catalog(vmb_api());
    $matches = array();
    foreach ((array) ($catalog['Products'] ?? array()) as $raw) {
        $product = (array) $raw;
        if (!empty($product['Deleted'])) { continue; }
        $code = trim((string) ($product['ProductCode'] ?? ''));
        $name = trim((string) ($product['Name'] ?? ''));
        $id = (int) ($product['ProductId'] ?? 0);
        if (!$id || $code === '' || $name === '') { continue; }
        $normalized_name = vms_normalize($name);
        $normalized_code = vms_normalize($code);
        $details = vms_product_details($catalog, $product);
        $barcodes = array();
        foreach ($details as $d) {
            foreach (array('Barcode', 'BarCode', 'ProductDetailCode') as $field) {
                if (isset($d[$field]) && trim((string) $d[$field]) !== '') {
                    $barcodes[] = vms_normalize($d[$field]);
                }
            }
        }
        $score = 0;
        if ($normalized_name === $term || $normalized_code === $term || in_array($term, $barcodes, true)) {
            $score = 100;
        } elseif (mb_stripos($normalized_name, $term, 0, 'UTF-8') !== false) {
            $score = 50;
        } elseif (mb_stripos($normalized_code, $term, 0, 'UTF-8') !== false) {
            $score = 40;
        } else {
            foreach ($barcodes as $barcode) {
                if (mb_stripos($barcode, $term, 0, 'UTF-8') !== false) { $score = 30; break; }
            }
        }
        if (!$score) { continue; }
        $matches[] = array(
            'score' => $score,
            'code' => $code,
            'name' => $name,
            'product_id' => $id,
            'detail_count' => count($details),
        );
    }
    usort($matches, function ($a, $b) {
        $diff = $b['score'] - $a['score'];
        return $diff ?: strcmp($a['name'], $b['name']);
    });
    $total = count($matches);
    $matches = array_slice($matches, 0, 8);
    foreach ($matches as &$m) {
        unset($m['score']);
        $m['woo_id'] = vms_existing_woo($m['code'], $m['product_id']);
    }
    unset($m);
    return array('items' => $matches, 'total' => $total);
}

function vms_key($product_id, $code) {
    return 'vms_site_' . hash('sha256', (string) $product_id . ':' . (string) $code);
}

function vms_public_result($record) {
    $id = (int) ($record['woo_id'] ?? 0);
    return array(
        'status' => (string) ($record['status'] ?? 'unknown'),
        'woo_id' => $id,
        'edit_url' => $id ? admin_url('post.php?post=' . $id . '&action=edit') : '',
        'note' => (string) ($record['note'] ?? ''),
    );
}

function vms_identify($payload, $fetch_catalog = true) {
    $id = (int) ($payload['product_id'] ?? 0);
    $code = trim((string) ($payload['code'] ?? ''));
    if ($id <= 0 || $code === '' || mb_strlen($code) > 80) {
        throw new InvalidArgumentException('شناسه کالای محک نامعتبر است. از جست‌وجوی ربات انتخاب کنید.');
    }
    $product = null;
    $details = array();
    if ($fetch_catalog) {
        $catalog = vmb_catalog(vmb_api());
        $product = vmb_by_code($catalog, $code);
        if (!$product || (int) ($product['ProductId'] ?? 0) !== $id) {
            throw new RuntimeException('کالا با این شناسه و کد در کاتالوگ محک پیدا نشد؛ دوباره جست‌وجو کنید.');
        }
        $details = vms_product_details($catalog, $product);
    }
    return array($id, $code, $product, $details);
}

function vms_import_status($payload) {
    list($id, $code) = vms_identify($payload, false);
    $record = get_option(vms_key($id, $code), null);
    if (is_array($record)) {
        // If PHP timed out after WC save, recover by exact SKU or our own mapping.
        if (in_array(($record['status'] ?? ''), array('pending', 'uncertain'), true)) {
            $woo_id = vms_existing_woo($code, $id);
            if ($woo_id) {
                $record['status'] = 'already_on_site';
                $record['woo_id'] = $woo_id;
                $record['note'] = 'این کالا در ووکامرس وجود دارد؛ ساخت دوباره انجام نشد.';
                update_option(vms_key($id, $code), $record, false);
            }
        }
        return vms_public_result($record);
    }
    $woo_id = vms_existing_woo($code, $id);
    if ($woo_id) {
        return vms_public_result(array('status' => 'already_on_site', 'woo_id' => $woo_id));
    }
    return vms_public_result(array('status' => 'not_imported'));
}

function vms_import_existing($payload) {
    list($id, $code, $p, $details) = vms_identify($payload);
    $key = vms_key($id, $code);
    $previous = get_option($key, null);
    if (is_array($previous)) { return vms_import_status($payload); }

    $woo_id = vms_existing_woo($code, $id);
    if ($woo_id) {
        return vms_public_result(array('status' => 'already_on_site', 'woo_id' => $woo_id));
    }
    if (count($details) > 1) {
        throw new RuntimeException('این کالا چند ردیف/تنوع حسابداری دارد. برای جلوگیری از انتقال اشتباه، ورود خودکار آن فعلاً مجاز نیست.');
    }
    if (!class_exists('WC_Product_Simple')) {
        throw new RuntimeException('ساخت محصول در ووکامرس در دسترس نیست.');
    }

    $record = array(
        'status' => 'pending', 'product_id' => $id, 'code' => $code,
        'note' => 'عملیات شروع شده است؛ تا مشخص‌شدن نتیجه، درخواست تازه نفرستید.',
    );
    // DB-level exclusive reservation. No retry can create a second Woo product.
    if (!add_option($key, $record, '', false)) {
        return vms_import_status($payload);
    }
    try {
        // Recheck after acquiring the reservation, even under concurrent requests.
        $woo_id = vms_existing_woo($code, $id);
        if ($woo_id) {
            $record['status'] = 'already_on_site';
        } else {
            $item = new WC_Product_Simple();
            $item->set_name(sanitize_text_field($p['Name'] ?? ''));
            $item->set_status('draft');
            $item->set_catalog_visibility('hidden');
            $item->set_sku($code);
            $item->set_manage_stock(false);
            // No accounting price/stock is imported without verifying unit conversion
            // and stock sync behavior. A draft cannot be purchased.
            $item->update_meta_data('_vesta_mahak_product_id', (string) $id);
            $item->update_meta_data('_vesta_mahak_product_code', $code);
            $item->update_meta_data('_vesta_mahak_import_source', 'bazara_existing_product');
            $woo_id = (int) $item->save();
            if (!$woo_id) { throw new RuntimeException('ذخیره پیش‌نویس در ووکامرس ناموفق بود.'); }
            $record['status'] = 'draft_created';
        }
        $record['woo_id'] = $woo_id;
        $record['note'] = 'کالا بدون تغییر در محک به‌صورت پیش‌نویس به سایت اضافه شد. عکس، قیمت، دسته‌بندی و موجودی را پیش از انتشار بررسی کنید.';
        update_option($key, $record, false);
    } catch (Throwable $exc) {
        $record['status'] = 'uncertain';
        $record['note'] = 'نتیجه ذخیره قطعی نیست؛ ابتدا وضعیت همین کالا را بررسی کنید. ' .
            mb_substr($exc->getMessage(), 0, 160);
        update_option($key, $record, false);
    }
    return vms_public_result($record);
}

add_action('plugins_loaded', function () {
    if (!isset($_GET['vbb']) || (string) $_GET['vbb'] !== '2') { return; }
    $op = sanitize_key((string) ($_GET['o'] ?? ''));
    if (!in_array($op, array('mahak_site_search', 'mahak_site_import', 'mahak_site_status'), true)) { return; }
    if (!function_exists('vbb_v2_authorize') || !function_exists('vmb_api')) { return; }
    list($validated_op, $payload) = vbb_v2_authorize();
    if ($validated_op !== $op) { vbb_fail('Invalid operation.', 400); }
    try {
        if ($op === 'mahak_site_search') {
            $result = vms_search($payload);
        } elseif ($op === 'mahak_site_import') {
            $result = vms_import_existing($payload);
        } else {
            $result = vms_import_status($payload);
        }
        vbb_no_cache();
        wp_send_json_success($result);
    } catch (Throwable $e) {
        vbb_fail($e->getMessage(), 400);
    }
}, PHP_INT_MAX - 2);
