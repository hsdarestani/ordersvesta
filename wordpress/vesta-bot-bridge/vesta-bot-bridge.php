<?php
/**
 * Plugin Name: Vesta Bot Bridge
 * Description: Unified secure bridge between the Vesta Telegram bot and WooCommerce, including signed chunked media upload and variable-product type repair.
 * Version: 1.2.2
 * Author: Vesta
 * Requires at least: 6.0
 * Requires PHP: 7.4
 */

if (!defined('ABSPATH')) {
    exit;
}

define('VBB_TOKEN_OPTION', 'vesta_bot_bridge_token');
define('VBB_VERSION', '1.2.2');

// All bridge features live inside this single WordPress plugin.
require_once __DIR__ . '/chunk-upload.php';
require_once __DIR__ . '/product-type.php';
require_once __DIR__ . '/paid-order.php';

register_activation_hook(__FILE__, function () {
    if (!get_option(VBB_TOKEN_OPTION)) {
        update_option(VBB_TOKEN_OPTION, wp_generate_password(48, false, false), false);
    }
    if (function_exists('vbb_repair_existing_variable_products')) {
        vbb_repair_existing_variable_products();
    }
});

function vbb_get_token() {
    $token = (string) get_option(VBB_TOKEN_OPTION, '');
    if ($token === '') {
        $token = wp_generate_password(48, false, false);
        update_option(VBB_TOKEN_OPTION, $token, false);
    }
    return $token;
}

function vbb_endpoint_url() {
    return home_url('/?vbb=2');
}

add_action('admin_menu', function () {
    add_submenu_page(
        'woocommerce',
        'Vesta Bot Bridge',
        'Vesta Bot Bridge',
        'manage_woocommerce',
        'vesta-bot-bridge',
        'vbb_admin_page'
    );
});

function vbb_admin_page() {
    if (!current_user_can('manage_woocommerce')) {
        wp_die('Access denied.');
    }

    if (isset($_POST['vbb_regenerate'])) {
        check_admin_referer('vbb_regenerate_token');
        update_option(VBB_TOKEN_OPTION, wp_generate_password(48, false, false), false);
        echo '<div class="notice notice-success"><p>توکن جدید ساخته شد. توکن قبلی از همین لحظه نامعتبر است.</p></div>';
    }

    if (isset($_POST['vbb_repair_types'])) {
        check_admin_referer('vbb_repair_types');
        if (function_exists('vbb_repair_existing_variable_products')) {
            vbb_repair_existing_variable_products();
        }
        echo '<div class="notice notice-success"><p>نوع محصولات دارای Variation بررسی و اصلاح شد.</p></div>';
    }

    $token = esc_attr(vbb_get_token());
    $endpoint = esc_html(vbb_endpoint_url());
    ?>
    <div class="wrap">
        <h1>Vesta Bot Bridge</h1>
        <p>نسخه <?php echo esc_html(VBB_VERSION); ?> — یک افزونه واحد برای اتصال امن ربات، آپلود سریع تصاویر و محصولات متغیر.</p>
        <table class="form-table" role="presentation">
            <tr>
                <th>وضعیت WooCommerce</th>
                <td><?php echo class_exists('WooCommerce') ? '<strong style="color:#16833b">فعال</strong>' : '<strong style="color:#b32d2e">WooCommerce فعال نیست</strong>'; ?></td>
            </tr>
            <tr>
                <th>Bridge Endpoint</th>
                <td><code><?php echo $endpoint; ?></code></td>
            </tr>
            <tr>
                <th>امکانات داخلی</th>
                <td>Signed GET + HMAC، آپلود Chunked بدون دانلود خارجی، اصلاح خودکار نوع Variable Product</td>
            </tr>
            <tr>
                <th>Bridge Token</th>
                <td>
                    <input id="vbb-token" type="text" readonly value="<?php echo $token; ?>" style="width:min(720px,100%);font-family:monospace" />
                    <button type="button" class="button" onclick="navigator.clipboard.writeText(document.getElementById('vbb-token').value);this.innerText='کپی شد ✓';">کپی</button>
                    <p class="description">توکن در درخواست‌های شبکه ارسال نمی‌شود؛ فقط برای ساخت امضای HMAC استفاده می‌شود.</p>
                </td>
            </tr>
        </table>
        <form method="post" style="display:inline-block;margin-left:8px" onsubmit="return confirm('توکن قبلی فوراً باطل شود؟');">
            <?php wp_nonce_field('vbb_regenerate_token'); ?>
            <button class="button button-secondary" name="vbb_regenerate" value="1">ساخت توکن جدید</button>
        </form>
        <form method="post" style="display:inline-block">
            <?php wp_nonce_field('vbb_repair_types'); ?>
            <button class="button button-secondary" name="vbb_repair_types" value="1">اصلاح نوع محصولات متغیر قبلی</button>
        </form>
        <hr />
        <p><strong>بعد از آپدیت افزونه لازم نیست توکن قبلی را عوض کنید.</strong> افزونه‌های Media Fix و Product Type Fix جداگانه دیگر لازم نیستند.</p>
    </div>
    <?php
}

function vbb_no_cache() {
    nocache_headers();
    header('X-Robots-Tag: noindex, nofollow', true);
    header('X-Vesta-Bridge: ' . VBB_VERSION, true);
}

function vbb_fail($message, $status = 400) {
    vbb_no_cache();
    wp_send_json_error(array('message' => (string) $message), (int) $status);
}

function vbb_b64url_decode($value) {
    $value = strtr((string) $value, '-_', '+/');
    $pad = strlen($value) % 4;
    if ($pad) {
        $value .= str_repeat('=', 4 - $pad);
    }
    return base64_decode($value, true);
}

function vbb_v2_request() {
    return isset($_GET['vbb']) && (string) $_GET['vbb'] === '2';
}

function vbb_v2_authorize() {
    $ts = isset($_GET['t']) ? (string) wp_unslash($_GET['t']) : '';
    $nonce = isset($_GET['n']) ? (string) wp_unslash($_GET['n']) : '';
    $op = isset($_GET['o']) ? sanitize_key(wp_unslash($_GET['o'])) : '';
    $data = isset($_GET['d']) ? (string) wp_unslash($_GET['d']) : '';
    $sig = isset($_GET['s']) ? strtolower((string) wp_unslash($_GET['s'])) : '';

    if (!ctype_digit($ts) || abs(time() - intval($ts)) > 300) {
        vbb_fail('Expired bridge request.', 401);
    }
    if (!preg_match('/^[a-f0-9]{20,64}$/', $nonce) || !preg_match('/^[a-f0-9]{64}$/', $sig)) {
        vbb_fail('Invalid bridge signature.', 401);
    }
    if ($op === '') {
        vbb_fail('Missing operation.', 400);
    }

    $message = 'v2|' . $ts . '|' . $nonce . '|' . $op . '|' . $data;
    $expected = hash_hmac('sha256', $message, vbb_get_token());
    if (!hash_equals($expected, $sig)) {
        vbb_fail('Invalid bridge signature.', 401);
    }

    $nonce_key = 'vbb_nonce_' . md5($nonce);
    if (get_transient($nonce_key)) {
        vbb_fail('Replay rejected.', 409);
    }
    set_transient($nonce_key, 1, 10 * MINUTE_IN_SECONDS);

    $payload = array();
    if ($data !== '') {
        $compressed = vbb_b64url_decode($data);
        if ($compressed === false) {
            vbb_fail('Invalid payload encoding.', 400);
        }
        $json = @gzuncompress($compressed);
        if ($json === false) {
            vbb_fail('Invalid payload compression.', 400);
        }
        $decoded = json_decode($json, true);
        if (!is_array($decoded)) {
            vbb_fail('Invalid payload JSON.', 400);
        }
        $payload = $decoded;
    }

    return array($op, $payload);
}

function vbb_handle_v2_request() {
    if (!vbb_v2_request()) {
        return;
    }
    // The tracking plugin extends the same signed transport. It is fully loaded
    // by this point, so let it claim its operations before the core dispatcher.
    if (function_exists('vpt_bot_bridge_route')) {
        vpt_bot_bridge_route();
    }
    list($op, $payload) = vbb_v2_authorize();
    vbb_dispatch($op, $payload);
    exit;
}

// Handle Bridge traffic immediately after all active plugins are loaded. This
// skips init, WP_Query, the theme, WP Rocket and other front-end hooks that can
// add tens of seconds on the production shop. Keep template_redirect as a
// compatibility fallback, although successful Bridge requests exit here.
add_action('plugins_loaded', 'vbb_handle_v2_request', PHP_INT_MAX);
add_action('template_redirect', 'vbb_handle_v2_request', 0);

add_action('wp_ajax_vesta_bot_bridge', 'vbb_handle_legacy_request');
add_action('wp_ajax_nopriv_vesta_bot_bridge', 'vbb_handle_legacy_request');
add_action('template_redirect', function () {
    if (isset($_GET['vesta_bot_bridge']) && (string) $_GET['vesta_bot_bridge'] === '1') {
        vbb_handle_legacy_request();
        exit;
    }
}, 1);

function vbb_handle_legacy_request() {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        vbb_fail('POST required.', 405);
    }
    $provided = isset($_POST['token']) ? (string) wp_unslash($_POST['token']) : '';
    if ($provided === '' || !hash_equals(vbb_get_token(), $provided)) {
        vbb_fail('Invalid bridge token.', 401);
    }
    $op = isset($_POST['op']) ? sanitize_key(wp_unslash($_POST['op'])) : '';
    $raw = isset($_POST['payload']) ? (string) wp_unslash($_POST['payload']) : '{}';
    $payload = json_decode($raw, true);
    vbb_dispatch($op, is_array($payload) ? $payload : array());
}

function vbb_dispatch($op, $payload) {
    if (!class_exists('WooCommerce') || !function_exists('wc_get_product')) {
        vbb_fail('WooCommerce is not active.', 503);
    }

    try {
        switch ($op) {
            case 'ping':
                $counts = wp_count_posts('product');
                $total = 0;
                foreach (array('publish', 'draft', 'pending', 'private') as $status) {
                    if (isset($counts->{$status})) {
                        $total += (int) $counts->{$status};
                    }
                }
                vbb_no_cache();
                wp_send_json_success(array(
                    'version' => VBB_VERSION,
                    'transport' => 'signed-get-hmac',
                    'product_count' => $total,
                    'site' => home_url('/'),
                ));
                break;

            case 'categories':
                $terms = get_terms(array('taxonomy' => 'product_cat', 'hide_empty' => false));
                if (is_wp_error($terms)) {
                    throw new Exception($terms->get_error_message());
                }
                $items = array();
                foreach ($terms as $term) {
                    $items[] = array(
                        'id' => (int) $term->term_id,
                        'name' => (string) $term->name,
                        'parent' => (int) $term->parent,
                        'count' => (int) $term->count,
                    );
                }
                vbb_no_cache();
                wp_send_json_success(array('categories' => $items));
                break;

            case 'recent_products':
                $products = wc_get_products(array(
                    'limit' => 10,
                    'orderby' => 'date',
                    'order' => 'DESC',
                    'status' => array('publish', 'draft', 'pending', 'private'),
                ));
                $items = array();
                foreach ($products as $product) {
                    $items[] = array(
                        'id' => (int) $product->get_id(),
                        'name' => (string) $product->get_name(),
                        'stock_quantity' => $product->get_stock_quantity(),
                        'price' => (string) $product->get_price(),
                        'permalink' => get_permalink($product->get_id()),
                    );
                }
                vbb_no_cache();
                wp_send_json_success(array('products' => $items));
                break;

            case 'import_media':
                vbb_no_cache();
                wp_send_json_success(vbb_import_media($payload));
                break;

            case 'upload_media':
                if (empty($_FILES['file'])) {
                    vbb_fail('No file uploaded.', 400);
                }
                require_once ABSPATH . 'wp-admin/includes/file.php';
                require_once ABSPATH . 'wp-admin/includes/media.php';
                require_once ABSPATH . 'wp-admin/includes/image.php';
                $attachment_id = media_handle_upload('file', 0);
                if (is_wp_error($attachment_id)) {
                    throw new Exception($attachment_id->get_error_message());
                }
                vbb_no_cache();
                wp_send_json_success(array(
                    'id' => (int) $attachment_id,
                    'source_url' => (string) wp_get_attachment_url($attachment_id),
                ));
                break;

            case 'create_product':
                vbb_no_cache();
                wp_send_json_success(vbb_create_product($payload));
                break;

            case 'create_variation':
                vbb_no_cache();
                wp_send_json_success(vbb_create_variation($payload));
                break;

            default:
                vbb_fail('Unknown operation.', 400);
        }
    } catch (Throwable $e) {
        vbb_fail($e->getMessage(), 500);
    }
}

function vbb_import_media($data) {
    $url = isset($data['url']) ? esc_url_raw($data['url']) : '';
    $filename = isset($data['filename']) ? sanitize_file_name($data['filename']) : 'vesta-product.jpg';
    $parts = wp_parse_url($url);
    $host = isset($parts['host']) ? strtolower($parts['host']) : '';
    if ($url === '' || $host !== 'ordersvesta.smarbiz.sbs') {
        throw new Exception('Remote media host is not allowed.');
    }

    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/media.php';
    require_once ABSPATH . 'wp-admin/includes/image.php';

    $tmp = download_url($url, 30);
    if (is_wp_error($tmp)) {
        throw new Exception($tmp->get_error_message());
    }

    $file = array('name' => $filename, 'tmp_name' => $tmp);
    $attachment_id = media_handle_sideload($file, 0);
    if (is_wp_error($attachment_id)) {
        @unlink($tmp);
        throw new Exception($attachment_id->get_error_message());
    }

    return array(
        'id' => (int) $attachment_id,
        'source_url' => (string) wp_get_attachment_url($attachment_id),
    );
}

function vbb_image_ids($images) {
    $ids = array();
    if (!is_array($images)) {
        return $ids;
    }
    foreach ($images as $image) {
        if (is_array($image) && !empty($image['id'])) {
            $id = absint($image['id']);
            if ($id) {
                $ids[] = $id;
            }
        }
    }
    return array_values(array_unique($ids));
}

function vbb_create_product($data) {
    $type = isset($data['type']) ? sanitize_key($data['type']) : 'simple';
    $product = ($type === 'variable') ? new WC_Product_Variable() : new WC_Product_Simple();

    $product->set_name(sanitize_text_field(isset($data['name']) ? $data['name'] : ''));
    $product->set_status(isset($data['status']) ? sanitize_key($data['status']) : 'publish');

    $category_ids = array();
    if (!empty($data['categories']) && is_array($data['categories'])) {
        foreach ($data['categories'] as $cat) {
            if (is_array($cat) && !empty($cat['id'])) {
                $category_ids[] = absint($cat['id']);
            }
        }
    }
    if ($category_ids) {
        $product->set_category_ids(array_values(array_unique($category_ids)));
    }

    $image_ids = vbb_image_ids(isset($data['images']) ? $data['images'] : array());
    if ($image_ids) {
        $product->set_image_id($image_ids[0]);
        if (count($image_ids) > 1) {
            $product->set_gallery_image_ids(array_slice($image_ids, 1));
        }
    }

    if ($type === 'simple') {
        $manage_stock = !empty($data['manage_stock']);
        $product->set_manage_stock($manage_stock);
        if ($manage_stock) {
            $product->set_stock_quantity(isset($data['stock_quantity']) ? max(0, intval($data['stock_quantity'])) : 0);
        }
        if (isset($data['regular_price']) && $data['regular_price'] !== '') {
            $product->set_regular_price(wc_format_decimal($data['regular_price']));
        }
        if (isset($data['sale_price']) && $data['sale_price'] !== '') {
            $product->set_sale_price(wc_format_decimal($data['sale_price']));
        }
    } else {
        $attrs = array();
        if (!empty($data['attributes']) && is_array($data['attributes'])) {
            foreach ($data['attributes'] as $raw_attr) {
                if (!is_array($raw_attr) || empty($raw_attr['name'])) {
                    continue;
                }
                $attribute = new WC_Product_Attribute();
                $attribute->set_id(0);
                $attribute->set_name(sanitize_text_field($raw_attr['name']));
                $options = array();
                foreach ((array) (isset($raw_attr['options']) ? $raw_attr['options'] : array()) as $option) {
                    $option = sanitize_text_field($option);
                    if ($option !== '') {
                        $options[] = $option;
                    }
                }
                $attribute->set_options(array_values(array_unique($options)));
                $attribute->set_visible(!isset($raw_attr['visible']) || (bool) $raw_attr['visible']);
                $attribute->set_variation(!isset($raw_attr['variation']) || (bool) $raw_attr['variation']);
                $attrs[] = $attribute;
            }
        }
        if ($attrs) {
            $product->set_attributes($attrs);
        }
    }

    $id = $product->save();
    if (!$id) {
        throw new Exception('Product could not be saved.');
    }

    if ($type === 'variable' && function_exists('vbb_force_variable_product_type')) {
        vbb_force_variable_product_type($id);
    }

    return array(
        'id' => (int) $id,
        'name' => (string) $product->get_name(),
        'permalink' => (string) get_permalink($id),
        'type' => $type,
    );
}

function vbb_create_variation($data) {
    $product_id = isset($data['product_id']) ? absint($data['product_id']) : 0;
    $payload = isset($data['variation']) && is_array($data['variation']) ? $data['variation'] : array();
    if (!$product_id || !wc_get_product($product_id)) {
        throw new Exception('Parent product not found.');
    }

    $variation = new WC_Product_Variation();
    $variation->set_parent_id($product_id);
    if (isset($payload['regular_price']) && $payload['regular_price'] !== '') {
        $variation->set_regular_price(wc_format_decimal($payload['regular_price']));
    }
    if (isset($payload['sale_price']) && $payload['sale_price'] !== '') {
        $variation->set_sale_price(wc_format_decimal($payload['sale_price']));
    }
    $manage_stock = !empty($payload['manage_stock']);
    $variation->set_manage_stock($manage_stock);
    if ($manage_stock) {
        $variation->set_stock_quantity(isset($payload['stock_quantity']) ? max(0, intval($payload['stock_quantity'])) : 0);
    }

    $attrs = array();
    if (!empty($payload['attributes']) && is_array($payload['attributes'])) {
        foreach ($payload['attributes'] as $raw_attr) {
            if (!is_array($raw_attr) || empty($raw_attr['name'])) {
                continue;
            }
            $key = sanitize_title($raw_attr['name']);
            $value = isset($raw_attr['option']) ? sanitize_text_field($raw_attr['option']) : '';
            if ($key !== '' && $value !== '') {
                $attrs[$key] = $value;
            }
        }
    }
    $variation->set_attributes($attrs);

    $id = $variation->save();
    if (!$id) {
        throw new Exception('Variation could not be saved.');
    }

    if (function_exists('vbb_force_variable_product_type')) {
        vbb_force_variable_product_type($product_id);
    }
    WC_Product_Variable::sync($product_id);
    wc_delete_product_transients($product_id);

    return array(
        'id' => (int) $id,
        'parent_id' => (int) $product_id,
        'permalink' => (string) get_permalink($product_id),
    );
}
