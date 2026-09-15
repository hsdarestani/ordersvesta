<?php
// Compact read-only WooCommerce order feed for the Telegram tracking-SMS flow.
// It reuses the existing HMAC-signed Bridge transport and exposes only fields
// required for name/city matching and SMS delivery.

if (!defined('ABSPATH')) {
    exit;
}

function vbb_sms_orders_record($order) {
    $shipping_first = (string) $order->get_shipping_first_name();
    $shipping_last = (string) $order->get_shipping_last_name();
    $billing_first = (string) $order->get_billing_first_name();
    $billing_last = (string) $order->get_billing_last_name();

    $first = $shipping_first !== '' ? $shipping_first : $billing_first;
    $last = $shipping_last !== '' ? $shipping_last : $billing_last;
    $city = (string) $order->get_shipping_city();
    if ($city === '') {
        $city = (string) $order->get_billing_city();
    }

    $address1 = (string) $order->get_shipping_address_1();
    $address2 = (string) $order->get_shipping_address_2();
    if ($address1 === '') {
        $address1 = (string) $order->get_billing_address_1();
        $address2 = (string) $order->get_billing_address_2();
    }

    return array(
        'id' => (int) $order->get_id(),
        'number' => (string) $order->get_order_number(),
        'status' => (string) $order->get_status(),
        'name' => trim($first . ' ' . $last),
        'city' => $city,
        'address' => trim($address1 . ' ' . $address2),
        'phone' => (string) $order->get_billing_phone(),
        'date_created' => $order->get_date_created() ? $order->get_date_created()->date('c') : '',
    );
}

function vbb_sms_orders_page($payload) {
    if (!function_exists('wc_get_orders')) {
        throw new Exception('WooCommerce orders API is unavailable.');
    }

    $page = max(1, isset($payload['page']) ? intval($payload['page']) : 1);
    $per_page = isset($payload['per_page']) ? intval($payload['per_page']) : 100;
    $per_page = max(1, min(100, $per_page));

    $query = wc_get_orders(array(
        'limit' => $per_page,
        'page' => $page,
        'paginate' => true,
        'orderby' => 'date',
        'order' => 'DESC',
        'return' => 'objects',
    ));

    $orders = array();
    foreach ((array) $query->orders as $order) {
        if (!$order instanceof WC_Order) {
            continue;
        }
        if (in_array($order->get_status(), array('cancelled', 'refunded', 'failed', 'trash'), true)) {
            continue;
        }
        $orders[] = vbb_sms_orders_record($order);
    }

    return array(
        'page' => $page,
        'per_page' => $per_page,
        'total' => isset($query->total) ? intval($query->total) : count($orders),
        'total_pages' => isset($query->max_num_pages) ? intval($query->max_num_pages) : 1,
        'orders' => $orders,
    );
}

function vbb_handle_sms_orders_request() {
    if (!function_exists('vbb_v2_request') || !function_exists('vbb_v2_authorize') || !vbb_v2_request()) {
        return;
    }
    $raw_op = isset($_GET['o']) ? sanitize_key(wp_unslash($_GET['o'])) : '';
    if ($raw_op !== 'sms_orders') {
        return;
    }

    list($op, $payload) = vbb_v2_authorize();
    try {
        $result = vbb_sms_orders_page($payload);
        vbb_no_cache();
        wp_send_json_success($result);
    } catch (Throwable $e) {
        vbb_fail($e->getMessage(), 500);
    }
    exit;
}

// Claim the operation before the main Bridge dispatcher consumes the signed nonce.
add_action('plugins_loaded', 'vbb_handle_sms_orders_request', PHP_INT_MAX - 2);
add_action('template_redirect', 'vbb_handle_sms_orders_request', -2);
