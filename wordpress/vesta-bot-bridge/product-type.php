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


function vbb_mutation_client_key($value) {
    $key = trim((string) $value);
    if ($key === '') {
        return '';
    }
    if (strlen($key) > 160 || !preg_match('/^[A-Za-z0-9:_-]{8,160}$/', $key)) {
        throw new Exception('Invalid client operation key.');
    }
    return $key;
}

function vbb_find_mutation_by_key($client_key, $post_type, $parent_id = 0) {
    if ($client_key === '') {
        return 0;
    }

    $args = array(
        'post_type' => $post_type,
        'post_status' => 'any',
        'posts_per_page' => 1,
        'fields' => 'ids',
        'meta_key' => '_vbb_client_key',
        'meta_value' => $client_key,
        'orderby' => 'ID',
        'order' => 'ASC',
        'no_found_rows' => true,
        'suppress_filters' => true,
    );
    if ($parent_id) {
        $args['post_parent'] = absint($parent_id);
    }
    $ids = get_posts($args);
    return !empty($ids) ? absint($ids[0]) : 0;
}

function vbb_mutation_lock_key($client_key) {
    return 'vbb_mutating_' . md5($client_key);
}

function vbb_mutation_existing_result($op, $id, $parent_id = 0) {
    if ($op === 'create_product') {
        $product = wc_get_product($id);
        if (!$product || !$product->exists()) {
            return array();
        }
        return array(
            'id' => (int) $id,
            'name' => (string) $product->get_name(),
            'permalink' => (string) get_permalink($id),
            'type' => (string) $product->get_type(),
            'already_exists' => true,
        );
    }

    $variation = wc_get_product($id);
    if (!$variation || !$variation->exists()) {
        return array();
    }
    return array(
        'id' => (int) $id,
        'parent_id' => (int) $parent_id,
        'permalink' => (string) get_permalink($parent_id),
        'already_exists' => true,
    );
}

// Store the idempotency key in the same WooCommerce object save. This is
// important because the expensive variable-product sync happens after the
// variation has already been saved: even if the HTTP response times out during
// that sync, a retry can find the saved object instead of creating a duplicate.
add_action('woocommerce_before_product_object_save', function ($product) {
    $context = isset($GLOBALS['vbb_mutation_context']) && is_array($GLOBALS['vbb_mutation_context'])
        ? $GLOBALS['vbb_mutation_context']
        : array();
    if (empty($context['client_key']) || !is_object($product) || !method_exists($product, 'update_meta_data')) {
        return;
    }

    $expected = isset($context['object_type']) ? (string) $context['object_type'] : '';
    if ($expected === 'product' && $product instanceof WC_Product_Variation) {
        return;
    }
    if ($expected === 'variation' && !($product instanceof WC_Product_Variation)) {
        return;
    }
    $product->update_meta_data('_vbb_client_key', (string) $context['client_key']);
}, 1, 1);

function vbb_handle_idempotent_mutation_request() {
    if (!function_exists('vbb_v2_request') || !vbb_v2_request()) {
        return;
    }

    $raw_op = isset($_GET['o']) ? sanitize_key(wp_unslash($_GET['o'])) : '';
    if (!in_array($raw_op, array('create_product', 'create_variation'), true)) {
        return;
    }

    list($op, $payload) = vbb_v2_authorize();
    $client_key = '';
    $lock_key = '';

    try {
        if ($op === 'create_product') {
            $client_key = vbb_mutation_client_key(isset($payload['client_key']) ? $payload['client_key'] : '');
            if ($client_key !== '') {
                $existing_id = vbb_find_mutation_by_key($client_key, 'product');
                if ($existing_id) {
                    $result = vbb_mutation_existing_result($op, $existing_id);
                    if ($result) {
                        vbb_no_cache();
                        wp_send_json_success($result);
                    }
                }
            }

            if ($client_key !== '') {
                $lock_key = vbb_mutation_lock_key($client_key);
                if (get_transient($lock_key)) {
                    vbb_fail('This product mutation is still processing. Retry shortly.', 409);
                }
                set_transient($lock_key, 1, 2 * MINUTE_IN_SECONDS);
                $GLOBALS['vbb_mutation_context'] = array(
                    'client_key' => $client_key,
                    'object_type' => 'product',
                );
            }

            $result = vbb_create_product($payload);
            if ($client_key !== '' && !empty($result['id'])) {
                update_post_meta(absint($result['id']), '_vbb_client_key', $client_key);
            }
        } else {
            $product_id = isset($payload['product_id']) ? absint($payload['product_id']) : 0;
            $variation_payload = isset($payload['variation']) && is_array($payload['variation'])
                ? $payload['variation']
                : array();
            $client_key = vbb_mutation_client_key(isset($variation_payload['client_key']) ? $variation_payload['client_key'] : '');

            if ($client_key !== '') {
                $existing_id = vbb_find_mutation_by_key($client_key, 'product_variation', $product_id);
                if ($existing_id) {
                    $result = vbb_mutation_existing_result($op, $existing_id, $product_id);
                    if ($result) {
                        vbb_no_cache();
                        wp_send_json_success($result);
                    }
                }

                $lock_key = vbb_mutation_lock_key($client_key);
                if (get_transient($lock_key)) {
                    vbb_fail('This variation mutation is still processing. Retry shortly.', 409);
                }
                set_transient($lock_key, 1, 2 * MINUTE_IN_SECONDS);
                $GLOBALS['vbb_mutation_context'] = array(
                    'client_key' => $client_key,
                    'object_type' => 'variation',
                );
            }

            $result = vbb_create_variation($payload);
            if ($client_key !== '' && !empty($result['id'])) {
                update_post_meta(absint($result['id']), '_vbb_client_key', $client_key);
            }
        }

        unset($GLOBALS['vbb_mutation_context']);
        if ($lock_key !== '') {
            delete_transient($lock_key);
        }
        if (is_array($result)) {
            $result['already_exists'] = false;
        }
        vbb_no_cache();
        wp_send_json_success($result);
    } catch (Throwable $e) {
        unset($GLOBALS['vbb_mutation_context']);
        if ($lock_key !== '') {
            delete_transient($lock_key);
        }
        vbb_fail($e->getMessage(), 500);
    }
    exit;
}

// product-type.php already defers these mutations to init/PHP_INT_MAX. Claim
// them one priority before the core dispatcher so we can make them idempotent.
add_action('init', 'vbb_handle_idempotent_mutation_request', PHP_INT_MAX - 1);

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
        // Keep the parent taxonomy correct, but DO NOT run a full variable-product
        // sync from this save hook. vbb_create_variation() performs the required
        // sync once after the child save. The old hook synced here as well, so each
        // Bridge variation triggered two expensive full-parent syncs and routinely
        // exceeded the bot's request timeout on the production shop.
        vbb_force_variable_product_type($parent_id);
    }
}
add_action('woocommerce_new_product_variation', 'vbb_variation_saved_force_parent_type', 999, 1);
add_action('woocommerce_update_product_variation', 'vbb_variation_saved_force_parent_type', 999, 1);
