<?php
// Contract tests only; no real Mahak requests and no WordPress/WooCommerce writes.
define('ABSPATH', __DIR__);
if (!function_exists('mb_strlen')) { function mb_strlen($v) { return strlen($v); } }
if (!function_exists('mb_stripos')) { function mb_stripos($a, $b, $o=0, $encoding='UTF-8') { return stripos($a, $b, $o); } }
if (!function_exists('mb_substr')) { function mb_substr($s, $start, $length) { return substr($s, $start, $length); } }

$GLOBALS['options'] = array();
$GLOBALS['wc_items'] = array();
$GLOBALS['save_count'] = 0;
$GLOBALS['mahak_write_count'] = 0;
$GLOBALS['catalog'] = array(
    'Products' => array(
        array('ProductCode'=>'998813','Name'=>'Liora Daily Cream','ProductId'=>777,'Deleted'=>false),
        array('ProductCode'=>'998814','Name'=>'Liora Serum','ProductId'=>778,'Deleted'=>false),
        array('ProductCode'=>'998815','Name'=>'Liora Multi Detail','ProductId'=>779,'Deleted'=>false),
    ),
    'ProductDetails' => array(
        array('ProductDetailId'=>9001,'ProductCode'=>'998813','ProductId'=>777,'Barcode'=>'6260000099','Price1'=>990000, 'Count1'=>22),
        array('ProductDetailId'=>9002,'ProductCode'=>'998814','ProductId'=>778,'Barcode'=>'6260000088','Count1'=>41),
        array('ProductDetailId'=>9003,'ProductCode'=>'998815','ProductId'=>779,'Barcode'=>'6a','Count1'=>20),
        array('ProductDetailId'=>9004,'ProductCode'=>'998815','ProductId'=>779,'Barcode'=>'6b','Count1'=>16),
    ),
);
function add_action($hook,$callback,$priority=10) {}
function sanitize_text_field($v) { return trim(strip_tags((string) $v)); }
function admin_url($path) { return 'https://example.test/wp-admin/' . $path; }
function get_option($key,$default=null) { return $GLOBALS['options'][$key] ?? $default; }
function add_option($key,$value,$deprecated='',$autoload=false) {
    if (array_key_exists($key,$GLOBALS['options'])) return false;
    $GLOBALS['options'][$key]=$value; return true;
}
function update_option($key,$value,$autoload=false) { $GLOBALS['options'][$key]=$value; return true; }
function get_posts($args) {
    foreach ($GLOBALS['wc_items'] as $id => $item) {
        if (($item['meta']['_vesta_mahak_product_id'] ?? '') === ($args['meta_value'] ?? '')) { return array($id); }
    }
    return array();
}
function wc_get_product_id_by_sku($sku) {
    foreach ($GLOBALS['wc_items'] as $id=>$item) {
        if (($item['sku'] ?? null) === $sku) { return $id; }
    }
    return 0;
}
class BazaraApi {
    public function __construct($unused) {}
    public function login_token() { return array('success'=>true); }
    public function get_all_data($token,$params) {
        return array('success'=>true,'message'=>$GLOBALS['catalog']);
    }
    public function set_all_data($token,$params) {
        $GLOBALS['mahak_write_count']++;
        throw new RuntimeException('Must never write to Mahak');
    }
}
class WC_Product_Simple {
    private $item = array('meta'=>array());
    public function set_name($v) { $this->item['name']=$v; }
    public function set_status($v) { $this->item['status']=$v; }
    public function set_catalog_visibility($v) { $this->item['visibility']=$v; }
    public function set_sku($v) { $this->item['sku']=$v; }
    public function set_manage_stock($v) { $this->item['manage_stock']=$v; }
    public function update_meta_data($key,$val) { $this->item['meta'][$key]=$val; }
    public function save() {
        $GLOBALS['save_count']++;
        $id = 3000 + $GLOBALS['save_count'];
        $GLOBALS['wc_items'][$id]=$this->item;
        return $id;
    }
}
require __DIR__ . '/../wordpress/mu-plugins/vesta-mahak-product-bridge.php';
require __DIR__ . '/../wordpress/mu-plugins/vesta-mahak-site-import.php';
function expect($ok, $message) { if (!$ok) { throw new RuntimeException($message); } }
$search = vms_search(array('query'=>'Daily'));
expect($search['total'] === 1, 'name search count incorrect');
expect($search['items'][0]['code'] === '998813', 'name search code incorrect');
$barcode = vms_search(array('query'=>'6260000099'));
expect($barcode['total'] === 1, 'barcode search count incorrect');
expect($barcode['items'][0]['product_id'] === 777, 'barcode did not find correct accounting product');
$payload = array('product_id'=>777, 'code'=>'998813');
$first = vms_import_existing($payload);
expect($first['status'] === 'draft_created', 'import did not create draft');
expect($GLOBALS['save_count'] === 1, 'first import must create one Woo product');
expect($GLOBALS['wc_items'][3001]['status'] === 'draft', 'must never publish directly');
expect($GLOBALS['wc_items'][3001]['visibility'] === 'hidden', 'draft must be hidden');
expect($GLOBALS['wc_items'][3001]['sku'] === '998813', 'Mahak product code must be SKU');
expect($GLOBALS['wc_items'][3001]['manage_stock'] === false, 'do not copy accounting stock');
expect(!isset($GLOBALS['wc_items'][3001]['price']), 'do not guess Rial/Toman prices');
expect($GLOBALS['wc_items'][3001]['meta']['_vesta_mahak_product_id'] === '777', 'accounting ID lost');
expect($GLOBALS['wc_items'][3001]['meta']['mahak_id'] === '777', 'Bazara Mahak parent link missing');
expect($GLOBALS['wc_items'][3001]['meta']['mahak_product_detail_id'] === '9001', 'Bazara Mahak detail link missing');
$again = vms_import_existing($payload);
expect($again['woo_id'] === 3001 && $GLOBALS['save_count'] === 1, 'duplicate import was not idempotent');
$existing = vms_import_existing(array('product_id'=>778,'code'=>'998814'));
expect($existing['status'] === 'draft_created', 'second product should import');
$by_barcode = vms_search(array('query'=>'6260000088'));
expect($by_barcode['items'][0]['woo_id'] === 3002, 'existing Woo product not detected');
try {
    vms_import_existing(array('product_id'=>779,'code'=>'998815'));
    throw new RuntimeException('Multiple accounting details were incorrectly imported as simple product');
} catch (RuntimeException $e) {
    expect(strpos($e->getMessage(), 'تنوع') !== false, 'wrong variation guard');
}
try {
    vms_import_existing(array('product_id'=>777,'code'=>'998814'));
    throw new RuntimeException('Mismatched Mahak identity accepted');
} catch (RuntimeException $e) {
    expect(strpos($e->getMessage(), 'پیدا نشد') !== false, 'identity check failed incorrectly');
}
expect($GLOBALS['mahak_write_count'] === 0, 'Mahak was mutated');
echo "MAHAK_SITE_IMPORT_CONTRACT=PASSED\n";
