<?php
/**
 * Plugin Name: Vesta Mahak Product Bridge
 * Description: Submit Liora's separately approved accounting products to Mahak via the existing signed Vesta bridge.
 * Version: 0.1.0
 *
 * IMPORTANT: Mahak SaveAllData confirms cloud acceptance, NOT desktop import.
 * This bridge never creates WooCommerce products or updates stock.
 */
if (!defined('ABSPATH')) { exit; }

function vmb_api() {
    if (!class_exists('BazaraApi')) {
        throw new RuntimeException('افزونه بازارا فعال نیست یا API آن بارگذاری نشده است.');
    }
    $api = new BazaraApi(true);
    $login = $api->login_token();
    if (!is_array($login) || empty($login['success'])) {
        throw new RuntimeException('ورود به API بازارا ناموفق بود.');
    }
    return $api;
}

function vmb_catalog($api) {
    $response = $api->get_all_data('', array(
        'fromProductVersion' => 0,
        'fromProductDetailVersion' => 0,
    ));
    if (!is_array($response) || empty($response['success']) || !is_array($response['message'] ?? null)) {
        throw new RuntimeException('دریافت فهرست کالاهای محک از بازارا ناموفق بود.');
    }
    return $response['message'];
}

function vmb_by_code($catalog, $code) {
    foreach (($catalog['Products'] ?? array()) as $product) {
        $p = (array) $product;
        if ((string) ($p['ProductCode'] ?? '') === (string) $code && empty($p['Deleted'])) {
            return $p;
        }
    }
    return null;
}

function vmb_request_key($key) {
    if (!is_string($key) || !preg_match('/^[a-f0-9]{32}$/D', $key)) {
        throw new InvalidArgumentException('شناسه یکتای درخواست نامعتبر است.');
    }
    return 'vmb_req_' . hash('sha256', $key);
}

function vmb_template($data) {
    $code = trim((string) ($data['template_code'] ?? ''));
    if (!preg_match('/^[0-9A-Za-z_-]{1,24}$/D', $code)) {
        throw new InvalidArgumentException('کد کالای الگو نامعتبر است.');
    }
    $catalog = vmb_catalog(vmb_api());
    $p = vmb_by_code($catalog, $code);
    if (!$p) { throw new RuntimeException('کالای الگو با این کد در فهرست محک پیدا نشد.'); }
    return array(
        'code' => (string) $p['ProductCode'],
        'name' => (string) ($p['Name'] ?? ''),
        'category_id' => (int) ($p['ProductCategoryId'] ?? 0),
        'unit_name' => (string) ($p['UnitName'] ?? 'عدد'),
        'unit_name2' => (string) ($p['UnitName2'] ?? 'عدد'),
    );
}

function vmb_public_state($record) {
    return array(
        'status' => (string) ($record['status'] ?? 'unknown'),
        'code' => (string) ($record['code'] ?? ''),
        'name' => (string) ($record['name'] ?? ''),
        'accepted_at' => (string) ($record['accepted_at'] ?? ''),
        'note' => (string) ($record['note'] ?? ''),
    );
}

function vmb_status($data) {
    $key = vmb_request_key((string) ($data['request_key'] ?? ''));
    $record = get_option($key, null);
    if (!is_array($record)) {
        return array('status' => 'not_submitted');
    }
    if (in_array($record['status'] ?? '', array('accepted_pending_sync','visible_in_cloud_export','unknown'), true)) {
        $catalog = vmb_catalog(vmb_api());
        $product = vmb_by_code($catalog, $record['code']);
        if ($product && (string) ($product['Name'] ?? '') === $record['name']) {
            $record['status'] = 'visible_in_cloud_export';
            $record['mahak_product_id'] = (int) ($product['ProductId'] ?? 0);
            update_option($key, $record, false);
        }
    }
    $state = vmb_public_state($record);
    if (!empty($record['mahak_product_id'])) {
        $state['mahak_product_id'] = (int) $record['mahak_product_id'];
    }
    return $state;
}

function vmb_submit($data) {
    $request_id = (string) ($data['request_key'] ?? '');
    $request_key = vmb_request_key($request_id);
    $name = sanitize_text_field(trim((string) ($data['name'] ?? '')));
    $code = trim((string) ($data['code'] ?? ''));
    $template_code = trim((string) ($data['template_code'] ?? ''));
    $price = trim((string) ($data['price_rial'] ?? ''));
    if (mb_strlen($name) < 3 || mb_strlen($name) > 180) {
        throw new InvalidArgumentException('نام کالا باید بین ۳ تا ۱۸۰ نویسه باشد.');
    }
    if (!preg_match('/^[0-9A-Za-z_-]{1,24}$/D', $code) ||
        !preg_match('/^[0-9A-Za-z_-]{1,24}$/D', $template_code) ||
        $code === $template_code) {
        throw new InvalidArgumentException('کد کالا یا کد الگو نامعتبر یا تکراری است.');
    }
    if (!preg_match('/^\d{1,14}$/D', $price)) {
        throw new InvalidArgumentException('قیمت باید عدد صحیح غیرمنفی به ریال باشد.');
    }
    $previous = get_option($request_key, null);
    if (is_array($previous)) {
        if ((string) ($previous['code'] ?? '') !== $code) {
            throw new RuntimeException('این شناسه درخواست قبلاً برای کالای دیگری استفاده شده است.');
        }
        return vmb_status(array('request_key' => $request_id));
    }
    $api = vmb_api();
    $catalog = vmb_catalog($api);
    if (vmb_by_code($catalog, $code)) {
        throw new RuntimeException('کد کالا از قبل در کاتالوگ محک وجود دارد؛ ارسال متوقف شد.');
    }
    $template = vmb_by_code($catalog, $template_code);
    if (!$template) { throw new RuntimeException('کالای الگو در کاتالوگ محک وجود ندارد.'); }

    // Permanent code reservation: a timeout MUST NOT cause an automatic resubmit
    // while the desktop accounting software may be importing the first request.
    $code_key = 'vmb_code_' . hash('sha256', $code);
    if (!add_option($code_key, $request_id, '', false)) {
        throw new RuntimeException('این کد کالا یک درخواست ثبت قبلی یا درحال‌انتظار دارد.');
    }
    $record = array(
        'status' => 'submitting',
        'code' => $code,
        'name' => $name,
        'template_code' => $template_code,
        'requested_at' => gmdate('c'),
    );
    if (!add_option($request_key, $record, '', false)) {
        return vmb_status(array('request_key' => $request_id));
    }

    $cid = 1100000000 + (hexdec(substr(hash('sha256', $request_id . ':p'), 0, 7)) % 500000000);
    $did = 1600000000 + (hexdec(substr(hash('sha256', $request_id . ':d'), 0, 7)) % 400000000);
    $unit = (string) ($template['UnitName'] ?? 'عدد');
    $unit2 = (string) ($template['UnitName2'] ?? $unit);
    $product = array(
        'productId' => 0, 'productClientId' => (int) $cid,
        'productCode' => $code,
        'productCategoryId' => (int) ($template['ProductCategoryId'] ?? 0),
        'name' => $name, 'unitName' => $unit, 'unitName2' => $unit2,
        'unitRatio' => 0, 'taxPercent' => 0, 'chargePercent' => 0,
        'type' => 0, 'tags' => '',
        'description' => 'Created from Liora Telegram bot; pending Mahak desktop sync',
        'deleted' => false,
    );
    $details = array(
        'productDetailId' => 0, 'productDetailClientId' => (int) $did,
        'productDetailCode' => 1, 'productId' => 0,
        'productClientId' => (int) $cid, 'productCode' => $code,
        'properties' => '', 'properties2' => '',
        'count1' => 0, 'count2' => 0,
        'barcode' => '', 'price1' => (int) $price, 'price2' => 0,
        'defaultSellPriceLevel' => 1, 'defaultDiscountLevel' => 1,
        'deleted' => false,
    );
    try {
        $raw = $api->set_all_data('', array(
            'products' => array($product),
            'productDetails' => array($details),
        ));
        $res = is_string($raw) ? json_decode($raw, true) : $raw;
        if (!is_array($res) || empty($res['success'])) {
            $reason = 'سرور محک درخواست ایجاد کالا را نپذیرفت.';
            $detail = $res['message'] ?? '';
            $parsed = is_string($detail) ? json_decode($detail, true) : $detail;
            if (is_array($parsed) && isset($parsed['Message'])) {
                $reason .= ' ' . mb_substr((string) $parsed['Message'], 0, 300);
            }
            $record['status'] = 'rejected';
            $record['note'] = $reason;
            update_option($request_key, $record, false);
            return vmb_public_state($record);
        }
        $record['status'] = 'accepted_pending_sync';
        $record['accepted_at'] = gmdate('c');
        $record['note'] = 'توسط API پذیرفته شد؛ ورود به محک ویندوزی هنوز تأیید نشده است.';
        update_option($request_key, $record, false);
        return vmb_public_state($record);
    } catch (Throwable $e) {
        $record['status'] = 'unknown';
        $record['note'] = 'پاسخ قطعی دریافت نشد؛ درخواست نباید خودکار دوباره ارسال شود.';
        update_option($request_key, $record, false);
        return vmb_public_state($record);
    }
}

add_action('plugins_loaded', function () {
    if (!isset($_GET['vbb']) || (string) $_GET['vbb'] !== '2') { return; }
    $op = sanitize_key((string) ($_GET['o'] ?? ''));
    if (!in_array($op, array('mahak_template','mahak_submit','mahak_status'), true)) { return; }
    if (!function_exists('vbb_v2_authorize')) { return; }
    list($validated_op, $payload) = vbb_v2_authorize();
    if ($validated_op !== $op) { vbb_fail('Invalid operation.', 400); }
    try {
        switch ($op) {
            case 'mahak_template': $response = vmb_template($payload); break;
            case 'mahak_submit': $response = vmb_submit($payload); break;
            case 'mahak_status': $response = vmb_status($payload); break;
        }
        vbb_no_cache();
        wp_send_json_success($response);
    } catch (Throwable $e) {
        vbb_fail($e->getMessage(), 400);
    }
}, PHP_INT_MAX - 1);
