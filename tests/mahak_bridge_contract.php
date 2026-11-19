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
$GLOBALS['options']['bazara_visitor_settings'] = array('selectCurrencySoftware'=>'toman','selectCurrencyPlugin'=>'toman');
function add_action($hook,$callback,$priority=10) {}
function sanitize_text_field($v) { return trim(strip_tags($v)); }
function wp_json_encode($v,$flags=0) { return json_encode($v,$flags); }
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
$GLOBALS['catalog']['ProductDetails'][]=array('ProductCode'=>'998813','ProductId'=>99989998,
    'ProductDetailId'=>99989999,'Price1'=>600000,'Deleted'=>false);
$confirmed=vmb_status(array('request_key'=>$payload['request_key']));
assert_true($confirmed['status']==='visible_in_cloud_export','Readback did not update state');
assert_true($confirmed['mahak_product_id']===99989998,'Mahak ID mismatch');
assert_true($GLOBALS['save_count']===1,'Readback must be read-only');
// New unified bot path: Toman must match both Bazara and Woo settings.
$toman = array('request_key'=>'2b35c05f574a4bc79c246a8a3a9d317c',
    'name'=>'LIORA NEW TOMAN PRODUCT','code'=>'998814',
    'template_code'=>'8079','price_toman'=>'2599000');
$res=vmb_submit($toman);
assert_true($res['status']==='accepted_pending_sync','Toman workflow not accepted');
assert_true($GLOBALS['outbound']['productDetails'][0]['price1']===2599000,
    'Toman amount changed before Mahak SaveAllData');
assert_true($GLOBALS['options']['vmb_req_'.hash('sha256',$toman['request_key'])]['price_unit']==='toman',
    'Toman unit not recorded');
$GLOBALS['options']['bazara_visitor_settings']['selectCurrencyPlugin']='rial';
try {
    $failed=$toman;$failed['request_key']='ae129c3392a44a5491d6b705abb81754';$failed['code']='998815';
    vmb_submit($failed);
    throw new RuntimeException('Currency mismatch wrongly allowed');
} catch (RuntimeException $exc) {
    assert_true(strpos($exc->getMessage(),'واحد پول')!==false,'Wrong currency mismatch failure');
}

// Verified active Mahak variable template: base detail code 0, group C=3,
// and one or more child details with C/V descriptors.
$GLOBALS['catalog']['Products'][] = [
    'ProductCode'=>'92503','Name'=>'Sample Brow Pencil','ProductId'=>10684982,
    'ProductCategoryId'=>418342,'UnitName'=>'عدد','UnitName2'=>'عدد','Deleted'=>false
];
$GLOBALS['catalog']['ProductDetails']=[
    ['ProductId'=>10684982,'ProductDetailId'=>12183195,'ProductDetailCode'=>0,
     'Properties'=>null,'Price1'=>110000,'Deleted'=>false],
    ['ProductId'=>10684982,'ProductDetailId'=>12222868,'ProductDetailCode'=>1000,
     'Properties'=>'[{"C":"3","V":"قهوه ای"}]','Price1'=>110000,'Deleted'=>false]
];
$GLOBALS['options']['bazara_visitor_settings']['selectCurrencyPlugin']='toman';
$profile=vmb_template(['template_code'=>'92503']);
assert_true(($profile['variation_template']['group_code']??null)==='3','Cannot read verified Mahak C group');
$variable=[
 'request_key'=>'a444d6a48df94778b9ea1b9196a9d028',
 'name'=>'Test Multiple Brow Colours','code'=>'998819',
 'template_code'=>'92503','price_toman'=>'110000',
 'variant_group_code'=>'3','attribute_name'=>'رنگ',
 'variants'=>[
   ['option'=>'قهوه ای','price_toman'=>'110000','stock_qty'=>'36'],
   ['option'=>'مشکی','price_toman'=>'120000','stock_qty'=>'11'],
 ],
 'stock_qty'=>'0'
];
assert_true(strlen($variable['request_key'])===32,'Invalid variable test key');
$before=$GLOBALS['save_count'];
$variableResult=vmb_submit($variable);
assert_true($variableResult['status']==='accepted_pending_sync','Cloud variant acceptance rejected');
assert_true(($variableResult['is_variable']??false)===true,'Variable result type lost');
assert_true($GLOBALS['save_count']===$before+1,'Must submit all variants in only one SaveAllData');
$details=$GLOBALS['outbound']['productDetails'];
assert_true(count($details)===3,'Mahak variable must have base detail and two child details');
assert_true($details[0]['productDetailCode']===0,'Base detail must be code 0');
assert_true($details[1]['productDetailCode']===1000 &&
    $details[2]['productDetailCode']===1001,'Unstable variation detail codes');
assert_true($details[1]['price1']===110000 && $details[2]['price1']===120000,
    'Individual Toman prices lost');
assert_true(json_decode($details[1]['properties'],true)[0]===['C'=>'3','V'=>'قهوه ای'],
    'Mahak C/V property group mapping corrupt');
assert_true($details[1]['count1']===0 && $details[2]['count1']===0,
    'Never pretend ProductDetail.Count1 is warehouse stock');
assert_true(($variableResult['stock_intent_status']??'')==='warehouse_document_required',
    'Pending actual inventory document must be explicit');
assert_true(vmb_submit($variable)['status']==='accepted_pending_sync','Idempotent variant submit failed');
assert_true($GLOBALS['save_count']===$before+1,'Duplicate variant SaveAllData');
$changed=$variable;$changed['variants'][0]['stock_qty']='99';
try {vmb_submit($changed);throw new RuntimeException('Same request payload changed');}
catch (RuntimeException $e) {
    assert_true(strpos($e->getMessage(),'شناسه درخواست')!==false,'Unexpected change rejection');
}
$bad=$variable;$bad['request_key']='b444d6a48df94778b9ea1b9196a9d028';$bad['code']='998820';
$bad['variant_group_code']='999';
try {vmb_submit($bad);throw new RuntimeException('Unverified property group created');}
catch (RuntimeException $e) {
    assert_true(strpos($e->getMessage(),'گروه تنوع')!==false,'Invalid C group not blocked');
}
assert_true($GLOBALS['save_count']===$before+1,'Failed variant validation triggered write');
$GLOBALS['catalog']['Products'][]=[
  'ProductCode'=>'998819','Name'=>'Test Multiple Brow Colours','ProductId'=>999819,
  'ProductCategoryId'=>418342,'Deleted'=>false
];
$seen=vmb_status(['request_key'=>$variable['request_key']]);
assert_true($seen['status']==='details_unverified',
    'Cloud parent alone must not imply completed variations');
$GLOBALS['catalog']['ProductDetails'][]=[
  'ProductId'=>999819,'ProductDetailId'=>290001,'ProductDetailCode'=>0,
  'Properties'=>null,'Price1'=>110000,'Deleted'=>false
];
$GLOBALS['catalog']['ProductDetails'][]=[
  'ProductId'=>999819,'ProductDetailId'=>290002,'ProductDetailCode'=>1000,
  'Properties'=>'[{"C":"3","V":"قهوه ای"}]','Price1'=>110000,'Deleted'=>false
];
$GLOBALS['catalog']['ProductDetails'][]=[
  'ProductId'=>999819,'ProductDetailId'=>290003,'ProductDetailCode'=>1001,
  'Properties'=>'[{"C":"3","V":"مشکی"}]','Price1'=>120000,'Deleted'=>false
];
$readback=vmb_status(['request_key'=>$variable['request_key']]);
assert_true($readback['status']==='visible_in_cloud_export',
    'Readback must verify all variation prices and values');
assert_true(($readback['mahak_product_id']??0)===999819,
    'Mahak parent identifier lost after variant acceptance');
assert_true($GLOBALS['save_count']===$before+1,'Readback must never write');
echo "MAHAK_VARIANT_CONTRACT=PASSED\n";

echo "MAHAK_CONTRACT_TEST=PASSED\n";
