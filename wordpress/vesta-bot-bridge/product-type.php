<?php
// Product-type consistency for Vesta Bot Bridge.
// Keeps parent products with variations stored as WooCommerce variable products.

if (!defined('ABSPATH')) {
    exit;
}

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

add_action('woocommerce_after_product_object_save', function ($product) {
    if (is_object($product) && $product instanceof WC_Product_Variable) {
        vbb_force_variable_product_type($product->get_id());
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
