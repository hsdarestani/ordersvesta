<?php
// Paid Vestaland marketplace order sync for Vesta Bot Bridge.
if (!defined('ABSPATH')) { exit; }

function vbb_vestaland_money_to_toman($value) {
    $amount = (float) $value;
    $currency = strtoupper((string) get_woocommerce_currency());
    if ($currency === 'IRR') { $amount = $amount / 10; }
    return (int) round($amount);
}

function vbb_vestaland_clean_address($raw) {
    $raw = is_array($raw) ? $raw : array();
    return array(
        'first_name' => sanitize_text_field($raw['first_name'] ?? ''),
        'last_name'  => sanitize_text_field($raw['last_name'] ?? ''),
        'company'    => sanitize_text_field($raw['company'] ?? ''),
        'address_1'  => sanitize_text_field($raw['address_1'] ?? ''),
        'address_2'  => sanitize_text_field($raw['address_2'] ?? ''),
        'city'       => sanitize_text_field($raw['city'] ?? ''),
        'state'      => sanitize_text_field($raw['state'] ?? ''),
        'postcode'   => sanitize_text_field($raw['postcode'] ?? ''),
        'country'    => 'IR',
        'email'      => sanitize_email($raw['email'] ?? ''),
        'phone'      => sanitize_text_field($raw['phone'] ?? ''),
    );
}

function vbb_vestaland_find_existing_order($receipt, $intent) {
    $orders = wc_get_orders(array(
        'limit' => 1,
        'return' => 'objects',
        'meta_query' => array(
            'relation' => 'OR',
            array('key' => '_vestaland_receipt', 'value' => $receipt),
            array('key' => '_vestaland_intent', 'value' => $intent),
        ),
    ));
    return !empty($orders) ? $orders[0] : null;
}

function vbb_create_paid_vestaland_order($payload) {
    if (!function_exists('wc_create_order') || !function_exists('wc_get_product')) {
        throw new Exception('WooCommerce order API is not available.');
    }

    $receipt = strtolower(trim((string) ($payload['receipt'] ?? '')));
    $intent = trim((string) ($payload['intent'] ?? ''));
    $expected = absint($payload['amount_toman'] ?? 0);
    $items = is_array($payload['items'] ?? null) ? $payload['items'] : array();
    $address = vbb_vestaland_clean_address($payload['address'] ?? array());

    if (!preg_match('/^[a-f0-9]{24,64}$/', $receipt)) { throw new Exception('Invalid Vestaland receipt.'); }
    if (!preg_match('/^[A-Za-z0-9_-]{20,128}$/', $intent)) { throw new Exception('Invalid Vestaland intent.'); }
    if ($expected < 1000 || $expected > 500000000 || !$items) { throw new Exception('Invalid Vestaland order amount/items.'); }
    foreach (array('first_name','last_name','address_1','city','state','postcode','phone') as $required) {
        if (trim((string) ($address[$required] ?? '')) === '') { throw new Exception('Incomplete Vestaland shipping address.'); }
    }

    $existing = vbb_vestaland_find_existing_order($receipt, $intent);
    if ($existing) {
        return array(
            'order_id' => (int) $existing->get_id(),
            'status' => (string) $existing->get_status(),
            'already_exists' => true,
        );
    }

    $verified = array();
    $computed = 0;
    foreach ($items as $row) {
        if (!is_array($row)) { throw new Exception('Invalid Vestaland line item.'); }
        $product_id = absint($row['id'] ?? 0);
        $quantity = max(1, min(20, absint($row['quantity'] ?? 1)));
        $paid_price = absint($row['price_toman'] ?? 0);
        $product = $product_id ? wc_get_product($product_id) : false;
        if (!$product || !$product->exists()) { throw new Exception('Vestaland product not found: ' . $product_id); }
        if (!$product->is_purchasable() || !$product->is_in_stock()) { throw new Exception('Vestaland product is no longer purchasable: ' . $product_id); }
        $current_price = vbb_vestaland_money_to_toman($product->get_price());
        if ($current_price <= 0 || $paid_price !== $current_price) {
            throw new Exception('Vestaland product price changed: ' . $product_id);
        }
        $computed += $current_price * $quantity;
        $verified[] = array($product, $quantity);
    }
    if ($computed !== $expected) { throw new Exception('Vestaland paid amount does not match Vesta items.'); }

    $order = wc_create_order(array('created_via' => 'vestaland'));
    if (is_wp_error($order)) { throw new Exception($order->get_error_message()); }

    try {
        $order->set_address($address, 'billing');
        $shipping = $address;
        unset($shipping['email'], $shipping['phone']);
        $order->set_address($shipping, 'shipping');

        foreach ($verified as $line) {
            $result = $order->add_product($line[0], $line[1]);
            if (!$result) { throw new Exception('Could not add Vestaland product to order.'); }
        }

        $order->set_payment_method('hamoon_zibal');
        $order->set_payment_method_title('Hamoon Cloud / Zibal');
        $order->set_transaction_id($receipt);
        $order->update_meta_data('_vestaland_source', 'vestaland-market');
        $order->update_meta_data('_vestaland_receipt', $receipt);
        $order->update_meta_data('_vestaland_intent', $intent);
        $order->update_meta_data('_vestaland_paid_toman', $expected);
        if (!empty($payload['payload_hash'])) {
            $order->update_meta_data('_vestaland_payload_hash', sanitize_text_field($payload['payload_hash']));
        }
        $order->calculate_totals(false);
        $calculated = vbb_vestaland_money_to_toman($order->get_total());
        if ($calculated !== $expected) { throw new Exception('WooCommerce calculated total differs from paid amount.'); }
        $order->save();
        $order->payment_complete($receipt);
        $order->add_order_note('پرداخت این سفارش در Vestaland از طریق Hamoon Cloud / Zibal تأیید شده است.');
        $order->save();

        return array(
            'order_id' => (int) $order->get_id(),
            'status' => (string) $order->get_status(),
            'already_exists' => false,
        );
    } catch (Throwable $e) {
        if ($order && $order->get_id()) {
            wp_delete_post($order->get_id(), true);
        }
        throw $e;
    }
}

function vbb_vestaland_paid_order_route() {
    if (!function_exists('vbb_v2_request') || !vbb_v2_request()) { return; }
    $op = isset($_GET['o']) ? sanitize_key(wp_unslash($_GET['o'])) : '';
    if ($op !== 'create_paid_order') { return; }
    list($authorized_op, $payload) = vbb_v2_authorize();
    if ($authorized_op !== 'create_paid_order') { vbb_fail('Invalid paid-order operation.', 400); }
    try {
        vbb_no_cache();
        wp_send_json_success(vbb_create_paid_vestaland_order($payload));
    } catch (Throwable $e) {
        vbb_fail($e->getMessage(), 500);
    }
    exit;
}
add_action('plugins_loaded', 'vbb_vestaland_paid_order_route', PHP_INT_MAX - 100);
