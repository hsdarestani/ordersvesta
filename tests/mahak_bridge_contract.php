<?php
// Contract test for Vesta Mahak MU bridge. No real network or database writes.
define('ABSPATH', __DIR__);
$GLOBALS['options'] = array();
$GLOBALS['catalog'] = array('Products' => array(
    array('ProductCode'=>'8079', 'Name'=>'Sample product', 'ProductId'=>10684917, 
        'ProductCategoryId'=>418353, 'UnitName'=>'عدد','UnitName2'=>'عدد','Deleted'=>false)
));
$GLOBALS['save_count'] = 0;
$GLOBALS['catalog_read_count'] = 0;
$GLOBALS['catalog_transient_failures'] = 1;
$GLOBALS['outbound'] = null;
function add_action($hook,$callback,$priority=10) {}
function sanitize_text_field($v) { return trim(strip_tags($v)); }
function get_option($key,$default=null) { return $GLOBALS['options'][$key] ?? $default; }
function add_option($key,$value,$deprecated='',$autoload=false) {
    if (array_key_exists($key,$GLOBALS['options'])) return false;
    $GLOBALS['options'][$key]=$value; return true;
}
function update_option($key,$value,$autoload=false) {
    $GLOBALS['options'][$key]=$value; return true;
}
class BazaraApi {
    public function __construct($value) {}
    public function login_token() { return array('success'=>true); }
    public function get_all_data($token,$input) {
        $GLOBALS['catalog_read_count']++;
        if ($GLOBALS['catalog_transient_failures'] > 0) {
            $GLOBALS['catalog_transient_failures']--;
            return array('success'=>false,'message'=>'transient catalog error');
        }
        return array('success'=>true,'message'=>$GLOBALS['catalog']);
    }
    public function set_all_data($token,$input) {
        $GLOBALS['save_count']++;
        $GLOBALS['outbound']=$input;
        return json_encode(array('success'=>true,'data'=>array('Result'=>true)));
    }
}
require __DIR__ . '/../wordpress/mu-plugins/vesta-mahak-product-bridge.php';
function assert_true($condition,$message) { if(!$condition) { throw new RuntimeException($message); } }
$payload = array(
    'request_key' => '7d82bb45118740c9a09f199cadfa789a',
    'name'=>'LIORA TEST PRODUCT', 'code'=>'998813',
    'template_code'=>'8079','price_rial'=>'600000',
);
$template=vmb_template(array('template_code'=>'8079'));
assert_true($template['category_id']===418353,'Template category mismatch');
assert_true($GLOBALS['catalog_read_count']===2,'Read-only transient catalog failure must be retried once');
$first=vmb_submit($payload);
assert_true($first['status']==='accepted_pending_sync','Expected cloud accepted but pending');
assert_true($GLOBALS['save_count']===1,'Must submit exactly once');
assert_true($GLOBALS['outbound']['products'][0]['tags']==='', 'Missing mandatory Tags field');
assert_true($GLOBALS['outbound']['products'][0]['productCategoryId']===418353, 'Category not mapped from template');
assert_true($GLOBALS['outbound']['productDetails'][0]['price1']===600000,'Price mismatch');
assert_true($GLOBALS['outbound']['productDetails'][0]['count1']===0,'Should never add inventory');
$repeat=vmb_submit($payload);
assert_true($repeat['status']==='accepted_pending_sync','Repeat must report previous state');
assert_true($GLOBALS['save_count']===1,'Repeat must not send another write');
try {
    $payload2=$payload;$payload2['request_key']='bc11aab144b94433bb222222cc222222';
    vmb_submit($payload2);
    throw new RuntimeException('Duplicate product code incorrectly accepted');
} catch (RuntimeException $exc) {
    assert_true(strpos($exc->getMessage(),'درخواست')!==false,'Unexpected duplicate-code error');
}
$status=vmb_status(array('request_key'=>$payload['request_key']));
assert_true($status['status']==='accepted_pending_sync','Unexported product must remain pending');
$GLOBALS['catalog']['Products'][]=array('ProductCode'=>'998813','Name'=>'LIORA TEST PRODUCT',
    'ProductId'=>99989998,'Deleted'=>false);
$confirmed=vmb_status(array('request_key'=>$payload['request_key']));
assert_true($confirmed['status']==='visible_in_cloud_export','Readback did not update state');
assert_true($confirmed['mahak_product_id']===99989998,'Mahak ID mismatch');
assert_true($GLOBALS['save_count']===1,'Readback must be read-only');
echo "MAHAK_CONTRACT_TEST=PASSED\n";
