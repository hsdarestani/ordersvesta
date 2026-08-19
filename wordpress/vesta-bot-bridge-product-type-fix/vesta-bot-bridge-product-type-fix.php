<?php
/**
 * Plugin Name: Vesta Bot Bridge - Product Type Fix
 * Description: Ensures products created with Vesta Bot Bridge are stored as Variable when they contain variations, and repairs existing affected products.
 * Version: 1.0.0
 * Author: Vesta
 * Requires PHP: 7.4
 */

if (!defined('ABSPATH')) {
    exit;
}

function vbbptf_force_variable($product_id) {
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

function vbbptf_repair_existing() {
    global $wpdb;

    // Any product that owns product_variation children must be a variable product.
    $parents = $wpdb->get_col(
        "SELECT DISTINCT post_parent
         FROM {$wpdb->posts}
         WHERE post_type = 'product_variation'
           AND post_parent > 0"
    );

    foreach ((array) $parents as $parent_id) {
        vbbptf_force_variable($parent_id);
        if (class_exists('WC_Product_Variable')) {
            WC_Product_Variable::sync(absint($parent_id));
        }
    }
}

register_activation_hook(__FILE__, 'vbbptf_repair_existing');

// When the Bridge saves a WC_Product_Variable, explicitly persist the product_type
// taxonomy after all normal WooCommerce save logic.
add_action('woocommerce_after_product_object_save', function ($product) {
    if (is_object($product) && $product instanceof WC_Product_Variable) {
        vbbptf_force_variable($product->get_id());
    }
}, 999, 1);

// Extra safety: as soon as any variation is created/updated, force its parent to variable.
function vbbptf_variation_saved($variation_id) {
    $parent_id = wp_get_post_parent_id(absint($variation_id));
    if ($parent_id) {
        vbbptf_force_variable($parent_id);
        if (class_exists('WC_Product_Variable')) {
            WC_Product_Variable::sync($parent_id);
        }
    }
}
add_action('woocommerce_new_product_variation', 'vbbptf_variation_saved', 999, 1);
add_action('woocommerce_update_product_variation', 'vbbptf_variation_saved', 999, 1);
