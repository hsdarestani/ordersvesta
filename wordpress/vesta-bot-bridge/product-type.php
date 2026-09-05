<?php
// Product consistency helpers for Vesta Bot Bridge.
// Keeps variable product taxonomy correct and persists fields carried by the
// signed create_product payload that are not handled by the core bridge object.

if (!defined('ABSPATH')) {
    exit;
}

require_once __DIR__ . '/paid-order.php';

// WooCommerce registers product post types/taxonomies on init. The Bridge's fast
// signed-GET path normally runs on plugins_loaded to avoid loading the theme, but
// taxonomy/product operations cannot safely run that early (product_cat does not
// exist yet and get_terms() returns "Invalid taxonomy"). For only the operations
// that need WooCommerce registration, defer the existing Bridge dispatcher to the
// end of init. Media/ping/paid-order traffic keeps the faster plugins_loaded path.
add_action('plugins_loaded', function () {
    if (!isset($_GET['vbb']) || (string) $_GET['vbb'] !== '2') {
        return;
    }

    $op = isset($_GET['o']) ? sanitize_key(wp_unslash($_GET['o'])) : '';
    $needs_init = array(
        'categories',
        'recent_products',
        'create_product',
        'create_variation',
    );

    if (!in_array($op, $needs_init, true)) {
        return;
    }

    remove_action('plugins_loaded', 'vbb_handle_v2_request', PHP_INT_MAX);
    add_action('init', 'vbb_handle_v2_request', PHP_INT_MAX);
}, PHP_INT_MAX - 1);

function vbb_force_variable_product_type($product_id) {
    $product_id = absint($product_id);
    if (!$product_id || get_post_type($product_id) !== 'product') {
        return;
    }

    $terms = wp_get_object_terms($product_id, 'product_type', array('fields' => 'slugs'));
    if (is_wp_error($terms) || count($terms) !== 1 || !in_array('variable', $terms, true)) {
        wp_set_object_terms($product_id, 'variable', 'product_type', false);
        clean_object_term_cache($product_id, 'product');
        clean_post_cache($product_id);
        if (function_exists('wc_delete_product_transients')) {
            wc_delete_product_transients($product_id);
        }
    }
}

function vbb_repair_existing_variable_products() {
    global $wpdb;

    $parents = $wpdb->get_col(
        "SELECT DISTINCT post_parent
         FROM {$wpdb->posts}
         WHERE post_type = 'product_variation'
           AND post_parent > 0"
    );

    foreach ((array) $parents as $parent_id) {
        vbb_force_variable_product_type($parent_id);
        if (class_exists('WC_Product_Variable')) {
            WC_Product_Variable::sync(absint($parent_id));
        }
    }
}

function vbb_current_create_product_payload() {
    if (!isset($_GET['vbb']) || (string) $_GET['vbb'] !== '2') {
        return array();
    }
    $op = isset($_GET['o']) ? sanitize_key(wp_unslash($_GET['o'])) : '';
    if ($op !== 'create_product') {
        return array();
    }
    $encoded = isset($_GET['d']) ? (string) wp_unslash($_GET['d']) : '';
    if ($encoded === '' || !function_exists('vbb_b64url_decode')) {
        return array();
    }
    $compressed = vbb_b64url_decode($encoded);
    if ($compressed === false) {
        return array();
    }
    $json = @gzuncompress($compressed);
    if ($json === false) {
        return array();
    }
    $payload = json_decode($json, true);
    return is_array($payload) ? $payload : array();
}

add_action('woocommerce_after_product_object_save', function ($product) {
    if (!is_object($product) || !method_exists($product, 'get_id')) {
        return;
    }

    $product_id = absint($product->get_id());
    if ($product instanceof WC_Product_Variable) {
        vbb_force_variable_product_type($product_id);
    }

    // During a signed Bridge create_product call, persist caption/short description
    // and weight directly. This avoids an extra REST endpoint and works for both
    // simple and variable parents.
    $payload = vbb_current_create_product_payload();
    if (!$payload) {
        return;
    }

    if (array_key_exists('short_description', $payload)) {
        wp_update_post(array(
            'ID' => $product_id,
            'post_excerpt' => wp_kses_post((string) $payload['short_description']),
        ));
    }

    if (isset($payload['weight']) && (string) $payload['weight'] !== '') {
        $weight = function_exists('wc_format_decimal')
            ? wc_format_decimal((string) $payload['weight'])
            : sanitize_text_field((string) $payload['weight']);
        update_post_meta($product_id, '_weight', $weight);
    }
}, 999, 1);

function vbb_variation_saved_force_parent_type($variation_id) {
    $parent_id = wp_get_post_parent_id(absint($variation_id));
    if ($parent_id) {
        vbb_force_variable_product_type($parent_id);
        if (class_exists('WC_Product_Variable')) {
            WC_Product_Variable::sync($parent_id);
        }
    }
}
add_action('woocommerce_new_product_variation', 'vbb_variation_saved_force_parent_type', 999, 1);
add_action('woocommerce_update_product_variation', 'vbb_variation_saved_force_parent_type', 999, 1);
