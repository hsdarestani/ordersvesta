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
        'management_version'=>2,'parent_id'=>(int)$p->get_parent_id(),
        'price_enabled'=>!$p->is_type('variable'),
        'currency'=>get_woocommerce_currency(),
        'regular_price'=>(string)$p->get_regular_price('edit'),
        'sale_price'=>(string)$p->get_sale_price('edit'),
        'current_price'=>(string)$p->get_price(),
        'price_version'=>vsr_price_version($p),
        'receivable'=>!$p->is_type('variable') && $p->managing_stock() &&
            (int)$p->get_stock_managed_by_id()===(int)$p->get_id() &&
            is_numeric($qty) && (float)$qty===(float)(int)$qty];
}

function vsr_price_version($p) {
    $from=$p->get_date_on_sale_from('edit');$to=$p->get_date_on_sale_to('edit');
    return hash('sha256',wp_json_encode([(string)$p->get_regular_price('edit'),
        (string)$p->get_sale_price('edit'),$from?$from->getTimestamp():null,
        $to?$to->getTimestamp():null,get_woocommerce_currency()]));
}
function vsr_price_plan($p,$d) {
    $mode=$d['price_mode']??'';
    $value=$d['new_price']??null;
    if (!in_array($mode,['regular','sale'],true) || !is_string($value) ||
        !preg_match('/^[1-9][0-9]{0,11}$/D',$value))
        throw new InvalidArgumentException('Invalid price.');
    if (!hash_equals(vsr_price_version($p),(string)($d['price_version']??'')))
        throw new RuntimeException('Price or discount changed; search again.');
    $regular=(string)$p->get_regular_price('edit');
    $sale=(string)$p->get_sale_price('edit');
    if ($mode==='regular' && $sale!=='' && (float)$value<=(float)$sale)
        throw new RuntimeException('Regular price must be higher than existing sale price.');
    if ($mode==='sale' && ($regular==='' || (float)$value>=(float)$regular))
        throw new RuntimeException('Sale price must be lower than regular price.');
    return ['mode'=>$mode,'value'=>$value,'before_regular'=>$regular,'before_sale'=>$sale,
        'version'=>vsr_price_version($p),'currency'=>get_woocommerce_currency()];
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
    if (filesize($file)<=450000 && in_array(wp_get_image_mime($file),['image/jpeg','image/png'],true))
        return ['image'=>base64_encode(file_get_contents($file))];
    if (filesize($file)>12000000) return ['image'=>''];
    require_once ABSPATH.'wp-admin/includes/image.php';
    $editor=wp_get_image_editor($file);
    if (is_wp_error($editor)) return ['image'=>''];
    $editor->resize(320,320,false);
    $editor->set_quality(82);
    $tmp=tempnam(get_temp_dir(),'vsr-');
    if (!$tmp) return ['image'=>''];
    $target=$tmp.'.jpg';
    try {
        $saved=$editor->save($target,'image/jpeg');
        if (is_wp_error($saved) || !is_file($target) || filesize($target)>450000) return ['image'=>''];
        return ['image'=>base64_encode(file_get_contents($target))];
    } finally {
        @unlink($tmp);
        @unlink($target);
    }
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

// Compare-and-set negative/absolute adjustments so a simultaneous customer order
// cannot be overwritten by an administrator's stale stock snapshot.
function vsr_guarded_stock($p,$expected,$target) {
    global $wpdb;
    $id=$p->get_id();
    if ($target<0) throw new RuntimeException('Negative stock is not allowed.');
    if ($target===$expected) return $expected;
    $rows=$wpdb->get_results($wpdb->prepare("SELECT meta_id,meta_value FROM {$wpdb->postmeta} WHERE post_id=%d AND meta_key='_stock'",$id),ARRAY_A);
    if (count($rows)!==1 || !is_numeric($rows[0]['meta_value']) || (float)$rows[0]['meta_value']!==(float)$expected)
        throw new RuntimeException('Stock changed; search again.');
    $changed=$wpdb->query($wpdb->prepare("UPDATE {$wpdb->postmeta} SET meta_value=%s WHERE meta_id=%d AND meta_value=%s",
        (string)$target,(int)$rows[0]['meta_id'],$rows[0]['meta_value']));
    if ($changed!==1) throw new RuntimeException('Stock changed; search again.');
    wp_cache_delete($id,'post_meta');
    // Run Woo's native stock synchronization without adding or removing another unit.
    $fresh=wc_get_product($id);
    $new=wc_update_product_stock($fresh,0,'increase');
    if (!is_numeric($new)) throw new RuntimeException('Stock write needs reconciliation.');
    return (int)$new;
}

function vsr_receive($d,$manage=false) {
    global $wpdb;
    $key=vsr_key($d['request_key']??'');
    $id=absint($d['id']??0);
    $amount=$d['amount']??null;
    $expected=$d['expected']??null;
    $actor=(string)($d['actor']??'');
    $mode=$manage ? ($d['stock_mode']??'') : 'increase';
    $price_requested=array_key_exists('price_mode',$d);
    if (!$id || !in_array($mode,['increase','decrease','set','none'],true) ||
        !is_int($amount) || $amount<0 || $amount>10000 ||
        (in_array($mode,['increase','decrease'],true) && $amount===0) ||
        ($mode==='none' && ($amount!==0 || !$price_requested)) ||
        ($mode!=='none' && !is_int($expected)) ||
        !preg_match('/^[0-9]{1,20}$/D',$actor))
        throw new InvalidArgumentException('Invalid stock receipt.');
    $fingerprint=[$id,$amount,$expected,$actor,$d['identity']??''];
    if ($manage) $fingerprint[]=['stock_mode',$mode];
    if ($price_requested) $fingerprint[]=['price',$d['price_mode'],$d['new_price']??null,$d['price_version']??null];
    $hash=hash('sha256',wp_json_encode($fingerprint));
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
        if ($p->is_type('variable') || !hash_equals($item['identity'],(string)($d['identity']??'')) ||
            ($mode!=='none' && (!$item['receivable'] || $item['quantity']!==$expected)))
            throw new RuntimeException('Stock or identity changed; search again.');
        if ($mode==='decrease' && $expected<$amount) throw new RuntimeException('Negative stock is not allowed.');
        // Production activation is explicit, after the inventory owner and downstream sync
        // have been verified. Read/search is usable without allowing any writes.
        if (get_option('vsr_stock_receipt_enabled','no')!=='yes')
            throw new RuntimeException('Stock receipts are not enabled.');
        $plan=$price_requested ? vsr_price_plan($p,$d) : null;
        $record=['status'=>'pending','hash'=>$hash,'product_id'=>$id,'name'=>$item['name'],
            'sku'=>$item['sku'],'actor'=>$actor,'before'=>$expected,'amount'=>$amount,
            'created_at'=>gmdate('c')];
        if ($manage) $record['stock_mode']=$mode;
        if ($plan) $record['price_plan']=$plan;
        if (!add_option($key,$record,'',false)) throw new RuntimeException('Receipt already exists.');
        // Atomic WooCommerce increase: never replace stock or edit the variable parent.
        // A crash after the write leaves a pending receipt for manual reconciliation.
        if ($mode==='none') {
            $record['quantity']=$item['quantity'];
        } else {
            $new=$mode==='increase' ? wc_update_product_stock($p,$amount,'increase') :
                vsr_guarded_stock($p,$expected,$mode==='set'?$amount:$expected-$amount);
            if (!is_numeric($new)) throw new RuntimeException('Stock write needs reconciliation.');
            $record['quantity']=(int)$new;
        }
        if ($plan) {
            // Persist the stock outcome before touching price. Never replay a partially
            // completed stock receipt, including after a PHP timeout.
            $record['status']=$mode==='none'?'price_pending':'stock_applied_price_pending';
            if (!update_option($key,$record,false)) throw new RuntimeException('Stock audit needs reconciliation.');
            try {
                $fresh=vsr_product($id);
                if (!hash_equals($plan['version'],vsr_price_version($fresh)))
                    throw new RuntimeException('Price changed after stock update.');
                if ($plan['mode']==='regular') $fresh->set_regular_price($plan['value']);
                else {
                    $fresh->set_sale_price($plan['value']);
                    // A newly created discount starts now; do not inherit obsolete dates.
                    // Existing sale schedules are preserved when changing an existing discount.
                    if ($plan['before_sale']==='') {
                        $fresh->set_date_on_sale_from(null);
                        $fresh->set_date_on_sale_to(null);
                    }
                }
                $fresh->save();
                $check=vsr_product($id);
                $actual=$plan['mode']==='regular' ? $check->get_regular_price('edit') : $check->get_sale_price('edit');
                if ((float)$actual!==(float)$plan['value']) throw new RuntimeException('Price readback mismatch.');
                if ($plan['mode']==='regular' && (string)$check->get_sale_price('edit')!==$plan['before_sale'])
                    throw new RuntimeException('Sale price changed unexpectedly.');
                if ($plan['mode']==='sale' && (string)$check->get_regular_price('edit')!==$plan['before_regular'])
                    throw new RuntimeException('Regular price changed unexpectedly.');
                $record['price_result']=['mode'=>$plan['mode'],'value'=>(string)$actual,
                    'regular'=>(string)$check->get_regular_price('edit'),'sale'=>(string)$check->get_sale_price('edit'),
                    'currency'=>$plan['currency']];
            } catch(Throwable $e) {
                $record['status']=$mode==='none'?'price_unverified':'stock_applied_price_unverified';
                $record['price_error']='Price update requires manual verification.';
                update_option($key,$record,false);
                return $record;
            }
        }
        $record['status']='applied';
        $record['completed_at']=gmdate('c');
        if (!update_option($key,$record,false)) throw new RuntimeException('Receipt audit needs reconciliation.');
        // The quantity and audit are committed already. Invalidate the public product
        // page (parent for a variation) without flushing the whole store.
        try {
            $page_id=$p->get_parent_id() ?: $id;
            wc_delete_product_transients($page_id);
            if (function_exists('rocket_clean_post')) rocket_clean_post($page_id);
        } catch (Throwable $e) {
            error_log('Vesta stock receipt: targeted cache refresh failed for product '.(int)$id);
        }
        return $record;
    } finally {
        $wpdb->get_var($wpdb->prepare('SELECT RELEASE_LOCK(%s)',$lock));
    }
}

// Only intercept our own signed operations, after WooCommerce registers products.
function vsr_group($id) {
    global $wpdb;
    $parent=vsr_product($id);
    if (!$parent->is_type('variable')) throw new RuntimeException('Not a variable product.');
    $ids=$wpdb->get_col($wpdb->prepare("SELECT ID FROM {$wpdb->posts} WHERE post_parent=%d AND post_type='product_variation' AND post_status='publish' ORDER BY menu_order,ID LIMIT 101",$id));
    if (!$ids || count($ids)>100) throw new RuntimeException('Group must contain 1 to 100 active variants.');
    $items=[];$versions=[];
    foreach($ids as $vid) {
        $v=vsr_product($vid);$item=vsr_describe($v);
        $items[]=$item;
        $versions[]=[$item['id'],$item['identity'],$item['price_version']];
    }
    return ['id'=>(int)$id,'name'=>$parent->get_name(),'currency'=>get_woocommerce_currency(),
        'count'=>count($items),'items'=>$items,'version'=>hash('sha256',wp_json_encode($versions))];
}
function vsr_group_price($d) {
    global $wpdb;
    $id=absint($d['id']??0);$key=vsr_key($d['request_key']??'');
    $actor=(string)($d['actor']??'');
    if (!$id || !preg_match('/^[0-9]{1,20}$/D',$actor) ||
        get_option('vsr_stock_receipt_enabled','no')!=='yes') throw new RuntimeException('Group price write unavailable.');
    $hash=hash('sha256',wp_json_encode(['group',$id,$actor,$d['version']??'',$d['price_mode']??'',$d['new_price']??'']));
    $lock='vsr_group_'.$id;$locks=[];
    if ((int)$wpdb->get_var($wpdb->prepare('SELECT GET_LOCK(%s,8)',$lock))!==1) throw new RuntimeException('Group busy.');
    try {
        $old=get_option($key,[]);
        if ($old) {
            if (($old['hash']??'')!==$hash) throw new RuntimeException('Receipt key conflict.');
            return $old;
        }
        $group=vsr_group($id);
        if (!hash_equals($group['version'],(string)($d['version']??''))) throw new RuntimeException('Group or prices changed; preview again.');
        // Lock every child in a deterministic order, sharing the single-item lock namespace.
        $ids=array_column($group['items'],'id');sort($ids,SORT_NUMERIC);
        foreach($ids as $vid) {
            $l='vsr_product_'.$vid;
            if ((int)$wpdb->get_var($wpdb->prepare('SELECT GET_LOCK(%s,2)',$l))!==1) throw new RuntimeException('Variant busy.');
            $locks[]=$l;
        }
        $group=vsr_group($id);
        if (!hash_equals($group['version'],(string)($d['version']??''))) throw new RuntimeException('Group or prices changed; preview again.');
        $plans=[];
        foreach($group['items'] as $item) {
            $v=vsr_product($item['id']);
            $plans[$item['id']]=vsr_price_plan($v,['price_mode'=>$d['price_mode']??'','new_price'=>$d['new_price']??null,'price_version'=>$item['price_version']]);
        }
        // All validations happen before any price write. Record partial outcomes explicitly.
        $record=['status'=>'group_pending','hash'=>$hash,'product_id'=>$id,'actor'=>$actor,'name'=>$group['name'],
            'count'=>$group['count'],'applied_ids'=>[],'target_ids'=>array_keys($plans),'price_mode'=>$d['price_mode'],'new_price'=>$d['new_price'],'created_at'=>gmdate('c')];
        if (!add_option($key,$record,'',false)) throw new RuntimeException('Receipt already exists.');
        try {
            foreach($plans as $vid=>$plan) {
                $v=vsr_product($vid);
                if (!hash_equals($plan['version'],vsr_price_version($v))) throw new RuntimeException('Variant price changed.');
                if ($plan['mode']==='regular') $v->set_regular_price($plan['value']);
                else {
                    $v->set_sale_price($plan['value']);
                    if ($plan['before_sale']==='') {$v->set_date_on_sale_from(null);$v->set_date_on_sale_to(null);}
                }
                $v->save();
                $check=vsr_product($vid);
                $actual=$plan['mode']==='regular'?$check->get_regular_price('edit'):$check->get_sale_price('edit');
                if ((float)$actual!==(float)$plan['value'] ||
                    ($plan['mode']==='regular' && (string)$check->get_sale_price('edit')!==$plan['before_sale']) ||
                    ($plan['mode']==='sale' && (string)$check->get_regular_price('edit')!==$plan['before_regular']))
                    throw new RuntimeException('Variant readback failed.');
                $record['applied_ids'][]=$vid;
                if (!update_option($key,$record,false)) throw new RuntimeException('Group audit failed.');
            }
            WC_Product_Variable::sync($id);
            wc_delete_product_transients($id);
            if (function_exists('rocket_clean_post')) rocket_clean_post($id);
            $record['status']='applied';
            $record['completed_at']=gmdate('c');
        } catch(Throwable $e) {
            $record['status']='group_unverified';
            $record['error']='Group price update requires reconciliation.';
            try { WC_Product_Variable::sync($id);wc_delete_product_transients($id);if(function_exists('rocket_clean_post'))rocket_clean_post($id); } catch(Throwable $ignored) {}
        }
        update_option($key,$record,false);
        return $record;
    } finally {
        foreach(array_reverse($locks) as $l) $wpdb->get_var($wpdb->prepare('SELECT RELEASE_LOCK(%s)',$l));
        $wpdb->get_var($wpdb->prepare('SELECT RELEASE_LOCK(%s)',$lock));
    }
}

function vsr_ops() { return ['stock_search','stock_detail','stock_photo','stock_receive','stock_receive_price','stock_manage','stock_group_detail','stock_group_price','stock_receipt_status']; }
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
            case 'stock_receive':
                if (array_key_exists('price_mode',$d)) throw new RuntimeException('Use combined stock and price operation.');
                $out=vsr_receive($d); break;
            case 'stock_receive_price':
                if (!array_key_exists('price_mode',$d)) throw new RuntimeException('Missing price choice.');
                $out=vsr_receive($d); break;
            case 'stock_manage': $out=vsr_receive($d,true); break;
            case 'stock_group_detail': $out=vsr_group(absint($d['id']??0)); break;
            case 'stock_group_price': $out=vsr_group_price($d); break;
            case 'stock_receipt_status': $out=vsr_status($d['request_key']??''); break;
            default: throw new RuntimeException('Invalid operation.');
        }
        vbb_no_cache();
        wp_send_json_success($out);
    } catch(Throwable $e) { vbb_fail($e->getMessage(),400); }
}
