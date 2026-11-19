<?php
/**
 * Plugin Name: Vesta Mahak Product Bridge
 * Description: Submit Liora's separately approved accounting products to Mahak via the existing signed Vesta bridge.
 * Version: 0.1.0
 *
 * IMPORTANT: Mahak SaveAllData confirms cloud acceptance, NOT desktop import.
 * This bridge never creates WooCommerce products or updates stock.
 */
if (!defined('ABSPATH')) { exit; }

function vmb_api() {
    if (!class_exists('BazaraApi')) {
        throw new RuntimeException('افزونه بازارا فعال نیست یا API آن بارگذاری نشده است.');
    }
    $api = new BazaraApi(true);
    $login = $api->login_token();
    if (!is_array($login) || empty($login['success'])) {
        throw new RuntimeException('ورود به API بازارا ناموفق بود.');
    }
    return $api;
}

function vmb_catalog($api) {
    // Bazara occasionally returns a transient unsuccessful catalog response.
    // Retry only the READ, never the SaveAllData mutation. No cached catalog
    // is used for duplicate-code validation.
    for ($attempt = 0; $attempt < 2; $attempt++) {
        $response = $api->get_all_data('', array(
            'fromProductVersion' => 0,
            'fromProductDetailVersion' => 0,
        ));
        if (is_array($response) && !empty($response['success']) && is_array($response['message'] ?? null)) {
            return $response['message'];
        }
        if ($attempt === 0) { usleep(300000); }
    }
    throw new RuntimeException('دریافت فهرست کالاهای محک از بازارا پس از دو تلاش ناموفق بود.');
}

function vmb_by_code($catalog, $code) {
    foreach (($catalog['Products'] ?? array()) as $product) {
        $p = (array) $product;
        if ((string) ($p['ProductCode'] ?? '') === (string) $code && empty($p['Deleted'])) {
            return $p;
        }
    }
    return null;
}

function vmb_request_key($key) {
    if (!is_string($key) || !preg_match('/^[a-f0-9]{32}$/D', $key)) {
        throw new InvalidArgumentException('شناسه یکتای درخواست نامعتبر است.');
    }
    return 'vmb_req_' . hash('sha256', $key);
}


/** Mahak stores variant identity as [{"C":"<group>","V":"<value>"}].
 * Use only a group verified on an existing active ERP template, never invent a C ID.
 */
function vmb_template_variant_group($catalog,$template) {
    $id=(int)($template['ProductId']??0);
    $groups=[];$seen=[];
    foreach((array)($catalog['ProductDetails']??[]) as $d) {
        if(!is_array($d)||!empty($d['Deleted'])||(int)($d['ProductId']??0)!==$id)continue;
        $json=$d['Properties']??null;
        if(!$json)continue;
        $attrs=is_string($json)?json_decode($json,true):$json;
        if(!is_array($attrs)||count($attrs)!==1||!is_array($attrs[0]))continue;
        $c=(string)($attrs[0]['C']??'');$v=trim((string)($attrs[0]['V']??''));
        if(!preg_match('/^[1-9][0-9]{0,5}$/D',$c)||$v==='')continue;
        $groups[$c][$v]=true;
    }
    if(count($groups)!==1)return null; // Multiple independent dimensions need a reviewed mapping.
    $c=(string)array_key_first($groups);
    return ['group_code'=>$c,'sample_values'=>array_slice(array_keys($groups[$c]),0,8)];
}
function vmb_normalized_variants($data,$template,$catalog,$price,$isToman) {
    $source=$data['variants']??null;
    if($source===null||$source===[])return null;
    if(!$isToman)throw new InvalidArgumentException('کالای متغیر باید قیمت تومانی داشته باشد.');
    if(!is_array($source)||count($source)<2||count($source)>20)
        throw new InvalidArgumentException('محصول متغیر باید ۲ تا ۲۰ تنوع یکتا داشته باشد.');
    $group=vmb_template_variant_group($catalog,$template);
    $code=trim((string)($data['variant_group_code']??''));
    if(!$group||$code!==$group['group_code'])
        throw new RuntimeException('گروه تنوع در کالای الگوی محک تأیید نشد. از یک محصول متغیر معتبر الگو بگیر.');
    $attribute=trim((string)($data['attribute_name']??''));
    if($attribute===''||mb_strlen($attribute)>60)
        throw new InvalidArgumentException('نام ویژگی محصول (مثلاً رنگ یا سایز) نامعتبر است.');
    $seen=[];$result=[];
    foreach($source as $v){
        if(!is_array($v))throw new InvalidArgumentException('ساختار تنوع نامعتبر است.');
        $name=sanitize_text_field(trim((string)($v['option']??'')));
        $amount=trim((string)($v['price_toman']??''));
        $stock=trim((string)($v['stock_qty']??''));
        if(mb_strlen($name)<1||mb_strlen($name)>70 ||
           preg_match('/[<>\[\]{}]/u',$name))
            throw new InvalidArgumentException('مقدار رنگ/مدل نامعتبر است.');
        $norm=mb_strtolower(preg_replace('/\s+/u',' ',trim($name)),'UTF-8');
        if(isset($seen[$norm]))throw new InvalidArgumentException('دو تنوع با نام یکسان ثبت شده‌اند.');
        $seen[$norm]=true;
        if(!preg_match('/^[1-9][0-9]{0,13}$/D',$amount))
            throw new InvalidArgumentException('قیمت هر تنوع باید عدد تومانی مثبت باشد.');
        if(!preg_match('/^(?:0|[1-9][0-9]{0,6})$/D',$stock))
            throw new InvalidArgumentException('موجودی هر تنوع باید عدد صحیح صفر تا ۹۹۹۹۹۹۹ باشد.');
        $result[]=['option'=>$name,'price_toman'=>$amount,'stock_qty'=>$stock];
    }
    return [
       'attribute_name'=>$attribute,'group_code'=>$code,
       'variants'=>$result
    ];
}
function vmb_client_id($request,$suffix,$floor,$span) {
    return (int)($floor+(hexdec(substr(hash('sha256',$request.':'.$suffix),0,7))%$span));
}

function vmb_template($data) {
    $code = trim((string) ($data['template_code'] ?? ''));
    if (!preg_match('/^[0-9A-Za-z_-]{1,24}$/D', $code)) {
        throw new InvalidArgumentException('کد کالای الگو نامعتبر است.');
    }
    $catalog = vmb_catalog(vmb_api());
    $p = vmb_by_code($catalog, $code);
    if (!$p) { throw new RuntimeException('کالای الگو با این کد در فهرست محک پیدا نشد.'); }
    return array(
        'code' => (string) $p['ProductCode'],
        'name' => (string) ($p['Name'] ?? ''),
        'category_id' => (int) ($p['ProductCategoryId'] ?? 0),
        'unit_name' => (string) ($p['UnitName'] ?? 'عدد'),
        'unit_name2' => (string) ($p['UnitName2'] ?? 'عدد'),
        'variation_template' => vmb_template_variant_group($catalog,$p),
    );
}

function vmb_public_state($record) {
    return array(
        'status' => (string) ($record['status'] ?? 'unknown'),
        'code' => (string) ($record['code'] ?? ''),
        'name' => (string) ($record['name'] ?? ''),
        'accepted_at' => (string) ($record['accepted_at'] ?? ''),
        'note' => (string) ($record['note'] ?? ''),
        'is_variable'=>is_array($record['variant_spec']??null),
        'variant_count'=>count((array)($record['variant_spec']['variants']??[])),
        'stock_intent_status'=>(string)($record['stock_intent_status']??'unknown'),
    );
}


/** Observe real Bazaar warehouse assets after desktop F2. NEVER create stock
 * from ProductDetail.Count1, which is not physical inventory in this account.
 */
function vmb_requested_inventory_status($record,$catalog) {
    global $wpdb;
    if(($record['stock_intent_status']??'none')==='none')
        return ['status'=>'none','requested_total'=>0];
    $id=(int)($record['mahak_product_id']??0);
    if(!$id)return ['status'=>'warehouse_document_required','requested_total'=>null];
    $spec=$record['variant_spec']??null;$requested=[];
    if(is_array($spec)){
        foreach($spec['variants'] as $v) {
            $requested[trim((string)$v['option'])]=(int)$v['stock_qty'];
        }
    }else $requested['__simple__']=(int)($record['requested_stock_qty']??0);
    $amount=array_sum($requested);
    if($amount<=0)return ['status'=>'none','requested_total'=>0];
    if(!isset($wpdb)||!is_object($wpdb))
        return ['status'=>'warehouse_document_required','requested_total'=>$amount];
    $assetsTable=$wpdb->prefix.'bazara_product_assets';
    $matched=0;$found=[];
    foreach((array)($catalog['ProductDetails']??[]) as $d){
        if(!is_array($d)||!empty($d['Deleted'])||(int)($d['ProductId']??0)!==$id)continue;
        $label='__simple__';
        if(is_array($spec)){
            $props=is_string($d['Properties']??null)?json_decode($d['Properties'],true):null;
            if(!is_array($props)||count($props)!==1 ||
               (string)($props[0]['C']??'')!==(string)$spec['group_code'])continue;
            $label=trim((string)($props[0]['V']??''));
        }
        if(!array_key_exists($label,$requested)||isset($found[$label]))continue;
        $detail=(int)($d['ProductDetailId']??0);
        if(!$detail)continue;
        $rows=$wpdb->get_results($wpdb->prepare(
           "SELECT StoreId,Count1 FROM {$assetsTable} WHERE ProductDetailId=%d AND Deleted=0 LIMIT 2",
           $detail),ARRAY_A);
        if(count($rows)!==1 || (int)$rows[0]['StoreId']<=0 ||
           !is_numeric($rows[0]['Count1']))continue;
        $found[$label]=true;
        if(abs((float)$rows[0]['Count1']-$requested[$label])<0.0001)$matched++;
    }
    return [
        'status'=>$matched===count($requested)?
             'warehouse_quantity_verified':'warehouse_document_required',
        'requested_total'=>$amount,
        'matched_variants'=>$matched,
        'total_variants'=>count($requested),
    ];
}

function vmb_status($data) {
    $key = vmb_request_key((string) ($data['request_key'] ?? ''));
    $record = get_option($key, null);
    if (!is_array($record)) {
        return array('status' => 'not_submitted');
    }
    if (in_array($record['status'] ?? '', array('accepted_pending_sync','visible_in_cloud_export','unknown','details_unverified'), true)) {
        $catalog = vmb_catalog(vmb_api());
        $product = vmb_by_code($catalog, $record['code']);
        if ($product && (string) ($product['Name'] ?? '') === $record['name']) {
            $id=(int)($product['ProductId']??0);
            $details=[];
            foreach((array)($catalog['ProductDetails']??[]) as $d)
                if(is_array($d)&&empty($d['Deleted'])&&(int)($d['ProductId']??0)===$id)
                    $details[]=$d;
            $expected=$record['variant_spec']??null;
            $complete=true;
            if(is_array($expected)){
                $children=[];
                foreach($details as $d) {
                    $attrs=is_string($d['Properties']??null)?json_decode($d['Properties'],true):null;
                    if(!is_array($attrs)||count($attrs)!==1)continue;
                    $row=$attrs[0];
                    if((string)($row['C']??'')!==(string)$expected['group_code'])continue;
                    $value=trim((string)($row['V']??''));
                    if(isset($children[$value])){$complete=false;break;}
                    $children[$value]=(int)($d['Price1']??-1);
                }
                if(count($children)!==count($expected['variants']))$complete=false;
                foreach($expected['variants'] as $v)
                   if(!isset($children[$v['option']]) ||
                      $children[$v['option']]!==(int)$v['price_toman'])$complete=false;
            }else{
                if(count($details)!==1||
                   (isset($record['price'])&&
                   (int)($details[0]['Price1']??-1)!==(int)$record['price']))
                   $complete=false;
            }
            $record['status']=$complete?'visible_in_cloud_export':'details_unverified';
            $record['mahak_product_id']=$id;
            $record['note']=$complete?
                'کالا و جزئیات قیمت/تنوع در محک ابری بازخوانی و تأیید شد.':
                'کالا در ابر موجود است اما جزئیات یا قیمت همه تنوع‌ها هنوز تطبیق ندارند؛ انتشار و اتصال متوقف است.';
            update_option($key, $record, false);
        }
    }
    $state = vmb_public_state($record);
    if(($record['status']??'')==='visible_in_cloud_export' &&
       ($record['stock_intent_status']??'none')==='warehouse_document_required'){
        $state['warehouse']=vmb_requested_inventory_status($record,$catalog??[]);
        $state['stock_intent_status']=$state['warehouse']['status'];
    }
    if (!empty($record['mahak_product_id'])) {
        $state['mahak_product_id'] = (int) $record['mahak_product_id'];
    }
    return $state;
}

function vmb_submit($data) {
    $request_id = (string) ($data['request_key'] ?? '');
    $request_key = vmb_request_key($request_id);
    $name = sanitize_text_field(trim((string) ($data['name'] ?? '')));
    $code = trim((string) ($data['code'] ?? ''));
    $template_code = trim((string) ($data['template_code'] ?? ''));
    // New unified bot flow always quotes and submits TOMAN, matching current
    // Bazara and Woo currency settings. Keep price_rial for old saved requests.
    $isToman=array_key_exists('price_toman',$data);
    $price = trim((string) ($isToman?$data['price_toman']:($data['price_rial']??'')));
    if($isToman) {
        $settings=get_option('bazara_visitor_settings',[]);
        if(!is_array($settings) ||
           ($settings['selectCurrencySoftware']??'')!=='toman' ||
           ($settings['selectCurrencyPlugin']??'')!=='toman')
           throw new RuntimeException('واحد پول محک و سایت روی تومان یکسان نیست؛ قبل از ثبت اصلاح کنید.');
    }
    if (mb_strlen($name) < 3 || mb_strlen($name) > 180) {
        throw new InvalidArgumentException('نام کالا باید بین ۳ تا ۱۸۰ نویسه باشد.');
    }
    if (!preg_match('/^[0-9A-Za-z_-]{1,24}$/D', $code) ||
        !preg_match('/^[0-9A-Za-z_-]{1,24}$/D', $template_code) ||
        $code === $template_code) {
        throw new InvalidArgumentException('کد کالا یا کد الگو نامعتبر یا تکراری است.');
    }
    if (!preg_match('/^\d{1,14}$/D', $price)) {
        throw new InvalidArgumentException('قیمت باید یک عدد صحیح به واحد انتخاب‌شده باشد.');
    }
    // Preserve exact payload fingerprint across retries of the one-time Mahak write.
    $hash=hash('sha256',wp_json_encode([
       'name'=>$name,'code'=>$code,'template'=>$template_code,
       'price'=>$price,'currency'=>$isToman?'toman':'legacy_rial',
       'group'=>$data['variant_group_code']??null,
       'attribute'=>$data['attribute_name']??null,
       'variants'=>$data['variants']??null,
    ],JSON_UNESCAPED_UNICODE));
    $previous = get_option($request_key, null);
    if (is_array($previous)) {
        if ((string) ($previous['code'] ?? '') !== $code ||
            (isset($previous['payload_hash']) && $previous['payload_hash']!==$hash)) {
            throw new RuntimeException('این شناسه درخواست قبلاً برای کالای دیگری استفاده شده است.');
        }
        return vmb_status(array('request_key' => $request_id));
    }
    $api = vmb_api();
    $catalog = vmb_catalog($api);
    if (vmb_by_code($catalog, $code)) {
        throw new RuntimeException('کد کالا از قبل در کاتالوگ محک وجود دارد؛ ارسال متوقف شد.');
    }
    $template = vmb_by_code($catalog, $template_code);
    if (!$template) { throw new RuntimeException('کالای الگو در کاتالوگ محک وجود ندارد.'); }
    $variantSpec=vmb_normalized_variants($data,$template,$catalog,$price,$isToman);
    if($variantSpec===null && !empty($data['variant_group_code']))
        throw new InvalidArgumentException('نوع محصول متغیر است اما هیچ تنوعی ارسال نشده.');
    // Stock requested by the operator is an *intent* only. Count1 does not
    // represent real Mahak warehouse stock: only a valid warehouse document
    // can create that. Never fake inventory in SaveAllData ProductDetails.
    $simpleStock=trim((string)($data['stock_qty']??'0'));
    if(!preg_match('/^(?:0|[1-9][0-9]{0,6})$/D',$simpleStock))
        throw new InvalidArgumentException('موجودی درخواست‌شده معتبر نیست.');
    if($variantSpec!==null && (int)$simpleStock!==0)
        throw new InvalidArgumentException('موجودی محصول متغیر باید در ردیف هر تنوع باشد.');

    // Permanent code reservation: a timeout MUST NOT cause an automatic resubmit
    // while the desktop accounting software may be importing the first request.
    $code_key = 'vmb_code_' . hash('sha256', $code);
    if (!add_option($code_key, $request_id, '', false)) {
        throw new RuntimeException('این کد کالا یک درخواست ثبت قبلی یا درحال‌انتظار دارد.');
    }
    $record = array(
        'status' => 'submitting',
        'code' => $code,
        'name' => $name,
        'template_code' => $template_code,
        'price_unit' => $isToman ? 'toman' : 'legacy_rial',
        'price'=>(int)$price,
        'payload_hash'=>$hash,
        'variant_spec'=>$variantSpec,
        'requested_stock_qty'=>$variantSpec===null?(int)$simpleStock:null,
        'stock_intent_status'=>$variantSpec!==null?
             (array_sum(array_map(fn($v)=>(int)$v['stock_qty'],$variantSpec['variants']))>0?
              'warehouse_document_required':'none'):
              ((int)$simpleStock>0?'warehouse_document_required':'none'),
        'requested_at' => gmdate('c'),
    );
    if (!add_option($request_key, $record, '', false)) {
        return vmb_status(array('request_key' => $request_id));
    }

    $cid = 1100000000 + (hexdec(substr(hash('sha256', $request_id . ':p'), 0, 7)) % 500000000);
    $did = vmb_client_id($request_id,'d',1600000000,400000000);
    $unit = (string) ($template['UnitName'] ?? 'عدد');
    $unit2 = (string) ($template['UnitName2'] ?? $unit);
    $product = array(
        'productId' => 0, 'productClientId' => (int) $cid,
        'productCode' => $code,
        'productCategoryId' => (int) ($template['ProductCategoryId'] ?? 0),
        'name' => $name, 'unitName' => $unit, 'unitName2' => $unit2,
        'unitRatio' => 0, 'taxPercent' => 0, 'chargePercent' => 0,
        'type' => 0, 'tags' => '',
        'description' => 'Created from Liora Telegram bot; pending Mahak desktop sync',
        'deleted' => false,
    );
    $base = array(
        'productDetailId' => 0, 'productDetailClientId' => (int) $did,
        'productDetailCode' => $variantSpec===null?1:0, 'productId' => 0,
        'productClientId' => (int) $cid, 'productCode' => $code,
        'properties' => '', 'properties2' => '',
        'count1' => 0, 'count2' => 0,
        'barcode' => '', 'price1' => (int) $price, 'price2' => 0,
        'defaultSellPriceLevel' => 1, 'defaultDiscountLevel' => 1,
        'deleted' => false,
    );
    $details = [$base];
    if($variantSpec!==null){
        $usedIds=[(int)$did=>true];
        foreach($variantSpec['variants'] as $i=>$v){
            $childId=vmb_client_id($request_id,'v'.$i,1600000000,400000000);
            if(isset($usedIds[$childId]))throw new RuntimeException('تعارض شناسه ردیف‌های تنوع؛ ثبت متوقف شد.');
            $usedIds[$childId]=true;
            $row=$base;
            $row['productDetailClientId']=$childId;
            $row['productDetailCode']=1000+$i;
            $row['properties']=wp_json_encode([
                ['C'=>(string)$variantSpec['group_code'],'V'=>$v['option']]
            ],JSON_UNESCAPED_UNICODE);
            $row['price1']=(int)$v['price_toman'];
            // Initial stock NOT written to Count1; separate valid ERP
            // warehouse document is required. Avoid false stock postings.
            $row['count1']=0;
            $details[]=$row;
        }
    }
    try {
        $raw = $api->set_all_data('', array(
            'products' => array($product),
            'productDetails' => $details,
        ));
        $res = is_string($raw) ? json_decode($raw, true) : $raw;
        if (!is_array($res) || empty($res['success'])) {
            $reason = 'سرور محک درخواست ایجاد کالا را نپذیرفت.';
            $detail = $res['message'] ?? '';
            $parsed = is_string($detail) ? json_decode($detail, true) : $detail;
            if (is_array($parsed) && isset($parsed['Message'])) {
                $reason .= ' ' . mb_substr((string) $parsed['Message'], 0, 300);
            }
            $record['status'] = 'rejected';
            $record['note'] = $reason;
            update_option($request_key, $record, false);
            return vmb_public_state($record);
        }
        $record['status'] = 'accepted_pending_sync';
        $record['accepted_at'] = gmdate('c');
        $record['note'] = 'کالا'.($variantSpec!==null?' با '.count($variantSpec['variants']).' تنوع':'').
          ' برای ثبت در محک پذیرفته شد؛ تأیید ابر و دریافت ویندوزی جداگانه است.'.
          ($record['stock_intent_status']==='warehouse_document_required'?
          ' موجودی در صف درخواست ثبت سند انبار است و هنوز موجودی واقعی محک محسوب نمی‌شود.':'');
        update_option($request_key, $record, false);
        return vmb_public_state($record);
    } catch (Throwable $e) {
        $record['status'] = 'unknown';
        $record['note'] = 'پاسخ قطعی دریافت نشد؛ درخواست نباید خودکار دوباره ارسال شود.';
        update_option($request_key, $record, false);
        return vmb_public_state($record);
    }
}

add_action('plugins_loaded', function () {
    if (!isset($_GET['vbb']) || (string) $_GET['vbb'] !== '2') { return; }
    $op = sanitize_key((string) ($_GET['o'] ?? ''));
    if (!in_array($op, array('mahak_template','mahak_submit','mahak_status'), true)) { return; }
    if (!function_exists('vbb_v2_authorize')) { return; }
    list($validated_op, $payload) = vbb_v2_authorize();
    if ($validated_op !== $op) { vbb_fail('Invalid operation.', 400); }
    try {
        switch ($op) {
            case 'mahak_template': $response = vmb_template($payload); break;
            case 'mahak_submit': $response = vmb_submit($payload); break;
            case 'mahak_status': $response = vmb_status($payload); break;
        }
        vbb_no_cache();
        wp_send_json_success($response);
    } catch (Throwable $e) {
        vbb_fail($e->getMessage(), 400);
    }
}, PHP_INT_MAX - 1);
