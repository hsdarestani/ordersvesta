<?php
/**
 * Plugin Name: Vesta signed Telegram stock receipt
 * Description: Exact product/variation lookup and auditable additive stock receipts.
 */
if (!defined('ABSPATH')) exit;

function vsr_product($id) {
    $p=wc_get_product(absint($id));
    if (!$p || !in_array($p->get_type(), ['simple','variable','variation'],true) ||
        !in_array($p->get_status(),['publish','private'],true))
        throw new RuntimeException('Product unavailable.');
    if ($p->is_type('variation')) {
        $parent=wc_get_product($p->get_parent_id());
        if (!$parent || !$parent->is_type('variable') ||
            !in_array($parent->get_status(),['publish','private'],true))
            throw new RuntimeException('Variation parent unavailable.');
    }
    return $p;
}

function vsr_describe($p) {
    $qty=$p->get_stock_quantity();
    $attrs=$p->is_type('variation') ? wc_get_formatted_variation($p,true,true,false) : '';
    $image=(int)$p->get_image_id();
    if (!$image && $p->is_type('variation')) {
        $parent=wc_get_product($p->get_parent_id());
        $image=$parent ? (int)$parent->get_image_id() : 0;
    }
    $identity=hash('sha256',wp_json_encode([$p->get_id(),$p->get_parent_id(),$p->get_type(),
        $p->get_name(),$p->get_sku(),$p->get_attributes()]));
    return ['id'=>$p->get_id(),'name'=>wp_strip_all_tags($p->get_name()),
        'sku'=>$p->get_sku(),'type'=>$p->get_type(),'attributes'=>wp_strip_all_tags($attrs),
        'quantity'=>is_numeric($qty) && (float)$qty===(float)(int)$qty ? (int)$qty : null,
        'image_id'=>$image,'identity'=>$identity,
        'receivable'=>!$p->is_type('variable') && $p->managing_stock() &&
            (int)$p->get_stock_managed_by_id()===(int)$p->get_id() &&
            is_numeric($qty) && (float)$qty===(float)(int)$qty];
}

function vsr_search($d) {
    global $wpdb;
    $parent=absint($d['parent']??0);
    $page=max(1,min(500,absint($d['page']??1)));
    $offset=($page-1)*5;
    if ($parent) {
        if (!vsr_product($parent)->is_type('variable')) throw new RuntimeException('Invalid parent.');
        $ids=$wpdb->get_col($wpdb->prepare(
            "SELECT ID FROM {$wpdb->posts} WHERE post_parent=%d AND post_type='product_variation'
             AND post_status='publish' ORDER BY menu_order,ID LIMIT 6 OFFSET %d",$parent,$offset));
    } else {
        $q=trim(sanitize_text_field($d['q']??''));
        if (mb_strlen($q)<2 || mb_strlen($q)>100) throw new RuntimeException('Invalid search.');
        $q=str_replace(['ي','ك','‌'],['ی','ک',' '],$q);
        $like='%'.$wpdb->esc_like($q).'%';
        $ids=$wpdb->get_col($wpdb->prepare(
            "SELECT DISTINCT p.ID FROM {$wpdb->posts} p
             LEFT JOIN {$wpdb->postmeta} s ON p.ID=s.post_id AND s.meta_key='_sku'
             WHERE p.post_status IN ('publish','private') AND
             ((p.post_type='product' AND
               REPLACE(REPLACE(REPLACE(p.post_title,'ي','ی'),'ك','ک'),'‌',' ') LIKE %s)
              OR (p.post_type IN ('product','product_variation') AND s.meta_value LIKE %s))
             ORDER BY p.ID DESC LIMIT 6 OFFSET %d",$like,$like,$offset));
    }
    $products=[];
    foreach(array_slice($ids,0,5) as $id) {
        try { $products[]=vsr_describe(vsr_product($id)); } catch(Throwable $e) { continue; }
    }
    return ['products'=>$products,'more'=>count($ids)>5];
}

function vsr_photo($id) {
    $p=vsr_describe(vsr_product($id));
    $aid=$p['image_id'];
    if (!$aid) return ['image'=>''];
    $file=get_attached_file($aid);
    $meta=wp_get_attachment_metadata($aid);
    if (!$file || !is_file($file)) return ['image'=>''];
    $thumb=$meta['sizes']['thumbnail']['file']??'';
    if ($thumb) {
        $candidate=dirname($file).DIRECTORY_SEPARATOR.basename($thumb);
        if (is_file($candidate)) $file=$candidate;
    }
    if (filesize($file)>450000 || !in_array(wp_get_image_mime($file),['image/jpeg','image/png'],true))
        return ['image'=>''];
    return ['image'=>base64_encode(file_get_contents($file))];
}

function vsr_key($key) {
    if (!is_string($key) || !preg_match('/^[a-f0-9]{32}$/D',$key))
        throw new InvalidArgumentException('Invalid receipt key.');
    return 'vsr_receipt_'.$key;
}

function vsr_status($key) {
    $r=get_option(vsr_key($key),[]);
    return $r ?: ['status'=>'unknown'];
}

function vsr_receive($d) {
    global $wpdb;
    $key=vsr_key($d['request_key']??'');
    $id=absint($d['id']??0);
    $amount=$d['amount']??null;
    $expected=$d['expected']??null;
    $actor=(string)($d['actor']??'');
    if (!$id || !is_int($amount) || $amount<1 || $amount>10000 ||
        !is_int($expected) || !preg_match('/^[0-9]{1,20}$/D',$actor))
        throw new InvalidArgumentException('Invalid stock receipt.');
    $hash=hash('sha256',wp_json_encode([$id,$amount,$expected,$actor,$d['identity']??'']));
    $lock='vsr_product_'.$id;
    if ((int)$wpdb->get_var($wpdb->prepare('SELECT GET_LOCK(%s,8)',$lock))!==1)
        throw new RuntimeException('Stock receipt busy.');
    try {
        $old=get_option($key,[]);
        if ($old) {
            if (($old['hash']??'')!==$hash) throw new RuntimeException('Receipt key conflict.');
            return $old; // pending is intentionally never replayed
        }
        $p=vsr_product($id);
        $item=vsr_describe($p);
        if (!$item['receivable'] || !hash_equals($item['identity'],(string)($d['identity']??'')) ||
            $item['quantity']!==$expected) throw new RuntimeException('Stock or identity changed; search again.');
        // Production activation is explicit, after the inventory owner and downstream sync
        // have been verified. Read/search is usable without allowing any writes.
        if (get_option('vsr_stock_receipt_enabled','no')!=='yes')
            throw new RuntimeException('Stock receipts are not enabled.');
        $record=['status'=>'pending','hash'=>$hash,'product_id'=>$id,'name'=>$item['name'],
            'sku'=>$item['sku'],'actor'=>$actor,'before'=>$expected,'amount'=>$amount,
            'created_at'=>gmdate('c')];
        if (!add_option($key,$record,'',false)) throw new RuntimeException('Receipt already exists.');
        // Atomic WooCommerce increase: never replace stock or edit the variable parent.
        // A crash after the write leaves a pending receipt for manual reconciliation.
        $new=wc_update_product_stock($p,$amount,'increase');
        if (!is_numeric($new)) throw new RuntimeException('Stock write needs reconciliation.');
        $record['status']='applied';
        $record['quantity']=(int)$new;
        $record['completed_at']=gmdate('c');
        if (!update_option($key,$record,false)) throw new RuntimeException('Receipt audit needs reconciliation.');
        return $record;
    } finally {
        $wpdb->get_var($wpdb->prepare('SELECT RELEASE_LOCK(%s)',$lock));
    }
}

// Only intercept our own signed operations, after WooCommerce registers products.
function vsr_ops() { return ['stock_search','stock_detail','stock_photo','stock_receive','stock_receipt_status']; }
add_action('plugins_loaded',function() {
    if ((string)($_GET['vbb']??'')!=='2' || !in_array(sanitize_key($_GET['o']??''),vsr_ops(),true)) return;
    remove_action('plugins_loaded','vbb_handle_v2_request',PHP_INT_MAX);
    add_action('init','vsr_dispatch',PHP_INT_MAX-4);
},PHP_INT_MAX-4);

function vsr_dispatch() {
    if (!function_exists('vbb_v2_authorize') || !did_action('woocommerce_init') ||
        !post_type_exists('product')) {
        wp_send_json_error(['message'=>'Stock bridge unavailable.'],503);
    }
    list($op,$d)=vbb_v2_authorize();
    try {
        switch($op) {
            case 'stock_search': $out=vsr_search($d); break;
            case 'stock_detail': $out=['product'=>vsr_describe(vsr_product($d['id']??0))]; break;
            case 'stock_photo': $out=vsr_photo($d['id']??0); break;
            case 'stock_receive': $out=vsr_receive($d); break;
            case 'stock_receipt_status': $out=vsr_status($d['request_key']??''); break;
            default: throw new RuntimeException('Invalid operation.');
        }
        vbb_no_cache();
        wp_send_json_success($out);
    } catch(Throwable $e) { vbb_fail($e->getMessage(),400); }
}
