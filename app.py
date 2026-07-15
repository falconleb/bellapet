from flask import Flask, render_template, session, request, redirect, url_for, jsonify, Response
from functools import wraps
from werkzeug.security import check_password_hash
import os, re, json, secrets, threading, time
from collections import defaultdict
import urllib.request as _urllib_req
from urllib.parse import unquote, urlparse, parse_qs
import seo as seo_mod
try:
    from PIL import Image
    _PILLOW = True
except ImportError:
    _PILLOW = False

import config
from database import get_db, init_db
import ai as ai_mod
import monitor as monitor_mod


# ── helpers ──

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'[\s_-]+', '-', text)

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

# ── n8n / API helpers ──────────────────────────────────────────

def _get_integration(key):
    """اقرأ قيمة من جدول integration_settings."""
    db = get_db()
    row = db.execute("SELECT value FROM integration_settings WHERE key=?", (key,)).fetchone()
    db.close()
    return row['value'] if row else None

def _save_integration(key, value):
    db = get_db()
    if value:
        db.execute("INSERT OR REPLACE INTO integration_settings (key,value) VALUES (?,?)", (key, value.strip()))
    else:
        db.execute("DELETE FROM integration_settings WHERE key=?", (key,))
    db.commit()
    db.close()

_PIXEL_KEYS = [
    'meta_pixel_id', 'meta_capi_token',
    'ga4_id', 'gtm_id',
    'clarity_id',
    'snap_pixel_id',
    'tiktok_pixel_id',
    'google_maps_key',
    'gemini_api_key',
    'anthropic_api_key',
    'gbp_place_id',
    'gbp_review_url',
]

def _get_pixels():
    """جلب كل قيم الـ pixels دفعة واحدة."""
    db = get_db()
    rows = db.execute(
        f"SELECT key, value FROM integration_settings WHERE key IN ({','.join('?'*len(_PIXEL_KEYS))})",
        _PIXEL_KEYS
    ).fetchall()
    db.close()
    return {r['key']: r['value'] for r in rows}


def _get_zones(db):
    """أقضية الشحن مرتبة."""
    return db.execute("SELECT * FROM shipping_zones ORDER BY sort_order, name_ar").fetchall()


def _zones_list(db, enabled_only=False):
    """list of dicts [{name_ar, name_en, fee, enabled}] للعرض في الـ templates."""
    q = "SELECT name_ar, name_en, fee, enabled FROM shipping_zones"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY sort_order, name_ar"
    return [dict(r) for r in db.execute(q).fetchall()]


def _zones_fees(db, enabled_only=True):
    """dict {name_ar: fee} للأقضية."""
    q = "SELECT name_ar, fee FROM shipping_zones"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY sort_order, name_ar"
    return {r['name_ar']: r['fee'] for r in db.execute(q).fetchall()}

def _auto_log(event_type: str, status: str = 'ok', summary: str = ''):
    """يسجّل حدث أتمتة بالـ DB."""
    try:
        db = get_db()
        db.execute(
            "INSERT INTO automation_logs (event_type, status, summary) VALUES (?,?,?)",
            (event_type, status, summary[:300])
        )
        db.commit()
        db.close()
    except Exception:
        pass

def _fire_webhook(url, payload):
    """POST JSON إلى n8n بدون blocking (thread منفصل) + يسجّل بالـ log."""
    if not url:
        return
    event = payload.get('event', 'webhook')
    def _send():
        try:
            data = json.dumps(payload).encode()
            req  = _urllib_req.Request(url, data=data,
                                       headers={'Content-Type': 'application/json'},
                                       method='POST')
            _urllib_req.urlopen(req, timeout=8)
            _auto_log(event, 'ok', f'→ {url[:60]}')
        except Exception as e:
            _auto_log(event, 'error', str(e)[:200])
    threading.Thread(target=_send, daemon=True).start()


def _send_capi_event(event_name, pixel_id, token, data: dict):
    """يبعث server-side event لـ Meta Conversions API (CAPI) في thread منفصل."""
    if not pixel_id or not token:
        return
    import hashlib, time as _time
    def _hash(v):
        return hashlib.sha256(v.strip().lower().encode()).hexdigest() if v else None

    user_data = {}
    if data.get('phone'):
        h = _hash(data['phone'])
        if h:
            user_data['ph'] = [h]
    if data.get('email'):
        h = _hash(data['email'])
        if h:
            user_data['em'] = [h]
    user_data['client_ip_address'] = data.get('ip', '')
    user_data['client_user_agent'] = data.get('ua', '')

    event_data = {
        'event_name': event_name,
        'event_time': int(_time.time()),
        'action_source': 'website',
        'event_source_url': data.get('url', 'https://bellapetstore.com/checkout'),
        'user_data': user_data,
    }
    if data.get('order_id'):
        event_data['event_id'] = f"order_{data['order_id']}"
    if data.get('value') is not None:
        event_data['custom_data'] = {
            'value': data['value'],
            'currency': data.get('currency', 'USD'),
            'order_id': str(data.get('order_id', '')),
        }

    payload = {
        'data': [event_data],
        'test_event_code': data.get('test_code'),
    }
    if not data.get('test_code'):
        payload.pop('test_event_code', None)

    url = f"https://graph.facebook.com/v19.0/{pixel_id}/events?access_token={token}"

    def _send():
        try:
            body = json.dumps(payload).encode()
            req = _urllib_req.Request(url, data=body,
                                      headers={'Content-Type': 'application/json'},
                                      method='POST')
            _urllib_req.urlopen(req, timeout=8)
            _auto_log('capi_event', 'ok', f'{event_name} → pixel {pixel_id}')
        except Exception as e:
            _auto_log('capi_event', 'error', str(e)[:200])
    threading.Thread(target=_send, daemon=True).start()


def _send_gbp_review_request(order_id, phone, review_url):
    """يسجّل طلب تقييم GBP — يُرسَل عبر n8n أو يُسجَّل في الـ log."""
    if not review_url:
        return
    db = get_db()
    wh = _get_integration('n8n_status_webhook') or _get_integration('n8n_order_webhook')
    db.close()
    payload = {
        'event': 'gbp_review_request',
        'order_id': order_id,
        'phone': phone,
        'review_url': review_url,
    }
    if wh:
        _fire_webhook(wh, payload)
    _auto_log('gbp_review_request', 'ok', f'order {order_id} → {phone}')


# ── Web Push (VAPID) ───────────────────────────────────────────

VAPID_PUBLIC  = "BH_1_iZYQJqB2-gXt8K47NOQ3SKj-NTmpEdE6Wi60w8H_dF4npl2KCP4gWVbaMYiO232gJAMDpVmXMrBGmTdGuQ"
VAPID_PRIVATE = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_EMAIL   = os.environ.get('VAPID_EMAIL', 'mailto:admin@bellapetlb.com')


def _push_send(subscription_info: dict, title: str, body: str, url: str = '/'):
    """يبعث push notification لـ subscription واحد."""
    try:
        from pywebpush import webpush, WebPushException
        webpush(
            subscription_info=subscription_info,
            data=json.dumps({'title': title, 'body': body, 'url': url}),
            vapid_private_key=VAPID_PRIVATE,
            vapid_claims={"sub": VAPID_EMAIL},
        )
        return True
    except Exception as e:
        _auto_log('push', 'error', str(e)[:200])
        return False


def _push_all_for_product(pid: int, title: str, body: str, product_url: str):
    """يبعث push لكل المشتركين بمنتج معين."""
    def _send():
        db = get_db()
        subs = db.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE product_id=?", (pid,)
        ).fetchall()
        db.close()
        sent = 0
        for s in subs:
            ok = _push_send(
                {'endpoint': s['endpoint'], 'keys': {'p256dh': s['p256dh'], 'auth': s['auth']}},
                title, body, product_url
            )
            if ok:
                sent += 1
        if sent:
            _auto_log('push_stock_return', 'ok', f'pid={pid} → {sent} إشعار')
    threading.Thread(target=_send, daemon=True).start()


def _push_broadcast(title: str, body: str, url: str = '/', log_event: str = 'push_broadcast'):
    """يبعث push لكل المشتركين العامين (product_id IS NULL)."""
    def _send():
        db = get_db()
        subs = db.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE product_id IS NULL"
        ).fetchall()
        db.close()
        sent, fail = 0, 0
        dead = []
        for s in subs:
            ok = _push_send(
                {'endpoint': s['endpoint'], 'keys': {'p256dh': s['p256dh'], 'auth': s['auth']}},
                title, body, url
            )
            if ok:
                sent += 1
            else:
                fail += 1
                dead.append(s['endpoint'])
        # احذف الـ subscriptions الميتة
        if dead:
            db2 = get_db()
            for ep in dead:
                db2.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (ep,))
            db2.commit(); db2.close()
        _auto_log(log_event, 'ok', f'📤 {sent} وصل | {fail} فشل')
    threading.Thread(target=_send, daemon=True).start()


# ── API auth ────────────────────────────────────────────────────

def _api_auth():
    """تحقق من API key — يرجع True إذا صحيح."""
    key = (
        request.headers.get('X-API-Key') or
        request.args.get('api_key') or
        ((request.get_json(silent=True) or {}).get('api_key') if request.is_json else None)
    )
    if not key:
        return False
    db = get_db()
    row = db.execute(
        "SELECT id FROM api_keys WHERE key=? AND is_active=1", (key,)
    ).fetchone()
    if row:
        db.execute("UPDATE api_keys SET last_used=datetime('now') WHERE key=?", (key,))
        db.commit()
    db.close()
    return bool(row)

def _order_payload(order_id, db):
    """بنِ payload كامل للطلب لإرساله لـ n8n."""
    order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        return None
    items = db.execute(
        """SELECT oi.qty, oi.price_at_order,
                  p.name_ar, p.name_en, p.slug
           FROM order_items oi
           JOIN products p ON p.id = oi.product_id
           WHERE oi.order_id=?""", (order_id,)
    ).fetchall()
    return {
        'order_id':      order['id'],
        'customer_name': order['customer_name'],
        'phone':         order['phone'],
        'area':          order['area'],
        'address_note':  order['address_note'],
        'total':         order['total'],
        'status':        order['status'],
        'created_at':    order['created_at'],
        'items': [
            {'name_ar': i['name_ar'], 'name_en': i['name_en'],
             'qty': i['qty'], 'price': i['price_at_order'],
             'slug': i['slug']}
            for i in items
        ],
    }

def _validate_product(form):
    errors = {}
    if not form.get('name_en', '').strip(): errors['name_en'] = True
    if not form.get('name_ar', '').strip(): errors['name_ar'] = True
    if not form.get('category_id'):         errors['category_id'] = True
    try:
        if float(form.get('price', '') or '') <= 0: raise ValueError
    except (TypeError, ValueError):
        errors['price'] = True
    return errors

def _slugify_ar(text):
    """Convert Arabic text to URL slug: keep Arabic chars + hyphens, replace spaces."""
    import re as _re
    text = text.strip()
    text = _re.sub(r'[^؀-ۿ\w\s-]', '', text, flags=_re.UNICODE)
    text = _re.sub(r'\s+', '-', text)
    text = _re.sub(r'-+', '-', text)
    return text.strip('-')

def _product_vals(form, slug, slug_ar=None):
    def fi(k): return int(form.get(k)) if form.get(k) else None
    def ff(k): return float(form.get(k)) if form.get(k) else None
    return (
        fi('category_id'), fi('subcategory_id'), slug, slug_ar or None,
        form.get('name_en','').strip(), form.get('name_ar','').strip(),
        form.get('brand','').strip() or None,
        form.get('benefit_en','').strip() or None,
        form.get('benefit_ar','').strip() or None,
        form.get('short_desc_ar','').strip() or None,
        form.get('short_desc_en','').strip() or None,
        form.get('description_en','').strip() or None,
        form.get('description_ar','').strip() or None,
        ff('price'), ff('discount_price'),
        int(form.get('stock_qty',0) or 0),
        1 if form.get('is_consumable') else 0,
        ff('consumption_grams_per_kg_day'), ff('package_weight_grams'),
        fi('min_age_months'), fi('max_age_months'),
        form.get('size_tag','all'),
        form.get('health_tags','').strip() or None,
        1 if form.get('is_featured') else 0,
        1 if form.get('is_active') else 0,
    )

def _compress_image(src_path, max_width=1200, quality=82):
    """Resize to max_width and save as WebP for best compression."""
    if not _PILLOW:
        return
    try:
        img = Image.open(src_path).convert('RGB')
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        webp_path = os.path.splitext(src_path)[0] + '.webp'
        img.save(webp_path, 'WEBP', quality=quality, method=4)
        if webp_path != src_path:
            try:
                os.remove(src_path)
            except OSError:
                pass
        return os.path.basename(webp_path)
    except Exception:
        return None

def _generate_srcset_sizes(base_path, widths=(400, 800)):
    """Generate smaller-width WebP variants for srcset alongside the full image."""
    if not _PILLOW:
        return
    try:
        img = Image.open(base_path).convert('RGB')
        stem = os.path.splitext(base_path)[0]
        for w in widths:
            if img.width <= w:
                continue
            ratio = w / img.width
            thumb = img.resize((w, int(img.height * ratio)), Image.LANCZOS)
            thumb.save(f'{stem}_{w}.webp', 'WEBP', quality=78, method=4)
    except Exception:
        pass

def _save_images(files, product_id, cur):
    os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
    for f in files:
        if not f or not f.filename: continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ('.jpg', '.jpeg', '.png', '.webp'): continue
        fname = f'{product_id}_{os.urandom(4).hex()}{ext}'
        fpath = os.path.join(config.UPLOAD_FOLDER, fname)
        f.save(fpath)
        compressed = _compress_image(fpath)
        if compressed:
            fname = compressed
            fpath = os.path.join(config.UPLOAD_FOLDER, fname)
        _generate_srcset_sizes(fpath)
        cur.execute('INSERT INTO product_images (product_id, filename) VALUES (?,?)',
                    (product_id, fname))

app = Flask(__name__)
app.config.from_object(config)

# ── Rate Limiting (SQLite — works across all gunicorn workers) ──
def _rate_limited(key: str, max_calls: int = 10, window: int = 60) -> bool:
    now = time.time()
    cutoff = now - window
    db = get_db()
    try:
        db.execute("DELETE FROM rate_limit_log WHERE ts < ?", (cutoff,))
        count = db.execute(
            "SELECT COUNT(*) FROM rate_limit_log WHERE key=? AND ts >= ?", (key, cutoff)
        ).fetchone()[0]
        if count >= max_calls:
            db.commit()
            return True
        db.execute("INSERT INTO rate_limit_log (key, ts) VALUES (?,?)", (key, now))
        db.commit()
        return False
    finally:
        db.close()

def _client_ip() -> str:
    return request.headers.get('X-Forwarded-For', request.remote_addr or '0').split(',')[0].strip()

# ── CSRF protection ────────────────────────────────────────────

def _csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']

_CSRF_EXEMPT_PREFIXES = ('/api/', '/admin/push/', '/webhook', '/sw.js')

_db_config_loaded = False
@app.before_request
def _load_db_config_once():
    global _db_config_loaded
    if _db_config_loaded:
        return
    _db_config_loaded = True
    try:
        for _k, _attr in [('gemini_api_key', 'GEMINI_API_KEY'), ('anthropic_api_key', 'ANTHROPIC_API_KEY'),
                           ('whatsapp_number', 'WHATSAPP_NUMBER')]:
            _v = _get_integration(_k)
            if _v:
                setattr(config, _attr, _v)
    except Exception:
        pass

@app.before_request
def csrf_check():
    if request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
        return
    if any(request.path.startswith(p) for p in _CSRF_EXEMPT_PREFIXES):
        return
    token = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token')
    if not token or not session.get('csrf_token') or token != session['csrf_token']:
        return 'طلب غير صالح — CSRF validation failed', 403

app.jinja_env.globals['csrf_token'] = _csrf_token

import markupsafe

@app.template_filter('nl2br')
def nl2br_filter(text):
    if not text:
        return ''
    escaped = markupsafe.escape(text)
    return markupsafe.Markup(str(escaped).replace('\n', '<br>'))


@app.errorhandler(404)
def page_not_found(e):
    try:
        db = get_db()
        db.execute(
            "INSERT INTO not_found_log (path, referrer, ua) VALUES (?, ?, ?)",
            (request.path, request.referrer or '', request.user_agent.string[:200])
        )
        db.commit()
        db.close()
    except Exception:
        pass
    return render_template('404.html'), 404

@app.errorhandler(500)
def server_error(e):
    return render_template('500.html'), 500


@app.before_request
def ensure_language():
    if "lang" not in session:
        session["lang"] = config.DEFAULT_LANGUAGE

@app.before_request
def handle_redirects():
    path = request.path.rstrip('/')  or '/'
    db = get_db()
    row = db.execute(
        "SELECT to_path FROM redirects WHERE from_path=? AND is_active=1", (path,)
    ).fetchone()
    db.close()
    if row:
        return redirect(row['to_path'], 301)


@app.context_processor
def inject_globals():
    lang = session.get("lang", config.DEFAULT_LANGUAGE)

    def pick(en_value, ar_value):
        return ar_value if lang == "ar" else en_value

    cart_count = sum(session.get("cart", {}).values())

    def get_seo(page_type, page_id=None, page_slug=None):
        db = get_db()
        if page_id is not None:
            row = db.execute(
                'SELECT * FROM seo_meta WHERE page_type=? AND page_id=?',
                (page_type, page_id)
            ).fetchone()
        else:
            row = db.execute(
                'SELECT * FROM seo_meta WHERE page_type=? AND page_slug=?',
                (page_type, page_slug)
            ).fetchone()
        db.close()
        return row

    # عدد طلبات الإشعار المنتظرة — للـ badge بالأدمن
    def _pending_notify():
        try:
            db = get_db()
            c  = db.execute("SELECT COUNT(*) as c FROM stock_notifications WHERE notified=0").fetchone()['c']
            db.close()
            return c
        except Exception:
            return 0

    # قائمة الأقضية المفعّلة — للـ sub cart inline form والـ selects
    def _global_zones():
        try:
            db = get_db()
            rows = db.execute(
                "SELECT name_ar, name_en, fee, enabled FROM shipping_zones ORDER BY sort_order, name_ar"
            ).fetchall()
            db.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    return {
        "lang": lang,
        "site_name_en": config.SITE_NAME_EN,
        "site_name_ar": config.SITE_NAME_AR,
        "currency": config.CURRENCY_SYMBOL,
        "whatsapp_number": config.WHATSAPP_NUMBER,
        "pick": pick,
        "cart_count": cart_count,
        "get_seo": get_seo,
        "seo_api_ready": bool(config.ANTHROPIC_API_KEY),
        "gemini_api_ready": bool(config.GEMINI_API_KEY),
        "pending_notify_count": _pending_notify(),
        "global_zones": _global_zones(),
        "pixels": _get_pixels(),
    }


@app.route("/set-language/<lang>")
def set_language_route(lang):
    if lang in config.SUPPORTED_LANGUAGES:
        session["lang"] = lang
    return redirect(request.referrer or url_for("index"))


def _best_tier_price(tiers, qty):
    """يرجع أفضل سعر وحدة حسب الكمية (أو None إذا ما في tiers)."""
    best = None
    for t in tiers:
        if qty >= t['min_qty']:
            best = t['price_per_unit']
    return best

def _get_perk(db, phone, order_total=0):
    """يجلب ميزة الزبون — يتحقق من تاريخ الانتهاء والشروط.
       يرجع (perk_row, condition_ok, condition_msg)"""
    if not phone:
        return None, False, None
    row = db.execute(
        "SELECT * FROM customer_perks WHERE phone=?", (phone.strip(),)
    ).fetchone()
    if not row:
        return None, False, None

    from datetime import datetime
    today = datetime.now().strftime('%Y-%m-%d')

    # انتهت صلاحية الميزة
    if row['expires_at'] and today > row['expires_at']:
        return None, False, None

    # التحقق من الشرط
    ct = row['condition_type'] or ''
    cv = row['condition_value'] or ''

    if ct == 'min_order' and cv:
        if order_total < float(cv):
            return row, False, f"الميزة تتفعل عند فاتورة ${ cv }+"
    elif ct == 'before_date' and cv:
        if today > cv:
            return row, False, "انتهت فترة العرض"
    elif ct == 'social' and cv:
        # لا يمكن التحقق تلقائياً — نعرضها كتذكير للزبون
        return row, True, f"تذكير: {cv}"

    return row, True, None


def _get_cart_promos(db, cart_total):
    """يجلب العروض العامة النشطة مع حالة كل واحدة بالنسبة لإجمالي السلة."""
    promos = db.execute(
        "SELECT * FROM cart_promotions WHERE is_active=1 ORDER BY threshold_amount"
    ).fetchall()
    result = []
    for p in promos:
        threshold = p['threshold_amount'] or 0
        unlocked  = cart_total >= threshold
        remaining = max(0, threshold - cart_total)
        pct       = min(100, int(cart_total / threshold * 100)) if threshold > 0 else 100
        gifts     = []
        if p['offer_type'] == 'free_gift':
            gifts = db.execute(
                "SELECT * FROM promo_gift_options WHERE promo_id=? AND is_active=1 ORDER BY sort_order",
                (p['id'],)
            ).fetchall()
        result.append({
            'promo': p, 'unlocked': unlocked,
            'remaining': remaining, 'pct': pct, 'gifts': gifts
        })
    return result


def _apply_promo(unit_price, qty, promo_type):
    """يطبق العرض ويرجع (subtotal, free_qty, saved)."""
    if promo_type == 'b2g1f' and qty >= 3:
        free_qty = qty // 3
        pay_qty  = qty - free_qty
        subtotal = pay_qty * unit_price
        saved    = free_qty * unit_price
        return subtotal, free_qty, saved
    return unit_price * qty, 0, 0.0

@app.route("/cart")
def cart():
    cart_data = session.get("cart", {})
    items = []
    total = 0.0
    db = get_db()
    if cart_data:
        pids = [int(k) for k in cart_data.keys()]
        placeholders = ','.join('?' * len(pids))
        all_products = {
            str(r['id']): r for r in db.execute(
                f"SELECT * FROM products WHERE id IN ({placeholders}) AND is_active=1", pids
            ).fetchall()
        }
        all_tiers_rows = db.execute(
            f"SELECT * FROM product_price_tiers WHERE product_id IN ({placeholders}) ORDER BY min_qty",
            pids
        ).fetchall()
        tiers_by_pid = {}
        for t in all_tiers_rows:
            tiers_by_pid.setdefault(t['product_id'], []).append(t)

        for pid_str, qty in cart_data.items():
            p = all_products.get(pid_str)
            if p:
                tiers = tiers_by_pid.get(p['id'], [])
                base_price = p["discount_price"] if p["discount_price"] else p["price"]
                tier_price = _best_tier_price(tiers, qty)
                price      = tier_price if tier_price is not None else base_price
                subtotal, free_qty, saved = _apply_promo(price, qty, p["promo_type"])
                if free_qty == 0:
                    subtotal = price * qty
                total     += subtotal
                items.append({"product": p, "qty": qty, "price": price,
                               "base_price": base_price, "subtotal": subtotal,
                               "has_tier": tier_price is not None and tier_price < base_price,
                               "tiers": tiers, "free_qty": free_qty, "promo_saved": saved})
        cart_promos = _get_cart_promos(db, total)
    else:
        cart_promos = []
    free_shipping_unlocked = any(
        cp['unlocked'] and cp['promo']['offer_type'] == 'free_shipping'
        for cp in cart_promos
    )
    zones      = _zones_fees(db)
    zones_list = _zones_list(db)
    db.close()
    selected_gift = session.get('selected_gift')
    return render_template("cart.html", items=items, total=total,
                           cart_promos=cart_promos, selected_gift=selected_gift,
                           free_shipping_unlocked=free_shipping_unlocked,
                           delivery_fees=zones, zones_list=zones_list, active_tab="cart")


@app.route("/cart/select-gift", methods=["POST"])
def cart_select_gift():
    gift_id   = request.form.get("gift_id", type=int)
    promo_id  = request.form.get("promo_id", type=int)
    if gift_id and promo_id:
        db   = get_db()
        gift = db.execute(
            "SELECT * FROM promo_gift_options WHERE id=? AND promo_id=? AND is_active=1",
            (gift_id, promo_id)
        ).fetchone()
        db.close()
        if gift:
            session['selected_gift'] = {
                'promo_id': promo_id, 'gift_id': gift_id,
                'name_ar': gift['name_ar'], 'name_en': gift['name_en']
            }
    elif gift_id == 0:
        session.pop('selected_gift', None)
    return jsonify({'ok': True})


@app.route("/cart/add", methods=["POST"])
def cart_add():
    product_id = request.form.get("product_id", type=int)
    qty = request.form.get("qty", 1, type=int)
    if not product_id or qty < 1:
        return {"ok": False}, 400
    cart = session.get("cart", {})
    key = str(product_id)
    cart[key] = cart.get(key, 0) + qty
    session["cart"] = cart
    session.modified = True
    return {"ok": True, "count": sum(cart.values())}


@app.route("/cart/update", methods=["POST"])
def cart_update():
    product_id = request.form.get("product_id", type=int)
    qty = request.form.get("qty", 0, type=int)
    cart = session.get("cart", {})
    key = str(product_id)
    if qty <= 0:
        cart.pop(key, None)
    else:
        cart[key] = qty
    session["cart"] = cart
    session.modified = True
    return {"ok": True, "count": sum(cart.values())}


@app.route("/cart/mini")
def cart_mini():
    cart_data = session.get("cart", {})
    items = []
    total = 0.0
    if cart_data:
        db = get_db()
        for pid_str, qty in cart_data.items():
            p = db.execute(
                """SELECT p.id, p.slug, p.name_ar, p.name_en, p.price, p.discount_price,
                          (SELECT filename FROM product_images
                           WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS primary_img
                   FROM products p WHERE p.id=? AND p.is_active=1""",
                (int(pid_str),)
            ).fetchone()
            if p:
                price = p["discount_price"] if p["discount_price"] else p["price"]
                subtotal = price * qty
                total += subtotal
                items.append({"id": p["id"], "slug": p["slug"],
                               "name_ar": p["name_ar"], "name_en": p["name_en"],
                               "price": price, "subtotal": subtotal, "qty": qty,
                               "img": p["primary_img"] or ""})
        promos_raw = _get_cart_promos(db, total)
        db.close()
        promos = [{"title_ar": cp["promo"]["title_ar"], "title_en": cp["promo"]["title_en"],
                   "offer_type": cp["promo"]["offer_type"],
                   "threshold": cp["promo"]["threshold_amount"] or 0,
                   "remaining": cp["remaining"], "pct": cp["pct"],
                   "unlocked": cp["unlocked"]}
                  for cp in promos_raw]
    else:
        db = get_db()
        promos_raw = _get_cart_promos(db, 0)
        db.close()
        promos = [{"title_ar": cp["promo"]["title_ar"], "title_en": cp["promo"]["title_en"],
                   "offer_type": cp["promo"]["offer_type"],
                   "threshold": cp["promo"]["threshold_amount"] or 0,
                   "remaining": cp["remaining"], "pct": 0,
                   "unlocked": False}
                  for cp in promos_raw]
    return jsonify({"items": items, "total": round(total, 2),
                    "count": sum(cart_data.values()) if cart_data else 0,
                    "promos": promos, "currency": config.CURRENCY_SYMBOL})


@app.route("/cart/clear", methods=["POST"])
def cart_clear():
    session.pop("cart", None)
    return redirect(url_for("cart"))


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    cart_data = session.get("cart", {})
    if not cart_data:
        return redirect(url_for("cart"))

    db = get_db()

    # اجمع المنتجات وتحقق من المخزون
    items = []
    total = 0.0
    for pid_str, qty in cart_data.items():
        p = db.execute(
            "SELECT * FROM products WHERE id = ? AND is_active = 1", (int(pid_str),)
        ).fetchone()
        if p:
            price = p["discount_price"] if p["discount_price"] else p["price"]
            subtotal, free_qty, saved = _apply_promo(price, qty, p["promo_type"])
            if free_qty == 0:
                subtotal = price * qty
            total += subtotal
            items.append({"product": p, "qty": qty, "price": price, "subtotal": subtotal,
                          "free_qty": free_qty, "promo_saved": saved})

    if request.method == "POST":
        name    = request.form.get("name", "").strip()
        phone   = request.form.get("phone", "").strip()
        area    = request.form.get("area", "").strip()
        note    = request.form.get("note", "").strip()

        if _rate_limited(f'checkout:{_client_ip()}', max_calls=5, window=60):
            return 'محاولات كثيرة — انتظر دقيقة', 429
        errors = {}
        if not name:
            errors["name"] = True
        if not phone or not re.match(r'^\+?[\d\s\-]{7,15}$', phone):
            errors["phone"] = True
        if not area:
            errors["area"] = True
        else:
            zone_row = db.execute("SELECT enabled, fee FROM shipping_zones WHERE name_ar=?", (area,)).fetchone()
            if not zone_row or not zone_row['enabled']:
                errors["area_unavailable"] = True

        # تطبيق ميزة الولاء
        perk, cond_ok, cond_msg = _get_perk(db, phone, total)
        perk_label   = None
        perk_savings = 0.0
        final_total  = total
        if perk:
            pt = perk['perk_type']
            pv = perk['perk_value'] or ''
            if pt == 'blocked':
                errors['blocked'] = True
            elif not cond_ok:
                # الشرط ما تحقق — بس اعرض رسالة، ما تطبق الخصم
                perk_label = cond_msg
            else:
                if pt == 'discount_pct' and pv:
                    pct          = float(pv)
                    perk_savings = round(total * pct / 100, 2)
                    final_total  = round(total - perk_savings, 2)
                    perk_label   = f'خصم {pv}%'
                    if cond_msg: perk_label += f' — {cond_msg}'
                elif pt in ('discount_fixed', 'voucher') and pv:
                    perk_savings = min(float(pv), total)
                    final_total  = round(total - perk_savings, 2)
                    perk_label   = f'خصم ${pv}' if pt == 'discount_fixed' else f'قسيمة ${pv}'
                    if cond_msg: perk_label += f' — {cond_msg}'
                elif pt == 'free_shipping':
                    perk_label   = 'شحن مجاني'
                    if cond_msg: perk_label += f' — {cond_msg}'

        # هدية العرض العام
        selected_gift = session.get('selected_gift')
        # تحقق أن الهدية لا تزال مفعّلة (الإجمالي وصل الحد)
        gift_note = None
        if selected_gift:
            promo = db.execute(
                "SELECT * FROM cart_promotions WHERE id=? AND is_active=1",
                (selected_gift['promo_id'],)
            ).fetchone()
            if promo and total >= (promo['threshold_amount'] or 0):
                gift_note = f"{selected_gift['name_ar']} / {selected_gift['name_en']}"
            else:
                session.pop('selected_gift', None)
                selected_gift = None

        if not errors and items:
            # رسوم التوصيل — صفر إذا الشحن مجاني
            delivery_fee = 0.0
            if zone_row and zone_row['enabled']:
                is_free_ship = perk and perk['perk_type'] == 'free_shipping' and cond_ok
                delivery_fee = 0.0 if is_free_ship else float(zone_row['fee'])
            grand_total = round(final_total + delivery_fee, 2)

            cur = db.cursor()
            rev_token = secrets.token_urlsafe(20)
            cur.execute(
                "INSERT INTO orders (customer_name, phone, area, address_note, total, delivery_fee, gift_note, review_token) VALUES (?,?,?,?,?,?,?,?)",
                (name, phone, area, note or None, grand_total, delivery_fee, gift_note, rev_token),
            )
            order_id = cur.lastrowid
            for item in items:
                cur.execute(
                    "INSERT INTO order_items (order_id, product_id, qty, price_at_order) VALUES (?,?,?,?)",
                    (order_id, item["product"]["id"], item["qty"], item["price"]),
                )
            db.commit()
            webhook_url = _get_integration('n8n_order_webhook')
            if webhook_url:
                payload = _order_payload(order_id, db)
                _fire_webhook(webhook_url, payload)
            # Meta CAPI Purchase event
            px = _get_pixels()
            _send_capi_event('Purchase', px.get('meta_pixel_id'), px.get('meta_capi_token'), {
                'order_id': order_id,
                'value': grand_total,
                'currency': 'USD',
                'phone': phone,
                'ip': request.remote_addr or '',
                'ua': request.headers.get('User-Agent', ''),
                'url': request.url,
            })
            db.close()
            session.pop("cart", None)
            session.pop("selected_gift", None)
            confirmed = session.get('confirmed_orders', [])
            confirmed.append(order_id)
            session['confirmed_orders'] = confirmed[-10:]
            return redirect(url_for("order_confirm", order_id=order_id))

        zones      = _zones_fees(db)
        zones_list = _zones_list(db)
        db.close()
        return render_template(
            "checkout.html", items=items, total=total,
            final_total=final_total, perk=perk, perk_label=perk_label,
            perk_savings=perk_savings, selected_gift=selected_gift,
            errors=errors, form=request.form,
            delivery_fees=zones, zones_list=zones_list, active_tab="cart"
        )

    selected_gift = session.get('selected_gift')
    zones      = _zones_fees(db)
    zones_list = _zones_list(db)
    db.close()
    return render_template(
        "checkout.html", items=items, total=total,
        final_total=total, perk=None, perk_label=None, perk_savings=0,
        selected_gift=selected_gift, delivery_fees=zones, zones_list=zones_list,
        errors={}, form={}, active_tab="cart"
    )


@app.route("/cart/quick-order", methods=["POST"])
def cart_quick_order():
    """يحفظ الطلب من السلة مباشرة (مستخدَم مع زر الواتساب) ويرجع JSON."""
    if _rate_limited(f'checkout:{_client_ip()}', max_calls=5, window=60):
        return jsonify({'ok': False, 'error': 'rate_limit'}), 429

    cart_data = session.get("cart", {})
    if not cart_data:
        return jsonify({'ok': False, 'error': 'empty_cart'}), 400

    name  = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    area  = request.form.get("area", "").strip()
    note  = request.form.get("note", "").strip()

    if not name or not phone or not area:
        return jsonify({'ok': False, 'error': 'missing_fields'}), 400

    db = get_db()

    zone_row = db.execute("SELECT enabled, fee FROM shipping_zones WHERE name_ar=?", (area,)).fetchone()
    if not zone_row or not zone_row['enabled']:
        db.close()
        return jsonify({'ok': False, 'error': 'area_unavailable'}), 400

    # بناء قائمة المنتجات وحساب الإجمالي
    items = []
    total = 0.0
    for pid_str, qty in cart_data.items():
        p = db.execute("SELECT * FROM products WHERE id=? AND is_active=1", (int(pid_str),)).fetchone()
        if p:
            price = p["discount_price"] if p["discount_price"] else p["price"]
            subtotal, free_qty, _ = _apply_promo(price, qty, p["promo_type"])
            if free_qty == 0:
                subtotal = price * qty
            total += subtotal
            items.append({"product": p, "qty": qty, "price": price, "subtotal": subtotal})

    if not items:
        db.close()
        return jsonify({'ok': False, 'error': 'no_items'}), 400

    # الولاء
    perk, cond_ok, _ = _get_perk(db, phone, total)
    final_total = total
    if perk and cond_ok:
        pt, pv = perk['perk_type'], perk['perk_value'] or ''
        if pt == 'blocked':
            db.close()
            return jsonify({'ok': False, 'error': 'blocked'}), 403
        elif pt == 'discount_pct' and pv:
            final_total = round(total - total * float(pv) / 100, 2)
        elif pt in ('discount_fixed', 'voucher') and pv:
            final_total = round(total - min(float(pv), total), 2)

    # هدية
    selected_gift = session.get('selected_gift')
    gift_note = None
    if selected_gift:
        promo = db.execute("SELECT * FROM cart_promotions WHERE id=? AND is_active=1",
                           (selected_gift['promo_id'],)).fetchone()
        if promo and total >= (promo['threshold_amount'] or 0):
            gift_note = f"{selected_gift['name_ar']} / {selected_gift['name_en']}"

    is_free_ship = perk and perk['perk_type'] == 'free_shipping' and cond_ok
    delivery_fee = 0.0 if is_free_ship else float(zone_row['fee'])
    grand_total  = round(final_total + delivery_fee, 2)

    cur = db.cursor()
    rev_token = secrets.token_urlsafe(20)
    cur.execute(
        "INSERT INTO orders (customer_name, phone, area, address_note, total, delivery_fee, gift_note, review_token) VALUES (?,?,?,?,?,?,?,?)",
        (name, phone, area, note or None, grand_total, delivery_fee, gift_note, rev_token),
    )
    order_id = cur.lastrowid
    for item in items:
        cur.execute(
            "INSERT INTO order_items (order_id, product_id, qty, price_at_order) VALUES (?,?,?,?)",
            (order_id, item["product"]["id"], item["qty"], item["price"]),
        )
    db.commit()

    webhook_url = _get_integration('n8n_order_webhook')
    if webhook_url:
        payload = _order_payload(order_id, db)
        _fire_webhook(webhook_url, payload)

    px = _get_pixels()
    _send_capi_event('Purchase', px.get('meta_pixel_id'), px.get('meta_capi_token'), {
        'order_id': order_id, 'value': grand_total, 'currency': 'USD', 'phone': phone,
        'ip': request.remote_addr or '', 'ua': request.headers.get('User-Agent', ''),
        'url': request.host_url + 'cart',
    })
    db.close()

    session.pop("cart", None)
    session.pop("selected_gift", None)
    confirmed = session.get('confirmed_orders', [])
    confirmed.append(order_id)
    session['confirmed_orders'] = confirmed[-10:]

    return jsonify({'ok': True, 'order_id': order_id})


@app.route("/order/<int:order_id>")
def order_confirm(order_id):
    if order_id not in session.get('confirmed_orders', []):
        return render_template('404.html'), 404
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        db.close()
        return redirect(url_for("index"))
    items = db.execute(
        """SELECT oi.qty, oi.price_at_order, p.name_en, p.name_ar
           FROM order_items oi JOIN products p ON oi.product_id = p.id
           WHERE oi.order_id = ?""",
        (order_id,),
    ).fetchall()
    db.close()
    return render_template("order_confirm.html", order=order, items=items, active_tab="orders")


# ── Notify Me when back in stock ──────────────────────────────────
@app.route('/notify-me-category', methods=['POST'])
def notify_me_category():
    cat_slug = (request.form.get('category_slug') or '').strip()
    phone    = (request.form.get('phone') or '').strip()
    if not cat_slug or not phone:
        return jsonify({'ok': False}), 400
    db  = get_db()
    cat = db.execute('SELECT name_ar, name_en FROM categories WHERE slug=?', (cat_slug,)).fetchone()
    db.close()
    webhook_url = _get_integration('n8n_notify_webhook')
    if webhook_url and cat:
        _fire_webhook(webhook_url, {
            'event':        'notify_me_category',
            'category_slug': cat_slug,
            'name_ar':      cat['name_ar'],
            'name_en':      cat['name_en'],
            'phone':        phone,
        })
    return jsonify({'ok': True})


@app.route('/pwa/event', methods=['POST'])
def pwa_event():
    data  = request.get_json(silent=True) or {}
    event = data.get('event', '').strip()
    ua    = (data.get('ua') or '')[:300]
    if event not in ('installed', 'launch'):
        return jsonify({'ok': False}), 400
    db = get_db()
    db.execute("INSERT INTO pwa_events (event, ua) VALUES (?,?)", (event, ua))
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/push/vapid-key')
def push_vapid_key():
    return jsonify({'publicKey': VAPID_PUBLIC})


@app.route('/push/subscribe', methods=['POST'])
def push_subscribe():
    data    = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint', '').strip()
    p256dh   = (data.get('keys') or {}).get('p256dh', '').strip()
    auth     = (data.get('keys') or {}).get('auth', '').strip()
    pid      = data.get('product_id')   # اختياري — لربط الـ subscription بمنتج
    phone    = (data.get('phone') or '').strip()

    if not endpoint or not p256dh or not auth:
        return jsonify({'ok': False, 'error': 'missing fields'}), 400

    db = get_db()
    try:
        db.execute(
            """INSERT INTO push_subscriptions (endpoint, p256dh, auth, product_id, phone)
               VALUES (?,?,?,?,?)
               ON CONFLICT(endpoint) DO UPDATE SET
                 p256dh=excluded.p256dh, auth=excluded.auth,
                 product_id=COALESCE(excluded.product_id, product_id),
                 phone=COALESCE(excluded.phone, phone)""",
            (endpoint, p256dh, auth, pid, phone or None)
        )
        db.commit()
    except Exception as e:
        db.close()
        return jsonify({'ok': False, 'error': str(e)}), 500
    db.close()
    return jsonify({'ok': True})


@app.route('/push/unsubscribe', methods=['POST'])
def push_unsubscribe():
    data = request.get_json(silent=True) or {}
    ep   = data.get('endpoint', '')
    if ep:
        db = get_db()
        db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (ep,))
        db.commit()
        db.close()
    return jsonify({'ok': True})


@app.route('/admin/push/stats')
@admin_required
def admin_push_stats():
    db = get_db()
    general = db.execute("SELECT COUNT(*) as c FROM push_subscriptions WHERE product_id IS NULL").fetchone()['c']
    product = db.execute("SELECT COUNT(*) as c FROM push_subscriptions WHERE product_id IS NOT NULL").fetchone()['c']
    db.close()
    return jsonify({'general': general, 'product': product, 'total': general + product})


@app.route('/admin/push/broadcast', methods=['POST'])
@admin_required
def admin_push_broadcast():
    title = request.form.get('push_title', '').strip()
    body  = request.form.get('push_body',  '').strip()
    url   = request.form.get('push_url',   '/').strip() or '/'
    if not title:
        return jsonify({'ok': False, 'error': 'title required'}), 400
    _push_broadcast(title, body, url, log_event='push_manual')
    db  = get_db()
    cnt = db.execute("SELECT COUNT(*) as c FROM push_subscriptions WHERE product_id IS NULL").fetchone()['c']
    db.close()
    return jsonify({'ok': True, 'recipients': cnt})


@app.route('/notify-me', methods=['POST'])
def notify_me():
    pid   = request.form.get('product_id', type=int)
    phone = (request.form.get('phone') or '').strip()
    name  = (request.form.get('name')  or '').strip() or None
    if not pid or not phone:
        return jsonify({'ok': False, 'error': 'بيانات ناقصة'}), 400
    db  = get_db()
    prod = db.execute('SELECT id, name_ar, name_en FROM products WHERE id=?', (pid,)).fetchone()
    if not prod:
        db.close()
        return jsonify({'ok': False, 'error': 'المنتج غير موجود'}), 404
    try:
        db.execute(
            'INSERT INTO stock_notifications (product_id, phone, name) VALUES (?,?,?)',
            (pid, phone, name)
        )
        db.commit()
    except Exception:
        db.close()
        return jsonify({'ok': True, 'already': True})
    webhook_url = _get_integration('n8n_notify_webhook')
    if webhook_url:
        _fire_webhook(webhook_url, {
            'event':      'notify_me',
            'product_id': pid,
            'name_ar':    prod['name_ar'],
            'name_en':    prod['name_en'],
            'phone':      phone,
            'name':       name,
        })
    db.close()
    return jsonify({'ok': True, 'already': False})


# ── review by token ──
@app.route('/review/<token>', methods=['GET', 'POST'])
def product_review(token):
    db = get_db()
    order = db.execute('SELECT * FROM orders WHERE review_token=?', (token,)).fetchone()
    if not order:
        db.close()
        return render_template('review_invalid.html'), 404

    items = db.execute(
        """SELECT oi.product_id, oi.qty, p.name_ar, p.name_en,
                  (SELECT filename FROM product_images
                   WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS img
           FROM order_items oi JOIN products p ON p.id=oi.product_id
           WHERE oi.order_id=?""",
        (order['id'],)
    ).fetchall()

    already = {r['product_id'] for r in
               db.execute('SELECT product_id FROM product_reviews WHERE order_id=?',
                          (order['id'],)).fetchall()}

    if request.method == 'POST':
        for item in items:
            pid = item['product_id']
            val = request.form.get(f'r_{pid}', '')
            if val.isdigit() and 1 <= int(val) <= 5:
                db.execute(
                    'INSERT OR REPLACE INTO product_reviews (order_id,product_id,rating) VALUES (?,?,?)',
                    (order['id'], pid, int(val))
                )
        db.commit()
        db.close()
        return render_template('review_thanks.html', name=order['customer_name'])

    db.close()
    return render_template('review.html', order=order, items=items, already=already)


# ════════════════════════════════════════
#  ADMIN
# ════════════════════════════════════════

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
    error = None
    if request.method == 'POST':
        if _rate_limited(f'login:{_client_ip()}', max_calls=5, window=60):
            return 'محاولات كثيرة — انتظر دقيقة', 429
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        db = get_db()
        user = db.execute('SELECT * FROM admin_users WHERE username=?', (username,)).fetchone()
        db.close()
        if user and check_password_hash(user['password_hash'], password):
            session['admin_logged_in'] = True
            session['admin_username'] = username
            return redirect(url_for('admin_dashboard'))
        error = True
    return render_template('admin/login.html', error=error)

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    session.pop('admin_username', None)
    return redirect(url_for('admin_login'))

@app.route('/admin/')
@admin_required
def admin_dashboard():
    from werkzeug.security import check_password_hash
    db = get_db()
    stats = {
        'products':    db.execute("SELECT COUNT(*) FROM products WHERE is_active=1").fetchone()[0],
        'orders_new':  db.execute("SELECT COUNT(*) FROM orders WHERE status='new'").fetchone()[0],
        'orders_total':db.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        'blog':        db.execute("SELECT COUNT(*) FROM blog_posts WHERE is_published=1").fetchone()[0],
    }
    recent = db.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 6").fetchall()
    admin_row = db.execute("SELECT password_hash FROM admin_users WHERE username=?",
                           (session.get('admin_username','admin'),)).fetchone()
    default_pw = admin_row and check_password_hash(admin_row['password_hash'], 'changeme123')
    db.close()
    return render_template('admin/dashboard.html', stats=stats, recent=recent,
                           active_admin='dashboard', default_pw_warning=default_pw)

# ── Products ──

@app.route('/admin/products')
@admin_required
def admin_products():
    db = get_db()
    products = db.execute(
        """SELECT p.*, c.name_en as cat_name FROM products p
           JOIN categories c ON p.category_id=c.id ORDER BY p.created_at DESC"""
    ).fetchall()
    db.close()
    return render_template('admin/products.html', products=products, active_admin='products')

@app.route('/admin/products/new', methods=['GET', 'POST'])
@admin_required
def admin_product_new():
    db = get_db()
    categories   = db.execute('SELECT * FROM categories ORDER BY sort_order').fetchall()
    subcategories= db.execute('SELECT * FROM subcategories ORDER BY category_id, sort_order').fetchall()
    errors, form = {}, {}
    if request.method == 'POST':
        form   = request.form
        errors = _validate_product(form)
        if not errors:
            slug = form.get('slug','').strip() or slugify(form.get('name_en',''))
            if db.execute('SELECT id FROM products WHERE slug=?', (slug,)).fetchone():
                slug += '-' + os.urandom(2).hex()
            slug_ar_raw = form.get('slug_ar','').strip() or _slugify_ar(form.get('name_ar',''))
            if db.execute('SELECT id FROM products WHERE slug_ar=?', (slug_ar_raw,)).fetchone():
                slug_ar_raw += '-' + os.urandom(2).hex()
            cur = db.cursor()
            cur.execute(
                """INSERT INTO products (category_id,subcategory_id,slug,slug_ar,name_en,name_ar,brand,
                   benefit_en,benefit_ar,short_desc_ar,short_desc_en,description_en,description_ar,price,discount_price,
                   stock_qty,is_consumable,consumption_grams_per_kg_day,package_weight_grams,
                   min_age_months,max_age_months,size_tag,health_tags,is_featured,is_active)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                _product_vals(form, slug, slug_ar_raw))
            new_pid  = cur.lastrowid
            name_ar  = request.form.get('name_ar', '').strip()
            prod_slug = slug
            _save_images(request.files.getlist('images'), new_pid, cur)
            brand_id = int(form.get('brand_id') or 0) or None
            qty_presets = form.get('qty_presets', '').strip() or None
            cur.execute('UPDATE products SET brand_id=?, qty_presets=? WHERE id=?', (brand_id, qty_presets, new_pid))
            db.commit(); db.close()
            # Push: أعلم المشتركين بالمنتج الجديد
            _push_broadcast(
                f'🆕 منتج جديد — {name_ar}',
                'تفضّل شوف آخر إضافاتنا!',
                f'/product/{prod_slug}',
                log_event='push_new_product'
            )
            return redirect(url_for('admin_products'))
    all_collections = db.execute('SELECT * FROM collections ORDER BY sort_order').fetchall()
    brands = db.execute('SELECT * FROM brands ORDER BY sort_order, name_ar').fetchall()
    db.close()
    return render_template('admin/product_form.html', product=None, images=[],
                           variants=[], price_tiers=[],
                           all_collections=all_collections, product_col_ids=[],
                           categories=categories, subcategories=subcategories,
                           brands=brands,
                           errors=errors, form=form, active_admin='products')

@app.route('/admin/products/<int:pid>/edit', methods=['GET', 'POST'])
@admin_required
def admin_product_edit(pid):
    db = get_db()
    product = db.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
    if not product: db.close(); return redirect(url_for('admin_products'))
    categories    = db.execute('SELECT * FROM categories ORDER BY sort_order').fetchall()
    subcategories = db.execute('SELECT * FROM subcategories ORDER BY category_id, sort_order').fetchall()
    images      = db.execute('SELECT * FROM product_images WHERE product_id=? ORDER BY sort_order', (pid,)).fetchall()
    variants    = db.execute('SELECT * FROM product_variants WHERE product_id=? ORDER BY sort_order', (pid,)).fetchall()
    price_tiers = db.execute('SELECT * FROM product_price_tiers WHERE product_id=? ORDER BY min_qty', (pid,)).fetchall()
    all_collections = db.execute('SELECT * FROM collections ORDER BY sort_order').fetchall()
    product_col_ids = [r['collection_id'] for r in
                       db.execute('SELECT collection_id FROM collection_products WHERE product_id=?', (pid,)).fetchall()]
    errors, form = {}, dict(product)
    if request.method == 'POST':
        form   = request.form
        errors = _validate_product(form)
        if not errors:
            slug = form.get('slug','').strip() or slugify(form.get('name_en',''))
            if db.execute('SELECT id FROM products WHERE slug=? AND id!=?', (slug, pid)).fetchone():
                slug += '-' + os.urandom(2).hex()
            slug_ar_raw = form.get('slug_ar','').strip() or _slugify_ar(form.get('name_ar',''))
            if db.execute('SELECT id FROM products WHERE slug_ar=? AND id!=?', (slug_ar_raw, pid)).fetchone():
                slug_ar_raw += '-' + os.urandom(2).hex()
            cur = db.cursor()
            cur.execute(
                """UPDATE products SET category_id=?,subcategory_id=?,slug=?,slug_ar=?,name_en=?,name_ar=?,
                   brand=?,benefit_en=?,benefit_ar=?,short_desc_ar=?,short_desc_en=?,description_en=?,description_ar=?,price=?,
                   discount_price=?,stock_qty=?,is_consumable=?,consumption_grams_per_kg_day=?,
                   package_weight_grams=?,min_age_months=?,max_age_months=?,size_tag=?,
                   health_tags=?,is_featured=?,is_active=?,
                   is_bundle=?,bundle_note_ar=?,bundle_note_en=?,
                   promo_label_ar=?,promo_label_en=?,promo_type=?,
                   store_rating=?,rating_note_ar=?,rating_note_en=?,
                   suitable_for_ar=?,suitable_for_en=?,
                   rating_cons_ar=?,rating_cons_en=?,
                   brand_id=?,qty_presets=?
                   WHERE id=?""",
                _product_vals(form, slug, slug_ar_raw) + (
                    1 if form.get('is_bundle') else 0,
                    form.get('bundle_note_ar','').strip() or None,
                    form.get('bundle_note_en','').strip() or None,
                    form.get('promo_label_ar','').strip() or None,
                    form.get('promo_label_en','').strip() or None,
                    form.get('promo_type','').strip() or None,
                    int(form.get('store_rating') or 0) or None,
                    form.get('rating_note_ar','').strip() or None,
                    form.get('rating_note_en','').strip() or None,
                    form.get('suitable_for_ar','').strip() or None,
                    form.get('suitable_for_en','').strip() or None,
                    form.get('rating_cons_ar','').strip() or None,
                    form.get('rating_cons_en','').strip() or None,
                    int(form.get('brand_id') or 0) or None,
                    form.get('qty_presets','').strip() or None,
                    pid,
                ))
            _save_images(request.files.getlist('images'), pid, cur)
            # حفظ المجموعات
            new_col_ids = [int(x) for x in form.getlist('collection_ids') if x]
            db.execute('DELETE FROM collection_products WHERE product_id=?', (pid,))
            for cid in new_col_ids:
                db.execute('INSERT OR IGNORE INTO collection_products (collection_id, product_id) VALUES (?,?)', (cid, pid))
            db.commit(); db.close()
            return redirect(url_for('admin_products'))
    specs = db.execute(
        'SELECT * FROM product_specs WHERE product_id=? ORDER BY sort_order, id', (pid,)
    ).fetchall()
    brands = db.execute('SELECT * FROM brands ORDER BY sort_order, name_ar').fetchall()
    db.close()
    return render_template('admin/product_form.html', product=product, images=images,
                           variants=variants, price_tiers=price_tiers, categories=categories,
                           subcategories=subcategories,
                           all_collections=all_collections,
                           product_col_ids=product_col_ids,
                           specs=specs, brands=brands,
                           errors=errors, form=form, active_admin='products')

@app.route('/admin/specs/save/<int:pid>', methods=['POST'])
@admin_required
def admin_specs_save(pid):
    db = get_db()
    data = request.get_json(force=True)
    new_ids = []
    for s in data.get('specs', []):
        if not s.get('label_ar') and not s.get('label_en'):
            new_ids.append(s.get('id', 0))
            continue
        if s.get('id', 0) > 0:
            db.execute(
                'UPDATE product_specs SET label_ar=?,label_en=?,value_ar=?,value_en=?,sort_order=? WHERE id=? AND product_id=?',
                (s['label_ar'], s.get('label_en',''), s.get('value_ar',''), s.get('value_en',''), s.get('sort_order',0), s['id'], pid)
            )
            new_ids.append(s['id'])
        else:
            cur = db.execute(
                'INSERT INTO product_specs (product_id,label_ar,label_en,value_ar,value_en,sort_order) VALUES (?,?,?,?,?,?)',
                (pid, s['label_ar'], s.get('label_en',''), s.get('value_ar',''), s.get('value_en',''), s.get('sort_order',0))
            )
            new_ids.append(cur.lastrowid)
    db.commit()
    db.close()
    return jsonify({'ok': True, 'ids': new_ids})

@app.route('/admin/specs/delete/<int:sid>', methods=['POST'])
@admin_required
def admin_specs_delete(sid):
    db = get_db()
    db.execute('DELETE FROM product_specs WHERE id=?', (sid,))
    db.commit()
    db.close()
    return jsonify({'ok': True})

@app.route('/admin/products/<int:pid>/delete', methods=['POST'])
@admin_required
def admin_product_delete(pid):
    db = get_db()
    imgs = db.execute('SELECT filename FROM product_images WHERE product_id=?', (pid,)).fetchall()
    for img in imgs:
        fpath = os.path.join(config.UPLOAD_FOLDER, img['filename'])
        try:
            if os.path.exists(fpath): os.remove(fpath)
        except OSError:
            pass
    db.execute('DELETE FROM products WHERE id=?', (pid,))
    db.commit(); db.close()
    return redirect(url_for('admin_products'))

@app.route('/admin/products/<int:pid>/image/<int:iid>/delete', methods=['POST'])
@admin_required
def admin_image_delete(pid, iid):
    db = get_db()
    img = db.execute('SELECT * FROM product_images WHERE id=? AND product_id=?', (iid, pid)).fetchone()
    if img:
        fpath = os.path.join(config.UPLOAD_FOLDER, img['filename'])
        if os.path.exists(fpath): os.remove(fpath)
        db.execute('DELETE FROM product_images WHERE id=?', (iid,))
        db.commit()
    db.close()
    return redirect(url_for('admin_product_edit', pid=pid))

@app.route('/admin/products/<int:pid>/image/<int:iid>/set-primary', methods=['POST'])
@admin_required
def admin_image_set_primary(pid, iid):
    db = get_db()
    imgs = db.execute('SELECT id FROM product_images WHERE product_id=? ORDER BY sort_order', (pid,)).fetchall()
    order = 1
    for img in imgs:
        if img['id'] == iid:
            db.execute('UPDATE product_images SET sort_order=0 WHERE id=?', (iid,))
        else:
            db.execute('UPDATE product_images SET sort_order=? WHERE id=?', (order, img['id']))
            order += 1
    db.commit(); db.close()
    return redirect(url_for('admin_product_edit', pid=pid))


@app.route('/admin/products/<int:pid>/variants/save', methods=['POST'])
@admin_required
def admin_variants_save(pid):
    """يحفظ كل الـ variants دفعة واحدة (replace)."""
    db = get_db()
    # احتفظ بالصور الموجودة قبل الحذف
    old_images = {r['id']: r['image_filename'] for r in
                  db.execute('SELECT id, image_filename FROM product_variants WHERE product_id=?', (pid,)).fetchall()}
    db.execute('DELETE FROM product_variants WHERE product_id=?', (pid,))

    types     = request.form.getlist('var_type')
    label_ar  = request.form.getlist('var_label_ar')
    label_en  = request.form.getlist('var_label_en')
    val_ar    = request.form.getlist('var_val_ar')
    val_en    = request.form.getlist('var_val_en')
    modif     = request.form.getlist('var_price')
    stocks    = request.form.getlist('var_stock')
    skus      = request.form.getlist('var_sku')
    old_ids   = request.form.getlist('var_old_id')
    actives   = request.form.getlist('var_active')   # checkboxes — value = index
    img_files = request.files.getlist('var_image')

    os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)

    for i, t in enumerate(types):
        if not t or not val_ar[i].strip(): continue

        # الصورة: جديدة مرفوعة أو محتفظ بالقديمة
        fname = None
        f = img_files[i] if i < len(img_files) else None
        if f and f.filename:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext in ('.jpg', '.jpeg', '.png', '.webp'):
                fname = f'var_{pid}_{os.urandom(4).hex()}{ext}'
                fpath = os.path.join(config.UPLOAD_FOLDER, fname)
                f.save(fpath)
                compressed = _compress_image(fpath)
                if compressed: fname = compressed
        else:
            # استرجع الصورة القديمة إن وجدت
            try:
                old_id = int(old_ids[i]) if i < len(old_ids) and old_ids[i] else None
                if old_id and old_id in old_images:
                    fname = old_images[old_id]
            except (ValueError, TypeError):
                pass

        is_active = 1 if str(i) in actives else 0
        db.execute("""
            INSERT INTO product_variants
              (product_id, variant_type, type_label_ar, type_label_en,
               value_ar, value_en, price_modifier, stock_qty, sku,
               image_filename, sort_order, is_active)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (pid, t, label_ar[i], label_en[i],
              val_ar[i], val_en[i],
              float(modif[i] or 0),
              int(stocks[i] or 0),
              skus[i] or None,
              fname, i, is_active))

    db.commit(); db.close()
    return redirect(url_for('admin_product_edit', pid=pid))


@app.route('/admin/products/<int:pid>/generate-description', methods=['POST'])
@admin_required
def admin_generate_description(pid):
    """يولّد benefit + description بالعربي والإنجليزي باستخدام Gemini."""
    # اقرأ المفتاح من DB مباشرة عند كل طلب (لا تعتمد على cache)
    gemini_key = _get_integration('gemini_api_key') or config.GEMINI_API_KEY
    anthropic_key = _get_integration('anthropic_api_key') or config.ANTHROPIC_API_KEY
    import sys as _sys
    print(f'[DBG-AI] gemini_key_len={len(gemini_key)} anthropic_key_len={len(anthropic_key)}', file=_sys.stderr, flush=True)
    if not gemini_key and not anthropic_key:
        return jsonify({'ok': False, 'error': 'مفتاح API غير محدد — اذهب إلى الإعدادات'})

    db = get_db()
    p  = db.execute('SELECT p.*, c.name_ar as cat_ar, c.name_en as cat_en FROM products p '
                    'LEFT JOIN categories c ON c.id=p.category_id WHERE p.id=?', (pid,)).fetchone()
    db.close()
    if not p:
        return jsonify({'ok': False, 'error': 'منتج غير موجود'})

    prompt = f"""أنت كاتب محتوى متخصص في متاجر الحيوانات الأليفة في لبنان.
اكتب وصفاً تسويقياً لهذا المنتج:

الاسم العربي: {p['name_ar'] or ''}
الاسم الإنجليزي: {p['name_en'] or ''}
الماركة: {p['brand'] or ''}
التصنيف: {p['cat_ar'] or p['cat_en'] or ''}

القواعد الصارمة:
- benefit_ar: جملة مفيدة واحدة (10-15 كلمة) تلخّص الفائدة الرئيسية للحيوان بالعربي
- benefit_en: نفسها بالإنجليزي (10-15 كلمة)
- short_desc_ar: 2-3 أسطر تسويقية جذابة بالعربي تقنع الزائر في أول 3 ثوانٍ، تحتوي على الكلمة المفتاحية، يليها 4 نقاط (•) تلخّص أهم الميزات (مثال: مكونات طبيعية 100% • خالٍ من الحبوب • يدعم صحة الفرو • مناسب للقطط الصغيرة)
- short_desc_en: نفسها بالإنجليزي
- description_ar: وصف مهيكل بالعربي (150-200 كلمة) يستخدم عناوين فرعية: المكونات والقيمة الغذائية، طريقة التقديم، لماذا تختاره؟ — يدمج كلمات محلية لبنانية بشكل طبيعي
- description_en: نفسه بالإنجليزي (150-200 كلمة)
- لا تذكر سعراً أو توصيلاً
- أسلوب ودود ومقنع

أعد JSON فقط بهذا الشكل بالضبط:
{{
  "benefit_ar": "...",
  "benefit_en": "...",
  "short_desc_ar": "...",
  "short_desc_en": "...",
  "description_ar": "...",
  "description_en": "..."
}}"""

    # استخدم المفتاح من DB مباشرة
    import urllib.request as _ur, json as _js, traceback as _tb
    result = None
    last_err = ''
    if gemini_key:
        try:
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{config.GEMINI_MODEL}:generateContent?key={gemini_key}")
            body = _js.dumps({
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "maxOutputTokens": 3000,
                    "temperature": 0.3,
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            }).encode()
            req = _ur.Request(url, data=body,
                   headers={"Content-Type": "application/json"}, method="POST")
            with _ur.urlopen(req, timeout=30) as r:
                data = _js.loads(r.read())
            print(f'[DBG-AI] raw_keys={list(data.keys())} candidates_len={len(data.get("candidates",[]))}', file=_sys.stderr, flush=True)
            cand = data["candidates"][0]
            print(f'[DBG-AI] finish={cand.get("finishReason")} parts_count={len(cand.get("content",{}).get("parts",[]))}', file=_sys.stderr, flush=True)
            for i, p in enumerate(cand.get("content",{}).get("parts",[])):
                print(f'[DBG-AI] part[{i}] keys={list(p.keys())} text_len={len(p.get("text",""))} text50={repr(p.get("text","")[:50])}', file=_sys.stderr, flush=True)
            parts = cand["content"]["parts"]
            text = ''.join(p.get("text", "") for p in parts).strip()
            start = text.find('{'); end = text.rfind('}') + 1
            print(f'[DBG-AI] combined_len={len(text)} start={start} end={end}', file=_sys.stderr, flush=True)
            if start >= 0 and end > start:
                result = _js.loads(text[start:end])
        except Exception as e:
            last_err = _tb.format_exc()
            print(f'[DBG-AI] exception: {last_err[:300]}', file=_sys.stderr, flush=True)

    print(f'[DBG-AI] result={result is not None} last_err_len={len(last_err)}', file=_sys.stderr, flush=True)
    if not result:
        err_msg = f'فشل التوليد — {last_err[:200]}' if last_err else 'فشل التوليد — تحقق من مفتاح API'
        return jsonify({'ok': False, 'error': err_msg})
    return jsonify({'ok': True, **result})


@app.route('/admin/debug-gemini')
@admin_required
def admin_debug_gemini():
    """مؤقت — لتشخيص مشكلة AI"""
    import urllib.request, json as _json, traceback as _tb
    key = config.GEMINI_API_KEY
    model = config.GEMINI_MODEL
    result = {'key_len': len(key), 'key_start': key[:8] if key else '', 'model': model}
    if not key:
        result['error'] = 'GEMINI_API_KEY فارغ'
        return jsonify(result)
    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={key}")
        body = _json.dumps({"contents": [{"parts": [{"text": "say hi"}]}],
                            "generationConfig": {"maxOutputTokens": 50}}).encode()
        req = urllib.request.Request(url, data=body,
              headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            data = _json.loads(r.read())
        result['gemini_raw'] = data
    except Exception as e:
        result['error'] = _tb.format_exc()
    return jsonify(result)


@app.route('/admin/products/<int:pid>/price-tiers/save', methods=['POST'])
@admin_required
def admin_price_tiers_save(pid):
    db = get_db()
    db.execute('DELETE FROM product_price_tiers WHERE product_id=?', (pid,))
    min_qtys   = request.form.getlist('tier_min_qty')
    prices     = request.form.getlist('tier_price')
    labels_ar  = request.form.getlist('tier_label_ar')
    labels_en  = request.form.getlist('tier_label_en')
    for i, (mq, pr) in enumerate(zip(min_qtys, prices)):
        try:
            mq_int = int(mq)
            pr_fl  = float(pr)
            if mq_int < 1 or pr_fl <= 0: continue
        except (ValueError, TypeError):
            continue
        db.execute(
            'INSERT INTO product_price_tiers (product_id, min_qty, price_per_unit, label_ar, label_en, sort_order) VALUES (?,?,?,?,?,?)',
            (pid, mq_int, pr_fl, labels_ar[i] if i < len(labels_ar) else None,
             labels_en[i] if i < len(labels_en) else None, i))
    db.commit(); db.close()
    return jsonify({'ok': True})


# ── Orders ──

@app.route('/admin/orders')
@admin_required
def admin_orders():
    db = get_db()
    sf         = request.args.get('status', '')
    rating_f   = request.args.get('rating', '')
    date_from  = request.args.get('date_from', '')
    date_to    = request.args.get('date_to', '')
    search     = request.args.get('search', '').strip()

    where, params = [], []
    if sf:
        where.append('o.status=?'); params.append(sf)
    if date_from:
        where.append('DATE(o.created_at)>=?'); params.append(date_from)
    if date_to:
        where.append('DATE(o.created_at)<=?'); params.append(date_to)
    if rating_f:
        where.append('cp.behavior_rating=?'); params.append(int(rating_f))
    if search:
        where.append('(o.customer_name LIKE ? OR o.phone LIKE ?)')
        params += [f'%{search}%', f'%{search}%']

    sql = """SELECT o.*, cp.behavior_rating, cp.behavior_note
             FROM orders o
             LEFT JOIN customer_perks cp ON cp.phone=o.phone
             {where}
             ORDER BY o.created_at DESC""".format(
        where='WHERE ' + ' AND '.join(where) if where else ''
    )
    orders = db.execute(sql, params).fetchall()
    db.close()
    return render_template('admin/orders.html', orders=orders,
                           status_filter=sf, rating_filter=rating_f,
                           date_from=date_from, date_to=date_to,
                           search=search, active_admin='orders')

@app.route('/admin/orders/<int:oid>/rate-customer', methods=['POST'])
@admin_required
def admin_rate_customer(oid):
    db  = get_db()
    order = db.execute('SELECT phone FROM orders WHERE id=?', (oid,)).fetchone()
    if order:
        rating = int(request.form.get('behavior_rating') or 0) or None
        note   = request.form.get('behavior_note', '').strip() or None
        db.execute(
            """INSERT INTO customer_perks (phone, perk_type, behavior_rating, behavior_note)
               VALUES (?, 'none', ?, ?)
               ON CONFLICT(phone) DO UPDATE SET
                 behavior_rating=excluded.behavior_rating,
                 behavior_note=excluded.behavior_note""",
            (order['phone'], rating, note)
        )
        db.commit()
    db.close()
    return redirect(url_for('admin_order_detail', oid=oid))

@app.route('/admin/orders/<int:oid>')
@admin_required
def admin_order_detail(oid):
    db = get_db()
    order = db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
    if not order: db.close(); return redirect(url_for('admin_orders'))
    items = db.execute(
        """SELECT oi.*, p.name_en, p.name_ar, p.slug FROM order_items oi
           JOIN products p ON oi.product_id=p.id WHERE oi.order_id=?""", (oid,)
    ).fetchall()
    status_log = db.execute(
        'SELECT * FROM order_status_log WHERE order_id=? ORDER BY created_at', (oid,)
    ).fetchall()
    cust_profile = db.execute(
        'SELECT behavior_rating, behavior_note FROM customer_perks WHERE phone=?',
        (order['phone'],)
    ).fetchone()
    db.close()
    return render_template('admin/order_detail.html', order=order, items=items,
                           status_log=status_log, cust_profile=cust_profile,
                           active_admin='orders')

@app.route('/admin/orders/<int:oid>/slip')
@admin_required
def admin_order_slip(oid):
    db = get_db()
    order = db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
    if not order: db.close(); return redirect(url_for('admin_orders'))
    items = db.execute(
        """SELECT oi.qty, oi.price_at_order, p.name_ar, p.name_en
           FROM order_items oi JOIN products p ON oi.product_id=p.id
           WHERE oi.order_id=?""", (oid,)
    ).fetchall()
    db.close()
    return render_template('admin/shipping_slip.html', order=order, items=items,
                           store_name=config.SITE_NAME_AR,
                           store_phone=config.WHATSAPP_NUMBER)


@app.route('/admin/orders/<int:oid>/status', methods=['POST'])
@admin_required
def admin_order_status(oid):
    s    = request.form.get('status', '')
    note = request.form.get('note', '').strip() or None
    tracking = request.form.get('tracking_number', '').strip() or None
    if s in ('new', 'confirmed', 'shipped', 'delayed', 'delivered', 'cancelled', 'returned'):
        db = get_db()
        db.execute('UPDATE orders SET status=? WHERE id=?', (s, oid))
        if tracking:
            db.execute('UPDATE orders SET tracking_number=? WHERE id=?', (tracking, oid))
        db.execute(
            'INSERT INTO order_status_log (order_id, status, note) VALUES (?,?,?)',
            (oid, s, note)
        )
        db.commit()
        webhook_url = _get_integration('n8n_status_webhook')
        if webhook_url:
            payload = _order_payload(oid, db)
            if payload:
                payload['event'] = 'status_changed'
                _fire_webhook(webhook_url, payload)
        if s == 'delivered':
            order_row = db.execute('SELECT phone FROM orders WHERE id=?', (oid,)).fetchone()
            review_url = _get_integration('gbp_review_url')
            if order_row and review_url:
                _send_gbp_review_request(oid, order_row['phone'], review_url)
        db.close()
    return redirect(url_for('admin_order_detail', oid=oid))

# ── Blog ──

@app.route('/admin/blog')
@admin_required
def admin_blog():
    db = get_db()
    posts = db.execute('SELECT * FROM blog_posts ORDER BY created_at DESC').fetchall()
    db.close()
    return render_template('admin/blog.html', posts=posts, active_admin='blog')

def _validate_blog(form):
    errors = {}
    if not form.get('title_en','').strip(): errors['title_en'] = True
    if not form.get('title_ar','').strip(): errors['title_ar'] = True
    return errors

def _blog_vals(form, slug):
    return (slug, form.get('title_en','').strip(), form.get('title_ar','').strip(),
            form.get('content_en','').strip() or None,
            form.get('content_ar','').strip() or None,
            1 if form.get('is_published') else 0)

@app.route('/admin/blog/new', methods=['GET', 'POST'])
@admin_required
def admin_blog_new():
    errors, form = {}, {}
    if request.method == 'POST':
        form = request.form
        errors = _validate_blog(form)
        if not errors:
            slug = form.get('slug','').strip() or slugify(form.get('title_en',''))
            db = get_db()
            db.execute(
                'INSERT INTO blog_posts (slug,title_en,title_ar,content_en,content_ar,is_published) VALUES (?,?,?,?,?,?)',
                _blog_vals(form, slug))
            db.commit(); db.close()
            return redirect(url_for('admin_blog'))
    return render_template('admin/blog_form.html', post=None, errors=errors, form=form, active_admin='blog')

@app.route('/admin/blog/<int:bid>/edit', methods=['GET', 'POST'])
@admin_required
def admin_blog_edit(bid):
    db = get_db()
    post = db.execute('SELECT * FROM blog_posts WHERE id=?', (bid,)).fetchone()
    if not post: db.close(); return redirect(url_for('admin_blog'))
    errors, form = {}, dict(post)
    if request.method == 'POST':
        form = request.form
        errors = _validate_blog(form)
        if not errors:
            slug = form.get('slug','').strip() or slugify(form.get('title_en',''))
            db.execute(
                'UPDATE blog_posts SET slug=?,title_en=?,title_ar=?,content_en=?,content_ar=?,is_published=? WHERE id=?',
                _blog_vals(form, slug) + (bid,))
            db.commit(); db.close()
            return redirect(url_for('admin_blog'))
    db.close()
    return render_template('admin/blog_form.html', post=post, errors=errors, form=form, active_admin='blog')

@app.route('/admin/blog/<int:bid>/delete', methods=['POST'])
@admin_required
def admin_blog_delete(bid):
    db = get_db()
    db.execute('DELETE FROM blog_posts WHERE id=?', (bid,))
    db.commit(); db.close()
    return redirect(url_for('admin_blog'))


# ════════════════════════════════════════
#  SEO
# ════════════════════════════════════════

# ════════════════════════════════════════
#  COLLECTIONS
# ════════════════════════════════════════

@app.route('/admin/customers')
@admin_required
def admin_customers():
    db = get_db()
    # كل الزبائن من الطلبات
    from_orders = db.execute('''
        SELECT phone,
               MAX(customer_name) as name,
               MAX(area)          as area,
               COUNT(*)           as order_count,
               SUM(total)         as total_spent,
               MAX(created_at)    as last_order,
               MAX(status)        as last_status
        FROM orders
        GROUP BY phone
        ORDER BY last_order DESC
    ''').fetchall()

    # الزبائن من notify_me (ممكن ما عندهم طلب)
    notify_phones = db.execute('''
        SELECT phone, name, COUNT(*) as notify_count,
               MAX(created_at) as registered_at,
               GROUP_CONCAT(p.name_ar, ' / ') as products_watching
        FROM stock_notifications sn
        JOIN products p ON p.id = sn.product_id
        WHERE sn.notified = 0
        GROUP BY phone
    ''').fetchall()
    notify_map = {r['phone']: dict(r) for r in notify_phones}

    customers = []
    seen = set()
    for o in from_orders:
        phone = o['phone']
        seen.add(phone)
        customers.append({
            'phone':            phone,
            'name':             o['name'],
            'area':             o['area'],
            'order_count':      o['order_count'],
            'total_spent':      round(o['total_spent'] or 0, 2),
            'last_order':       o['last_order'],
            'last_status':      o['last_status'],
            'notify':           notify_map.get(phone),
            'source':           'order',
        })
    # زبائن من notify فقط (بدون طلبات)
    for r in notify_phones:
        if r['phone'] not in seen:
            customers.append({
                'phone':       r['phone'],
                'name':        r['name'] or '—',
                'area':        '—',
                'order_count': 0,
                'total_spent': 0,
                'last_order':  r['registered_at'],
                'last_status': '—',
                'notify':      dict(r),
                'source':      'notify',
            })

    total_customers = len(customers)
    total_revenue   = sum(c['total_spent'] for c in customers)
    notify_count    = db.execute("SELECT COUNT(*) as c FROM stock_notifications WHERE notified=0").fetchone()['c']
    db.close()
    return render_template('admin/customers.html',
                           customers=customers,
                           total_customers=total_customers,
                           total_revenue=round(total_revenue, 2),
                           notify_count=notify_count,
                           active_admin='customers')


@app.route('/admin/automations', methods=['GET', 'POST'])
@admin_required
def admin_automations():
    db  = get_db()
    msg = None

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'save_webhooks':
            for key in ('n8n_order_webhook', 'n8n_status_webhook',
                        'n8n_notify_webhook', 'n8n_low_stock_webhook', 'webhook_secret'):
                val = request.form.get(key, '').strip()
                db.execute(
                    "INSERT INTO integration_settings (key,value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, val or None)
                )
            db.commit()
            msg = 'saved_webhooks'

        elif action == 'save_builtin':
            for key in ('auto_notify_stock', 'low_stock_threshold',
                        'telegram_token', 'telegram_chat_id'):
                val = request.form.get(key, '').strip()
                db.execute(
                    "INSERT INTO integration_settings (key,value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, val or None)
                )
            db.commit()
            msg = 'saved_builtin'

        elif action == 'test_webhook':
            key = request.form.get('webhook_key', '')
            url = _get_integration(key)
            if url:
                _fire_webhook(url, {'event': 'test', 'source': 'admin', 'webhook_key': key})
                msg = 'test_sent'
            else:
                msg = 'test_no_url'

        elif action == 'clear_logs':
            db.execute("DELETE FROM automation_logs")
            db.commit()
            msg = 'logs_cleared'

    def _g(k):
        r = db.execute("SELECT value FROM integration_settings WHERE key=?", (k,)).fetchone()
        return r['value'] if r else ''

    settings = {k: _g(k) for k in (
        'n8n_order_webhook', 'n8n_status_webhook',
        'n8n_notify_webhook', 'n8n_low_stock_webhook', 'webhook_secret',
        'auto_notify_stock', 'low_stock_threshold',
        'telegram_token', 'telegram_chat_id',
    )}
    logs  = db.execute(
        "SELECT * FROM automation_logs ORDER BY created_at DESC LIMIT 40"
    ).fetchall()
    stats = db.execute(
        "SELECT event_type, status, COUNT(*) as cnt FROM automation_logs GROUP BY event_type, status"
    ).fetchall()
    notify_count = db.execute(
        "SELECT COUNT(*) as c FROM stock_notifications WHERE notified=0"
    ).fetchone()['c']
    db.close()
    return render_template('admin/automations.html',
                           settings=settings, logs=[dict(r) for r in logs],
                           stats=[dict(r) for r in stats],
                           notify_count=notify_count,
                           msg=msg, active_admin='automations')


@app.route('/admin/notify-leads')
@admin_required
def admin_notify_leads():
    db   = get_db()
    rows = db.execute('''
        SELECT sn.id, sn.phone, sn.name, sn.notified, sn.created_at,
               p.name_ar, p.name_en, p.stock_qty
        FROM stock_notifications sn
        JOIN products p ON p.id = sn.product_id
        ORDER BY sn.created_at DESC
    ''').fetchall()
    db.close()
    return render_template('admin/notify_leads.html', leads=[dict(r) for r in rows],
                           active_admin='notify')

@app.route('/admin/notify-leads/<int:lid>/mark-notified', methods=['POST'])
@admin_required
def admin_notify_mark(lid):
    db = get_db()
    db.execute('UPDATE stock_notifications SET notified=1 WHERE id=?', (lid,))
    db.commit()
    db.close()
    return redirect(url_for('admin_notify_leads'))


@app.route('/admin/collections')
@admin_required
def admin_collections():
    db = get_db()
    cols = db.execute("""
        SELECT c.*, COUNT(cp.product_id) as product_count
        FROM collections c
        LEFT JOIN collection_products cp ON cp.collection_id = c.id
        GROUP BY c.id ORDER BY c.sort_order, c.created_at DESC
    """).fetchall()
    db.close()
    return render_template('admin/collections.html', collections=cols, active_admin='collections')


@app.route('/admin/collections/new', methods=['GET', 'POST'])
@admin_required
def admin_collection_new():
    errors, form = {}, {}
    db = get_db()
    all_products = db.execute(
        "SELECT p.*, c.name_ar as cat_ar FROM products p JOIN categories c ON c.id=p.category_id WHERE p.is_active=1 ORDER BY c.sort_order, p.name_ar"
    ).fetchall()
    if request.method == 'POST':
        form = request.form
        if not form.get('name_ar','').strip(): errors['name_ar'] = True
        if not form.get('name_en','').strip(): errors['name_en'] = True
        if not errors:
            slug = form.get('slug','').strip() or slugify(form.get('name_en',''))
            cur = db.cursor()
            cur.execute("""
                INSERT INTO collections (slug, name_ar, name_en, description_ar, description_en, is_active, sort_order)
                VALUES (?,?,?,?,?,?,?)
            """, (slug, form['name_ar'], form['name_en'],
                  form.get('description_ar',''), form.get('description_en',''),
                  1 if form.get('is_active') else 0,
                  int(form.get('sort_order') or 0)))
            cid = cur.lastrowid
            for pid in request.form.getlist('product_ids'):
                cur.execute('INSERT OR IGNORE INTO collection_products (collection_id, product_id) VALUES (?,?)', (cid, int(pid)))
            db.commit(); db.close()
            return redirect(url_for('admin_collections'))
    db.close()
    return render_template('admin/collection_form.html', collection=None, errors=errors,
                           form=form, all_products=all_products, selected_ids=[], active_admin='collections')


@app.route('/admin/collections/<int:cid>/edit', methods=['GET', 'POST'])
@admin_required
def admin_collection_edit(cid):
    db = get_db()
    col = db.execute('SELECT * FROM collections WHERE id=?', (cid,)).fetchone()
    if not col: db.close(); return redirect(url_for('admin_collections'))
    all_products = db.execute(
        "SELECT p.*, c.name_ar as cat_ar FROM products p JOIN categories c ON c.id=p.category_id WHERE p.is_active=1 ORDER BY c.sort_order, p.name_ar"
    ).fetchall()
    selected_ids = [r['product_id'] for r in db.execute(
        'SELECT product_id FROM collection_products WHERE collection_id=?', (cid,)
    ).fetchall()]
    errors, form = {}, dict(col)
    if request.method == 'POST':
        form = request.form
        if not form.get('name_ar','').strip(): errors['name_ar'] = True
        if not form.get('name_en','').strip(): errors['name_en'] = True
        if not errors:
            slug = form.get('slug','').strip() or slugify(form.get('name_en',''))
            db.execute("""
                UPDATE collections SET slug=?,name_ar=?,name_en=?,description_ar=?,
                description_en=?,is_active=?,sort_order=? WHERE id=?
            """, (slug, form['name_ar'], form['name_en'],
                  form.get('description_ar',''), form.get('description_en',''),
                  1 if form.get('is_active') else 0,
                  int(form.get('sort_order') or 0), cid))
            db.execute('DELETE FROM collection_products WHERE collection_id=?', (cid,))
            for pid in request.form.getlist('product_ids'):
                db.execute('INSERT OR IGNORE INTO collection_products (collection_id, product_id) VALUES (?,?)', (cid, int(pid)))
            db.commit(); db.close()
            return redirect(url_for('admin_collections'))
    db.close()
    return render_template('admin/collection_form.html', collection=col, errors=errors,
                           form=form, all_products=all_products,
                           selected_ids=selected_ids, active_admin='collections')


@app.route('/admin/collections/<int:cid>/delete', methods=['POST'])
@admin_required
def admin_collection_delete(cid):
    db = get_db()
    db.execute('DELETE FROM collections WHERE id=?', (cid,))
    db.commit(); db.close()
    return redirect(url_for('admin_collections'))


@app.route('/admin/brands')
@admin_required
def admin_brands():
    db = get_db()
    brands = db.execute(
        '''SELECT b.*, COUNT(p.id) AS product_count
           FROM brands b LEFT JOIN products p ON p.brand_id = b.id
           GROUP BY b.id ORDER BY b.sort_order, b.name_ar'''
    ).fetchall()
    db.close()
    return render_template('admin/brands.html', brands=brands, active_admin='brands')


@app.route('/admin/brands/new', methods=['GET', 'POST'])
@admin_required
def admin_brand_new():
    errors, form = {}, {}
    if request.method == 'POST':
        form = request.form.to_dict()
        name_ar = form.get('name_ar', '').strip()
        name_en = form.get('name_en', '').strip()
        slug    = form.get('slug', '').strip()
        if not name_ar: errors['name_ar'] = True
        if not name_en: errors['name_en'] = True
        if not slug:    slug = name_en.lower().replace(' ', '-')
        if not errors:
            logo_filename = None
            f = request.files.get('logo')
            if f and f.filename:
                import os, uuid
                from PIL import Image as PILImage
                logo_filename = f'{uuid.uuid4().hex}.webp'
                dest_dir = os.path.join(app.static_folder, 'img', 'brands')
                os.makedirs(dest_dir, exist_ok=True)
                img = PILImage.open(f.stream).convert('RGBA')
                img.thumbnail((300, 150), PILImage.LANCZOS)
                img.save(os.path.join(dest_dir, logo_filename), 'WEBP', quality=85)
            db = get_db()
            db.execute(
                'INSERT INTO brands (slug, name_ar, name_en, logo_filename, description_ar, description_en, sort_order, stars, made_in_ar, made_in_en, is_vet, badge_ar, badge_en) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (slug, name_ar, name_en, logo_filename,
                 form.get('description_ar', '').strip() or None,
                 form.get('description_en', '').strip() or None,
                 int(form.get('sort_order', 0) or 0),
                 int(form.get('stars', 0) or 0),
                 form.get('made_in_ar', '').strip() or None,
                 form.get('made_in_en', '').strip() or None,
                 1 if form.get('is_vet') else 0,
                 form.get('badge_ar', '').strip() or None,
                 form.get('badge_en', '').strip() or None)
            )
            db.commit(); db.close()
            return redirect(url_for('admin_brands'))
    return render_template('admin/brand_form.html', brand=None, errors=errors, form=form, active_admin='brands')


@app.route('/admin/brands/<int:bid>/edit', methods=['GET', 'POST'])
@admin_required
def admin_brand_edit(bid):
    db = get_db()
    brand = db.execute('SELECT * FROM brands WHERE id=?', (bid,)).fetchone()
    if not brand:
        db.close()
        return redirect(url_for('admin_brands'))
    errors, form = {}, dict(brand)
    if request.method == 'POST':
        form = request.form.to_dict()
        name_ar = form.get('name_ar', '').strip()
        name_en = form.get('name_en', '').strip()
        slug    = form.get('slug', '').strip()
        if not name_ar: errors['name_ar'] = True
        if not name_en: errors['name_en'] = True
        if not slug:    slug = name_en.lower().replace(' ', '-')
        if not errors:
            logo_filename = brand['logo_filename']
            f = request.files.get('logo')
            if f and f.filename:
                import os, uuid
                from PIL import Image as PILImage
                logo_filename = f'{uuid.uuid4().hex}.webp'
                dest_dir = os.path.join(app.static_folder, 'img', 'brands')
                os.makedirs(dest_dir, exist_ok=True)
                img = PILImage.open(f.stream).convert('RGBA')
                img.thumbnail((300, 150), PILImage.LANCZOS)
                img.save(os.path.join(dest_dir, logo_filename), 'WEBP', quality=85)
            db.execute(
                'UPDATE brands SET slug=?, name_ar=?, name_en=?, logo_filename=?, description_ar=?, description_en=?, sort_order=?, stars=?, made_in_ar=?, made_in_en=?, is_vet=?, badge_ar=?, badge_en=? WHERE id=?',
                (slug, name_ar, name_en, logo_filename,
                 form.get('description_ar', '').strip() or None,
                 form.get('description_en', '').strip() or None,
                 int(form.get('sort_order', 0) or 0),
                 int(form.get('stars', 0) or 0),
                 form.get('made_in_ar', '').strip() or None,
                 form.get('made_in_en', '').strip() or None,
                 1 if form.get('is_vet') else 0,
                 form.get('badge_ar', '').strip() or None,
                 form.get('badge_en', '').strip() or None,
                 bid)
            )
            db.commit(); db.close()
            return redirect(url_for('admin_brands'))
    db.close()
    return render_template('admin/brand_form.html', brand=brand, errors=errors, form=form, active_admin='brands')


@app.route('/admin/brands/<int:bid>/delete', methods=['POST'])
@admin_required
def admin_brand_delete(bid):
    db = get_db()
    db.execute('UPDATE products SET brand_id=NULL WHERE brand_id=?', (bid,))
    db.execute('DELETE FROM brands WHERE id=?', (bid,))
    db.commit(); db.close()
    return redirect(url_for('admin_brands'))


@app.route('/admin/shipping', methods=['GET', 'POST'])
@admin_required
def admin_shipping():
    db = get_db()
    msg = None

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'save_zones':
            zones = db.execute("SELECT id FROM shipping_zones").fetchall()
            for z in zones:
                zid = z['id']
                fee     = request.form.get(f'fee_{zid}', '4').strip()
                enabled = 1 if request.form.get(f'enabled_{zid}') else 0
                try:
                    fee = float(fee)
                except ValueError:
                    fee = 4.0
                name_en = request.form.get(f'name_en_{zid}', '').strip()
                db.execute("UPDATE shipping_zones SET fee=?, enabled=?, name_en=? WHERE id=?", (fee, enabled, name_en, zid))
            # إعدادات الأيام
            for key in ('sub_delivery_days_min', 'sub_delivery_days_max'):
                val = request.form.get(key, '').strip()
                if val:
                    db.execute("INSERT OR REPLACE INTO integration_settings (key,value) VALUES (?,?)", (key, val))
            db.commit()
            msg = 'تم الحفظ ✅'

    zones = _get_zones(db)

    # طلبات معلقة per قضاء
    pending_rows = db.execute(
        "SELECT area, COUNT(*) as cnt FROM orders WHERE status NOT IN ('delivered','cancelled') GROUP BY area"
    ).fetchall()
    pending_map = {r['area']: r['cnt'] for r in pending_rows}

    # إعدادات الأيام
    def _g(k, d):
        r = db.execute("SELECT value FROM integration_settings WHERE key=?", (k,)).fetchone()
        return r['value'] if r else d

    days_min = _g('sub_delivery_days_min', '2')
    days_max = _g('sub_delivery_days_max', '4')

    total_pending = sum(pending_map.values())

    db.close()
    return render_template('admin/shipping.html',
                           active_admin='shipping',
                           zones=zones,
                           pending_map=pending_map,
                           total_pending=total_pending,
                           days_min=days_min,
                           days_max=days_max,
                           msg=msg)


@app.route('/admin/integrations', methods=['GET', 'POST'])
@admin_required
def admin_integrations():
    msg = None
    if request.method == 'POST':
        for key in _PIXEL_KEYS:
            _save_integration(key, request.form.get(key, ''))
        msg = 'saved'
    pixels = _get_pixels()
    return render_template('admin/integrations.html',
                           pixels=pixels, msg=msg,
                           active_admin='integrations')


@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    msg = None
    db = get_db()
    if request.method == 'POST':
        action = request.form.get('action')

        def _save_setting(key, value):
            db.execute(
                "INSERT INTO integration_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value)
            )
            db.commit()

        if action == 'gemini_key':
            key = request.form.get('gemini_key', '').strip()
            _save_setting('gemini_api_key', key)
            import config as _cfg
            _cfg.GEMINI_API_KEY = key
            msg = 'api_key_saved'

        elif action == 'api_key':
            key = request.form.get('api_key', '').strip()
            _save_setting('anthropic_api_key', key)
            import config as _cfg
            _cfg.ANTHROPIC_API_KEY = key
            msg = 'api_key_saved'

        elif action == 'whatsapp':
            number = request.form.get('whatsapp_number', '').strip().lstrip('+')
            _save_setting('whatsapp_number', number)
            import config as _cfg
            _cfg.WHATSAPP_NUMBER = number
            msg = 'whatsapp_saved'

    gemini_ready    = bool(config.GEMINI_API_KEY    or _get_integration('gemini_api_key'))
    anthropic_ready = bool(config.ANTHROPIC_API_KEY or _get_integration('anthropic_api_key'))
    db.close()
    return render_template('admin/settings.html', active_admin='settings', msg=msg,
                           gemini_api_ready=gemini_ready,
                           seo_api_ready=anthropic_ready)


@app.route('/admin/seo')
@admin_required
def admin_seo():
    db = get_db()
    products   = db.execute("""
        SELECT p.id, p.name_ar, p.name_en, p.slug,
               s.meta_title_ar, s.generated_at
        FROM products p
        LEFT JOIN seo_meta s ON s.page_type='product' AND s.page_id=p.id
        WHERE p.is_active=1 ORDER BY p.name_ar
    """).fetchall()
    categories = db.execute("""
        SELECT c.id, c.name_ar, c.name_en, c.slug,
               s.meta_title_ar, s.generated_at
        FROM categories c
        LEFT JOIN seo_meta s ON s.page_type='category' AND s.page_id=c.id
        ORDER BY c.sort_order
    """).fetchall()
    blog_posts = db.execute("""
        SELECT b.id, b.title_ar, b.title_en, b.slug,
               s.meta_title_ar, s.generated_at
        FROM blog_posts b
        LEFT JOIN seo_meta s ON s.page_type='blog' AND s.page_id=b.id
        WHERE b.is_published=1 ORDER BY b.created_at DESC
    """).fetchall()
    home_seo = db.execute(
        "SELECT * FROM seo_meta WHERE page_type='static' AND page_slug='home'"
    ).fetchone()
    db.close()
    return render_template('admin/seo.html',
        products=products, categories=categories,
        blog_posts=blog_posts, home_seo=home_seo,
        active_admin='seo')


@app.route('/admin/seo/generate', methods=['POST'])
@admin_required
def admin_seo_generate():
    page_type = request.form.get('page_type')
    page_id   = request.form.get('page_id', type=int)
    page_slug = request.form.get('page_slug')

    if not config.ANTHROPIC_API_KEY:
        return jsonify({'ok': False, 'error': 'ANTHROPIC_API_KEY غير موجود في config.py'})

    db = get_db()
    result = None

    if page_type == 'product' and page_id:
        row = db.execute(
            'SELECT p.*, c.name_ar as cat_ar FROM products p JOIN categories c ON c.id=p.category_id WHERE p.id=?',
            (page_id,)
        ).fetchone()
        if row:
            result = seo_mod.generate_product_seo(dict(row), row['cat_ar'])

    elif page_type == 'category' and page_id:
        row = db.execute('SELECT * FROM categories WHERE id=?', (page_id,)).fetchone()
        if row:
            result = seo_mod.generate_category_seo(dict(row))

    elif page_type == 'blog' and page_id:
        row = db.execute('SELECT * FROM blog_posts WHERE id=?', (page_id,)).fetchone()
        if row:
            result = seo_mod.generate_blog_seo(dict(row))

    elif page_type == 'static' and page_slug:
        result = seo_mod.generate_static_seo(page_slug)

    if not result:
        db.close()
        return jsonify({'ok': False, 'error': 'فشل التوليد — تحقق من API key'})

    # احفظ أو حدّث
    if page_id:
        db.execute('''
            INSERT INTO seo_meta
              (page_type, page_id, meta_title_ar, meta_title_en,
               meta_desc_ar, meta_desc_en, keywords_ar, keywords_en,
               og_title, og_description, generated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now'))
            ON CONFLICT(page_type, page_id) DO UPDATE SET
              meta_title_ar=excluded.meta_title_ar,
              meta_title_en=excluded.meta_title_en,
              meta_desc_ar=excluded.meta_desc_ar,
              meta_desc_en=excluded.meta_desc_en,
              keywords_ar=excluded.keywords_ar,
              keywords_en=excluded.keywords_en,
              og_title=excluded.og_title,
              og_description=excluded.og_description,
              generated_at=excluded.generated_at
        ''', (page_type, page_id,
              result.get('meta_title_ar'), result.get('meta_title_en'),
              result.get('meta_desc_ar'),  result.get('meta_desc_en'),
              result.get('keywords_ar'),   result.get('keywords_en'),
              result.get('og_title'),      result.get('og_description')))
    else:
        db.execute('''
            INSERT INTO seo_meta
              (page_type, page_slug, meta_title_ar, meta_title_en,
               meta_desc_ar, meta_desc_en, keywords_ar, keywords_en,
               og_title, og_description, generated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now'))
            ON CONFLICT(page_type, page_slug) DO UPDATE SET
              meta_title_ar=excluded.meta_title_ar,
              meta_title_en=excluded.meta_title_en,
              meta_desc_ar=excluded.meta_desc_ar,
              meta_desc_en=excluded.meta_desc_en,
              keywords_ar=excluded.keywords_ar,
              keywords_en=excluded.keywords_en,
              og_title=excluded.og_title,
              og_description=excluded.og_description,
              generated_at=excluded.generated_at
        ''', (page_type, page_slug,
              result.get('meta_title_ar'), result.get('meta_title_en'),
              result.get('meta_desc_ar'),  result.get('meta_desc_en'),
              result.get('keywords_ar'),   result.get('keywords_en'),
              result.get('og_title'),      result.get('og_description')))

    db.commit()
    db.close()
    return jsonify({'ok': True, 'data': result})


@app.route('/admin/seo/generate-all', methods=['POST'])
@admin_required
def admin_seo_generate_all():
    """يولّد SEO لكل المنتجات دفعة واحدة (بطيء — يستغرق وقتاً)."""
    if not config.ANTHROPIC_API_KEY:
        return jsonify({'ok': False, 'error': 'ANTHROPIC_API_KEY غير موجود'})

    db = get_db()
    products = db.execute(
        'SELECT p.*, c.name_ar as cat_ar FROM products p JOIN categories c ON c.id=p.category_id WHERE p.is_active=1'
    ).fetchall()

    done = 0
    for row in products:
        result = seo_mod.generate_product_seo(dict(row), row['cat_ar'])
        if result:
            db.execute('''
                INSERT INTO seo_meta
                  (page_type, page_id, meta_title_ar, meta_title_en,
                   meta_desc_ar, meta_desc_en, keywords_ar, keywords_en,
                   og_title, og_description, generated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now'))
                ON CONFLICT(page_type, page_id) DO UPDATE SET
                  meta_title_ar=excluded.meta_title_ar, meta_title_en=excluded.meta_title_en,
                  meta_desc_ar=excluded.meta_desc_ar, meta_desc_en=excluded.meta_desc_en,
                  keywords_ar=excluded.keywords_ar, keywords_en=excluded.keywords_en,
                  og_title=excluded.og_title, og_description=excluded.og_description,
                  generated_at=excluded.generated_at
            ''', ('product', row['id'],
                  result.get('meta_title_ar'), result.get('meta_title_en'),
                  result.get('meta_desc_ar'),  result.get('meta_desc_en'),
                  result.get('keywords_ar'),   result.get('keywords_en'),
                  result.get('og_title'),      result.get('og_description')))
            done += 1

    db.commit()
    db.close()
    return jsonify({'ok': True, 'done': done})


def _build_merchant_feed(lang):
    db = get_db()
    products = db.execute("""
        SELECT p.slug, p.name_ar, p.name_en, p.brand,
               p.price, p.discount_price, p.stock_qty,
               p.description_ar, p.description_en,
               (SELECT filename FROM product_images
                WHERE product_id=p.id ORDER BY sort_order,id LIMIT 1) as img
        FROM products p WHERE p.is_active=1
    """).fetchall()
    db.close()

    base  = request.host_url.rstrip('/')
    is_ar = (lang == 'ar')

    def esc(s):
        return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    site_name = config.SITE_NAME_AR if is_ar else config.SITE_NAME_EN
    ch_desc   = 'متجر بيلا لجميع مستلزمات وحيوانات أليفة' if is_ar else 'Bella Pet Store — pet supplies for your beloved animals'

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss xmlns:g="http://base.google.com/ns/1.0" version="2.0">',
        '<channel>',
        f'<title>{esc(site_name)}</title>',
        f'<link>{base}</link>',
        f'<description>{ch_desc}</description>',
    ]

    for p in products:
        price    = p['discount_price'] or p['price']
        avail    = 'in_stock' if (p['stock_qty'] or 0) > 0 else 'out_of_stock'
        name_p   = (p['name_ar'] if is_ar else p['name_en']) or (p['name_en'] if is_ar else p['name_ar']) or ''
        desc_p   = (p['description_ar'] if is_ar else p['description_en']) or (p['description_en'] if is_ar else p['description_ar']) or name_p
        name     = esc(name_p)
        desc     = esc(desc_p)
        img_url  = f"{base}/static/img/products/{p['img']}" if p['img'] else ''
        prod_url = f"{base}/products/{p['slug']}?lang={lang}"
        brand    = esc(p['brand'] or (config.SITE_NAME_AR if is_ar else config.SITE_NAME_EN))

        lines += [
            '<item>',
            f'  <g:id>{p["slug"]}</g:id>',
            f'  <g:title>{name}</g:title>',
            f'  <g:description>{desc[:5000]}</g:description>',
            f'  <g:link>{prod_url}</g:link>',
            f'  <g:image_link>{img_url}</g:image_link>' if img_url else '',
            f'  <g:condition>new</g:condition>',
            f'  <g:availability>{avail}</g:availability>',
            f'  <g:price>{price:.2f} USD</g:price>',
            f'  <g:brand>{brand}</g:brand>',
            '  <g:google_product_category>Animals &amp; Pet Supplies &gt; Pet Supplies</g:google_product_category>',
            f'  <g:identifier_exists>no</g:identifier_exists>',
            '</item>',
        ]

    lines += ['</channel>', '</rss>']
    return Response('\n'.join(l for l in lines if l), mimetype='application/xml')


@app.route('/feed-en.xml')
def merchant_feed_en():
    return _build_merchant_feed('en')


@app.route('/feed-ar.xml')
def merchant_feed_ar():
    return _build_merchant_feed('ar')


@app.route('/feed.xml')
def merchant_feed():
    return redirect('/feed-en.xml', code=301)


@app.route('/sitemap.xml')
def sitemap():
    db = get_db()
    products   = db.execute('SELECT slug FROM products WHERE is_active=1').fetchall()
    categories = db.execute('SELECT slug FROM categories').fetchall()
    blog_posts = db.execute('SELECT slug FROM blog_posts WHERE is_published=1').fetchall()
    db.close()

    base = request.host_url.rstrip('/')
    urls = [
        {'loc': base + '/',                        'priority': '1.0', 'freq': 'daily'},
        {'loc': base + url_for('blog'),            'priority': '0.7', 'freq': 'weekly'},
        {'loc': base + url_for('shipping_info'),   'priority': '0.6', 'freq': 'monthly'},
        {'loc': base + url_for('returns'),         'priority': '0.6', 'freq': 'monthly'},
    ]
    for c in categories:
        urls.append({'loc': base + url_for('category', slug=c['slug']), 'priority': '0.8', 'freq': 'weekly'})
    for p in products:
        urls.append({'loc': base + url_for('product', slug=p['slug']), 'priority': '0.9', 'freq': 'weekly'})
    for b in blog_posts:
        urls.append({'loc': base + '/blog/' + b['slug'], 'priority': '0.6', 'freq': 'monthly'})

    xml = render_template('sitemap.xml', urls=urls)
    return Response(xml, mimetype='application/xml')


@app.route('/googlea9170893005bbdb7.html')
def google_verify():
    return Response('google-site-verification: googlea9170893005bbdb7.html', mimetype='text/html')


@app.route('/robots.txt')
def robots():
    base = request.host_url.rstrip('/')
    txt = (
        f"User-agent: *\n"
        f"Allow: /\n"
        f"Disallow: /admin/\n"
        f"Disallow: /cart\n"
        f"Disallow: /checkout\n"
        f"Disallow: /my-orders\n"
        f"Disallow: /search\n"
        f"Disallow: /order-confirm\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return Response(txt, mimetype='text/plain')


@app.route("/product/<path:slug>")
def product(slug):
    db = get_db()
    p = db.execute(
        """SELECT p.*,
                  c.name_en as cat_name_en, c.name_ar as cat_name_ar, c.slug as cat_slug,
                  s.name_en as sub_name_en, s.name_ar as sub_name_ar, s.slug as sub_slug
           FROM products p
           JOIN categories c ON p.category_id = c.id
           LEFT JOIN subcategories s ON p.subcategory_id = s.id
           WHERE p.slug = ? OR p.slug_ar = ?""",
        (slug, slug),
    ).fetchone()
    if not p:
        db.close()
        return render_template('404.html'), 404
    # إذا وصل عبر slug_ar → فرض اللغة العربية
    if p['slug_ar'] and slug == p['slug_ar']:
        session['lang'] = 'ar'
    if not p['is_active']:
        alternatives = db.execute(
            """SELECT p.*,
                      (SELECT filename FROM product_images
                       WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS primary_img
               FROM products p
               WHERE p.category_id = ? AND p.is_active = 1
               ORDER BY p.is_featured DESC, p.created_at DESC LIMIT 6""",
            (p['category_id'],)
        ).fetchall()
        db.close()
        return render_template('product_discontinued.html', p=p, alternatives=alternatives), 410

    images = db.execute(
        "SELECT * FROM product_images WHERE product_id = ? ORDER BY sort_order",
        (p["id"],),
    ).fetchall()

    related = db.execute(
        """SELECT p.*,
                  (SELECT filename FROM product_images
                   WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS primary_img
           FROM products p
           WHERE p.category_id = ? AND p.id != ? AND p.is_active = 1
           ORDER BY p.is_featured DESC, p.created_at DESC LIMIT 4""",
        (p["category_id"], p["id"]),
    ).fetchall()

    seo = db.execute(
        'SELECT * FROM seo_meta WHERE page_type=? AND page_id=?', ('product', p['id'])
    ).fetchone()
    variants = db.execute(
        'SELECT * FROM product_variants WHERE product_id=? AND is_active=1 ORDER BY sort_order',
        (p['id'],)
    ).fetchall()
    price_tiers = db.execute(
        'SELECT * FROM product_price_tiers WHERE product_id=? ORDER BY min_qty',
        (p['id'],)
    ).fetchall()
    cust_rating = db.execute(
        'SELECT ROUND(AVG(rating),1) as avg, COUNT(*) as cnt FROM product_reviews WHERE product_id=?',
        (p['id'],)
    ).fetchone()
    specs = db.execute(
        'SELECT * FROM product_specs WHERE product_id=? ORDER BY sort_order, id',
        (p['id'],)
    ).fetchall()
    sub_cfg    = _sub_settings(db)
    zones      = _get_zones(db)
    zones_fees = _zones_fees(db)   # {name_ar: fee} enabled only
    db.close()
    return render_template(
        "product.html",
        p=p,
        images=images,
        related=related,
        variants=variants,
        price_tiers=price_tiers,
        seo_data=seo,
        cust_rating=cust_rating,
        specs=specs,
        sub_cfg=sub_cfg,
        shipping_zones=zones,
        zones_fees=zones_fees,
        active_tab="categories",
    )


@app.route("/category/<slug>")
def category(slug):
    db = get_db()
    cat = db.execute("SELECT * FROM categories WHERE slug = ?", (slug,)).fetchone()
    if not cat:
        db.close()
        return redirect(url_for("index"))

    subcategories = db.execute(
        "SELECT * FROM subcategories WHERE category_id = ? ORDER BY sort_order",
        (cat["id"],),
    ).fetchall()

    active_sub   = request.args.get("sub")
    active_brand = request.args.get("brand")
    price_min    = request.args.get("min", type=float)
    price_max    = request.args.get("max", type=float)
    sections     = []
    brand_tiles  = []
    active_brand_row = None

    if active_sub:
        sub_row = db.execute(
            "SELECT id FROM subcategories WHERE category_id = ? AND slug = ?",
            (cat["id"], active_sub),
        ).fetchone()
        if sub_row:
            # تحقق إذا في ماركات مربوطة بمنتجات هذا القسم الفرعي
            brand_tiles = db.execute(
                """SELECT b.*, COUNT(p.id) AS product_count
                   FROM brands b
                   JOIN products p ON p.brand_id = b.id
                   WHERE p.category_id = ? AND p.subcategory_id = ? AND p.is_active = 1
                   GROUP BY b.id ORDER BY b.sort_order, b.name_ar""",
                (cat["id"], sub_row["id"])
            ).fetchall()

            if active_brand and brand_tiles:
                active_brand_row = db.execute(
                    'SELECT * FROM brands WHERE slug=?', (active_brand,)
                ).fetchone()

            # بناء query المنتجات مع فلاتر اختيارية
            q  = """SELECT p.*,
                           (SELECT filename FROM product_images
                            WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS primary_img
                    FROM products p
                    WHERE p.category_id=? AND p.subcategory_id=? AND p.is_active=1"""
            params = [cat["id"], sub_row["id"]]
            if active_brand_row:
                q += " AND p.brand_id=?"
                params.append(active_brand_row["id"])
            if price_min is not None:
                q += " AND COALESCE(p.discount_price, p.price) >= ?"
                params.append(price_min)
            if price_max is not None:
                q += " AND COALESCE(p.discount_price, p.price) <= ?"
                params.append(price_max)
            q += " ORDER BY p.is_featured DESC, p.created_at DESC"
            products = db.execute(q, params).fetchall()

            # سعر min/max لكل المنتجات في هذا القسم (لشريط الأسعار)
            price_range = db.execute(
                """SELECT MIN(COALESCE(discount_price, price)) AS mn,
                          MAX(COALESCE(discount_price, price)) AS mx
                   FROM products WHERE category_id=? AND subcategory_id=? AND is_active=1""",
                (cat["id"], sub_row["id"])
            ).fetchone()
        else:
            active_sub = None
            price_range = None
            products = db.execute(
                """SELECT p.*,
                          (SELECT filename FROM product_images
                           WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS primary_img
                   FROM products p
                   WHERE p.category_id = ? AND p.is_active = 1
                   ORDER BY p.is_featured DESC, p.created_at DESC""",
                (cat["id"],),
            ).fetchall()
    else:
        price_range = None
        products = db.execute(
            """SELECT p.*,
                      (SELECT filename FROM product_images
                       WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS primary_img
               FROM products p
               WHERE p.category_id = ? AND p.is_active = 1
               ORDER BY p.is_featured DESC, p.created_at DESC""",
            (cat["id"],),
        ).fetchall()
        # query واحدة لكل منتجات الـ category مع أول صورة لكل منتج
        all_hub_prods = db.execute(
            """SELECT p.*,
                      (SELECT filename FROM product_images
                       WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS primary_img
               FROM products p
               WHERE p.category_id=? AND p.is_active=1
               ORDER BY p.is_featured DESC, p.created_at DESC""",
            (cat["id"],)
        ).fetchall()
        # نجمّع المنتجات حسب subcategory_id بالـ Python
        from collections import defaultdict as _dd
        _by_sub = _dd(list)
        for _p in all_hub_prods:
            if _p["subcategory_id"]:
                _by_sub[_p["subcategory_id"]].append(_p)
        for sub in subcategories:
            sections.append({"sub": dict(sub), "products": _by_sub[sub["id"]][:6]})

    seo = db.execute(
        'SELECT * FROM seo_meta WHERE page_type=? AND page_id=?', ('category', cat['id'])
    ).fetchone()
    # og:image: صورة الكاتيجوري المرفوعة أولاً، ثم أول صورة منتج
    cat_card_img = db.execute(
        "SELECT filename FROM category_card_images WHERE category_slug=?", (cat['slug'],)
    ).fetchone()
    if cat_card_img:
        category_image = 'categories/' + cat_card_img['filename']
    else:
        first_img = db.execute(
            """SELECT pi.filename FROM product_images pi
               JOIN products p ON p.id = pi.product_id
               WHERE p.category_id = ? AND p.is_active = 1
               ORDER BY p.is_featured DESC, pi.sort_order LIMIT 1""",
            (cat['id'],)
        ).fetchone()
        category_image = ('products/' + first_img['filename']) if first_img else None
    db.close()
    return render_template(
        "category.html",
        category=cat,
        subcategories=subcategories,
        products=products,
        sections=sections,
        active_sub=active_sub,
        brand_tiles=brand_tiles,
        active_brand=active_brand,
        active_brand_row=active_brand_row,
        price_range=price_range,
        price_min=price_min,
        price_max=price_max,
        seo_data=seo,
        category_image=category_image,
        active_tab="categories",
    )


@app.route("/wishlist")
def wishlist():
    return render_template("wishlist.html", active_tab="wishlist")


@app.route("/my-orders", methods=["GET", "POST"])
def my_orders():
    orders = []
    phone = ""
    searched = False
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        searched = True
        phone_digits = ''.join(c for c in phone if c.isdigit())
        if phone and 7 <= len(phone_digits) <= 15:
            db = get_db()
            orders_raw = db.execute(
                """SELECT * FROM orders WHERE phone=? ORDER BY created_at DESC LIMIT 10""",
                (phone,)
            ).fetchall()
            orders = []
            for o in orders_raw:
                items = db.execute(
                    """SELECT oi.*, p.name_en, p.name_ar FROM order_items oi
                       JOIN products p ON p.id = oi.product_id
                       WHERE oi.order_id = ?""",
                    (o["id"],)
                ).fetchall()
                log = db.execute(
                    'SELECT * FROM order_status_log WHERE order_id=? ORDER BY created_at',
                    (o["id"],)
                ).fetchall()
                orders.append({"order": o, "items": items, "log": log})
            db.close()
    return render_template("my_orders.html", orders=orders, phone=phone,
                           searched=searched, active_tab="orders")


@app.route("/sw.js")
def service_worker():
    with open(os.path.join(os.path.dirname(__file__), 'static', 'sw.js'), encoding='utf-8') as _f:
        _sw_content = _f.read()
    resp = Response(_sw_content, mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route("/")
def index():
    db = get_db()
    categories = db.execute(
        "SELECT * FROM categories ORDER BY sort_order"
    ).fetchall()

    # category card images
    cat_imgs_rows = db.execute("SELECT category_slug, filename FROM category_card_images").fetchall()
    cat_images = {r['category_slug']: r['filename'] for r in cat_imgs_rows}

    # homepage sections (order + visibility)
    sections_rows = db.execute(
        "SELECT section_id, is_visible FROM homepage_sections ORDER BY sort_order"
    ).fetchall()
    sections = [dict(s) for s in sections_rows] if sections_rows else [
        {'section_id': s, 'is_visible': 1} for s in ['offers', 'featured', 'blog', 'why']
    ]

    offers = db.execute(
        """SELECT p.*,
                  (SELECT filename FROM product_images
                   WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS primary_img
           FROM products p
           WHERE p.is_active = 1
             AND (p.discount_price IS NOT NULL
                  OR p.promo_label_ar IS NOT NULL
                  OR p.promo_type IS NOT NULL)
           ORDER BY p.discount_price IS NOT NULL DESC, p.created_at DESC LIMIT 8"""
    ).fetchall()

    featured = db.execute(
        """SELECT p.*,
                  (SELECT filename FROM product_images
                   WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS primary_img
           FROM products p
           WHERE p.is_active = 1 AND p.is_featured = 1
           ORDER BY p.created_at DESC LIMIT 8"""
    ).fetchall()

    blog_posts = db.execute(
        """SELECT * FROM blog_posts
           WHERE is_published = 1
           ORDER BY created_at DESC LIMIT 4"""
    ).fetchall()

    seo = db.execute(
        "SELECT * FROM seo_meta WHERE page_type='static' AND page_slug='home'"
    ).fetchone()
    db.close()
    return render_template(
        "index.html",
        categories=categories,
        cat_images=cat_images,
        sections=sections,
        offers=offers,
        featured=featured,
        blog_posts=blog_posts,
        seo_data=seo,
        active_tab="home",
    )


# ══════════════════════════════════════════════════════════════
#  REST API  —  /api/v1/...
#  كل endpoint يحتاج:  X-API-Key: <key>  أو  ?api_key=<key>
# ══════════════════════════════════════════════════════════════

def _api_err(msg, code=401):
    return jsonify({'error': msg}), code

@app.route('/api/v1/diet-planner', methods=['POST'])
def api_diet_planner():
    if not _api_auth():
        return _api_err('unauthorized')
    # ── Breed knowledge base (from PDF) — structured tips, no AI needed ──
    _BREED_INFO = {
        # cats
        'scottish': {
            'ar': 'سكوتش فولد',
            'en': 'Scottish Fold',
            'notes_ar': 'يعاني سكوتش فولد من طفرة وراثية في الغضاريف (FOCD) تسبب آلام المفاصل والعمود الفقري — غالبًا يخفي ألمه. احرص على صندوق فضالت منخفض الحواف، طعام وماء على مستوى الأرض، وسرير طبي ناعم. تجنّب ثني أذنيه أو الضغط على جسده.',
            'notes_en': 'Scottish Fold suffers from a genetic cartilage mutation (FOCD) causing joint and spine pain — often hides pain. Use low-entry litter box, keep food/water at floor level, provide orthopedic bedding. Never bend ears or press on body.',
        },
        'persian': {
            'ar': 'فارسي / شيرازي',
            'en': 'Persian / Shirazi',
            'notes_ar': 'الفارسي هادئ وقليل الحركة ويميل للملل — يحتاج إثراءً بيئيًا ذهنيًا. حساس جدًا للتوتر ويعبّر عنه بتساقط الشعر. طعامه الرطب مهم لمنع مشاكل الكلى. وجهه المفلطح يسبب أحيانًا صعوبة في التنفس والأكل.',
            'notes_en': 'Persian is calm and low-energy, prone to boredom — needs mental enrichment. Highly stress-sensitive, shows it through hair loss. Wet food important for kidney health. Flat face can cause breathing and eating difficulties.',
        },
        'siamese': {
            'ar': 'سيامي',
            'en': 'Siamese',
            'notes_ar': 'السيامي اجتماعي جدًا ويعاني من قلق الانفصال الشديد — يحتاج تفاعلًا يوميًا مستمرًا. يعبّر عن توتره بالمواء المفرط وتساقط الشعر من اللعق القهري. مناسب للبيوت ذات الحضور الدائم.',
            'notes_en': 'Siamese is highly social and suffers from severe separation anxiety — needs constant daily interaction. Expresses stress through excessive meowing and compulsive licking/hair loss. Best for households with frequent human presence.',
        },
        'british': {
            'ar': 'بريتيش شورت هير',
            'en': 'British Shorthair',
            'notes_ar': 'البريتيش مستقل وكسول بطبعه ويستخدم الأكل كترفيه — معرّض للسمنة الشديدة. يرفض الحمل والاحتضان القسري. استخدم ألغاز الطعام بدل الطبق العادي وجدوله جلسات لعب يومية لا تقل عن 30 دقيقة.',
            'notes_en': 'British Shorthair is independent and lazy by nature, uses food as entertainment — prone to severe obesity. Resists being held or cuddled. Use food puzzles instead of bowls and schedule at least 30 min of daily play.',
        },
        'bengal': {
            'ar': 'بنغال',
            'en': 'Bengal',
            'notes_ar': 'البنغال أعلى طاقةً من أي سلالة قطط أخرى ويتحول للتخريب عند غياب التحفيز. يحتاج ألعابًا تفاعلية تحاكي الصيد وأشجار قطط مرتفعة. يميل للعدوانية الموجهة نحو الملل.',
            'notes_en': 'Bengal has the highest energy of any cat breed and turns destructive without stimulation. Needs interactive hunting-style toys and tall cat trees. Prone to frustration-based aggression.',
        },
        'maincoon': {
            'ar': 'مين كون',
            'en': 'Maine Coon',
            'notes_ar': 'مين كون ضخم الحجم ويعاني من تساقط الشعر الكثيف وكرات الشعر المتكررة — يحتاج تمشيطًا يوميًا وأكلًا رطبًا لتسهيل الهضم. اجتماعي ومحب للمياه.',
            'notes_en': 'Maine Coon is large and suffers from heavy shedding and frequent hairballs — needs daily brushing and wet food to aid digestion. Social and unusually fond of water.',
        },
        'ragdoll': {
            'ar': 'راغدول',
            'en': 'Ragdoll',
            'notes_ar': 'الراغدول هادئ ومحب للاحتضان لكن فراؤه الطويل يحتاج عناية يومية. يميل للسمنة لقلة نشاطه. حساس للتغيير المفاجئ في البيئة أو الروتين.',
            'notes_en': 'Ragdoll is calm and loves cuddling but its long fur requires daily grooming. Prone to obesity due to low activity. Sensitive to sudden environmental or routine changes.',
        },
        # dogs
        'german': {
            'ar': 'جيرمن شيبرد',
            'en': 'German Shepherd',
            'notes_ar': 'الجيرمن شيبرد يحتاج شغلًا ذهنيًا وبدنيًا مكثفًا — بدونه يتحول للتخريب وعض الأثاث. غريزة السيطرة قوية فيه، معرّض لحراسة الموارد والعدوانية. يحتاج ساعة تمرين يومية على الأقل ونشاطًا ذهنيًا كتمارين الطاعة.',
            'notes_en': 'German Shepherd needs intense mental and physical work — without it turns destructive and chews furniture. Strong controlling instinct, prone to resource guarding and aggression. Needs at least 1 hour daily exercise plus mental activity like obedience training.',
        },
        'husky': {
            'ar': 'هاسكي',
            'en': 'Husky',
            'notes_ar': 'الهاسكي طاقته لا تنتهي — يحفر، يهرب، ويدمر عند الملل. يحتاج جريًا يوميًا مكثفًا وحديقة محاطة بسياج عالٍ. شديد الاستقلالية ولا يناسب أصحاب المنازل الصغيرة.',
            'notes_en': 'Husky has boundless energy — digs, escapes, and destroys when bored. Needs intensive daily running and a high-fenced yard. Highly independent, not suitable for small apartment living.',
        },
        'golden': {
            'ar': 'مسترد ذهبي',
            'en': 'Golden Retriever',
            'notes_ar': 'المسترد الذهبي ودود جدًا لدرجة القفز على الضيوف وسحب المقود بعنف. يميل لمص الأشياء وعضها. يحتاج تدريبًا مبكرًا على "اجلس" قبل الترحيب. عرضة لزيادة الوزن عند التقدم في العمر.',
            'notes_en': 'Golden Retriever is so friendly it jumps on guests and pulls the leash hard. Tends to mouth and chew objects. Needs early training on "sit" before greeting. Prone to weight gain with age.',
        },
        'labrador': {
            'ar': 'لابرادور',
            'en': 'Labrador',
            'notes_ar': 'اللابرادور مولع بالأكل بشكل مرضي ومعرّض جدًا للسمنة — يجب تحديد الكميات بدقة. يقفز على الناس ويسحب المقود بقوة. نشيط ويحتاج تمرينًا يوميًا منتظمًا وألعاب استرداد.',
            'notes_en': 'Labrador has an obsessive relationship with food and is highly prone to obesity — portions must be strictly controlled. Jumps on people and pulls leash hard. Active, needs daily exercise and retrieval games.',
        },
        'poodle': {
            'ar': 'بودل',
            'en': 'Poodle',
            'notes_ar': 'البودل ذكي جدًا ويميل للقلق والتعلق الشديد بصاحبه. يحتاج تحفيزًا ذهنيًا كالألغاز والتدريب المتقدم. شعره يحتاج حلاقة منتظمة. حساس للضوضاء والتغييرات المفاجئة.',
            'notes_en': 'Poodle is highly intelligent and prone to anxiety and strong owner attachment. Needs mental stimulation like puzzles and advanced training. Coat requires regular grooming. Sensitive to noise and sudden changes.',
        },
        'maltese': {
            'ar': 'مالتيز',
            'en': 'Maltese',
            'notes_ar': 'المالتيز صغير وحساس جدًا — يعاني من قلق الانفصال والنباح المفرط. يحتاج تعاملًا لطيفًا وتدريجيًا على الاستقلالية. شعره الطويل الأبيض يحتاج عناية يومية لمنع التشابك.',
            'notes_en': 'Maltese is small and highly sensitive — suffers from separation anxiety and excessive barking. Needs gentle gradual independence training. Long white coat requires daily care to prevent matting.',
        },
        'chihuahua': {
            'ar': 'شيواوا',
            'en': 'Chihuahua',
            'notes_ar': 'الشيواوا صغير لكن شخصيته كبيرة — يعاني من عدوانية الخوف والحساسية الشديدة للأصوات. يرتجف عند البرد أو التوتر ويحتاج ملابس دافئة شتاءً. لا تتركه يتسلط لصغر حجمه.',
            'notes_en': 'Chihuahua is small but big-tempered — suffers from fear aggression and extreme noise sensitivity. Shivers in cold or stress and needs warm clothing in winter. Don\'t let it develop "small dog syndrome" just because of its size.',
        },
        'shitzu': {
            'ar': 'شيتزو',
            'en': 'Shih Tzu',
            'notes_ar': 'الشيتزو وجهه المفلطح يسبب صعوبة في التنفس — تجنب الحرارة الشديدة والتمرين المكثف. عيناه بارزة تحتاج تنظيفًا يوميًا. شعره يحتاج تمشيطًا مستمرًا لمنع التشابك.',
            'notes_en': 'Shih Tzu\'s flat face causes breathing difficulties — avoid heat and intense exercise. Prominent eyes need daily cleaning. Coat requires constant brushing to prevent matting.',
        },
        'bulldog': {
            'ar': 'بولدوغ / باغ',
            'en': 'Bulldog / Pug',
            'notes_ar': 'البولدوغ والباغ يعانيان من مشاكل تنفسية (BOAS) — ممنوع التمرين في الحر أو التعريض للشمس. معرضان للسمنة الشديدة وتراكم الطيات الجلدية التي تحتاج تنظيفًا أسبوعيًا. أكل بارد ومناخ مكيف ضروريان.',
            'notes_en': 'Bulldog and Pug suffer from breathing issues (BOAS) — no exercise in heat or sun exposure. Highly prone to obesity and skin fold infections needing weekly cleaning. Cool food and air-conditioned environment are essential.',
        },
        # birds
        'african_grey': {
            'ar': 'أفريكان غري (كاسكو)',
            'en': 'African Grey',
            'notes_ar': 'الأفريكان غري من أذكى الطيور وأكثرها حساسيةً للوحدة — ينتف ريشه عند الملل أو الإهمال. يعاني من نقص الكالسيوم الحاد عند تغذيته على البذور فقط. يحتاج ألعاب تفكيك يومية وتفاعلًا مكثفًا.',
            'notes_en': 'African Grey is among the most intelligent and loneliness-sensitive birds — plucks feathers when bored or neglected. Suffers severe calcium deficiency on seed-only diet. Needs daily destructible toys and intensive interaction.',
        },
        'cockatiel': {
            'ar': 'كوكاتيل',
            'en': 'Cockatiel',
            'notes_ar': 'الكوكاتيل حساس جدًا للظلال الليلية ويصطدم بجدران القفص (Night Frights) — يحتاج إضاءة ليلية خافتة. خصلة رأسه تعبّر عن حالته: مفرودة=هادئ، مسطحة=خائف، رأسية=غاضب. الإناث معرضة لاحتباس البيض.',
            'notes_en': 'Cockatiel is very sensitive to night shadows and crashes into cage walls (Night Frights) — needs dim night light. Head crest expresses mood: fanned=calm, flat=scared, erect=angry. Females prone to egg binding.',
        },
        'lovebird': {
            'ar': 'طيور الحب',
            'en': 'Lovebird',
            'notes_ar': 'طيور الحب اجتماعية جدًا وتحتاج رفيقًا من نوعها وإلا تعاني من القلق والعزلة. عدوانية هرمونية موسمية طبيعية. تحتاج قفصًا واسعًا مع ألعاب متنوعة وخشب للعض.',
            'notes_en': 'Lovebirds are extremely social and need a companion of the same species or suffer anxiety and isolation. Seasonal hormonal aggression is normal. Need spacious cage with varied toys and chewing wood.',
        },
        'budgie': {
            'ar': 'بادجيريغار (بودجي)',
            'en': 'Budgerigar',
            'notes_ar': 'البودجي ممكن يصاب بعدوانية هرمونية مفاجئة ومحاولات تزاوج مع الألعاب وإنتاج مفرط للبيض — قلل ساعات الإضاءة لـ٨-١٠ ساعات فقط وأزل الأماكن المظلمة التي يعتبرها أعشاشًا.',
            'notes_en': 'Budgie can suffer sudden hormonal aggression, toy-mating attempts, and excessive egg-laying — reduce light exposure to 8-10 hours only and remove dark hideaways it treats as nests.',
        },
        'canary': {
            'ar': 'كناري',
            'en': 'Canary',
            'notes_ar': 'الكناري يصمت فجأة ويفقد شهيته عند التوتر — عادةً بسبب قربه من طيور كبيرة أو قطط. يحتاج قفص طيران أفقي واسع بعيدًا عن حركة البيت. المرآة الصغيرة تساعده على الاسترخاء.',
            'notes_en': 'Canary suddenly goes silent and loses appetite when stressed — usually from proximity to large birds or cats. Needs wide horizontal flight cage away from household traffic. Small mirror helps it relax.',
        },
        'quaker': {
            'ar': 'كويكر',
            'en': 'Quaker Parrot',
            'notes_ar': 'الكويكر يمزق ريش أجنحته عند عدم الاستقرار البيئي. يحتاج بيئة ثابتة وروتينًا منتظمًا. اجتماعي ويحب بناء الأعشاش — وفّر له مواد آمنة لهذا الغرض.',
            'notes_en': 'Quaker destroys its own wing feathers when its environment is unstable. Needs a stable environment and consistent routine. Social and loves nest-building — provide safe materials for this.',
        },
        # small pets
        'rabbit': {
            'ar': 'أرنب',
            'en': 'Rabbit',
            'notes_ar': 'الأرنب يحتاج قش التيموثي بكميات غير محدودة يوميًا لصحة أسنانه وهضمه. تدريبه على الصندوق ممكن وسهل. يعض الأسلاك الكهربائية — احمها بأنابيب بلاستيكية. لا يحب الحمل القسري.',
            'notes_en': 'Rabbit needs unlimited Timothy hay daily for dental and digestive health. Can be litter-trained easily. Chews electrical cords — protect with plastic tubing. Dislikes being forcibly held.',
        },
        'guinea_pig': {
            'ar': 'خنزير غيني',
            'en': 'Guinea Pig',
            'notes_ar': 'خنزير غيني كائن اجتماعي جدًا — يصاب برعب نفسي عند العيش وحيدًا. احتفظ بهم في أزواج متوافقة. يعبّر عن توتره بالصراخ المرتفع أو التجمد. يحتاج قفصًا واسعًا بعيدًا عن الكلاب والقطط.',
            'notes_en': 'Guinea Pig is highly social — suffers psychological trauma when kept alone. Keep them in compatible pairs. Expresses stress by loud squealing or freezing. Needs wide cage kept far from dogs and cats.',
        },
        'hamster': {
            'ar': 'هامستر',
            'en': 'Hamster',
            'notes_ar': 'الهامستر منفرد بطبعه — لا تضعه مع هامستر آخر أبدًا. يحتاج حوضًا زجاجيًا واسعًا بتراب عمقه ١٥-٢٠ سم للحفر الغريزي. عجلة جري مغلقة وواسعة ضرورية. نشيط ليلًا فقط.',
            'notes_en': 'Hamster is solitary by nature — never house with another hamster. Needs wide glass tank with 15-20cm deep substrate for instinctive digging. Enclosed wide running wheel is essential. Active only at night.',
        },
        # fish
        'betta': {
            'ar': 'بيتا',
            'en': 'Betta',
            'notes_ar': 'البيتا يعاني من اضطراب كيس العوم عند الإفراط في التغذية الجافة — صمه ٣-٤ أيام وقدّم دفنيا كعلاج. يقضم ذيله عند الملل في الأحواض الفارغة. لا تضعه مع بيتا ذكر آخر.',
            'notes_en': 'Betta suffers swim bladder disorder from dry food overfeeding — fast 3-4 days and offer Daphnia as treatment. Tail-bites when bored in bare tanks. Never house with another male betta.',
        },
        'goldfish': {
            'ar': 'ذهبي (جولدفيش)',
            'en': 'Goldfish',
            'notes_ar': 'الجولدفيش حساس جدًا لتراكم الأمونيا — يحتاج حوضًا كبيرًا وفلترًا قويًا. اللهاث عند السطح علامة تحذير لجودة ماء رديئة. لا تطعمه زيادة عن اللازم أبدًا.',
            'notes_en': 'Goldfish is extremely sensitive to ammonia buildup — needs large tank and powerful filter. Gasping at surface is a warning sign of poor water quality. Never overfeed.',
        },
        'cichlid': {
            'ar': 'سيكليد',
            'en': 'Cichlid',
            'notes_ar': 'السيكليد إقليمي وعدواني خاصة وقت التزاوج. أعد ترتيب الحوض لكسر الأقاليم القديمة. استخدم فواصل مؤقتة للسمك المهاجم وأضف أسماكًا تشتيت صغيرة هادئة.',
            'notes_en': 'Cichlid is territorial and aggressive especially during breeding. Rearrange tank to break old territories. Use temporary dividers for aggressors and add small peaceful dither fish.',
        },
        'guppy': {
            'ar': 'جابي / نيون',
            'en': 'Guppy / Neon Tetra',
            'notes_ar': 'الجابي والنيون حساسان لجودة الماء — أي ارتفاع بالأمونيا يظهر فورًا على ألوانهم. يحتاجون مجموعات لا أفرادًا. النيون خاصةً يحتاج ماءً باردًا نسبيًا وتغييرًا أسبوعيًا منتظمًا.',
            'notes_en': 'Guppy and Neon Tetra are sensitive to water quality — any ammonia spike shows immediately in their colors. Need groups, not individuals. Neon especially needs cooler water and regular weekly changes.',
        },
        'koi': {
            'ar': 'كوي',
            'en': 'Koi',
            'notes_ar': 'الكوي يحتاج بركة كبيرة خارجية لا أقل من ١٠٠٠ لتر. حساس لجودة الماء والأمونيا. يمكنه التعرف على مالكه والأكل من يده. يحتاج فلترًا ضخمًا ومظلة من الشمس المباشرة.',
            'notes_en': 'Koi needs a large outdoor pond of at least 1000 liters. Sensitive to water quality and ammonia. Can recognize its owner and eat from hand. Needs heavy filtration and shade from direct sun.',
        },
    }

    data       = request.get_json(silent=True) or {}
    pet_type   = data.get('pet_type', 'cat').lower()
    breed      = (data.get('breed') or '').strip().lower()
    pet_name   = (data.get('pet_name') or '').strip()
    age_months = float(data.get('age_months') or 12)
    weight_kg  = float(data.get('weight_kg')  or 4)
    conditions = data.get('conditions') or []
    lang       = data.get('lang', 'ar')

    # ── Pet name mapping ───────────────────────────────────────────
    pet_names = {
        'ar': {'cats':'قطة','dogs':'كلب','birds':'طائر','fish':'سمكة','small-pets':'حيوان صغير'},
        'en': {'cats':'cat','dogs':'dog','birds':'bird','fish':'fish','small-pets':'small pet'},
    }
    # support both singular and plural slugs
    slug_map = {'cat':'cats','dog':'dogs','bird':'birds','fish':'fish','small-pet':'small-pets',
                'cats':'cats','dogs':'dogs','birds':'birds','small-pets':'small-pets'}
    pet_slug = slug_map.get(pet_type, pet_type)
    pet_label_ar = pet_names['ar'].get(pet_slug, pet_type)
    pet_label_en = pet_names['en'].get(pet_slug, pet_type)
    name_str = pet_name or (pet_label_ar if lang == 'ar' else pet_label_en)

    # ── Product matching ───────────────────────────────────────────
    keyword_map = {
        'hair_loss':        ['omega', 'skin', 'coat', 'شعر', 'جلد', 'ريش', 'feather', 'omega-3'],
        'grain_allergy':    ['grain free', 'grain-free', 'gluten', 'خالي من الحبوب'],
        'obesity':          ['light', 'diet', 'weight', 'وزن', 'خفيف', 'slim'],
        'lethargy':         ['energy', 'active', 'طاقة', 'نشاط', 'vitality'],
        'castrated':        ['sterilized', 'neutered', 'castrated', 'معقم'],
        'sensitive_stomach':['sensitive', 'digestive', 'هضم', 'حساس', 'gastro'],
    }
    search_terms = []
    for c in conditions:
        search_terms.extend(keyword_map.get(c, []))
    if not search_terms:
        search_terms = ['']

    db = get_db()
    matched, seen = [], set()
    for term in search_terms:
        like = f'%{term}%'
        rows = db.execute("""
            SELECT p.id, p.slug, p.name_ar, p.name_en, p.price, p.discount_price,
                   p.health_tags, p.stock_qty,
                   (SELECT pi.filename FROM product_images pi WHERE pi.product_id=p.id ORDER BY pi.sort_order LIMIT 1) as img
            FROM products p
            JOIN categories c ON c.id = p.category_id
            WHERE p.is_active=1 AND p.stock_qty>0
              AND (c.slug=? OR c.slug='all')
              AND (p.health_tags LIKE ? OR p.description_ar LIKE ? OR p.description_en LIKE ?
                   OR p.name_ar LIKE ? OR p.name_en LIKE ?)
            ORDER BY p.is_featured DESC, p.stock_qty DESC
            LIMIT 6
        """, (pet_slug, like, like, like, like, like)).fetchall()
        for r in rows:
            if r['id'] not in seen:
                seen.add(r['id'])
                matched.append(dict(r))
        if len(matched) >= 5:
            break

    if not matched:
        rows = db.execute("""
            SELECT p.id, p.slug, p.name_ar, p.name_en, p.price, p.discount_price,
                   p.stock_qty,
                   (SELECT pi.filename FROM product_images pi WHERE pi.product_id=p.id ORDER BY pi.sort_order LIMIT 1) as img
            FROM products p
            JOIN categories c ON c.id = p.category_id
            WHERE p.is_active=1 AND p.stock_qty>0 AND c.slug=?
            ORDER BY p.is_featured DESC LIMIT 4
        """, (pet_slug,)).fetchall()
        if not rows:
            rows = db.execute("""
                SELECT p.id, p.slug, p.name_ar, p.name_en, p.price, p.discount_price,
                       p.stock_qty,
                       (SELECT pi.filename FROM product_images pi WHERE pi.product_id=p.id ORDER BY pi.sort_order LIMIT 1) as img
                FROM products p WHERE p.is_active=1 AND p.stock_qty>0
                ORDER BY p.is_featured DESC LIMIT 4
            """).fetchall()
        matched = [dict(r) for r in rows]
    db.close()

    products_out = []
    for p in matched[:5]:
        img_url = f"/static/img/products/{p['img']}" if p.get('img') else None
        products_out.append({
            'id':             p['id'],
            'slug':           p['slug'],
            'name':           p['name_ar'] if lang == 'ar' else p['name_en'],
            'price':          p['price'],
            'discount_price': p['discount_price'],
            'img':            img_url,
        })

    # ── Structured tips (no AI — 100% reliable) ──────────────────
    _TIPS = {
      'scottish': {'ar':[
        ('🍽️','التغذية','أعطِه أكلاً رطباً عالي البروتين (دجاج أو سمك) مرتين يومياً، الصباح والمساء. الكمية المثالية ٤٠-٥٠ غرام لكل وجبة حسب وزنه. ارفع الوعاء ٥-٨ سم عن الأرض على حامل صغير لتخفيف الضغط على رقبته ومفاصله الأمامية. ماء نظيف دائماً متوفر - يُفضّل نافورة مياه صغيرة لأنها تشجعه على الشرب الكافي.'),
        ('🚫','تجنّب','لا تضغط على أذنيه أو تثنيها أبداً - هذا يسبب ألماً حاداً بسبب طفرة الغضروف. لا تجبره على الجلوس في أوضاع مكوّرة لفترة طويلة. الأكل الجاف كوجبة وحيدة يجفف جسمه ويرهق كلاه - استخدمه كمكافأة فقط. تجنّب حمله من منطقة الصدر أو الأرجل.'),
        ('💡','نصيحة السلالة','صندوق الفضالت يجب أن يكون حواف منخفضة جداً (٦-٨ سم) لأن دخول صندوق عالي الجوانب مؤلم لمفاصله. وفّر له سرير ميموري فوم دافئ بعيد عن التيارات الهوائية. الحرارة المعتدلة تخفف ألم مفاصله في الشتاء. العب معه بألعاب أرضية فقط - لا تجبره على القفز لأعلى.'),
        ('⚠️','انتبه','إذا لاحظت توقفه عن اللعب فجأة، أو صعوبة في النهوض من الأرض، أو مشية متأرجحة - هذه علامات ألم مفاصل حاد يحتاج طبيباً فوراً. سكوتش فولد يخفي ألمه، فلا تنتظر حتى تصبح الأعراض واضحة جداً. الكشف المبكر يمنع الأذى الدائم.')
      ],'en':[
        ('🍽️','Feeding','High-protein wet food (chicken or fish) twice daily, morning and evening. Ideal portion is 40-50g per meal depending on weight. Raise the bowl 5-8cm on a small stand to reduce neck and front joint pressure. Always fresh water available — a small fountain encourages adequate drinking.'),
        ('🚫','Avoid','Never press or fold the ears — this causes acute pain due to the cartilage mutation. Don\'t force prolonged curled sitting positions. Dry food as the sole diet dehydrates and stresses kidneys — use only as an occasional treat. Avoid lifting from the chest or legs.'),
        ('💡','Breed tip','Litter box must have very low entry sides (6-8cm) — climbing over a high-sided box is painful for the joints. Provide warm memory foam bedding away from drafts. Warmth relieves joint pain in winter. Play with floor-level toys only; never force jumping.'),
        ('⚠️','Watch out','Sudden stop in play, difficulty rising from the floor, or swaying gait = acute joint pain requiring immediate vet attention. Scottish Folds mask pain well — don\'t wait until symptoms become severe. Early detection prevents permanent damage.')
      ]},
      'persian': {'ar':[
        ('🍽️','التغذية','أكل رطب صغير الحبيبات أو مهروس مرتين يومياً - الوجه المفلطح يصعّب عليه مضغ الحبيبات الكبيرة. تجنّب أوعية الأكل العميقة واستخدم طبق مسطّح واسع ليتناول طعامه براحة. الماء ضروري جداً لصحة كلاه الحساسة - اجعله دائماً طازجاً. أضف مكمّل أوميغا-٣ أسبوعياً يحافظ على لمعان فراؤه الكثيف.'),
        ('🚫','تجنّب','الأكل الجاف الكبير الحجم يضر بوجهه المفلطح ويصعّب تنفسه أثناء الأكل. البيئات الصاخبة والضجيج المفاجئ يسببان توتراً شديداً له. تغيير الروتين اليومي فجأة يربكه ويجعله ينسحب. لا تدعه في أماكن مرتفعة حرارتها لأن تنفسه صعب أصلاً.'),
        ('💡','نصيحة السلالة','فراؤه الكثيف الطويل يحتاج تمشيطاً يومياً ١٠-١٥ دقيقة بفرشاة ناعمة لمنع التشابك وكرات الشعر. ثنايا وجهه تتراكم فيها الرطوبة - امسح المنطقة حول العيون والأنف يومياً بقطعة قماش مبللة. حمّامه كل ٤-٦ أسابيع بشامبو لطيف يحافظ على فراؤه.'),
        ('⚠️','انتبه','صعوبة في التنفس أثناء النوم أو الأكل، أو شخير مفاجئ قوي، يمكن أن يشير لانسداد في مجرى الهواء. أيضاً، إذا توقف عن العناية بنفسه أو لم يأكل يوماً كاملاً - زيارة الطبيب لا تتأخر.')
      ],'en':[
        ('🍽️','Feeding','Small-kibble or mashed wet food twice daily — the flat face makes chewing large kibble difficult. Use a flat wide plate rather than a deep bowl for comfortable eating. Water is critical for sensitive kidneys — keep it always fresh. Add a weekly omega-3 supplement to maintain coat quality.'),
        ('🚫','Avoid','Large dry kibble is hard on the flat face and causes breathing difficulty while eating. Noisy environments and sudden sounds cause severe stress. Abrupt routine changes confuse and withdraw this breed. Don\'t leave it in hot spaces as breathing is already labored.'),
        ('💡','Breed tip','Thick long coat needs 10-15 min daily brushing with a soft brush to prevent matting and hairballs. Facial folds trap moisture — wipe around eyes and nose daily with a damp cloth. Bath every 4-6 weeks with gentle shampoo maintains coat quality.'),
        ('⚠️','Watch out','Breathing difficulty during sleep or eating, or sudden loud snoring, may indicate airway obstruction. Also if it stops grooming itself or refuses food for a full day — see a vet without delay.')
      ]},
      'siamese': {'ar':[
        ('🍽️','التغذية','أكل رطب عالي البروتين مرتين يومياً - الصباح قبل تغادر البيت والمساء عند العودة. كميات محسوبة حسب وزنه (٥٠-٦٠ غرام/وجبة) لأنه نشيط ويحرق طاقة أكثر من القطط الهادئة. أضف أحياناً حبات تونة طازجة كمكافأة - يحبها وهي غنية بالبروتين. ماء نظيف دائماً متوفر.'),
        ('🚫','تجنّب','لا تتركه وحيداً أكثر من ٨ ساعات يومياً - الوحدة الطويلة تسبب قلقاً حاداً وتساقط شعر وخدش الأثاث. لا تعاقبه على المواء المفرط بالصراخ عليه - يزيد توتره. لا تغيّر بيئته فجأة بدون فترة تكيّف تدريجية.'),
        ('💡','نصيحة السلالة','هذا الحيوان يحتاج رفيقاً حقيقياً - إما قط آخر متوافق أو حضورك البشري اليومي لساعات كافية. العاب تفاعلية بالليزر أو الريشة ٢٠-٣٠ دقيقة يومياً تفرّغ طاقته وتحسّن مزاجه. رفوف جدارية وشجرة قطط تعطيه مساحة لاستكشاف وتسلق.'),
        ('⚠️','انتبه','مواء مستمر وعالٍ لساعات حتى بعد عودتك للبيت + لعق قهري لجسمه بشكل مفرط + تساقط شعر = قلق انفصال حاد. هذه حالة نفسية تحتاج استشارة طبيب بيطري سلوكي، ولا تُحلّ بالعقاب.')
      ],'en':[
        ('🍽️','Feeding','High-protein wet food twice daily — morning before you leave and evening on return. Measured portions by weight (50-60g/meal) as this active breed burns more calories. Occasional fresh tuna as a treat works well. Always fresh clean water.'),
        ('🚫','Avoid','Don\'t leave alone more than 8 hours daily — prolonged solitude causes anxiety, hair loss, and furniture scratching. Never punish excessive meowing by shouting — it increases stress. Don\'t change its environment suddenly without a gradual adaptation period.'),
        ('💡','Breed tip','This cat needs real companionship — either another compatible cat or sufficient daily human presence. Interactive play (laser/feather wand) 20-30 min daily releases energy and improves mood. Wall shelves and a cat tree provide climbing and exploration space.'),
        ('⚠️','Watch out','Continuous loud meowing even after you return + compulsive excessive self-licking + hair loss = severe separation anxiety. This is a psychological condition needing a veterinary behaviorist consult — punishment makes it worse.')
      ]},
      'british': {'ar':[
        ('🍽️','التغذية','وجبتان محسوبتان بدقة يومياً (٥٠-٦٠ غرام/وجبة) ولا شيء بينهما. استخدم لعبة ألغاز الطعام أو الكرة المثقّبة ليحصل على طعامه وهو يتحرك - هذا يجعله يحرق سعرات وهو يأكل. مزيج ٥٠٪ رطب و٥٠٪ جاف مثالي للبريتيش لأنه يحب القوام الجاف لكنه يحتاج الرطوبة.'),
        ('🚫','تجنّب','الطعام الحر المفتوح (الأكل متاح طوال اليوم) يؤدي للسمنة في أسابيع لهذه السلالة. لا تحمله قسراً أو تجبره على الاحتضان - يفضّل الجلوس بقربك باختياره. لا تكافئه بأكل إضافي - المكافأة الجسدية أفضل منه.'),
        ('💡','نصيحة السلالة','جلسات لعب إجبارية ٢٠-٣٠ دقيقة مساءً ضرورية لمنع السمنة - حتى لو رفض في البداية، الريشة والحبل يحركّانه. زنه شهرياً والوزن المثالي للبريتيش الذكر ٤-٧ كغ والأنثى ٣-٥ كغ. هذه السلالة تبدو كسولة لكنها تحتاج تحريك مستمر.'),
        ('⚠️','انتبه','إذا لاحظت أنه يلهث عند أقل مجهود، أو بطنه يصبح منتفخاً ومرئياً من الجانبين، أو يصعب عليه النهوض - هذه علامات سمنة مفرطة تضغط على قلبه. السمنة في البريتيش تؤدي لمشاكل قلبية مبكرة، وزّنه الآن وراجع الطبيب.')
      ],'en':[
        ('🍽️','Feeding','Two precisely measured meals daily (50-60g/meal), nothing between. Use puzzle feeders or treat balls so eating involves movement and burns calories. A 50/50 wet-dry mix is ideal — this breed likes dry texture but needs the hydration of wet food.'),
        ('🚫','Avoid','Free-feeding leads to obesity in weeks for this breed. Never force-hold or cuddle — it prefers sitting near you by choice. Don\'t use food as reward — physical affection on its terms is better.'),
        ('💡','Breed tip','Mandatory play sessions 20-30 min each evening prevent obesity — even if initially reluctant, feather wands and strings work. Weigh monthly: ideal weight for males is 4-7kg, females 3-5kg. This breed appears lazy but needs consistent exercise.'),
        ('⚠️','Watch out','Panting at minimal effort, visibly swollen belly from both sides, or difficulty rising = dangerous obesity straining its heart. Obesity in British Shorthairs leads to early cardiac issues — weigh it now and see the vet.')
      ]},
      'bengal': {'ar':[
        ('🍽️','التغذية','أكل رطب عالي البروتين (٥٥-٧٠٪ بروتين حيواني) ثلاث مرات يومياً - الصباح والظهر والمساء. البنغال حيوان بري بالأصل ويحتاج كميات أكثر من القطط المنزلية بنسبة ٢٠-٣٠٪. أضف جزءاً صغيراً من الأرز المسلوق أو البطاطا أحياناً لأنه يتقبل الكربوهيدرات بشكل جيد. ماء بارد ونظيف دائماً.'),
        ('🚫','تجنّب','الملل هو أخطر شيء على البنغال - بدون تحفيز كافٍ سيخرّب الأثاث والستائر والكوابل الكهربائية. لا تحبسه في غرفة صغيرة فارغة أبداً. الضرب أو العقاب الجسدي يحوّله لحيوان عدواني خطير - هو ذكي ويتذكر.'),
        ('💡','نصيحة السلالة','شجرة قطط عالية (١.٥ متر+) ورفوف جدارية على ارتفاعات مختلفة ضرورية لأنه يحب الارتفاع. ألعاب تحاكي الصيد (فأر على عصا، ليزر مع مكافأة حقيقية) ٣٠-٤٥ دقيقة يومياً. بعض أصحاب البنغال يُعلّمونه المشي بشريط والإحضار - هو ذكي بما يكفي لذلك.'),
        ('⚠️','انتبه','عدوانية مفاجئة نحو الناس أو القطط الأخرى، أو تدمير الأثاث رغم وجود ألعاب كافية، يعني أن البيئة لا تلبّي احتياجاته. راجع كمية التحفيز اليومي أولاً قبل أي قرار آخر - ٩٠٪ من مشاكل البنغال سببها الملل لا المرض.')
      ],'en':[
        ('🍽️','Feeding','High-protein wet food (55-70% animal protein) three times daily — morning, noon, and evening. Bengals are wild-origin cats needing 20-30% more food than domestic breeds. Occasionally add small cooked rice or sweet potato — they tolerate carbs well. Always cold fresh water.'),
        ('🚫','Avoid','Boredom is the single biggest danger to a Bengal — without enough stimulation it will destroy furniture, curtains, and electrical cords. Never confine to a small empty room. Physical punishment turns this breed aggressive and it has a long memory.'),
        ('💡','Breed tip','Tall cat tree (1.5m+) and wall shelves at different heights are essential as Bengals love elevation. Hunting-style play (mouse on stick, laser with real reward) 30-45 min daily. Some Bengal owners teach them leash walking and fetch — they are smart enough.'),
        ('⚠️','Watch out','Sudden aggression toward people or other cats, or destroying furniture despite having toys, signals the environment is not meeting its needs. Assess daily stimulation level first before any other decision — 90% of Bengal behavior problems stem from boredom, not illness.')
      ]},
      'maincoon': {'ar':[
        ('🍽️','التغذية','أكل رطب عالي البروتين بكميات أعلى من متوسط القطط (٧٠-٨٠ غرام/وجبة) مرتين يومياً لأن حجمه كبير. أضف كبسولة أوميغا-٣ مفتوحة على طعامه مرة أسبوعياً لصحة فراؤه الطويل. نافورة مياه تشجعه على الشرب الكافي ومهمة لصحة كلاه. تجنّب الاقتصار على الأكل الجاف.'),
        ('🚫','تجنّب','الأكل الجاف فقط يزيد كرات الشعر بشكل خطير ويعرّضه لانسداد معوي. الاستحمام المتكرر يجفف فراؤه الطبيعي - مرة كل ٦-٨ أسابيع كافية ومع شامبو مخصص للفراء الطويل. لا تقص فراؤه إلا للضرورة الطبية.'),
        ('💡','نصيحة السلالة','فرشاة Slicker يومياً ١٠-١٥ دقيقة تمنع التشابك وكرات الشعر - يستمتع بهذا الوقت معك. ركّز التمشيط على المنطقة خلف الأذنين وتحت الإبطين حيث يتشابك الشعر أولاً. أعطِه معجون مخصص لكرات الشعر مرتين أسبوعياً في موسم التساقط.'),
        ('⚠️','انتبه','قيء أصفر أو أخضر رغوي مرتين أو أكثر في اليوم + خمول + توقف عن الأكل = انسداد كرة شعر في المعدة. هذه حالة طارئة تستدعي الطبيب الليلة. لا تنتظر اليوم التالي لأن الانسداد قد يكون كاملاً.')
      ],'en':[
        ('🍽️','Feeding','High-protein wet food in larger portions than average cats (70-80g/meal) twice daily — this is a large breed. Add an opened omega-3 capsule on food once weekly for coat health. A water fountain encourages adequate drinking and supports kidney health. Avoid dry-only diet.'),
        ('🚫','Avoid','Dry food only causes dangerous hairball buildup with real intestinal blockage risk. Over-bathing strips natural coat oils — once every 6-8 weeks with long-coat shampoo is sufficient. Don\'t cut the coat except for medical necessity.'),
        ('💡','Breed tip','Daily Slicker brush 10-15 min prevents tangles and hairballs — it enjoys this bonding time. Focus behind ears and under armpits where matting starts first. Give hairball paste twice weekly during shedding season.'),
        ('⚠️','Watch out','Yellow or foamy green vomiting twice or more in one day + lethargy + food refusal = hairball blockage in the stomach. This is a tonight emergency — don\'t wait until morning as the blockage may be complete.')
      ]},
      'ragdoll': {'ar':[
        ('🍽️','التغذية','أكل رطب مرتين يومياً بكميات محسوبة (٥٠-٦٠ غرام/وجبة) لأنه يأكل بشراهة إذا ترك الطعام مفتوحاً. أضف أحياناً دجاجاً مسلوقاً بدون ملح كمكافأة - يحبه وهو بروتين ممتاز. ماء نظيف دائماً متوفر لأن فراؤه الكثيف يزيد حاجته للترطيب.'),
        ('🚫','تجنّب','لا تترك طعامه مفتوحاً طوال اليوم - يأكل حتى التخمة ويصاب بالسمنة بسرعة. تغيير منزله أو أثاثه فجأة يسبب له توتراً واضحاً - قدّم التغييرات تدريجياً. لا تدعه يختلط مع حيوانات عدوانية لأنه سلمي جداً ولا يدافع عن نفسه.'),
        ('💡','نصيحة السلالة','يستمتع بالتمشيط اليومي اللطيف ١٠ دقائق - اجعله وقت مميز بينكما. اجتماعي جداً ويعاني عند الإهمال - خصّص له وقتاً يومياً للعب والاحتضان. يتأقلم بشكل ممتاز مع الأطفال والحيوانات الأخرى إذا قُدّمت بشكل صحيح.'),
        ('⚠️','انتبه','خمول غير مبرر + أكل أقل من نصف وجبته المعتادة لأكثر من يومين متتاليين = زيارة طبيب ضرورية. الراغدول يخفي ألمه ومرضه لأنه هادئ بطبعه، فلا تنتظر حتى تصبح الأعراض واضحة.')
      ],'en':[
        ('🍽️','Feeding','Wet food twice daily in measured portions (50-60g/meal) — this breed overeats if food is left out freely. Occasionally add plain boiled chicken without salt as a treat — excellent protein source. Always fresh water as its thick coat increases hydration needs.'),
        ('🚫','Avoid','Never leave food out all day — it will overeat and gain weight quickly. Sudden home or furniture changes cause visible stress — introduce changes gradually. Don\'t let it mix with aggressive animals; Ragdolls are very peaceful and won\'t defend themselves.'),
        ('💡','Breed tip','Enjoys gentle daily grooming 10 minutes — make it a special bonding time. Very social and suffers from neglect — allocate daily play and cuddle time. Adapts excellently to children and other pets when introduced properly.'),
        ('⚠️','Watch out','Unexplained lethargy + eating less than half its normal portion for more than 2 days = vet visit needed. Ragdolls mask pain and illness because of their calm nature — don\'t wait for obvious symptoms.')
      ]},
      # dogs
      'german': {'ar':[
        ('🍽️','التغذية','أكل جاف أو رطب عالي البروتين (٢٦٪+ بروتين) وجبتين يومياً - الصباح والمساء. الكمية المثالية ٣-٤ أكواب جاف أو ٣٠٠-٤٠٠ غرام رطب حسب وزنه ونشاطه. لا تطعمه قبل التمرين بساعة أو بعده مباشرة - الجيرمن عرضة لانتفاخ المعدة الخطير. ماء نظيف متاح طوال اليوم خصوصاً بعد التمرين.'),
        ('🚫','تجنّب','الملل والعزل الطويل أخطر شيء على الجيرمن - بدون تحفيز يومي يتحول لتدمير الأثاث وعض الجدران. لا تتركه وحيداً أكثر من ٦ ساعات. الضرب والعقاب القاسي يضرّان بثقته بك ويجعلانه قلقاً. تجنّب الأكل السريع - استخدم وعاء بطيء.'),
        ('💡','نصيحة السلالة','يحتاج ساعة تمرين يومياً كحد أدنى - مشي سريع + جري أو لعب استرداد. أضف ١٥-٢٠ دقيقة تدريب ذهني يومياً (أوامر جديدة، ألعاب بحث، ألغاز طعام) لأن عقله يحتاج تحفيزاً مثل جسمه تماماً. كلب جيرمن مشغول ذهنياً وبدنياً = كلب هادئ وسعيد في البيت.'),
        ('⚠️','انتبه','تصلب أو ضعف تدريجي في الأرجل الخلفية، خصوصاً عند النهوض من الأرض أو بعد النوم الطويل = خلل ورك وراثي (HD) شائع جداً في هذه السلالة. الكشف المبكر قبل سن ٢ سنة يمنع الأذى الدائم. راجع الطبيب ولا تنتظر تطور الأعراض.')
      ],'en':[
        ('🍽️','Feeding','High-protein food twice daily. Never feed right before or after exercise — bloat risk is real for this breed.'),
        ('🚫','Avoid','Boredom and isolation are its biggest threats. Without mental and physical work it will destroy your home.'),
        ('💡','Breed tip','1 hour daily exercise + mental training (commands, puzzles) is a necessity, not a luxury. A busy dog is a calm dog.'),
        ('⚠️','Watch out','Stiffness or weakness in back legs = hip dysplasia (HD), very common in this breed. See a vet immediately.')
      ]},
      'husky': {'ar':[
        ('🍽️','التغذية','أكل عالي البروتين والدهون (٣٠٪+ بروتين، ١٨٪+ دهون) وجبتين يومياً. في الشتاء يحتاج كميات أكبر بـ٢٠٪ لأنه يحرق طاقة لتدفئة جسمه. في الصيف اللبناني الحار يأكل أقل تلقائياً - هذا طبيعي تماماً ولا تجبره. ماء بارد دائماً متوفر وقدّمه بوعاء معدني لا بلاستيك في الحر.'),
        ('🚫','تجنّب','الحر الشديد فوق ٢٨ درجة خطر جدي على حياته - لا تمشِ معه في الظهيرة أبداً. ممنوع تركه في شمس مباشرة أو سيارة مغلقة حتى ولو دقائق. لا تقص فراؤه الكثيف صيفاً - هو يعزل الحرارة ويحميه، قصّه يضرّ أكثر مما ينفع.'),
        ('💡','نصيحة السلالة','يحتاج ساعتين تمرين يومياً كحد أدنى - مشي سريع مبكراً قبل الحر وركض أو ألعاب مساءً. حديقة بسياج عالٍ (١.٨م+) ضرورية لأنه يحفر تحت الأسيجة المنخفضة أو يقفز فوقها. بدون تمرين كافٍ يصبح مدمّراً ومزعجاً بشكل لا يُحتمل.'),
        ('⚠️','انتبه','لهاث مفرط مع لعاب زائد + تمدد على الأرض ورفض الحركة في الجو الحار = ضربة شمس طارئة. أدخله مكيّف فوراً وبلّل رقبته وقدميه بماء بارد (ليس ثلج) واتصل بالطبيب.')
      ],'en':[
        ('🍽️','Feeding','High-protein high-fat food (30%+ protein, 18%+ fat) twice daily. In winter needs 20% more to fuel body warmth. Eating less in Lebanon\'s summer heat is completely normal — never force it. Always cold water in a metal bowl, not plastic, in hot weather.'),
        ('🚫','Avoid','Heat above 28°C is a genuine life threat — never walk in midday. No direct sun or closed car even for minutes. Don\'t shave the thick coat in summer — it insulates against heat and shaving causes more harm than good.'),
        ('💡','Breed tip','Needs 2 hours daily exercise minimum — early morning walk before heat and evening run or play. Garden fencing must be 1.8m+ as it digs under low fences or leaps over them. Without adequate exercise it becomes destructively loud.'),
        ('⚠️','Watch out','Excessive panting with drooling + lying flat refusing to move in hot weather = heat stroke emergency. Move to AC immediately, wet neck and paws with cool water (not ice), and call a vet.')
      ]},
      'golden': {'ar':[
        ('🍽️','التغذية','وجبتان يومياً بكميات محسوبة بدقة حسب وزنه المثالي - الجولدن الذكر ٢٥-٣٤ كغ والأنثى ٢٥-٢٩ كغ. استخدم كوباً قياسياً لا "حفنة" لأن الكميات تتراكم. قلل الكميات ١٠٪ كل ٦ أشهر عند الكبر لأن نشاطه يقل. اقتصر على أكل واحد نوعه ولا تخلط.'),
        ('🚫','تجنّب','لا تترك طعامه مفتوحاً - سيأكل كل ما أمامه دون توقف. كل مكافأة إضافية هي سعرات تتراكم في صمت وتؤدي لسمنة مبكرة. تجنّب رياضة الجري الشديد قبل عمر ١٨ شهراً لأن مفاصله لم تكتمل بعد.'),
        ('💡','نصيحة السلالة','٤٥ دقيقة مشي يومياً + ألعاب استرداد (رمي الكرة في الماء مثالي) تحافظ على وزنه وصحة مفاصله. درّبه على "اجلس" قبل الترحيب بالضيوف من عمر ٣ أشهر - القفز على الناس عادة تتكرّس بسرعة وتصعب إزالتها لاحقاً. يحب الأطفال ويتعلم معهم بسرعة.'),
        ('⚠️','انتبه','أي كتلة أو تورم تحت الجلد - حتى لو لم يبدُ مؤلماً - يجب فحصه خلال أسبوع. الغولدن ريتريفر أكثر سلالات الكلاب عرضة للأورام، والكشف المبكر ينقذ الحياة. لا تنتظر حتى تكبر الكتلة.')
      ],'en':[
        ('🍽️','Feeding','Two precisely measured meals based on ideal weight — male Goldens 25-34kg, females 25-29kg. Use a measuring cup, not a handful, as portions add up invisibly. Reduce portions 10% every 6 months as the dog ages and activity drops. Stick to one food type, don\'t mix.'),
        ('🚫','Avoid','Never leave food accessible — it will eat everything without stopping. Every extra treat is silent weight gain. Avoid intense running before 18 months of age as joints are still developing.'),
        ('💡','Breed tip','45 min daily walk + retrieval games (water fetch is ideal) maintain weight and joint health. Train "sit" before greeting guests from 3 months old — jumping becomes deeply ingrained quickly and is hard to undo later. Loves children and learns quickly with them.'),
        ('⚠️','Watch out','Any lump or swelling under the skin — even if painless — must be examined within a week. Golden Retrievers have the highest cancer rate among dog breeds, and early detection saves lives. Don\'t wait for it to grow.')
      ]},
      'labrador': {'ar':[
        ('🍽️','التغذية','وجبتان يومياً بكميات صارمة - الذكر ٢٥-٣٥ كغ والأنثى ٢٥-٣٢ كغ. لا تثق بعيونه الحزينة الشهيرة - سيطلب أكلاً حتى لو أكل للتو. استخدم وعاء بطيء الأكل (Slow Feeder) يجبره على الأكل ببطء ويمنع الابتلاع السريع. ماء نظيف دائماً.'),
        ('🚫','تجنّب','الطعام الحر المفتوح مدمّر لهذه السلالة تحديداً - لاب يترك طعامه مفتوحاً = لاب سمين. لا تطعمه مباشرة قبل أو بعد الجري بساعة لأن انتفاخ المعدة GDV يمكن أن يكون قاتلاً. تجنّب المكافآت الدهنية كالجبن والنقانق.'),
        ('💡','نصيحة السلالة','٤٥ دقيقة جري أو سباحة يومياً تحافظ على وزنه ومفاصله. يحب الإحضار (Fetch) بشكل استثنائي - هذه اللعبة تُنهك طاقته الزائدة في وقت قصير. اللاب الذكي ويحفظ الأوامر بسرعة - التدريب من سن ٨ أسابيع يبني شخصية رائعة.'),
        ('⚠️','انتبه','وزن يتزايد رغم الكميات المحسوبة + لهاث عند المشي العادي + صعوبة في الجلوس أو النهوض = مشكلة ورك (HD) شائعة. الكشف بالأشعة قبل سن سنتين يسمح بالعلاج المبكر الفعّال.')
      ],'en':[
        ('🍽️','Feeding','Two strict measured meals daily — males 25-35kg ideal, females 25-32kg. Never trust the famous sad eyes — it will beg immediately after eating. Use a slow feeder bowl to prevent gulping. Clean water always available.'),
        ('🚫','Avoid','Free-feeding is destructive for this breed specifically — an unsupervised Lab becomes obese fast. Never feed within an hour before or after running — GDV bloat can be fatal. Avoid fatty treats like cheese or sausage.'),
        ('💡','Breed tip','45 min of running or swimming daily maintains weight and joint health. It loves Fetch obsessively — this game exhausts excess energy quickly. Labs are highly intelligent and learn commands fast — training from 8 weeks builds an exceptional temperament.'),
        ('⚠️','Watch out','Weight gain despite measured portions + panting on normal walks + difficulty sitting or rising = hip dysplasia (HD). X-ray screening before age 2 allows effective early treatment.')
      ]},
      'poodle': {'ar':[
        ('🍽️','التغذية','أكل عالي الجودة قليل الحبوب وجبتين يومياً - البودل حساس للقمح والذرة كثيراً. إذا غيّرت نوع الأكل، افعل ذلك تدريجياً على ١٠ أيام (٢٥٪ جديد / ٧٥٪ قديم ثم ٥٠-٥٠ ثم ٧٥-٢٥) لتجنب اضطراب معدته. ماء نظيف دائماً.'),
        ('🚫','تجنّب','تجنّب تركه وحيداً أكثر من ٥-٦ ساعات يومياً - القلق يسبب عنده لعق قدميه بشكل قهري أو نتف فراؤه. لا تكرر نفس التمرين يومياً - يملّ سريعاً ويحتاج تنويعاً. تجنّب الماء الراكد قرب أذنيه بعد الاستحمام - عرضة لالتهابات الأذن.'),
        ('💡','نصيحة السلالة','أذكى الكلاب على الإطلاق - يتعلم أوامر جديدة في ٥ محاولات فقط. خصّص ٢٠ دقيقة تدريب ذهني يومياً (ألغاز طعام، أوامر جديدة، إخفاء أشياء ليبحث عنها) إضافةً للتمرين البدني. التنويع في الأنشطة يمنع الملل والعادات العصبية.'),
        ('⚠️','انتبه','احمرار مزمن حول العيون أو الفم أو القدمين، أو حكة متكررة - هذه علامات حساسية غذائية أو بيئية شائعة جداً في البودل. اعزل المسبب بتغيير الأكل لنوع بروتين واحد جديد (مثلاً بطة أو سمك فقط) لمدة ٨ أسابيع ثم راجع الطبيب.')
      ],'en':[
        ('🍽️','Feeding','High-quality low-grain food twice daily — Poodles are commonly sensitive to wheat and corn. When switching food, do it over 10 days (25% new/75% old → 50/50 → 75/25) to avoid stomach upset. Always clean water.'),
        ('🚫','Avoid','Avoid leaving alone more than 5-6 hours — anxiety causes compulsive paw licking or coat plucking. Don\'t repeat the same exercise daily — it bores quickly and needs variety. Keep ears dry after bathing; prone to ear infections from trapped moisture.'),
        ('💡','Breed tip','Ranked the most intelligent dog breed — learns new commands in just 5 attempts. Add 20 min daily mental training (puzzle feeders, new commands, hiding objects to find) alongside physical exercise. Activity variety prevents boredom and nervous habits.'),
        ('⚠️','Watch out','Chronic redness around eyes, mouth, or paws, or repeated scratching = very common food or environmental allergy in Poodles. Isolate the cause by switching to a single novel protein (duck or fish only) for 8 weeks, then see a vet.')
      ]},
      'maltese': {'ar':[
        ('🍽️','التغذية','وجبتان صغيرتان يومياً (٤٠-٦٠ غرام إجمالاً حسب وزنه) - الملتيز الكبير لا يتجاوز ٣.٥ كغ. أكل رطب صغير الحبيبات أو مطحون مناسب لفمه الصغير. تجنّب أي وجبة دسمة أو دهنية تؤثر على كبده الحساس. ماء نظيف دائماً في وعاء صغير يناسب حجمه.'),
        ('🚫','تجنّب','لا ترفعه بالقوة من يديه أو ذراعيه - عظامه رقيقة تنكسر بسهولة. البرد الشديد والأرضيات الرطبة تؤثر على صدره. لا تتركه يقفز من أماكن عالية - كسر العظام شائع في السلالات الصغيرة جداً. تجنّب الضغط على صدره عند حمله.'),
        ('💡','نصيحة السلالة','شعره الأبيض الطويل الناعم يحتاج تمشيطاً يومياً ١٠ دقائق بمشط ناعم لمنع التشابك. الشعر حول العيون يجب ربطه أو قصّه لأنه يسبب تهيّج مزمن. استحمامه كل ١٠-١٤ يوماً ضروري مع تجفيف كامل لأن رطوبة الجلد تسبب التهابات.'),
        ('⚠️','انتبه','نباح مستمر لساعات + ارتجاف رغم دفء الجو + رفض الأكل = ألم داخلي أو قلق حاد. الملتيز يبالغ في إخفاء ألمه أحياناً ثم تظهر الأعراض فجأة - لا تتجاهل أي تغيير في سلوكه بسبب صغر حجمه.')
      ],'en':[
        ('🍽️','Feeding','Two small meals daily (40-60g total depending on weight) — adult Maltese shouldn\'t exceed 3.5kg. Small-kibble or mashed wet food suits the tiny mouth. Avoid rich fatty meals that stress its sensitive liver. Fresh water in a small size-appropriate bowl always.'),
        ('🚫','Avoid','Never lift by hands or arms — thin bones fracture easily. Cold floors and damp surfaces affect the chest. Don\'t let it jump from height — bone fractures are common in very small breeds. Avoid pressing on the chest when holding.'),
        ('💡','Breed tip','Long silky white coat needs 10 min daily combing with a fine comb to prevent tangles. Hair near eyes must be tied or trimmed to prevent chronic irritation. Bathing every 10-14 days with complete drying is essential — skin moisture causes infections.'),
        ('⚠️','Watch out','Hours of continuous barking + shivering despite warm temperature + refusing food = internal pain or acute anxiety. Maltese sometimes hide pain well then symptoms appear suddenly — never dismiss behavioral changes due to its small size.')
      ]},
      'chihuahua': {'ar':[
        ('🍽️','التغذية','وجبتان أو ثلاث صغيرة يومياً إلزامية - الشيواوا عرضة لانخفاض السكر (Hypoglycemia) بين الوجبات الطويلة. كمية ٣٠-٤٠ غرام يومياً كافية لكلب ٢ كغ. حبيبات صغيرة الحجم مخصصة للسلالات الصغيرة. لا تطعمه الطعام الإنساني حتى القطعة الصغيرة لأن كبده لا يتحمله.'),
        ('🚫','تجنّب','لا ترفعه من معصميه أو تجذبه - كتفاه رفيعان يتأذيان بسهولة. البرد يؤثر عليه جداً لأن جسمه الصغير يفقد الحرارة بسرعة. لا تسمح له بالتسلط على الكبار والغرباء لمجرد صغر حجمه - هذا السلوك يجعله قلقاً ومتوتراً دائماً.'),
        ('💡','نصيحة السلالة','ارتجافه الشهير ليس من الطيش بل من البرد أو الخوف - ملابس دافئة ناعمة شتاءً وبيئة هادئة تغير سلوكه كلياً. يحتاج ٢٠-٣٠ دقيقة مشي يومياً - لا تستهين بحاجته للتمرين بسبب صغر حجمه. يعيش ١٤-١٦ سنة مع رعاية جيدة.'),
        ('⚠️','انتبه','ارتجاف مستمر مع ضعف عام وخمول وعدم رغبة في الحركة = انخفاض سكر الدم الطارئ. أعطه فوراً نقطة عسل طبيعي أو شراب سكر على لسانه واذهب للطبيب - هذه حالة تستدعي معالجة خلال ساعات.')
      ],'en':[
        ('🍽️','Feeding','Two to three small meals daily are mandatory — Chihuahuas are prone to hypoglycemia between long meal gaps. About 30-40g daily for a 2kg dog. Small-breed-specific kibble size only. Never feed human food even in tiny amounts — its liver cannot handle it.'),
        ('🚫','Avoid','Never lift by the wrists or pull — thin shoulders injure easily. Cold affects it severely as the small body loses heat fast. Don\'t allow dominance behavior toward adults and strangers just because of its size — this makes it chronically anxious.'),
        ('💡','Breed tip','The famous shivering is from cold or fear, not attitude — soft warm clothing in winter and a calm environment transform its behavior completely. Needs 20-30 min daily walking — don\'t underestimate exercise needs because of size. Lives 14-16 years with good care.'),
        ('⚠️','Watch out','Continuous shivering with general weakness, lethargy, and reluctance to move = emergency hypoglycemia. Give a drop of natural honey or sugar syrup on the tongue immediately, then go to a vet — this needs treatment within hours.')
      ]},
      'shitzu': {'ar':[
        ('🍽️','التغذية','أكل رطب أو جاف ناعم الحبيبات مرتين يومياً (٥٠-٧٠ غرام/وجبة). وجهه المفلطح يجعل التقاط الحبيبات الكبيرة صعباً - استخدم وعاءً مسطحاً واسعاً. أضف قليلاً من ماء دافئ للأكل الجاف لتليينه. ماء نظيف دائماً متوفر وبعيد عن شعر وجهه.'),
        ('🚫','تجنّب','الحر فوق ٢٧ درجة خطر جدي على تنفسه المحدود أصلاً - لا تمشِ معه في الظهيرة. الشمس المباشرة ممنوعة. لا تقص شعره قصيراً جداً صيفاً - الشعر الطويل يعزل الحرارة. تجنّب الأماكن المغبرة والدخان لأن مجرى هوائيه ضيّق.'),
        ('💡','نصيحة السلالة','عيناه البارزة الكبيرة تتراكم حولها إفرازات يومياً - امسحها كل صباح بقطعة قماش مبللة بالماء الدافئ. شعره يحتاج تمشيطاً كل يومين من جذر الشعر لمنع التشابك العميق. قصّه بشكل Lion Cut كل شهرين يسهّل العناية اليومية.'),
        ('⚠️','انتبه','شخير مفاجئ قوي لم يكن موجوداً سابقاً + صعوبة في التنفس عند أقل مجهود + لثّة شاحبة = انسداد مجرى هوائي أو مشكلة حنك. في الصيف تصبح هذه حالة طارئة خطيرة. راجع الطبيب فوراً.')
      ],'en':[
        ('🍽️','Feeding','Soft wet or fine-kibble dry food twice daily (50-70g/meal). The flat face makes picking up large kibble difficult — use a flat wide bowl. Adding a little warm water to dry food softens it. Always clean water available away from facial hair.'),
        ('🚫','Avoid','Heat above 27°C is seriously dangerous for its already-limited breathing — no midday walks. Direct sun is forbidden. Don\'t cut coat too short in summer — longer hair insulates against heat. Avoid dusty spaces and smoke as the airway is narrow.'),
        ('💡','Breed tip','Prominent large eyes accumulate discharge daily — wipe each morning with a warm damp cloth. Coat needs brushing every 2 days from the root to prevent deep matting. A Lion Cut trim every 2 months makes daily care much easier.'),
        ('⚠️','Watch out','Sudden new loud snoring + breathing difficulty at minimal exertion + pale gums = airway obstruction or palate issue. In summer this becomes a serious emergency. See a vet immediately.')
      ]},
      'bulldog': {'ar':[
        ('🍽️','التغذية','وجبتان يومياً من أكل متوازن معتدل الدهون (١٨٪ دهون كحد أقصى) وعالي البروتين. الكمية ٢٠٠-٢٥٠ غرام يومياً للكلب ٢٥ كغ. استخدم وعاء طعام مسطّحاً مرفوعاً قليلاً ليتناول طعامه دون إجهاد رقبته. ماء بارد متوفر طوال اليوم.'),
        ('🚫','تجنّب','التمرين في الجو الحار ممنوع منعاً باتاً - حتى ١٠ دقائق مشي في حر لبنان الصيفي قد تكون قاتلة. ثواني في سيارة مغلقة كافية لضربة شمس. لا تطعمه طعاماً دهنياً. لا تشغّله بألعاب تستلزم الجري - التمشيط اليومي الهادئ كافٍ.'),
        ('💡','نصيحة السلالة','طيات جلده تحتجز الرطوبة والبكتيريا - نظّفها أسبوعياً بمنديل مبلّل بمحلول مطهّر خفيف مخصص للكلاب. ركّز على طيات الوجه وتحت الذيل والمناطق الإبطية. ذيله القصير أحياناً يصعب تنظيفه - لا تهمله.'),
        ('⚠️','انتبه','لهاث شديد مستمر + تلوّن اللسان أو اللثة للأزرق أو الأبيض = ضائقة تنفسية طارئة. أدخله مكيّف فوراً وضع منشفة مبللة بالماء البارد على رقبته واتصل بالطبيب. لا تنتظر أكثر من ١٠ دقائق.')
      ],'en':[
        ('🍽️','Feeding','Two daily meals of balanced low-fat (max 18% fat) high-protein food. About 200-250g daily for a 25kg dog. Use a flat raised bowl for comfortable eating without neck strain. Cold water available all day.'),
        ('🚫','Avoid','Exercise in hot weather is absolutely forbidden — even 10 minutes walking in Lebanon\'s summer heat can be fatal. Seconds in a closed car is enough for heat stroke. No fatty food. No running games — a calm daily short walk is sufficient.'),
        ('💡','Breed tip','Skin folds trap moisture and bacteria — clean weekly with a damp wipe using a mild dog-safe antiseptic. Focus on facial folds, under the tail, and armpit areas. The short screw tail can be hard to reach — don\'t neglect it.'),
        ('⚠️','Watch out','Intense continuous panting + tongue or gums turning blue or white = respiratory emergency. Move to AC immediately, place a cold wet towel on the neck, and call a vet. Don\'t wait more than 10 minutes.')
      ]},
      # birds
      'african_grey': {'ar':[
        ('🍽️','التغذية','كريات غذائية (Pellets) تشكّل ٦٠٪ من وجبته اليومية + خضار طازجة ٣٠٪ (جزر، سبانخ، فلفل أحمر، بروكلي) + بذور ١٠٪ فقط كمكافأة. أضف نصف بيضة مسلوقة أسبوعياً لتعويض الكالسيوم والبروتين. الطعام المتنوع الألوان يحفّزه على الأكل - قدّم ألواناً مختلفة في كل وجبة. ماء نظيف يُغيّر مرتين يومياً.'),
        ('🚫','تجنّب','نظام البذور الحصري = نقص فيتامين A وكالسيوم يسبب أمراضاً مزمنة في سنوات. أبخرة الطبخ (خاصة مقلاة التيفال) سامة جداً وقاتلة للطيور - لا يكون قفصه قريباً من المطبخ. ممنوع: أفوكادو، شوكولا، كافيين، بصل، ثوم، ملح. لا ترشّ أي بخاخ أو معطر في الغرفة.'),
        ('💡','نصيحة السلالة','ذكاؤه يعادل طفل ٥ سنوات - يحتاج ألعاب تفكيك جديدة كل أسبوع أو سيملّ وينتف ريشه. وزّع طعامه في أماكن مختلفة داخل القفص ليبحث عنه (Foraging) بدل وضعه في وعاء فقط - هذا يشغله ساعات. خصّص ٢-٣ ساعات تفاعل مباشر يومياً معه.'),
        ('⚠️','انتبه','نتف الريش الذاتي بشكل متكرر في مناطق معينة = إما ضيق نفسي حاد أو التهاب جلدي. لا تتجاهله ظناً أنه عادة - هو يؤذي نفسه فعلاً. استشر طبيب طيور متخصص فوراً لأن الحالة تتفاقم إن تُركت بدون تدخل.')
      ],'en':[
        ('🍽️','Feeding','Pellets make up 60% of the daily diet + 30% fresh vegetables (carrots, spinach, red pepper, broccoli) + 10% seeds as treats only. Add half a boiled egg weekly for calcium and protein. Colorful varied food motivates eating — offer different colors each meal. Fresh water changed twice daily.'),
        ('🚫','Avoid','Seed-only diet = vitamin A and calcium deficiency causing chronic disease over years. Cooking fumes (especially Teflon pans) are deadly to birds — keep the cage away from the kitchen. Forbidden: avocado, chocolate, caffeine, onion, garlic, salt. Never spray aerosols or fragrances in the room.'),
        ('💡','Breed tip','Intelligence equivalent to a 5-year-old child — needs new destructible toys every week or it will self-pluck out of boredom. Distribute food in different spots inside the cage (foraging) instead of just one bowl — this keeps it occupied for hours. Dedicate 2-3 hours of direct daily interaction.'),
        ('⚠️','Watch out','Repeated self-feather plucking in specific areas = severe psychological distress OR skin inflammation. Never dismiss it as a habit — it is genuinely self-harming. See a specialist bird vet immediately as the condition worsens without intervention.')
      ]},
      'cockatiel': {'ar':[
        ('🍽️','التغذية','كريات غذائية ٥٠٪ + بذور متنوعة ٣٠٪ + خضار طازجة ٢٠٪ (جزر مبشور، سبانخ، بروكلي) يومياً. الجزر المبشور مفضّل لأنه غني بفيتامين A الضروري جداً للكوكاتيل. قدّم بذور الدوّار (عباد الشمس) بكميات محدودة كمكافأة فقط - دهنية جداً. ماء نظيف يومياً.'),
        ('🚫','تجنّب','وضع القفص قرب المطبخ ممنوع - أبخرة الطبخ والبخاخات والعطور والشموع المعطرة سامة جداً. لا تعرّضه لتيارات هواء بارد (مكيّف يصبّ عليه مباشرة). تجنّب الضجيج المفاجئ ليلاً - الكوكاتيل يعاني من Night Frights أكثر من أي طائر آخر.'),
        ('💡','نصيحة السلالة','Night Frights تحدث عندما يستيقظ مذعوراً في الظلام ويصطدم بجدران القفص - ترك ضوء ليلي خافت جداً في الغرفة يمنعها. غطّ ثلاثة جهات من القفص بقماش مسامٍ ليلاً ليشعر بالأمان. هو اجتماعي ويستمتع بصحبتك - خصّص له ١٥ دقيقة يومياً على الأقل.'),
        ('⚠️','انتبه','اصطدام بجدران القفص ليلاً + صراخ مذعور عند الاستيقاظ + ريش منتفش صباحاً = إجهاد شديد من Night Frights. إذا تكررت ليلتين = عدّل الإضاءة الليلية فوراً. ريش مستمر الانتفاش نهاراً مع خمول = مرض يستدعي الطبيب.')
      ],'en':[
        ('🍽️','Feeding','50% pellets + 30% mixed seeds + 20% fresh vegetables (grated carrot, spinach, broccoli) daily. Grated carrot is preferred as it\'s rich in vitamin A essential for cockatiels. Sunflower seeds are a treat only — very fatty. Fresh water daily.'),
        ('🚫','Avoid','Cage near the kitchen is forbidden — cooking fumes, sprays, fragrances and scented candles are highly toxic. Don\'t expose to cold air drafts from AC blowing directly. Avoid sudden loud noises at night — cockatiels suffer Night Frights more than any other bird.'),
        ('💡','Breed tip','Night Frights happen when it wakes terrified in darkness and crashes into cage walls — a very dim night light in the room prevents this. Cover three sides of the cage with breathable fabric at night for security. It is social and enjoys your company — give at least 15 min daily.'),
        ('⚠️','Watch out','Crashing into cage walls at night + terrified screaming on waking + puffed feathers in the morning = severe Night Frights stress. Recurring two nights in a row = fix the night lighting immediately. Continuously puffed feathers during the day with lethargy = illness requiring a vet.')
      ]},
      'lovebird': {'ar':[
        ('🍽️','التغذية','بذور متنوعة ٤٠٪ + كريات غذائية ٤٠٪ + خضار طازجة ٢٠٪ يومياً (جزر، سبانخ، فلفل). حجر الكالسيوم (Cuttlebone) دائماً متوفر داخل القفص - ضروري لصحة عظامه ومنقاره. قدّم فاكهة طازجة كالتفاح والإجاص مرة أسبوعياً كمكافأة. ماء نظيف يُغيّر يومياً.'),
        ('🚫','تجنّب','احتجازه وحيداً = اكتئاب حاد يظهر بصمت ونتف ريش. القفص الضيق الذي لا يستطيع فيه فرد جناحيه يسبب ضموراً عضلياً. أبخرة الطبخ والبخاخات سامة. لا تضعه قرب نافذة بدون حماية - يخاف من الطيور الكبيرة التي تراها.'),
        ('💡','نصيحة السلالة','يحتاج إما رفيقاً متوافقاً من نوعه (تقديم تدريجي على أسبوعين) أو تفاعلاً يومياً مكثفاً معك ٣٠+ دقيقة. قفص طيران واسع (٦٠×٦٠×٩٠ سم كحد أدنى للزوج) يسمح له بالطيران الحقيقي. يحب المرايا والألعاب الصغيرة الملونة داخل القفص.'),
        ('⚠️','انتبه','أنثى تجلس في زاوية القفص + انتفاش مستمر + ثقل ملحوظ في البطن + توقف عن الحركة = احتباس بيض (Egg Binding) حالة طارئة خطيرة جداً. ضعها في مكان دافئ وهادئ واذهب لطبيب طيور فوراً - التأخر يمكن أن يكون قاتلاً.')
      ],'en':[
        ('🍽️','Feeding','40% mixed seeds + 40% pellets + 20% fresh vegetables daily (carrot, spinach, pepper). Cuttlebone always available in the cage — essential for bone and beak health. Fresh fruit like apple or pear once weekly as a treat. Fresh water changed daily.'),
        ('🚫','Avoid','Solitary confinement = severe depression shown by silence and feather plucking. A cage too small to spread wings causes muscle atrophy. Cooking fumes and sprays are toxic. Don\'t place near an unprotected window — it fears large birds it can see outside.'),
        ('💡','Breed tip','Needs either a compatible companion (introduced gradually over 2 weeks) or intensive daily interaction with you 30+ min. A spacious flight cage (min 60×60×90cm for a pair) allows real flying. Enjoys mirrors and small colorful toys inside the cage.'),
        ('⚠️','Watch out','Female sitting in cage corner + continuous puffing + noticeably heavy abdomen + stopped moving = Egg Binding, a very serious emergency. Place in a warm quiet spot and go to a bird vet immediately — delay can be fatal.')
      ]},
      'budgie': {'ar':[
        ('🍽️','التغذية','بذور بادجي متنوعة ٥٠٪ + كريات غذائية ٣٠٪ + خضار طازجة ٢٠٪ يومياً. خضار مفيدة: جزر مبشور، سبانخ، بروكلي، فلفل. حجر الكالسيوم دائماً داخل القفص. أعصان خشبية طبيعية بسماكات مختلفة (ليمون، سدر) ضرورية لصحة قدميه ومنعقاره من الاستطالة.'),
        ('🚫','تجنّب','قلل الإضاءة لـ١٠ ساعات فقط يومياً - الإضاءة الزائدة تنشّط هرموناته وتجعله عدوانياً ومحبطاً. أزل أي مكان مظلم مغلق داخل القفص لأنه يتخذه كعش ويزيد نشاطه الهرموني. لا تداعب ريشه على الجسم - داعبة الرأس فقط.'),
        ('💡','نصيحة السلالة','اشتري بادجي منذ صغره (٦-٨ أسابيع) وعلّمه من البداية - يتعلم الكلام والتفاعل بشكل مذهل. البادجي يعيش ٨-١٢ سنة مع رعاية جيدة. يحب الموسيقى الهادئة والتلفزيون في الخلفية - يرطرط معها. وفّر له مرآة ورنين صغير في القفص.'),
        ('⚠️','انتبه','ذيل يتأرجح بوضوح عند كل شهيق وزفير + صوت تنفس مسموع أو صفير + خمول = مشكلة تنفسية أو التهاب هوائي. لا تنتظر - الطيور الصغيرة تتدهور بسرعة. طبيب طيور متخصص اليوم لأن العلاج المبكر ينقذ.')
      ],'en':[
        ('🍽️','Feeding','50% mixed budgie seeds + 30% pellets + 20% fresh vegetables daily. Beneficial veggies: grated carrot, spinach, broccoli, pepper. Cuttlebone always in the cage. Natural wooden perches of varying thickness (citrus, lotus wood) are essential for healthy feet and preventing beak overgrowth.'),
        ('🚫','Avoid','Limit lighting to 10 hours daily — excessive light activates hormones making it aggressive and frustrated. Remove any dark enclosed space inside the cage — it uses it as a nest and increases hormonal activity. Only stroke the head — never pet body feathers.'),
        ('💡','Breed tip','Get a budgie young (6-8 weeks) and train from the start — it learns speech and interaction remarkably well. Budgies live 8-12 years with good care. Enjoys soft music and TV in the background — chirps along with it. Provide a mirror and small bells in the cage.'),
        ('⚠️','Watch out','Tail clearly bobbing with every breath + audible breathing sound or wheezing + lethargy = respiratory problem or air-sac infection. Don\'t wait — small birds deteriorate rapidly. See a specialist bird vet today as early treatment saves lives.')
      ]},
      'canary': {'ar':[
        ('🍽️','التغذية','بذور كناري متنوعة ٦٠٪ (دخن، كتان، حبة سوداء) + خضار طازجة ٢٠٪ + فاكهة ٢٠٪ (تفاح، إجاص، توت) يومياً. أضف بيضة مسلوقة مهروسة صغيرة أسبوعياً خاصةً في موسم التعشيش والانسلاخ. ماء نظيف يُغيّر كل صباح بدون استثناء. حجر كالسيوم داخل القفص دائماً.'),
        ('🚫','تجنّب','لا تضعه قرب طيور كبيرة أو قطط حتى من وراء زجاج - الضغط البصري المستمر يوقف غناءه تماماً ويجعله متوتراً. الموسيقى الصاخبة تربكه. تيارات الهواء الباردة تمرّضه. أبخرة الطبخ والدخان ممنوعة.'),
        ('💡','نصيحة السلالة','قفص أفقي واسع (لا أقل من ٦٠سم طولاً) يشجعه على الطيران ذهاباً وإياباً مما يحسّن غناءه. مرآة صغيرة داخل القفص ترفع معنوياته لأنه يعتقد أن هناك كناري آخر. الذكر يغني فقط - إذا صمت بشكل مفاجئ فهذه إشارة تحذير.'),
        ('⚠️','انتبه','كناري صامت فجأة بعد غناء منتظم + ريش منتفش باستمرار + نوم مفرط على العصا = مرض حاد. الكناري يخفي مرضه بشكل استثنائي حتى اللحظة الأخيرة (غريزة الفريسة) - عندما تظهر الأعراض يكون الوضع متأخراً. طبيب طيور اليوم.')
      ],'en':[
        ('🍽️','Feeding','60% mixed canary seeds (millet, flaxseed, nigella) + 20% fresh vegetables + 20% fruit (apple, pear, berries) daily. Add a small mashed boiled egg weekly especially during nesting and molting seasons. Fresh water changed every morning without exception. Cuttlebone always in the cage.'),
        ('🚫','Avoid','Don\'t place near large birds or cats even through glass — constant visual stress completely silences it and causes chronic anxiety. Loud music disturbs it. Cold air drafts cause illness. Cooking fumes and smoke are forbidden.'),
        ('💡','Breed tip','A wide horizontal cage (minimum 60cm length) encourages back-and-forth flight which improves singing quality. A small mirror inside the cage lifts its mood as it believes another canary is present. Only males sing — sudden silence after regular singing is a warning sign.'),
        ('⚠️','Watch out','A canary suddenly silent after regular singing + continuously puffed feathers + excessive sleeping on the perch = acute illness. Canaries hide illness exceptionally well until the very last moment (prey-animal instinct) — by the time symptoms appear the situation is already serious. See a bird vet today.')
      ]},
      'quaker': {'ar':[
        ('🍽️','التغذية','كريات غذائية ٥٠٪ + خضار متنوعة طازجة ٣٠٪ (جزر، ذرة، بازلاء، بروكلي) + بذور ٢٠٪ يومياً. يحب ألوان الطعام الزاهية - قدّم تشكيلة ملونة في كل وجبة. يحب الأرز المسلوق والخبز الأسمر كمكافأة أسبوعية. ماء نظيف يومياً وحمام ماء أسبوعي يحبه كثيراً.'),
        ('🚫','تجنّب','تغيير ترتيب القفص أو الأثاث المحيط به فجأة يسبب قلقاً حاداً - هو طائر يحب الاستقرار. لا تغيّر مكانه في البيت دون تدرّج. أبخرة الطبخ والبخاخات ممنوعة. تجنّب ترك لعبة يهتم بها كثيراً بدون تبديل - الارتباط الزائد بلعبة يسبب له اضطراباً.'),
        ('💡','نصيحة السلالة','يحب بناء الأعشاش بشكل غريزي - وفّر له قش وأوراق ورق آمنة يبني بها داخل القفص. يتعلم الكلام والألحان بسرعة مذهلة مع التكرار اليومي. اجتماعي جداً ويتعلق بصاحبه - التفاعل اليومي المنتظم يجعله طائراً استثنائياً.'),
        ('⚠️','انتبه','نتف ريش الصدر أو الأجنحة الذاتي بشكل متكرر = الطائر في ضيق نفسي من بيئة غير مستقرة أو تغييرات مفاجئة. أعد النظر في روتينه اليومي وبيئته المحيطة قبل أي شيء آخر. إذا استمر أسبوعاً - طبيب طيور متخصص.')
      ],'en':[
        ('🍽️','Feeding','50% pellets + 30% varied fresh vegetables (carrot, corn, peas, broccoli) + 20% seeds daily. Loves colorful food — offer a colorful assortment each meal. Enjoys cooked rice and whole grain bread as weekly treats. Fresh water daily and a weekly water bath it enjoys greatly.'),
        ('🚫','Avoid','Suddenly rearranging the cage or surrounding furniture causes acute anxiety — this is a stability-loving bird. Don\'t change its home location without gradual transition. Cooking fumes and sprays are forbidden. Avoid letting it over-attach to one toy — excessive bonding with objects causes behavioral disorders.'),
        ('💡','Breed tip','Instinctively loves nest-building — provide safe straw and paper strips to build with inside the cage. Learns speech and melodies remarkably fast with daily repetition. Very social and bonds deeply with its owner — regular daily interaction makes it an exceptional companion.'),
        ('⚠️','Watch out','Repeated self-plucking of chest or wing feathers = psychological distress from an unstable environment or sudden changes. Reassess its daily routine and surroundings before anything else. If it continues for a week — see a specialist bird vet.')
      ]},
      # small pets
      'rabbit': {'ar':[
        ('🍽️','التغذية','قش تيموثي بكميات غير محدودة طوال اليوم - يجب أن يكون ٧٠٪ من غذائه. أضف كوباً من الخضار الورقية الداكنة يومياً (سبانخ، بقدونس، كزبرة، خضار الهندباء) + ملعقتان كبيرتان حبوب أرنب مخصصة. الفاكهة مكافأة أسبوعية صغيرة فقط (قطعة تفاح بدون بذور). ماء نظيف في وعاء ثقيل لا يُقلب يومياً.'),
        ('🚫','تجنّب','ممنوع تماماً: خس جبي (سام)، حمضيات، نشويات، بطاطا، بذور، سكريات مصنوعة. أسلاك الكهرباء والخشب والأثاث في خطر دائم من أسنانه المتنامية - احمها. الأرانب لا تتقيأ لذا إذا أكل شيئاً ضاراً يكون الخطر مضاعفاً.'),
        ('💡','نصيحة السلالة','أسنان الأرنب تنمو ٢-٣ مم أسبوعياً بشكل مستمر مدى الحياة - بدون قش كافٍ تطول وتتشوه وتؤلمه ألماً شديداً يمنعه من الأكل. القش ليس ترفاً بل عملية طحن ضرورية. وفّر له مساحة جري يومياً خارج القفص ساعتين على الأقل.'),
        ('⚠️','انتبه','توقف كامل عن أكل القش والطعام لـ١٢ ساعة = GI Stasis (توقف الجهاز الهضمي) حالة طارئة تهدد الحياة في ٢٤-٤٨ ساعة. لا تنتظر صباح الغد - هذا الليلة. جهازه الهضمي يجب أن يتحرك باستمرار وأي توقف هو طوارئ.')
      ],'en':[
        ('🍽️','Feeding','Unlimited Timothy hay all day — must make up 70% of the diet. Add one cup of dark leafy greens daily (spinach, parsley, cilantro, dandelion greens) + 2 tablespoons species-specific pellets. Fruit is a small weekly treat only (apple slice without seeds). Clean water in a heavy non-tipping bowl, changed daily.'),
        ('🚫','Avoid','Absolutely forbidden: iceberg lettuce (toxic), citrus, starchy foods, potatoes, seeds, processed sugars. Electrical cords, wood, and furniture are always at risk from constantly growing teeth — protect them. Rabbits cannot vomit so if it eats something harmful the danger doubles.'),
        ('💡','Breed tip','Rabbit teeth grow 2-3mm weekly continuously for life — without enough hay they elongate and deform, causing severe pain that prevents eating. Hay isn\'t optional — it\'s the grinding mechanism that prevents dental disease. Provide at least 2 hours daily free-run time outside the cage.'),
        ('⚠️','Watch out','Complete refusal to eat hay or food for 12 hours = GI Stasis (digestive system shutdown), a life-threatening emergency within 24-48 hours. Don\'t wait until morning — go tonight. The digestive system must move continuously and any stoppage is an emergency.')
      ]},
      'guinea_pig': {'ar':[
        ('🍽️','التغذية','قش تيموثي بكميات غير محدودة + كوب خضار غنية بفيتامين C يومياً (فلفل أحمر هو الأغنى، بقدونس، كرفس، كيوي). الخيار والخس الروماني مقبولان ومحببان. حبوب خنزير غينيا مخصصة ملعقتان صغيرتان يومياً. ممنوع اعتماد الحبوب كمصدر فيتامين C لأنها تتحلل بسرعة بعد الفتح.'),
        ('🚫','تجنّب','ممنوع: خس جبي، بطاطا، بصل، ثوم، نباتات الفلانتيلا، فاكهة بكميات كبيرة. لا تتركه وحيداً دون رفيق - الوحدة تسبب ضغطاً نفسياً حاداً وأمراضاً جسدية. لا تمسكه من أعلى كالمفترس - امسكه من الجانبين بكلتا يديك.'),
        ('💡','نصيحة السلالة','يعيش بشكل أفضل وأسعد مع رفيق أو أكثر من نفس الجنس. صريره العالي المستمر (Wheek) = سعادة وطلب انتباه. تجمّده المفاجئ التام = خوف شديد أو خطر. ناعم جداً ومناسب للأطفال - يؤنس كثيراً مع التعامل اليومي اللطيف.'),
        ('⚠️','انتبه','فقدان شهية ملحوظ + خمول وعدم تحرك + توقف عن التبرز لأكثر من ٦ ساعات = انسداد هضمي طارئ. أيضاً إذا لاحظت شعثاً في فراؤه أو خدوشاً - قد يكون يتعرض لعدوان من رفيقه. طبيب متخصص خلال ٤٨ ساعة.')
      ],'en':[
        ('🍽️','Feeding','Unlimited Timothy hay + one cup vitamin C-rich vegetables daily (red pepper is richest, parsley, celery, kiwi). Cucumber and romaine lettuce are accepted and liked. 2 teaspoons species-specific pellets daily. Never rely on pellets as a vitamin C source — it degrades quickly after opening the bag.'),
        ('🚫','Avoid','Forbidden: iceberg lettuce, potatoes, onion, garlic, avocado, large amounts of fruit. Never leave alone without a companion — solitude causes acute psychological stress and physical illness. Don\'t grab from above like a predator — support from both sides with both hands.'),
        ('💡','Breed tip','Lives happier and healthier with one or more same-sex companions. Loud continuous wheek = happiness and seeking attention. Sudden complete freeze = intense fear or perceived danger. Very gentle and suitable for children — becomes very affectionate with daily gentle handling.'),
        ('⚠️','Watch out','Noticeable loss of appetite + lethargy and not moving + no droppings for more than 6 hours = digestive obstruction emergency. Also if you notice ruffled coat or scratches — it may be receiving aggression from its companion. Specialist vet within 48 hours.')
      ]},
      'hamster': {'ar':[
        ('🍽️','التغذية','حبوب هامستر جاهزة متنوعة + كميات صغيرة جداً من الخضار الطازجة ليلاً فقط (جزرة صغيرة، قطعة خيار، ورقة سبانخ). حجم الوجبة الكاملة يومياً لا يتجاوز ملعقة كبيرة واحدة. يخزّن طعامه في مخبأ - لا تزيل مخزونه ولا تزعجه. ماء نظيف في زجاجة تقطير يومياً.'),
        ('🚫','تجنّب','لا تضعه مع هامستر آخر أبداً تحت أي ظرف - يتقاتلان حتى الموت حتى لو كانا أخوين. لا توقظه أثناء النوم النهاري قسراً - نظامه الليلي جزء من صحته. الحرارة فوق ٢٨ درجة تسبب له بيات شتوي اصطناعي (Torpor) خطير.'),
        ('💡','نصيحة السلالة','الحوض الزجاجي الواسع (٨٠×٤٠ سم كحد أدنى) بتراب خاص بعمق ٢٠-٣٠ سم ضروري لغريزة الحفر. عجلة الجري يجب أن تكون واسعة (٢٨سم+ للسوري) ومغلقة الجوانب لمنع التواء عموده الفقري. يمكن تدريبه على يدك بصبر وتكرار يومي لطيف.'),
        ('⚠️','انتبه','رطوبة أو إسهال في المنطقة الخلفية + ضعف وتراخٍ واضح + توقف عن الأكل = Wet Tail مرض بكتيري خطير جداً يقتل في ٤٨-٧٢ ساعة. طبيب الليلة بدون تأجيل. أيضاً إذا وجدته في وضع تجمّد كالنوم الثقيل مع تنفس بطيء = Torpor من البرد أو الإجهاد.')
      ],'en':[
        ('🍽️','Feeding','Varied hamster pellet mix + very small amounts of fresh vegetables at night only (small carrot, cucumber piece, spinach leaf). Total daily food amount: no more than one tablespoon. It hoards food in a hidden cache — never remove the stash or disturb it. Fresh water in a drip bottle changed daily.'),
        ('🚫','Avoid','Never house with another hamster under any circumstance — they fight to the death even as siblings. Never forcibly wake during daytime sleep — its nocturnal schedule is part of its health. Temperatures above 28°C trigger dangerous artificial hibernation (Torpor).'),
        ('💡','Breed tip','Wide glass tank (min 80×40cm) with 20-30cm deep specialized substrate is essential for instinctive burrowing. Running wheel must be large (28cm+ for Syrian) and solid-sided to prevent spinal twisting. Can be tamed to accept your hand with daily patient gentle handling over several weeks.'),
        ('⚠️','Watch out','Wetness or diarrhea around hindquarters + visible weakness and limpness + refusing food = Wet Tail bacterial disease, kills in 48-72 hours. Vet tonight without delay. Also if you find it frozen in deep sleep-like state with slow breathing = Torpor from cold or stress — warm it up slowly.')
      ]},
      'chinchilla': {'ar':[
        ('🍽️','التغذية','قش تيموثي بكميات غير محدودة + ملعقة كبيرة حبوب شينشيلا مخصصة يومياً. الخضار الجافة (بقدونس مجفف، أوراق عشبة) بكميات صغيرة جداً كمكافأة أسبوعية. الأكل الرطب والفاكهة الطازجة ممنوعان تقريباً - معدته حساسة جداً للرطوبة. ماء نظيف في زجاجة تقطير يومياً.'),
        ('🚫','تجنّب','الحرارة فوق ٢٥ درجة خطر وفاة - لبنان الصيف قاتل لهذا الحيوان بدون مكيّف. الرطوبة العالية أيضاً مميتة. ممنوع تماماً الاستحمام بالماء - يعفن فراؤه الكثيف ويمرض. لا تتركه يقفز من ارتفاعات عالية - كسر الأطراف شائع.'),
        ('💡','نصيحة السلالة','حمام رمل جاف مخصص للشينشيلا (Sand Bath) ثلاث مرات أسبوعياً ١٠-١٥ دقيقة كل مرة ينظف فراؤه الخارق الكثافة. الرمل المخصص فقط - لا رمل بناء أو رمل عادي. نشيط جداً ليلاً ويحتاج قفصاً متعدد الطوابق للقفز والتسلق. يعيش ١٠-١٥ سنة.'),
        ('⚠️','انتبه','لهاث سريع مستمر + تمدد على الأرض رافضاً الحركة + أذنان حمراوان ساخنتان في الجو الحار = ضربة شمس طارئة. أدخله مكيّف فوراً وضع قطعة رخام باردة (مُبرّدة مسبقاً) في القفص وبلّل أذنيه فقط (لا جسمه) بماء بارد. طبيب فوراً.')
      ],'en':[
        ('🍽️','Feeding','Unlimited Timothy hay + one tablespoon species-specific chinchilla pellets daily. Dried vegetables (dried parsley, herb leaves) in very small amounts as a weekly treat. Wet food and fresh fruit are almost forbidden — its digestive system is extremely sensitive to moisture. Fresh water in drip bottle daily.'),
        ('🚫','Avoid','Temperatures above 25°C are lethal — Lebanon\'s summer is deadly for this animal without AC. High humidity is equally fatal. Never bathe with water — its extremely dense coat rots and causes illness. Don\'t let it jump from height — limb fractures are common.'),
        ('💡','Breed tip','Dedicated dry sand bath (chinchilla-specific sand only) three times weekly 10-15 minutes each cleans its extraordinarily dense coat. Only designated sand — never building sand or regular sand. Very active at night and needs a multi-level cage for jumping and climbing. Lives 10-15 years.'),
        ('⚠️','Watch out','Rapid continuous panting + lying flat refusing to move + hot red ears in warm weather = heat stroke emergency. Move to AC immediately, place a pre-cooled marble tile in the cage, and wet only its ears (not its body) with cool water. Vet immediately.')
      ]},
      # fish
      'betta': {'ar':[
        ('🍽️','التغذية','٤-٥ حبات حبوب بيتا مخصصة مرة واحدة يومياً - لا أكثر. يومان صيام كاملان أسبوعياً (مثلاً الأربعاء والجمعة) ضروريان لصحة هضمه ومنع انتفاخ المعدة. أحياناً قدّم طعاماً مجمداً حياً (دفنيا أو آرتيميا) كمكافأة أسبوعية لأنه أكثر غذائية وأقرب لطعامه الطبيعي. لا تضع أكثر مما يأكله في دقيقتين.'),
        ('🚫','تجنّب','لا تضعه مع أي بيتا ذكر آخر أبداً - القتال يبدأ في ثوانٍ. تجنّب الديكورات البلاستيكية ذات الحواف الحادة أو الصخور الخشنة - زعانفه رقيقة وتتمزق. لا تضعه في كأس أو وعاء صغير - هذا يسبب ضغطاً وتلوثاً مستمراً. تجنّب الماء البارد تحت ٢٤ درجة.'),
        ('💡','نصيحة السلالة','حوض ١٥-٢٠ لتراً الحجم المثالي مع مرشّح هادئ التدفق (تدفق قوي يتعبه). نباتات طبيعية عريضة الأوراق (Anubias, Java Fern) يستريح عليها قرب السطح ضرورية - في الطبيعة يستريح على أوراق قرب سطح الماء. بيتا يتعرف على صاحبه ويتفاعل معه - اجعل التواصل يومياً.'),
        ('⚠️','انتبه','سباحة جانبية مائلة أو صعوبة في الغوص/الطفو = اضطراب كيس العوم. أوقف الأكل ٣ أيام كاملة ثم قدّم حبة بازلاء مسلوقة مقشّرة صغيرة أو دفنيا حية. إذا لم يتحسن خلال أسبوع - حوض عزل وتشخيص أعمق.')
      ],'en':[
        ('🍽️','Feeding','4-5 species-specific betta pellets once daily — no more. Two complete fasting days weekly (e.g. Wednesday and Friday) are essential for digestive health and preventing bloat. Occasionally offer frozen live food (Daphnia or Artemia) as a weekly treat — more nutritious and closer to natural prey. Never put in more than what it eats in 2 minutes.'),
        ('🚫','Avoid','Never house with any other male betta — fighting begins within seconds. Avoid plastic decorations with sharp edges or rough rocks — fins are delicate and tear easily. Never keep in a cup or tiny bowl — causes chronic stress and pollution. Avoid water below 24°C.'),
        ('💡','Breed tip','A 15-20 liter tank is the ideal size with a gentle low-flow filter (strong current exhausts it). Wide-leaf live plants (Anubias, Java Fern) near the surface for resting are essential — in nature it rests on leaves near the surface. Bettas recognize their owner and interact — make daily contact part of the routine.'),
        ('⚠️','Watch out','Tilted sideways swimming or difficulty diving/floating = swim bladder disorder. Stop feeding for 3 full days then offer one small skinned boiled pea or live Daphnia. If no improvement within a week — quarantine tank and deeper diagnosis.')
      ]},
      'goldfish': {'ar':[
        ('🍽️','التغذية','حبوب جولدفيش غارقة (Sinking Pellets) مرتين يومياً بكمية تُأكل خلال دقيقة واحدة بالضبط. استخدم الحبوب الغارقة لا الطافية - ابتلاع الهواء مع الطعام الطافي يسبب اضطراب عوم مزمن. أضف خضاراً مسلوقة (بازلاء مقشورة، خيار) مرة أسبوعياً كتنويع صحي. ماء مُزيل كلور دائماً.'),
        ('🚫','تجنّب','الإفراط في الطعام هو أكثر أسباب موت السمك الذهبي شيوعاً - يلوث الماء بسرعة ويسبب تسمم الأمونيا. لا تضعها في حوض صغير - كل سمكة تحتاج ٣٠-٤٠ لتر وليس ١٠. لا تضع سمك ذهبي مع أسماك حارة المياه. تجنّب الأكل الحي بدون عزل مسبق.'),
        ('💡','نصيحة السلالة','فلتر قوي مزدوج ضروري لأن السمك الذهبي من أكثر الأسماك إنتاجاً للفضالت. غيّر ٣٠٪ الماء أسبوعياً بماء مُزيل كلور أُضيف قبل ٢٤ ساعة على الأقل. فحص الأمونيا والنترات أسبوعياً - الأمونيا يجب أن تكون صفر دائماً.'),
        ('⚠️','انتبه','لهاث مستمر عند سطح الماء + أو احمرار الزعانف أو جسم السمكة = أمونيا أو نترات مرتفعة. غيّر ٣٠٪ الماء فوراً، أوقف الأكل يومين، وافحص مستويات الكيمياء. إذا استمر اللهاث بعد تغيير الماء - فلتر لا يعمل بكفاءة.')
      ],'en':[
        ('🍽️','Feeding','Sinking goldfish pellets twice daily — only as much as eaten in exactly one minute. Use sinking pellets, not floating — swallowing air with surface food causes chronic float disorder. Add boiled vegetables (skinned peas, cucumber) once weekly as healthy variety. Always dechlorinated water.'),
        ('🚫','Avoid','Overfeeding is the most common cause of goldfish death — pollutes water rapidly and causes ammonia poisoning. Never use a small tank — each fish needs 30-40 liters, not 10. Never mix with tropical fish. Avoid live food without prior quarantine.'),
        ('💡','Breed tip','A powerful dual filter is essential as goldfish are among the heaviest waste-producing fish. Change 30% of water weekly with water dechlorinated at least 24 hours in advance. Test ammonia and nitrates weekly — ammonia must always read zero.'),
        ('⚠️','Watch out','Continuous gasping at the water surface + OR redness on fins or body = elevated ammonia or nitrates. Change 30% water immediately, stop feeding for 2 days, and test water chemistry. If gasping continues after water change — the filter is not working efficiently.')
      ]},
      'cichlid': {'ar':[
        ('🍽️','التغذية','حبوب سيكليد مخصصة مرتين يومياً بكمية تُأكل في ٢-٣ دقائق. السيكليد النباتي (مثل Mbuna) يحتاج حبوباً نباتية وأعشاب طازجة. السيكليد اللاحم (مثل Oscars) يقبل فريسة حية أو مجمدة أسبوعياً. تحقق من نوعه قبل اختيار الطعام لأن الفرق كبير.'),
        ('🚫','تجنّب','لا تجمع سيكليد كبير مع أسماك صغيرة هادئة - ستختفي خلال ليلة. لا تضف سمكة جديدة للحوض مباشرة - عزل أسبوعين في حوض منفصل يمنع انتقال الأمراض. لا تضع سيكليد من مناطق مختلفة معاً إذ أن توافقها معقد.'),
        ('💡','نصيحة السلالة','عند إضافة سمكة جديدة للحوض، أعد ترتيب كل الصخور والكهوف قبلها - هذا يكسر الأقاليم القائمة ويعطي الجميع فرصة متساوية. وفّر كهوفاً ومخابئ كافية (عدد أكبر من عدد الأسماك بواحد). حوض كبير يقلل العدوانية بشكل كبير.'),
        ('⚠️','انتبه','مطاردة مستمرة لسمكة واحدة + زعانفها ممزقة + تختبئ باستمرار = عدوانية مفرطة ستؤدي لوفاتها. افصل المهاجم بفاصل شبكي أسبوعاً أو أضف ١٠-١٥ سمكة صغيرة لتشتت العدوانية. الحل الدائم قد يكون حوض أكبر.')
      ],'en':[
        ('🍽️','Feeding','Cichlid-specific pellets twice daily in amounts eaten within 2-3 minutes. Herbivorous cichlids (like Mbuna) need plant-based pellets and fresh algae. Carnivorous cichlids (like Oscars) accept live or frozen prey weekly. Verify your species before choosing food — the difference is significant.'),
        ('🚫','Avoid','Never mix a large cichlid with small peaceful fish — they will disappear overnight. Never add a new fish directly to the tank — 2-week quarantine in a separate tank prevents disease transfer. Don\'t mix cichlids from different geographic regions as compatibility is complex.'),
        ('💡','Breed tip','When adding a new fish, rearrange all rocks and caves first — this breaks existing territories and gives everyone an equal start. Provide more hiding spots than fish count. A larger tank significantly reduces aggression.'),
        ('⚠️','Watch out','Continuous chasing of one specific fish + shredded fins + constant hiding = severe aggression that will lead to its death. Separate the aggressor with a mesh divider for a week, or add 10-15 dither fish to distribute aggression. The permanent solution may be a larger tank.')
      ]},
      'guppy': {'ar':[
        ('🍽️','التغذية','رقائق جوبي صغيرة مرتين يومياً بكميات ضئيلة جداً (ما يأكلونه في دقيقتين). أضف طعاماً مجمداً حياً (دفنيا أو آرتيميا مجمدة) مرتين أسبوعياً - يحسّن ألوانهم بشكل ملحوظ ويرفع مناعتهم. الإناث الحوامل تحتاج تغذية أفضل قليلاً. ماء نظيف دائماً.'),
        ('🚫','تجنّب','ماء بارد تحت ٢٢ درجة يبطئ مناعتهم ويجعلهم عرضة لأمراض الجلد والطفيليات. لا تضعهم مع أسماك كبيرة أو عدوانية (بيتا ذكر، سيكليد). لا تحتجز ذكراً واحداً مع إناث كثيرات - سيُرهقهن باستمرار.'),
        ('💡','نصيحة السلالة','يعيشون بشكل أفضل وأجمل في مجموعات ٨-١٢ فرداً في حوض ٤٠+ لتر. نباتات كثيفة (Guppy Grass, Java Moss) توفر مخابئ للأسماك الصغيرة والإناث الحوامل. تغيير ٢٠-٢٥٪ الماء أسبوعياً يحافظ على ألوانهم الزاهية وصحتهم العامة.'),
        ('⚠️','انتبه','شحوب ألوان تدريجي + تقوّس عمودي للجسم (كـ"C" أو "S") + سباحة بطيئة بالقاع = مرض خطير (غالباً بكتيري أو طفيلي). عزل السمكة المريضة فوراً في حوض علاج منفصل لمنع انتشار المرض. لا تضف دواء للحوض الرئيسي مباشرة.')
      ],'en':[
        ('🍽️','Feeding','Small guppy flakes twice daily in very small amounts (what they eat in 2 minutes). Add frozen live food (Daphnia or frozen Artemia) twice weekly — noticeably improves color and boosts immunity. Pregnant females need slightly better nutrition. Always clean water.'),
        ('🚫','Avoid','Water below 22°C slows immunity and makes them prone to skin diseases and parasites. Never house with large or aggressive fish (male betta, cichlid). Don\'t keep one male with too many females — he will continuously harass them to exhaustion.'),
        ('💡','Breed tip','Thrive best in groups of 8-12 in a 40+ liter tank. Dense plants (Guppy Grass, Java Moss) provide hiding spots for fry and pregnant females. Weekly 20-25% water changes maintain vibrant colors and overall health.'),
        ('⚠️','Watch out','Gradual color fading + vertical body curving (like "C" or "S") + slow bottom swimming = serious illness (usually bacterial or parasitic). Immediately isolate the sick fish in a separate treatment tank to prevent spread. Never add medication directly to the main tank.')
      ]},
      'koi': {'ar':[
        ('🍽️','التغذية','أكل كوي مخصص ٣-٤ مرات يومياً في الصيف (بروتين ٣٠-٣٥٪). في الخريف حين تنخفض الحرارة تحت ١٥ درجة انتقل لأكل منخفض البروتين سهل الهضم. أوقف الطعام كلياً تحت ١٠ درجات - هضمه يتوقف وأي طعام في معدته يتعفن. ماء مُهوّى دائماً.'),
        ('🚫','تجنّب','أي أدوات أو ديكورات نحاسية أو زنكية في البركة = سمّ مباشر للكوي حتى بكميات ضئيلة. الشمس المباشرة لأكثر من ٦ ساعات ترفع الماء لدرجات خطيرة. لا تضف سمك جديد بدون عزل ٣-٤ أسابيع - الأوبئة في برك الكوي مدمرة.'),
        ('💡','نصيحة السلالة','كوي يعيش ٢٠-٣٥ سنة ويصبح جزءاً من العائلة - هو استثمار حقيقي. بركة لا تقل عن ٥٠٠٠ لتر مع فلتر بيولوجي ضخم. فحص جودة الماء أسبوعياً (أمونيا، نترات، pH، أكسجين) أهم من الطعام. الكوي الصحي نشيط ولامع الألوان ويأتي لمقدمة البركة عند رؤيتك.'),
        ('⚠️','انتبه','قرح حمراء أو بيضاء على جسم الكوي + خمول في قاع البركة + فقدان قشور = عدوى بكتيرية (Aeromonas/Pseudomonas) تنتشر بسرعة. عزل السمكة المريضة فوراً وراجع طبيب أسماك متخصص - الانتظار أسبوعاً يمكن أن يعني خسارة البركة كلها.')
      ],'en':[
        ('🍽️','Feeding','Species-specific koi food 3-4 times daily in summer (30-35% protein). In autumn as temperature drops below 15°C switch to low-protein easily digestible food. Stop feeding completely below 10°C — digestion halts and any food in the stomach will rot. Always well-aerated water.'),
        ('🚫','Avoid','Any copper or zinc tools or decorations in the pond = direct poison for koi even in tiny amounts. Direct sun more than 6 hours raises water to dangerous temperatures. Never add new fish without 3-4 week quarantine — pond epidemics are devastating.'),
        ('💡','Breed tip','Koi live 20-35 years and become part of the family — a true long-term investment. Minimum 5000-liter pond with a large biological filter. Weekly water quality testing (ammonia, nitrates, pH, oxygen) is more important than feeding. Healthy koi are active, vibrantly colored, and come to the front of the pond when they see you.'),
        ('⚠️','Watch out','Red or white ulcers on the koi body + lethargy at pond bottom + scale loss = bacterial infection (Aeromonas/Pseudomonas) that spreads rapidly. Immediately quarantine the sick fish and consult a specialist fish vet — waiting a week could mean losing the entire pond.')
      ]},
    }

    # ── Generic fallback tips per pet type ───────────────────────
    _GENERIC = {
      'cats': {'ar':[
        ('🍽️','التغذية','أكل رطب عالي البروتين مرتين يومياً - الصباح والمساء. النسبة المثالية ٧٠٪ رطب و٣٠٪ جاف كحد أقصى لأن القطط لا تشرب ماءً كافياً طبيعياً ويُعوّض الأكل الرطب النقص. قدّم ماءً نظيفاً دائماً بعيداً عن وعاء الطعام (القطط تفضّل ذلك غريزياً). الكمية المثالية ٤٠-٥٠ غرام/وجبة حسب وزنها.'),
        ('🚫','تجنّب','ممنوع تماماً: بصل، ثوم، كراث (حتى مطبوخ أو مجفف) - يدمرون كرات الدم الحمراء. شوكولا، كافيين، عنب، زبيب - سامة. أفوكادو، حليب بقر، جوز المكاديميا. تجنّب الأكل الإنساني بشكل عام حتى ما يبدو بريئاً.'),
        ('💡','نصيحة','القطط تخفي مرضها غريزياً لأنها في الطبيعة فريسة وكاسرة معاً. راقب هذه الإشارات الدقيقة: تغيير في كمية الأكل أو الشرب، تغيير في صندوق الفضالت، خمول غير معتاد، أو عزل نفسها عنك. هذه كثيراً ما تسبق الأعراض الواضحة بأيام.'),
        ('⚠️','انتبه','توقف كامل عن الأكل لأكثر من ٢٤ ساعة = خطر تضخم الكبد الدهني (Hepatic Lipidosis) عند القطط. دم في البول أو صعوبة في التبوّل (خاصةً الذكور) = انسداد بولي طارئ. كلا الحالتين تستدعيان طبيباً فوراً.')
      ],'en':[
        ('🍽️','Feeding','High-protein wet food twice daily — morning and evening. Ideal ratio is 70% wet, 30% dry maximum, as cats don\'t drink enough water naturally and wet food compensates. Always keep fresh water away from the food bowl (cats instinctively prefer this). Ideal portion 40-50g per meal based on weight.'),
        ('🚫','Avoid','Absolutely forbidden: onion, garlic, leeks (even cooked or dried) — destroy red blood cells. Chocolate, caffeine, grapes, raisins — toxic. Avocado, cow\'s milk, macadamia nuts. Avoid human food generally, even what seems harmless.'),
        ('💡','Tip','Cats instinctively hide illness as they are both predator and prey in nature. Watch for these subtle signs: changes in eating or drinking amounts, changes in litter box habits, unusual lethargy, or withdrawing from you. These often precede visible symptoms by days.'),
        ('⚠️','Watch out','Complete food refusal for more than 24 hours = risk of hepatic lipidosis (fatty liver) in cats. Blood in urine or difficulty urinating (especially males) = urinary blockage emergency. Both require a vet immediately.')
      ]},
      'dogs': {'ar':[
        ('🍽️','التغذية','وجبتان يومياً صباح ومساء بكميات محددة حسب الوزن المثالي لا الفعلي. إذا كان الكلب يزيد عن وزنه المثالي احسب الكمية حسب الوزن المراد الوصول إليه. ماء نظيف متاح طوال اليوم وغيّره مرتين. لا تطعمه مباشرة قبل أو بعد التمرين.'),
        ('🚫','تجنّب','ممنوع: شوكولا (التيوبرومين سام)، عنب وزبيب (فشل كلوي)، بصل وثوم وكراث (تسمم دم)، أفوكادو، مكسرات ماكاديميا، عجين خمير، عظام مطبوخة (تتكسر وتثقب الأمعاء). تجنّب إطعامه من الطاولة - تخلق عادة سيئة ومشاكل هضمية.'),
        ('💡','نصيحة','التمرين اليومي ليس رفاهية بل ضرورة صحية ونفسية. كلب لا يتمرن كافياً يطوّر: قلق، عدوانية، تخريب منزل، ونباح مفرط. ٣٠ دقيقة مشي سريع يومياً حد أدنى - أضف ألعاب استرداد أو سباحة للنتائج الأفضل. كلب متعب = كلب هادئ سعيد.'),
        ('⚠️','انتبه','انتفاخ بطن مفاجئ + محاولات تقيؤ فاشلة + قلق وعدم راحة = GDV (التواء المعدة) حالة طارئة مميتة خلال ساعات. إسعاف بيطري فوري - لا تنتظر. شائع في السلالات الكبيرة ذات الصدر العميق.')
      ],'en':[
        ('🍽️','Feeding','Two meals daily, morning and evening, in portions based on ideal weight — not actual weight if overweight. If the dog exceeds ideal weight, calculate portions for the target weight, not current. Clean water accessible all day, changed twice. Never feed immediately before or after exercise.'),
        ('🚫','Avoid','Forbidden: chocolate (theobromine is toxic), grapes and raisins (kidney failure), onion, garlic, and leeks (blood poisoning), avocado, macadamia nuts, raw yeast dough, cooked bones (splinter and perforate intestines). Avoid table feeding — creates bad habits and digestive issues.'),
        ('💡','Tip','Daily exercise is a health and psychological necessity, not a luxury. Under-exercised dogs develop: anxiety, aggression, home destruction, and excessive barking. 30 min brisk daily walk as minimum — add retrieval games or swimming for best results. A tired dog is a calm happy dog.'),
        ('⚠️','Watch out','Sudden bloated belly + unsuccessful vomiting attempts + restlessness and discomfort = GDV (gastric torsion), a deadly emergency within hours. Emergency vet immediately — don\'t wait. Common in large deep-chested breeds.')
      ]},
      'birds': {'ar':[
        ('🍽️','التغذية','كريات غذائية (Pellets) ٥٠٪ من النظام الغذائي + خضار طازجة ٣٠٪ (جزر، فلفل، سبانخ، بروكلي) + بذور ٢٠٪ كمكافأة فقط - ليس كأساس. ماء نظيف يُغيّر كل يوم بدون استثناء - حوض الماء القديم يُنمّي البكتيريا. حجر كالسيوم داخل القفص دائماً. تغذية متنوعة الألوان يومياً تضمن تغطية جميع الفيتامينات.'),
        ('🚫','تجنّب','ممنوع تماماً: أفوكادو (سام جداً)، شوكولا، كافيين، بصل، ثوم، ملح، أي طعام مطبوخ بزيت أو توابل. دخان السيجارة، أبخرة الطبخ (خاصة التيفال)، البخاخات والمعطرات والشموع المعطرة سامة جداً للجهاز التنفسي الرقيق للطيور. القفص قرب المطبخ خطر.'),
        ('💡','نصيحة','الطيور تخفي مرضها غريزياً (غريزة الفريسة) حتى اللحظة الأخيرة. راقب هذه الإشارات المبكرة: تغيير في الصوت أو الغناء، تغيير في كمية الأكل أو الشرب، نوم مفرط في النهار، وقوف على قدم واحدة غير معتاد. إذا لاحظت أياً منها - طبيب طيور.'),
        ('⚠️','انتبه','ريش منتفش باستمرار خلال النهار + خمول وعدم تحرك + إغماض العيون نهاراً + توقف عن الغناء = مرض حاد. الطيور تتدهور بسرعة مخيفة لأنها تخفي المرض حتى نقطة اللا عودة. طبيب طيور متخصص اليوم - ليس الغد.')
      ],'en':[
        ('🍽️','Feeding','Pellets 50% of diet + 30% fresh vegetables (carrot, pepper, spinach, broccoli) + 20% seeds as treats only — never as the base. Fresh water changed every single day without exception — stale water breeds bacteria. Calcium stone always in the cage. Colorful varied food daily ensures full vitamin coverage.'),
        ('🚫','Avoid','Absolutely forbidden: avocado (highly toxic), chocolate, caffeine, onion, garlic, salt, any food cooked with oil or spices. Cigarette smoke, cooking fumes (especially Teflon), aerosol sprays, fragrances, and scented candles are extremely toxic to birds\' delicate respiratory systems. Cage near the kitchen is a hazard.'),
        ('💡','Tip','Birds instinctively hide illness (prey animal instinct) until the very last moment. Watch for these early signs: change in voice or singing pattern, change in food or water consumption, excessive daytime sleeping, unusual one-legged resting. If you notice any of them — see a bird vet.'),
        ('⚠️','Watch out','Continuously puffed feathers during the day + lethargy + closed eyes during the day + stopped singing = acute illness. Birds deteriorate with terrifying speed because they conceal illness until the point of no return. Specialist bird vet today — not tomorrow.')
      ]},
      'fish': {'ar':[
        ('🍽️','التغذية','طعام مخصص لنوع سمكتك مرة أو مرتين يومياً - بكمية تُأكل خلال دقيقتين فقط لا أكثر. الإفراط في الطعام يلوث الماء بسرعة ويقتل الأسماك من التسمم. أيام صيام مرة أو مرتين أسبوعياً مفيدة لمعظم الأسماك. طعام متنوع (حبوب + مجمد + طبيعي) يحسّن الألوان والصحة.'),
        ('🚫','تجنّب','ممنوع استخدام ماء صنبور مباشرة بدون مزيل كلور - الكلور يقتل البكتيريا النافعة في الفلتر ويسمم الأسماك. لا تنظف الفلتر بالكامل دفعة واحدة. لا تضع نباتات منزلية أو أشياء ملوّنة غير آمنة للأسماك في الحوض. تجنّب إضافة سمكة جديدة بدون عزل.'),
        ('💡','نصيحة','فحص جودة الماء أسبوعياً (أمونيا = صفر، نترات < ٤٠، pH مناسب للنوع) أهم بكثير من الطعام لصحة الأسماك على المدى البعيد. مجموعة فحص بسيطة تكلف قليلاً وتوفر عليك خسارة الأسماك. تغيير ٢٠-٣٠٪ من الماء أسبوعياً أساسي.'),
        ('⚠️','انتبه','لهاث مستمر عند سطح الماء + شحوب ملحوظ في الألوان + سباحة غير طبيعية (جانباً أو قاعاً أو دوائر) = جودة ماء رديئة أو مرض. غيّر ٣٠٪ الماء فوراً وافحص الكيمياء. إذا استمرت الأعراض بعد ٢٤ ساعة - عزل واستشارة متخصص.')
      ],'en':[
        ('🍽️','Feeding','Species-specific food once or twice daily — only as much as eaten in 2 minutes maximum. Overfeeding pollutes water rapidly and kills fish through toxicity. One or two fasting days weekly benefit most fish species. Varied diet (pellets + frozen + live food) noticeably improves color and health.'),
        ('🚫','Avoid','Never use tap water directly without dechlorinator — chlorine kills the beneficial bacteria in the filter and poisons fish. Never clean the entire filter at once. Don\'t place household plants or non-aquarium-safe items in the tank. Always quarantine new fish before adding.'),
        ('💡','Tip','Weekly water quality testing (ammonia = zero, nitrates < 40, appropriate pH for species) matters far more than food for long-term fish health. A simple test kit costs little and saves fish lives. Changing 20-30% of tank water weekly is fundamental.'),
        ('⚠️','Watch out','Continuous gasping at the water surface + noticeably fading colors + abnormal swimming (sideways, at bottom, in circles) = poor water quality or disease. Change 30% water immediately and test chemistry. If symptoms persist after 24 hours — quarantine and consult a specialist.')
      ]},
      'small-pets': {'ar':[
        ('🍽️','التغذية','طعام مخصص لنوع حيوانك الصغير مع ألياف طازجة يومية - القش الطازج لأغلب الحيوانات الصغيرة هو الأساس لا الإضافة. ماء نظيف في زجاجة تقطير (لا وعاء مفتوح) يُغيّر يومياً. تجنّب الإفراط في الحبوب الجاهزة - كثير منها يحتوي سكريات مخفية. وفّر تنويعاً في الخضار المناسبة لنوعه.'),
        ('🚫','تجنّب','ممنوع: شوكولا، بصل، ثوم، حمضيات، سكريات مصنوعة، نشويات بكميات كبيرة. تجنّب التطعيم بالطعام الإنساني العشوائي. الحيوانات الصغيرة حساسة جداً للتغيير المفاجئ في الغذاء - أي تغيير يكون تدريجياً على ٧-١٠ أيام. الحرارة الشديدة فوق ٢٨ درجة خطر على معظمها.'),
        ('💡','نصيحة','الحيوانات الصغيرة (أرانب، خنازير غينيا، هامستر، شينشيلا) تخفي مرضها غريزياً كفرائس. افحص هذه الأشياء يومياً: كمية الأكل المتناولة، كمية الماء، كمية الفضالت وشكلها، وطبيعة نشاطه. تغيير أي منها مؤشر مبكر مهم.'),
        ('⚠️','انتبه','خمول مفاجئ غير معتاد + توقف عن الأكل لأكثر من ١٢-٢٤ ساعة + توقف عن التبرز = طارئ هضمي في معظم الحيوانات الصغيرة. لا تنتظر حتى الغد. طبيب متخصص بالحيوانات الصغيرة (Exotic Vet) فوراً لأن الأطباء العاديين قد لا يعرفون كيفية التعامل معها.')
      ],'en':[
        ('🍽️','Feeding','Species-specific food with daily fresh fiber — fresh hay for most small animals is the foundation, not just an addition. Clean water in a drip bottle (not an open bowl) changed daily. Avoid over-relying on commercial pellets — many contain hidden sugars. Provide variety in appropriate vegetables for your specific species.'),
        ('🚫','Avoid','Forbidden: chocolate, onion, garlic, citrus, processed sugars, large amounts of starchy foods. Avoid random human food sharing. Small animals are very sensitive to sudden dietary changes — any change should be gradual over 7-10 days. Temperatures above 28°C are dangerous for most small pets.'),
        ('💡','Tip','Small animals (rabbits, guinea pigs, hamsters, chinchillas) instinctively hide illness as prey species. Check these things daily: amount of food consumed, water intake, quantity and consistency of droppings, and activity level. Any change in these is an important early indicator.'),
        ('⚠️','Watch out','Unusual sudden lethargy + refusing food for more than 12-24 hours + no droppings = digestive emergency in most small animals. Don\'t wait until tomorrow. See an Exotic Vet immediately — general vets may not know how to properly treat them.')
      ]},
    }

    breed_data = _TIPS.get(breed, {})
    raw_tips = breed_data.get(lang, []) or _GENERIC.get(pet_slug, {}).get(lang, [])
    breed_label = _BREED_INFO.get(breed, {}).get('ar' if lang=='ar' else 'en', '')

    tips_out = [{'icon': t[0], 'title': t[1], 'text': t[2]} for t in raw_tips]

    return jsonify({
        'ok':          True,
        'tips':        tips_out,
        'breed_label': breed_label,
        'products':    products_out,
    })


@app.route('/api/v1/products', methods=['GET'])
def api_products():
    if not _api_auth(): return _api_err('unauthorized')
    q         = request.args.get('q', '').strip()
    pet_type  = request.args.get('pet', '').strip().lower()   # dog, cat, bird, fish, small
    category  = request.args.get('category', '').strip()
    max_price = request.args.get('max_price', type=float)
    limit     = min(int(request.args.get('limit', 20)), 100)

    _pet_slugs = {'dog':'dogs','cat':'cats','bird':'birds','fish':'fish','small':'small-pets'}

    sql = '''SELECT p.id, p.slug, p.name_ar, p.name_en, p.brand,
                    p.price, p.discount_price, p.stock_qty,
                    p.benefit_ar, p.benefit_en, p.health_tags, p.size_tag,
                    c.slug as cat_slug, c.name_ar as cat_ar, c.name_en as cat_en
             FROM products p JOIN categories c ON c.id=p.category_id
             WHERE p.is_active=1'''
    params = []
    if q:
        sql += ' AND (p.name_ar LIKE ? OR p.name_en LIKE ? OR p.brand LIKE ?)'
        params += [f'%{q}%', f'%{q}%', f'%{q}%']
    if pet_type and pet_type in _pet_slugs:
        sql += ' AND c.slug=?'
        params.append(_pet_slugs[pet_type])
    if category:
        sql += ' AND (c.slug=? OR c.name_en LIKE ?)'
        params += [category, f'%{category}%']
    if max_price:
        sql += ' AND (COALESCE(p.discount_price, p.price)) <= ?'
        params.append(max_price)
    sql += f' ORDER BY p.is_featured DESC, p.created_at DESC LIMIT {limit}'

    db   = get_db()
    rows = db.execute(sql, params).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/v1/products/<int:pid>', methods=['GET'])
def api_product(pid):
    if not _api_auth(): return _api_err('unauthorized')
    db = get_db()
    row = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    db.close()
    if not row: return _api_err('not found', 404)
    return jsonify(dict(row))

@app.route('/api/v1/products/<int:pid>/stock', methods=['PUT'])
def api_product_stock(pid):
    if not _api_auth(): return _api_err('unauthorized')
    data = request.get_json(silent=True) or {}
    qty  = data.get('stock_qty')
    if qty is None: return _api_err('stock_qty required', 400)
    qty = int(qty)
    db  = get_db()
    old = db.execute("SELECT stock_qty, name_ar FROM products WHERE id=?", (pid,)).fetchone()
    db.execute("UPDATE products SET stock_qty=? WHERE id=?", (qty, pid))
    db.commit()

    # ── Auto-notify: لما البضاعة ترجع ──
    if old and old['stock_qty'] == 0 and qty > 0:
        prod_row  = db.execute("SELECT slug FROM products WHERE id=?", (pid,)).fetchone()
        prod_url  = f'/product/{prod_row["slug"]}' if prod_row else '/'
        push_title = f'🎉 {old["name_ar"]} — عاد للمخزون!'
        push_body  = 'المنتج اللي كنت تنتظره أصبح متاحاً الآن'

        # Web Push (PWA) — لكل المشتركين بهالمنتج
        _push_all_for_product(pid, push_title, push_body, prod_url)

        auto_on = _get_integration('auto_notify_stock')
        if auto_on == '1':
            leads = db.execute(
                "SELECT phone, name FROM stock_notifications WHERE product_id=? AND notified=0", (pid,)
            ).fetchall()
            if leads:
                wh = _get_integration('n8n_notify_webhook')
                for lead in leads:
                    if wh:
                        _fire_webhook(wh, {
                            'event':      'stock_returned',
                            'product_id': pid,
                            'name_ar':    old['name_ar'],
                            'phone':      lead['phone'],
                            'name':       lead['name'],
                            'stock_qty':  qty,
                        })
                db.execute(
                    "UPDATE stock_notifications SET notified=1 WHERE product_id=? AND notified=0", (pid,)
                )
                db.commit()
                _auto_log('stock_returned', 'ok', f'{old["name_ar"]} → {len(leads)} إشعار')

    # ── تحذير مخزون منخفض ──
    threshold = _get_integration('low_stock_threshold')
    if threshold and qty <= int(threshold) and qty > 0:
        wh = _get_integration('n8n_low_stock_webhook') or _get_integration('n8n_order_webhook')
        if wh:
            _fire_webhook(wh, {
                'event':      'low_stock',
                'product_id': pid,
                'name_ar':    old['name_ar'] if old else '',
                'stock_qty':  qty,
                'threshold':  int(threshold),
            })
        _auto_log('low_stock', 'ok', f'مخزون {old["name_ar"] if old else pid} وصل {qty}')

    db.close()
    return jsonify({'ok': True, 'product_id': pid, 'stock_qty': qty})

@app.route('/api/v1/orders', methods=['GET'])
def api_orders():
    if not _api_auth(): return _api_err('unauthorized')
    status = request.args.get('status')
    db = get_db()
    if status:
        rows = db.execute(
            "SELECT * FROM orders WHERE status=? ORDER BY created_at DESC LIMIT 100", (status,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/v1/orders/<int:oid>', methods=['GET'])
def api_order(oid):
    if not _api_auth(): return _api_err('unauthorized')
    db = get_db()
    payload = _order_payload(oid, db)
    db.close()
    if not payload: return _api_err('not found', 404)
    return jsonify(payload)

@app.route('/api/v1/orders/<int:oid>/status', methods=['PUT'])
def api_order_status(oid):
    if not _api_auth(): return _api_err('unauthorized')
    data   = request.get_json(silent=True) or {}
    status = data.get('status', '')
    valid  = ('new', 'confirmed', 'shipped', 'delivered', 'cancelled')
    if status not in valid:
        return _api_err(f'status must be one of {valid}', 400)
    db = get_db()
    db.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
    db.commit()
    # fire status webhook
    webhook_url = _get_integration('n8n_status_webhook')
    if webhook_url:
        payload = _order_payload(oid, db)
        if payload:
            payload['event'] = 'status_changed'
            _fire_webhook(webhook_url, payload)
    db.close()
    return jsonify({'ok': True, 'order_id': oid, 'status': status})

@app.route('/api/v1/advisor', methods=['POST'])
def api_v1_advisor():
    """وكيل AI يسأل بيتي عن توصية — بيرجع reply + منتجات."""
    if not _api_auth(): return _api_err('unauthorized')
    data    = request.get_json(silent=True) or {}
    message = data.get('message', '').strip()
    history = data.get('history', [])
    lang    = data.get('lang', 'ar')
    if not message: return _api_err('message required', 400)
    result   = ai_mod.product_advisor(message, history, lang)
    products = []
    if result['product_ids']:
        db   = get_db()
        pids = result['product_ids']
        rows = db.execute(
            f'''SELECT p.id, p.slug, p.name_ar, p.name_en, p.price, p.discount_price,
                       p.stock_qty, p.benefit_ar, p.benefit_en
                FROM products p WHERE p.id IN ({",".join("?"*len(pids))}) AND p.is_active=1''',
            pids
        ).fetchall()
        db.close()
        pid_map  = {r['id']: dict(r) for r in rows}
        products = [pid_map[i] for i in pids if i in pid_map]
    return jsonify({'reply': result['reply'], 'products': products, 'action': result.get('action','')})


@app.route('/api/v1/promotions', methods=['GET'])
def api_v1_promotions():
    """قائمة العروض النشطة + طبقات الأسعار."""
    if not _api_auth(): return _api_err('unauthorized')
    db     = get_db()
    promos = db.execute(
        'SELECT offer_type, threshold_amount, title_ar, title_en FROM cart_promotions WHERE is_active=1 ORDER BY threshold_amount'
    ).fetchall()
    tiers  = db.execute(
        '''SELECT pt.product_id, p.name_ar, p.name_en, pt.min_qty, pt.price_per_unit
           FROM product_price_tiers pt JOIN products p ON p.id=pt.product_id
           WHERE p.is_active=1 ORDER BY pt.product_id, pt.min_qty'''
    ).fetchall()
    db.close()
    return jsonify({
        'promotions': [dict(r) for r in promos],
        'price_tiers': [dict(r) for r in tiers],
    })


@app.route('/api/v1/orders', methods=['POST'])
def api_create_order():
    """n8n يقدر يسجل طلب مباشرة (مثلاً من نموذج خارجي)."""
    if not _api_auth(): return _api_err('unauthorized')
    data = request.get_json(silent=True) or {}
    name  = data.get('customer_name', '').strip()
    phone = data.get('phone', '').strip()
    area  = data.get('area', '').strip()
    note  = data.get('address_note', '').strip() or None
    items = data.get('items', [])
    if not (name and phone and area and items):
        return _api_err('customer_name, phone, area, items are required', 400)
    db  = get_db()
    cur = db.cursor()
    total = 0.0
    resolved = []
    for it in items:
        pid = it.get('product_id')
        qty = int(it.get('qty', 1))
        p = db.execute("SELECT * FROM products WHERE id=? AND is_active=1", (pid,)).fetchone()
        if not p: continue
        price = p['discount_price'] if p['discount_price'] else p['price']
        total += price * qty
        resolved.append((pid, qty, price))
    if not resolved:
        db.close()
        return _api_err('no valid products', 400)
    cur.execute(
        "INSERT INTO orders (customer_name, phone, area, address_note, total) VALUES (?,?,?,?,?)",
        (name, phone, area, note, round(total, 2))
    )
    oid = cur.lastrowid
    for pid, qty, price in resolved:
        cur.execute(
            "INSERT INTO order_items (order_id, product_id, qty, price_at_order) VALUES (?,?,?,?)",
            (oid, pid, qty, price)
        )
    db.commit()
    payload = _order_payload(oid, db)
    db.close()
    return jsonify({'ok': True, 'order_id': oid, 'total': round(total, 2)}), 201


# ── Incoming Webhook from n8n ──────────────────────────────────
@app.route('/webhooks/n8n', methods=['POST'])
def webhook_n8n():
    """
    n8n يبعت أوامر للمتجر.
    Header مطلوب: X-Webhook-Secret: <secret>
    Body JSON:
      { "action": "update_stock",  "product_id": 5,  "stock_qty": 100 }
      { "action": "update_status", "order_id": 12,   "status": "shipped" }
      { "action": "deactivate_product", "product_id": 5 }
    """
    secret = _get_integration('webhook_secret')
    if not secret:
        return jsonify({'error': 'webhook_secret not configured'}), 403
    incoming = request.headers.get('X-Webhook-Secret', '')
    if incoming != secret:
        return jsonify({'error': 'forbidden'}), 403

    data   = request.get_json(silent=True) or {}
    action = data.get('action', '')
    db     = get_db()

    if action == 'update_stock':
        pid = data.get('product_id')
        qty = data.get('stock_qty')
        if pid and qty is not None:
            db.execute("UPDATE products SET stock_qty=? WHERE id=?", (int(qty), pid))
            db.commit()
            db.close()
            return jsonify({'ok': True, 'action': action})

    elif action == 'update_status':
        oid    = data.get('order_id')
        status = data.get('status', '')
        valid  = ('new', 'confirmed', 'shipped', 'delivered', 'cancelled')
        if oid and status in valid:
            db.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
            db.commit()
            db.close()
            return jsonify({'ok': True, 'action': action})

    elif action == 'deactivate_product':
        pid = data.get('product_id')
        if pid:
            db.execute("UPDATE products SET is_active=0 WHERE id=?", (pid,))
            db.commit()
            db.close()
            return jsonify({'ok': True, 'action': action})

    elif action == 'activate_product':
        pid = data.get('product_id')
        if pid:
            db.execute("UPDATE products SET is_active=1 WHERE id=?", (pid,))
            db.commit()
            db.close()
            return jsonify({'ok': True, 'action': action})

    db.close()
    return jsonify({'error': 'unknown action'}), 400


# ── Admin: API Keys Management ─────────────────────────────────
@app.route('/admin/api-keys', methods=['GET', 'POST'])
@admin_required
def admin_api_keys():
    db  = get_db()
    msg = None
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'generate':
            label = request.form.get('label', 'n8n key').strip() or 'n8n key'
            key   = 'ps_' + secrets.token_hex(24)
            db.execute("INSERT INTO api_keys (key, label) VALUES (?,?)", (key, label))
            db.commit()
            msg = ('generated', key)
        elif action == 'revoke':
            kid = request.form.get('key_id')
            db.execute("UPDATE api_keys SET is_active=0 WHERE id=?", (kid,))
            db.commit()
            msg = ('revoked', None)
        elif action == 'save_n8n':
            for setting in ('n8n_order_webhook', 'n8n_status_webhook', 'webhook_secret'):
                val = request.form.get(setting, '').strip()
                db.execute(
                    "INSERT INTO integration_settings (key,value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (setting, val or None)
                )
            db.commit()
            msg = ('saved_n8n', None)

    keys = db.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
    n8n_order  = db.execute("SELECT value FROM integration_settings WHERE key='n8n_order_webhook'").fetchone()
    n8n_status = db.execute("SELECT value FROM integration_settings WHERE key='n8n_status_webhook'").fetchone()
    wh_secret  = db.execute("SELECT value FROM integration_settings WHERE key='webhook_secret'").fetchone()
    db.close()
    return render_template(
        'admin/api_keys.html',
        api_keys=keys,
        msg=msg,
        n8n_order_webhook  = n8n_order['value']  if n8n_order  else '',
        n8n_status_webhook = n8n_status['value'] if n8n_status else '',
        webhook_secret     = wh_secret['value']  if wh_secret  else '',
        active_admin='api_keys',
    )


# ══════════════════════════════════════════════════════════════
#  AI Routes
# ══════════════════════════════════════════════════════════════

@app.route('/search')
def search():
    q           = request.args.get('q',         '').strip()
    pet_filter  = request.args.get('pet',       '').strip()
    max_price   = request.args.get('max_price', '').strip()
    in_stock    = request.args.get('in_stock',  '').strip()

    has_filters = any([pet_filter, max_price, in_stock])
    if not q and not has_filters:
        return render_template('search.html', query='', results=[],
                               pet_filter='', max_price='', in_stock='',
                               active_tab='categories')

    db      = get_db()
    results = []

    if q:
        ids = ai_mod.smart_search(q)
        if ids:
            placeholders = ','.join('?' * len(ids))
            sql = f'''SELECT p.*,
                           (SELECT filename FROM product_images
                            WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS primary_img,
                           c.slug as cat_slug
                    FROM products p
                    JOIN categories c ON c.id = p.category_id
                    WHERE p.id IN ({placeholders}) AND p.is_active=1'''
            params = list(ids)
            if pet_filter:
                sql += ' AND c.slug=?'
                params.append(pet_filter)
            if max_price:
                try:
                    sql += ' AND COALESCE(p.discount_price, p.price) <= ?'
                    params.append(float(max_price))
                except ValueError:
                    pass
            if in_stock:
                sql += ' AND p.stock_qty > 0'
            rows = db.execute(sql, params).fetchall()
            row_map = {r['id']: r for r in rows}
            results = [row_map[i] for i in ids if i in row_map]
    else:
        sql = '''SELECT p.*,
                       (SELECT filename FROM product_images
                        WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS primary_img,
                       c.slug as cat_slug
                FROM products p
                JOIN categories c ON c.id = p.category_id
                WHERE p.is_active=1'''
        params = []
        if pet_filter:
            sql += ' AND c.slug=?'
            params.append(pet_filter)
        if max_price:
            try:
                sql += ' AND COALESCE(p.discount_price, p.price) <= ?'
                params.append(float(max_price))
            except ValueError:
                pass
        if in_stock:
            sql += ' AND p.stock_qty > 0'
        sql += ' ORDER BY p.is_featured DESC, p.created_at DESC LIMIT 60'
        results = db.execute(sql, params).fetchall()

    db.close()
    return render_template('search.html', query=q, results=results,
                           pet_filter=pet_filter, max_price=max_price, in_stock=in_stock,
                           active_tab='categories')


@app.route('/api/advisor', methods=['POST'])
def api_advisor():
    if _rate_limited(f'advisor:{_client_ip()}', max_calls=30, window=60):
        return jsonify({'error': 'rate limit exceeded'}), 429
    data    = request.get_json(silent=True) or {}
    message = data.get('message', '').strip()
    history = data.get('history', [])
    lang    = data.get('lang', 'ar')
    if not message:
        return jsonify({'error': 'message required'}), 400
    result = ai_mod.product_advisor(message, history, lang)
    # اجلب تفاصيل المنتجات المقترحة
    products = []
    if result['product_ids']:
        db = get_db()
        pids = result['product_ids']
        rows = db.execute(
            f'''SELECT p.id, p.slug, p.name_ar, p.name_en, p.price, p.discount_price,
                       (SELECT filename FROM product_images
                        WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS primary_img
                FROM products p
                WHERE p.id IN ({",".join("?"*len(pids))}) AND p.is_active=1''',
            pids
        ).fetchall()
        db.close()
        pid_map = {r['id']: dict(r) for r in rows}
        products = [pid_map[i] for i in pids if i in pid_map]
    return jsonify({'reply': result['reply'], 'products': products, 'action': result.get('action', '')})


@app.route('/api/advisor-image', methods=['POST'])
def api_advisor_image():
    if _rate_limited(f'advisor:{_client_ip()}', max_calls=30, window=60):
        return jsonify({'error': 'rate limit exceeded'}), 429
    data    = request.get_json(silent=True) or {}
    image   = data.get('image', '')   # base64
    history = data.get('history', [])
    lang    = data.get('lang', 'ar')
    if not image:
        return jsonify({'error': 'image required'}), 400
    result = ai_mod.advisor_image(image, history, lang)
    products = []
    if result.get('product_ids'):
        db   = get_db()
        pids = result['product_ids']
        rows = db.execute(
            f'''SELECT p.id, p.slug, p.name_ar, p.name_en, p.price, p.discount_price,
                       (SELECT filename FROM product_images
                        WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS primary_img
                FROM products p
                WHERE p.id IN ({",".join("?"*len(pids))}) AND p.is_active=1''',
            pids
        ).fetchall()
        db.close()
        pid_map  = {r['id']: dict(r) for r in rows}
        products = [pid_map[i] for i in pids if i in pid_map]
    return jsonify({'reply': result['reply'], 'products': products, 'action': result.get('action', '')})


@app.route('/api/track', methods=['POST'])
def api_track():
    """يستقبل أحداث التتبع من analytics.js."""
    data  = request.get_json(silent=True) or {}
    sid   = data.get('session_id', '')   # visit ID
    uid   = data.get('user_id', '')      # user ID ثابت
    if not sid:
        return '', 204

    etype = data.get('event_type', '')
    db    = get_db()

    # أنشئ أو حدّث الـ visit session
    existing = db.execute(
        'SELECT id FROM analytics_sessions WHERE id=?', (sid,)
    ).fetchone()

    if not existing:
        db.execute(
            '''INSERT OR IGNORE INTO analytics_sessions
               (id, user_id, device_type, os, browser, screen, language,
                referrer, utm_source, utm_medium, utm_campaign, landing_page)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (sid, uid,
             data.get('device_type'), data.get('os'), data.get('browser'),
             data.get('screen'), data.get('language'),
             data.get('referrer'), data.get('utm_source'),
             data.get('utm_medium'), data.get('utm_campaign'),
             data.get('landing_page') or data.get('page'))
        )
    else:
        db.execute(
            "UPDATE analytics_sessions SET last_seen=datetime('now'), page_count=page_count+1 WHERE id=?",
            (sid,)
        )

    # page_exit — نجمع المدة الكلية للزيارة
    if etype == 'page_exit':
        try:
            extra = json.loads(data.get('extra') or '{}')
            secs  = int(extra.get('seconds', 0))
            if secs > 0:
                db.execute(
                    "UPDATE analytics_sessions SET duration_sec=COALESCE(duration_sec,0)+? WHERE id=?",
                    (secs, sid)
                )
        except Exception:
            pass

    # سجّل الـ event
    if etype not in ('time_on_page',):
        db.execute(
            '''INSERT INTO analytics_events
               (session_id, event_type, page, product_id, product_slug, search_query, extra)
               VALUES (?,?,?,?,?,?,?)''',
            (sid, etype,
             data.get('page'),
             data.get('product_id'),
             data.get('product_slug'),
             data.get('search_query'),
             data.get('extra'))
        )

    if etype == 'purchase':
        db.execute(
            "UPDATE analytics_sessions SET converted=1, order_id=? WHERE id=?",
            (data.get('order_id'), sid)
        )

    db.commit()
    db.close()
    return '', 204


@app.route('/admin/analytics/session/<sid>')
@admin_required
def admin_session_timeline(sid):
    db = get_db()
    session = db.execute(
        'SELECT * FROM analytics_sessions WHERE id=?', (sid,)
    ).fetchone()
    if not session:
        return 'جلسة غير موجودة', 404

    events = db.execute('''
        SELECT e.*, p.name_ar as product_name, p.slug as product_slug
        FROM analytics_events e
        LEFT JOIN products p ON p.id = e.product_id
        WHERE e.session_id = ?
        ORDER BY e.created_at ASC
    ''', (sid,)).fetchall()

    db.close()

    # بناء timeline مقروء
    timeline = []
    for ev in events:
        extra = {}
        try:
            extra = json.loads(ev['extra'] or '{}')
        except Exception:
            pass

        icon, label = {
            'page_view':      ('🌐', f"دخل {ev['page']}"),
            'product_view':   ('👁️', f"فتح المنتج: {ev['product_name'] or ev['page']}"),
            'add_to_cart':    ('🛒', f"أضاف للسلة: {ev['product_name'] or ''}"),
            'search':         ('🔍', f"بحث عن: {ev['search_query'] or ''}"),
            'scroll_depth':   ('📜', f"مرّ {extra.get('pct','')}% من الصفحة {ev['page']}"),
            'click':          ('🖱️', f"ضغط على: {extra.get('el','')}"),
            'begin_checkout': ('💳', 'بدأ إتمام الطلب'),
            'purchase':       ('✅', f"اشترى! طلب #{extra.get('order_id','')}"),
            'cart_abandon':   ('😔', 'ترك السلة بدون شراء'),
            'page_exit':      ('🚪', f"طلع من {ev['page']} بعد {extra.get('seconds',0)} ثانية — مرّ {extra.get('scroll_pct',0)}%"),
        }.get(ev['event_type'], ('•', ev['event_type']))

        timeline.append({
            'time':  ev['created_at'],
            'icon':  icon,
            'label': label,
            'type':  ev['event_type'],
        })

    return render_template('admin/session_timeline.html',
                           session=session, timeline=timeline,
                           active_admin='analytics')


# ── Short link in-memory cache (TTL 5 min) ─────────────────────
_link_cache = {}          # code → (target_url, expires_at)
_CACHE_TTL  = 300         # seconds

def _cache_get(code):
    entry = _link_cache.get(code)
    if entry and time.time() < entry[1]:
        return entry[0]
    return None

def _cache_set(code, url):
    _link_cache[code] = (url, time.time() + _CACHE_TTL)

def _cache_del(code):
    _link_cache.pop(code, None)


@app.route('/r/<code>')
def short_link(code):
    # 1. ابحث بالـ cache أولاً — لا DB، لا latency
    target = _cache_get(code)

    if target is None:
        db   = get_db()
        link = db.execute('SELECT target_url FROM short_links WHERE code=?', (code,)).fetchone()
        if not link:
            db.close()
            return redirect(url_for('index'))
        target = link['target_url']
        _cache_set(code, target)
        # نحدّث الكليكات بـ thread منفصل — لا يأخر الـ redirect
        def _inc():
            try:
                d = get_db(); d.execute('UPDATE short_links SET clicks=clicks+1 WHERE code=?', (code,)); d.commit(); d.close()
            except Exception: pass
        threading.Thread(target=_inc, daemon=True).start()
        db.close()
    else:
        # cache hit — نحدّث الكليكات بـ background thread
        def _inc():
            try:
                d = get_db(); d.execute('UPDATE short_links SET clicks=clicks+1 WHERE code=?', (code,)); d.commit(); d.close()
            except Exception: pass
        threading.Thread(target=_inc, daemon=True).start()

    # 2. target_url يحتوي على UTM params — المتصفح يأخذها مباشرة
    #    مو referrer header — هيك UTM ما تضيع حتى لو المنصة تحذف الـ referrer
    return redirect(target, code=302)


@app.route('/admin/links')
@admin_required
def admin_links():
    db    = get_db()
    links = db.execute('SELECT * FROM short_links ORDER BY created_at DESC').fetchall()

    # لكل رابط: استخرج utm_campaign من target_url واستعلم عن الزيارات الفعلية
    links_data = []
    for l in links:
        qs       = parse_qs(urlparse(l['target_url']).query)
        campaign = qs.get('utm_campaign', [None])[0]
        source   = qs.get('utm_source',   [None])[0]

        visits = buyers = 0
        if campaign:
            row = db.execute("""
                SELECT COUNT(*) as v, SUM(converted) as b
                FROM analytics_sessions
                WHERE utm_campaign=? OR utm_campaign=?
            """, (campaign, unquote(campaign))).fetchone()
            visits = row['v'] or 0
            buyers = int(row['b'] or 0)

        drop = round((l['clicks'] - visits) / l['clicks'] * 100) if l['clicks'] > 0 else 0
        links_data.append({**dict(l), 'visits': visits, 'buyers': buyers,
                           'drop': drop, 'campaign': campaign, 'source': source})

    db.close()
    base = request.host_url.rstrip('/')
    return render_template('admin/links.html', links=links_data, base=base, active_admin='links')


@app.route('/admin/links/create', methods=['POST'])
@admin_required
def admin_links_create():
    target = request.form.get('target_url', '').strip()
    label  = request.form.get('label', '').strip()
    code   = request.form.get('code', '').strip()
    if not target:
        return redirect(url_for('admin_links'))
    if not code:
        code = secrets.token_urlsafe(4)[:6]
    db = get_db()
    try:
        db.execute('INSERT INTO short_links (code, target_url, label) VALUES (?,?,?)',
                   (code, target, label or None))
        db.commit()
    except Exception:
        pass
    db.close()
    return redirect(url_for('admin_links'))


@app.route('/admin/links/<code>/delete', methods=['POST'])
@admin_required
def admin_links_delete(code):
    db = get_db()
    db.execute('DELETE FROM short_links WHERE code=?', (code,))
    db.commit()
    db.close()
    _cache_del(code)
    return redirect(url_for('admin_links'))


@app.route('/admin/analytics/campaign/<path:name>')
@admin_required
def admin_campaign_detail(name):
    db = get_db()
    campaign_dec = unquote(name)

    # إجمالي
    summary = db.execute("""
        SELECT COUNT(*) as visits, SUM(converted) as buyers,
               ROUND(SUM(converted)*100.0/COUNT(*),1) as cvr,
               COALESCE(SUM(o.total),0) as revenue,
               MIN(s.started_at) as first_seen, MAX(s.started_at) as last_seen
        FROM analytics_sessions s
        LEFT JOIN orders o ON o.id=s.order_id
        WHERE s.utm_campaign=? OR s.utm_campaign=?
    """, (campaign_dec, name)).fetchone()

    # يومي
    daily = db.execute("""
        SELECT DATE(s.started_at) as day, COUNT(*) as visits,
               SUM(converted) as buyers, COALESCE(SUM(o.total),0) as revenue
        FROM analytics_sessions s
        LEFT JOIN orders o ON o.id=s.order_id
        WHERE s.utm_campaign=? OR s.utm_campaign=?
        GROUP BY day ORDER BY day DESC LIMIT 30
    """, (campaign_dec, name)).fetchall()

    # أجهزة
    devices = db.execute("""
        SELECT device_type, COUNT(*) as cnt
        FROM analytics_sessions
        WHERE utm_campaign=? OR utm_campaign=?
        GROUP BY device_type ORDER BY cnt DESC
    """, (campaign_dec, name)).fetchall()

    # صفحات الهبوط
    landings = db.execute("""
        SELECT landing_page, COUNT(*) as cnt
        FROM analytics_sessions
        WHERE utm_campaign=? OR utm_campaign=?
        GROUP BY landing_page ORDER BY cnt DESC LIMIT 10
    """, (campaign_dec, name)).fetchall()

    # آخر 20 زيارة
    sessions = db.execute("""
        SELECT s.id, s.started_at, s.device_type, s.os, s.converted,
               s.duration_sec, s.landing_page, o.total as order_total
        FROM analytics_sessions s
        LEFT JOIN orders o ON o.id=s.order_id
        WHERE s.utm_campaign=? OR s.utm_campaign=?
        ORDER BY s.started_at DESC LIMIT 20
    """, (campaign_dec, name)).fetchall()

    db.close()
    return render_template('admin/campaign_detail.html',
        name=campaign_dec, summary=summary, daily=daily,
        devices=devices, landings=landings, sessions=sessions,
        active_admin='analytics')


@app.route('/admin/analytics/campaign/<path:name>/archive', methods=['POST'])
@admin_required
def admin_campaign_archive(name):
    db = get_db()
    campaign_dec = unquote(name)
    db.execute(
        "INSERT OR IGNORE INTO campaign_archive (campaign) VALUES (?)",
        (campaign_dec,)
    )
    db.commit()
    db.close()
    return redirect(url_for('admin_analytics'))


@app.route('/admin/analytics/campaign/<path:name>/unarchive', methods=['POST'])
@admin_required
def admin_campaign_unarchive(name):
    db = get_db()
    db.execute("DELETE FROM campaign_archive WHERE campaign=?", (unquote(name),))
    db.commit()
    db.close()
    return redirect(url_for('admin_analytics'))


@app.route('/admin/pwa')
@admin_required
def admin_pwa():
    db = get_db()
    total_installed = db.execute(
        "SELECT COUNT(*) as c FROM pwa_events WHERE event='installed'"
    ).fetchone()['c']
    total_launches  = db.execute(
        "SELECT COUNT(*) as c FROM pwa_events WHERE event='launch'"
    ).fetchone()['c']
    # تثبيتات آخر 30 يوم
    by_day = db.execute("""
        SELECT DATE(created_at) as day, event, COUNT(*) as cnt
        FROM pwa_events
        WHERE created_at >= DATE('now','-30 days')
        GROUP BY day, event
        ORDER BY day DESC
    """).fetchall()
    # آخر 20 حدث
    recent = db.execute(
        "SELECT * FROM pwa_events ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    # push subscribers
    push_general = db.execute(
        "SELECT COUNT(*) as c FROM push_subscriptions WHERE product_id IS NULL"
    ).fetchone()['c']
    push_product = db.execute(
        "SELECT COUNT(*) as c FROM push_subscriptions WHERE product_id IS NOT NULL"
    ).fetchone()['c']
    db.close()

    # تجميع by_day لعرضها
    days_map = {}
    for r in by_day:
        d = r['day']
        if d not in days_map:
            days_map[d] = {'installed': 0, 'launch': 0}
        days_map[d][r['event']] = r['cnt']
    chart_days = sorted(days_map.keys())

    return render_template('admin/pwa.html',
        total_installed=total_installed,
        total_launches=total_launches,
        push_general=push_general,
        push_product=push_product,
        chart_days=chart_days,
        days_map=days_map,
        recent=[dict(r) for r in recent],
        active_admin='pwa')


@app.route('/admin/analytics')
@admin_required
def admin_analytics():
    db = get_db()

    # ── أرقام عامة (آخر 30 يوم) ──
    stats = {}
    stats['sessions'] = db.execute(
        "SELECT COUNT(*) FROM analytics_sessions WHERE started_at >= date('now','-30 days')"
    ).fetchone()[0]
    stats['converted'] = db.execute(
        "SELECT COUNT(*) FROM analytics_sessions WHERE converted=1 AND started_at >= date('now','-30 days')"
    ).fetchone()[0]
    stats['conversion_rate'] = (
        round(stats['converted'] / stats['sessions'] * 100, 1) if stats['sessions'] else 0
    )
    stats['cart_abandons'] = db.execute(
        """SELECT COUNT(DISTINCT session_id) FROM analytics_events
           WHERE event_type='cart_abandon' AND created_at >= date('now','-30 days')"""
    ).fetchone()[0]
    stats['searches'] = db.execute(
        """SELECT COUNT(*) FROM analytics_events
           WHERE event_type='search' AND created_at >= date('now','-30 days')"""
    ).fetchone()[0]

    # Bounce rate: جلسات بـ page_view واحدة فقط
    bounced = db.execute("""
        SELECT COUNT(*) FROM analytics_sessions s
        WHERE s.started_at >= date('now','-30 days')
          AND (SELECT COUNT(*) FROM analytics_events e
               WHERE e.session_id=s.id AND e.event_type='page_view') = 1
    """).fetchone()[0]
    stats['bounce_rate'] = round(bounced / stats['sessions'] * 100, 1) if stats['sessions'] else 0

    # ── Funnel ──
    funnel = {}
    for step in ['page_view','product_view','add_to_cart','begin_checkout','purchase']:
        funnel[step] = db.execute(
            """SELECT COUNT(DISTINCT session_id) FROM analytics_events
               WHERE event_type=? AND created_at >= date('now','-30 days')""",
            (step,)
        ).fetchone()[0]

    # ── أكثر المنتجات مشاهدةً ──
    top_products = db.execute(
        """SELECT p.name_ar, p.slug, COUNT(*) as views
           FROM analytics_events e
           JOIN products p ON p.id = e.product_id
           WHERE e.event_type='product_view' AND e.created_at >= date('now','-30 days')
           GROUP BY e.product_id ORDER BY views DESC LIMIT 10"""
    ).fetchall()

    # ── أكثر المنتجات إضافةً للسلة بدون شراء ──
    abandoned_products = db.execute(
        """SELECT p.name_ar, COUNT(*) as cnt
           FROM analytics_events e
           JOIN products p ON p.id = e.product_id
           WHERE e.event_type='add_to_cart'
             AND e.created_at >= date('now','-30 days')
             AND e.session_id NOT IN (
               SELECT session_id FROM analytics_events WHERE event_type='purchase'
             )
           GROUP BY e.product_id ORDER BY cnt DESC LIMIT 5"""
    ).fetchall()

    # ── مصادر الزيارات ──
    sources = db.execute(
        """SELECT COALESCE(NULLIF(referrer,''), 'مباشر') as src, COUNT(*) as cnt
           FROM analytics_sessions
           WHERE started_at >= date('now','-30 days')
           GROUP BY src ORDER BY cnt DESC LIMIT 8"""
    ).fetchall()

    # ── الأجهزة ──
    devices = db.execute(
        """SELECT device_type, COUNT(*) as cnt
           FROM analytics_sessions
           WHERE started_at >= date('now','-30 days')
           GROUP BY device_type ORDER BY cnt DESC"""
    ).fetchall()

    # ── أنظمة التشغيل ──
    os_stats = db.execute(
        """SELECT os, COUNT(*) as cnt
           FROM analytics_sessions
           WHERE started_at >= date('now','-30 days')
           GROUP BY os ORDER BY cnt DESC"""
    ).fetchall()

    # ── أكثر الكلمات بحثاً ──
    top_searches = db.execute(
        """SELECT search_query, COUNT(*) as cnt
           FROM analytics_events
           WHERE event_type='search' AND search_query IS NOT NULL
             AND created_at >= date('now','-30 days')
           GROUP BY search_query ORDER BY cnt DESC LIMIT 10"""
    ).fetchall()

    # ── خصائص الزبائن اللي اشتروا (للإعلانات) ──
    buyer_profile = db.execute(
        """SELECT s.device_type, s.os, s.browser,
                  COALESCE(NULLIF(s.referrer,''), 'مباشر') as source,
                  s.language
           FROM analytics_sessions s
           WHERE s.converted=1
             AND s.started_at >= date('now','-90 days')"""
    ).fetchall()

    # ── أكثر المناطق طلباً ──
    top_areas = db.execute(
        """SELECT area, COUNT(*) as cnt, SUM(total) as revenue
           FROM orders
           WHERE created_at >= date('now','-30 days')
             AND status != 'cancelled'
           GROUP BY area ORDER BY cnt DESC LIMIT 12"""
    ).fetchall()

    # ── المناطق اللي اشترت أكتر من مرة (زبائن مخلصون) ──
    loyal_areas = db.execute(
        """SELECT area, COUNT(DISTINCT phone) as customers,
                  ROUND(AVG(total),2) as avg_order
           FROM orders
           WHERE status IN ('delivered','confirmed','shipped')
             AND created_at >= date('now','-90 days')
           GROUP BY area HAVING customers > 1
           ORDER BY customers DESC LIMIT 8"""
    ).fetchall()

    # ── Source ROI — أي رابط جاب أكثر زيارات ومبيعات ──
    source_roi = db.execute("""
        SELECT
            COALESCE(NULLIF(s.utm_source, ''),
                     NULLIF(s.referrer, ''),
                     'مباشر') AS src,
            COUNT(*)                                    AS visits,
            SUM(s.converted)                            AS buyers,
            ROUND(SUM(s.converted) * 100.0 / COUNT(*), 1) AS cvr,
            COALESCE(SUM(o.total), 0)                   AS revenue,
            ROUND(COALESCE(SUM(o.total),0) / COUNT(*), 2) AS rps
        FROM analytics_sessions s
        LEFT JOIN orders o ON o.id = s.order_id
        WHERE s.started_at >= date('now', '-30 days')
        GROUP BY src
        ORDER BY revenue DESC
        LIMIT 12
    """).fetchall()

    # ── UTM Campaigns ──
    archived = {r[0] for r in db.execute("SELECT campaign FROM campaign_archive").fetchall()}
    utm_campaigns_raw = db.execute("""
        SELECT
            NULLIF(utm_campaign,'') AS campaign,
            utm_source,
            COUNT(*)                                       AS visits,
            SUM(converted)                                 AS buyers,
            ROUND(SUM(converted)*100.0/COUNT(*),1)         AS cvr,
            COALESCE(SUM(o.total),0)                       AS revenue
        FROM analytics_sessions s
        LEFT JOIN orders o ON o.id = s.order_id
        WHERE utm_campaign != '' AND utm_campaign IS NOT NULL
          AND s.started_at >= date('now','-30 days')
          AND utm_campaign NOT IN (SELECT campaign FROM campaign_archive)
        GROUP BY campaign, utm_source
        ORDER BY revenue DESC LIMIT 10
    """).fetchall()
    utm_campaigns = [
        {**dict(r), 'campaign': unquote(r['campaign'] or '')}
        for r in utm_campaigns_raw
    ]

    # ── أكثر صفحات الخروج (Exit Pages) ──
    exit_pages = db.execute("""
        SELECT page,
               COUNT(*) as exits,
               ROUND(AVG(CAST(json_extract(extra,'$.scroll_pct') AS INTEGER)),0) as avg_scroll,
               ROUND(AVG(CAST(json_extract(extra,'$.seconds') AS INTEGER)),0) as avg_sec
        FROM analytics_events
        WHERE event_type='page_exit'
          AND created_at >= date('now','-14 days')
        GROUP BY page
        ORDER BY exits DESC LIMIT 8
    """).fetchall()

    # ── آخر الجلسات (لعرض Timeline) ──
    recent_sessions = db.execute("""
        SELECT s.id, s.started_at, s.device_type, s.os,
               COALESCE(NULLIF(s.referrer,''), 'مباشر') as src,
               s.page_count, s.converted, s.landing_page,
               s.duration_sec,
               (SELECT COUNT(*) FROM analytics_events e WHERE e.session_id=s.id) as event_count,
               (SELECT COUNT(*) FROM analytics_sessions s2 WHERE s2.user_id=s.user_id AND s2.user_id IS NOT NULL) as total_visits,
               (SELECT CASE
                  WHEN EXISTS(SELECT 1 FROM analytics_events WHERE session_id=s.id AND event_type='purchase')       THEN 'purchase'
                  WHEN EXISTS(SELECT 1 FROM analytics_events WHERE session_id=s.id AND event_type='begin_checkout') THEN 'begin_checkout'
                  WHEN EXISTS(SELECT 1 FROM analytics_events WHERE session_id=s.id AND event_type='add_to_cart')    THEN 'add_to_cart'
                  WHEN EXISTS(SELECT 1 FROM analytics_events WHERE session_id=s.id AND event_type='product_view')   THEN 'product_view'
                  WHEN EXISTS(SELECT 1 FROM analytics_events WHERE session_id=s.id AND event_type='search')         THEN 'search'
                  ELSE 'page_view' END) as top_event
        FROM analytics_sessions s
        WHERE s.started_at >= date('now','-7 days')
        ORDER BY s.started_at DESC LIMIT 50
    """).fetchall()

    db.close()
    return render_template(
        'admin/analytics.html',
        stats=stats, funnel=funnel,
        top_products=top_products,
        abandoned_products=abandoned_products,
        sources=sources, devices=devices,
        os_stats=os_stats, top_searches=top_searches,
        buyer_profile=[dict(r) for r in buyer_profile],
        top_areas=top_areas, loyal_areas=loyal_areas,
        source_roi=source_roi, utm_campaigns=utm_campaigns, archived_campaigns=archived,
        exit_pages=exit_pages, recent_sessions=recent_sessions,
        active_admin='analytics',
    )


@app.route('/api/order-lookup')
def api_order_lookup():
    phone = request.args.get('phone', '').strip().lstrip('+').replace(' ', '').replace('-', '')
    if len(phone) < 7:
        return jsonify({'error': 'رقم غير صحيح'}), 400
    db  = get_db()
    orders = db.execute(
        """SELECT o.id, o.status, o.total, o.area, o.created_at,
                  GROUP_CONCAT(p.name_ar || ' ×' || oi.qty, ' | ') as items
           FROM orders o
           JOIN order_items oi ON oi.order_id = o.id
           JOIN products p ON p.id = oi.product_id
           WHERE replace(replace(replace(o.phone,'+',''),' ',''),'-','') LIKE ?
           GROUP BY o.id
           ORDER BY o.created_at DESC LIMIT 5""",
        (f'%{phone[-8:]}%',)
    ).fetchall()
    db.close()
    STATUS_AR = {
        'new':       '🕐 قيد المراجعة',
        'confirmed': '✅ تم التأكيد',
        'shipped':   '🚚 خرج للتوصيل',
        'delivered': '📦 تم التسليم',
        'cancelled': '❌ ملغي',
    }
    result = []
    for o in orders:
        result.append({
            'id':     o['id'],
            'status': STATUS_AR.get(o['status'], o['status']),
            'total':  o['total'],
            'area':   o['area'],
            'date':   o['created_at'][:10],
            'items':  o['items'],
        })
    return jsonify({'orders': result})


@app.route('/admin/ai-insights')
@admin_required
def admin_ai_insights():
    insights  = ai_mod.analyze_orders()
    alerts    = ai_mod.inventory_alerts()
    return render_template('admin/ai_insights.html',
                           insights=insights,
                           alerts=alerts,
                           active_admin='insights')


# ── كاتب الوصف ─────────────────────────────────────────────────

@app.route('/admin/ai-describe')
@admin_required
def admin_ai_describe():
    db = get_db()
    categories = db.execute('SELECT id, name_ar FROM categories ORDER BY sort_order').fetchall()

    # جلب guides لكل التصنيفات + global
    guides = {r['category_id']: r for r in
              db.execute('SELECT * FROM ai_style_guide').fetchall()}

    products = db.execute('''
        SELECT p.id, p.name_ar, p.slug, p.brand, c.id as cat_id, c.name_ar as cat_ar,
               CASE WHEN p.description_ar IS NULL OR p.description_ar='' THEN 0 ELSE 1 END as has_desc
        FROM products p
        JOIN categories c ON c.id=p.category_id
        WHERE p.is_active=1
        ORDER BY has_desc ASC, c.sort_order, p.name_ar
    ''').fetchall()

    # مسودات pending للمراجعة
    drafts = db.execute('''
        SELECT d.*, p.name_ar as product_name, c.name_ar as cat_ar
        FROM product_desc_drafts d
        JOIN products p ON p.id = d.product_id
        JOIN categories c ON c.id = p.category_id
        WHERE d.status = 'pending'
        ORDER BY d.created_at DESC
    ''').fetchall()

    db.close()
    return render_template('admin/ai_describe.html',
                           categories=categories, guides=guides,
                           products=products, drafts=drafts,
                           active_admin='describe')


@app.route('/admin/ai-describe/style', methods=['POST'])
@admin_required
def admin_ai_style_save():
    cat_id = int(request.form.get('category_id', 0))
    ai_mod.update_style_guide(
        cat_id,
        request.form.get('tone_ar', ''),
        request.form.get('tone_en', ''),
        request.form.get('structure', ''),
    )
    flash('تم حفظ دليل الأسلوب', 'success')
    return redirect(url_for('admin_ai_describe'))


@app.route('/api/ai-describe/<int:pid>', methods=['POST'])
@admin_required
def api_ai_describe(pid):
    result = ai_mod.generate_description(pid)
    return jsonify(result)


@app.route('/api/ai-describe/approve/<int:did>', methods=['POST'])
@admin_required
def api_ai_approve(did):
    ok = ai_mod.approve_description(did)
    return jsonify({'ok': ok})


@app.route('/api/ai-describe/reject/<int:did>', methods=['POST'])
@admin_required
def api_ai_reject(did):
    ai_mod.reject_description(did)
    return jsonify({'ok': True})


@app.route('/api/ai-describe/bulk', methods=['POST'])
@admin_required
def api_ai_describe_bulk():
    db  = get_db()
    ids = [r['id'] for r in db.execute(
        "SELECT id FROM products WHERE is_active=1 AND (description_ar IS NULL OR description_ar='') ORDER BY id"
    ).fetchall()]
    db.close()

    results = {'done': 0, 'failed': 0, 'draft_ids': []}
    for pid in ids:
        res = ai_mod.generate_description(pid)
        if res.get('ok'):
            results['done'] += 1
            results['draft_ids'].append(res.get('draft_id'))
        else:
            results['failed'] += 1

    return jsonify(results)


# ── Upsell — يفلتر المخزون الصفري عند التحميل ─────────────────

@app.route('/api/upsell/<int:pid>')
def api_upsell(pid):
    ids = ai_mod.get_upsell(pid)
    if not ids:
        return jsonify({'products': []})
    db   = get_db()
    # stock_qty > 0 هنا — الفلترة الحية بدون إبطال الـ cache
    rows = db.execute(f'''
        SELECT p.id, p.name_ar, p.name_en, p.slug,
               p.price, p.discount_price,
               (SELECT filename FROM product_images
                WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS primary_img
        FROM products p
        WHERE p.id IN ({",".join("?" for _ in ids)})
          AND p.is_active=1
          AND p.stock_qty > 0
        LIMIT 3
    ''', ids).fetchall()
    db.close()
    return jsonify({'products': [dict(r) for r in rows]})


# ── حملات إعلانية ──────────────────────────────────────────────

@app.route('/admin/ai-campaigns')
@admin_required
def admin_ai_campaigns():
    ideas = ai_mod.campaign_ideas()
    return render_template('admin/ai_campaigns.html',
                           ideas=ideas, active_admin='insights')


@app.route('/admin/ai-suggestions')
@admin_required
def admin_ai_suggestions():
    db   = get_db()
    sugg = db.execute(
        "SELECT * FROM ai_suggestions WHERE status='pending' ORDER BY created_at DESC"
    ).fetchall()
    done = db.execute(
        "SELECT * FROM ai_suggestions WHERE status!='pending' ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    db.close()
    return render_template('admin/ai_suggestions.html',
                           suggestions=sugg, done=done, active_admin='suggestions')


@app.route('/admin/ai-suggestions/generate', methods=['POST'])
@admin_required
def admin_ai_suggestions_generate():
    ai_mod.generate_suggestions()
    return redirect(url_for('admin_ai_suggestions'))


@app.route('/admin/ai-suggestions/<int:sid>/dismiss', methods=['POST'])
@admin_required
def admin_suggestion_dismiss(sid):
    db = get_db()
    db.execute("UPDATE ai_suggestions SET status='dismissed' WHERE id=?", (sid,))
    db.commit(); db.close()
    return redirect(url_for('admin_ai_suggestions'))


@app.route('/admin/ai-suggestions/<int:sid>/execute', methods=['POST'])
@admin_required
def admin_suggestion_execute(sid):
    db   = get_db()
    sugg = db.execute("SELECT * FROM ai_suggestions WHERE id=?", (sid,)).fetchone()
    if sugg and sugg['status'] == 'pending':
        try:
            data = json.loads(sugg['action_data'] or '{}')
        except Exception:
            data = {}

        if sugg['type'] == 'promo' and data.get('product_id'):
            db.execute("UPDATE products SET discount_price=? WHERE id=?",
                       (data['discount_price'], data['product_id']))

        db.execute("UPDATE ai_suggestions SET status='executed' WHERE id=?", (sid,))
        db.commit()

        # إذا WA — افتح رابط بالـ redirect
        if sugg['type'] == 'reactivate' and data.get('phone') and data.get('wa_message'):
            db.close()
            phone = data['phone'].replace(' ','').replace('-','').replace('+','')
            wa_url = f"https://wa.me/961{phone}?text={__import__('urllib.parse', fromlist=['quote']).quote(data['wa_message'])}"
            return redirect(wa_url)

    db.close()
    return redirect(url_for('admin_ai_suggestions'))


CAT_IMG_FOLDER = os.path.join(
    os.path.dirname(__file__), 'static', 'img', 'categories'
)

@app.route("/admin/homepage", methods=["GET", "POST"])
@admin_required
def admin_homepage():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "sections":
            # Save section order + visibility
            for sec_id in ["offers", "featured", "blog", "why"]:
                sort_val = request.form.get(f"sort_{sec_id}", 99)
                visible   = 1 if request.form.get(f"vis_{sec_id}") else 0
                db.execute(
                    """INSERT INTO homepage_sections (section_id, sort_order, is_visible)
                       VALUES (?,?,?)
                       ON CONFLICT(section_id) DO UPDATE SET sort_order=excluded.sort_order,
                       is_visible=excluded.is_visible""",
                    (sec_id, int(sort_val), visible)
                )
            db.commit()

        elif action == "cat_image":
            cat_slug = request.form.get("cat_slug", "").strip()
            f = request.files.get("cat_img")
            if cat_slug and f and f.filename:
                os.makedirs(CAT_IMG_FOLDER, exist_ok=True)
                ext = os.path.splitext(f.filename)[1].lower()
                if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
                    ext = '.jpg'
                fname = f'cat_{cat_slug}_{os.urandom(4).hex()}{ext}'
                fpath = os.path.join(CAT_IMG_FOLDER, fname)
                f.save(fpath)
                if _PILLOW:
                    new_name = _compress_image(fpath, max_width=800, quality=82)
                    if new_name:
                        fname = new_name
                db.execute(
                    """INSERT INTO category_card_images (category_slug, filename)
                       VALUES (?,?)
                       ON CONFLICT(category_slug) DO UPDATE SET filename=excluded.filename""",
                    (cat_slug, fname)
                )
                db.commit()

        elif action == "del_cat_image":
            cat_slug = request.form.get("cat_slug", "").strip()
            row = db.execute(
                "SELECT filename FROM category_card_images WHERE category_slug=?", (cat_slug,)
            ).fetchone()
            if row:
                try:
                    os.remove(os.path.join(CAT_IMG_FOLDER, row['filename']))
                except OSError:
                    pass
                db.execute("DELETE FROM category_card_images WHERE category_slug=?", (cat_slug,))
                db.commit()

        elif action == "cat_description":
            cat_slug    = request.form.get("cat_slug", "").strip()
            desc_ar     = request.form.get("description_ar", "").strip() or None
            desc_en     = request.form.get("description_en", "").strip() or None
            if cat_slug:
                db.execute(
                    "UPDATE categories SET description_ar=?, description_en=? WHERE slug=?",
                    (desc_ar, desc_en, cat_slug)
                )
                db.commit()

        db.close()
        return redirect(url_for("admin_homepage"))

    # GET
    categories = db.execute("SELECT * FROM categories ORDER BY sort_order").fetchall()
    sections = db.execute(
        "SELECT * FROM homepage_sections ORDER BY sort_order"
    ).fetchall()
    if not sections:
        for i, sid in enumerate(["offers", "featured", "blog", "why"]):
            db.execute(
                "INSERT OR IGNORE INTO homepage_sections (section_id, sort_order, is_visible) VALUES (?,?,1)",
                (sid, i)
            )
        db.commit()
        sections = db.execute(
            "SELECT * FROM homepage_sections ORDER BY sort_order"
        ).fetchall()

    cat_imgs_rows = db.execute("SELECT category_slug, filename FROM category_card_images").fetchall()
    cat_images = {r['category_slug']: r['filename'] for r in cat_imgs_rows}
    db.close()
    return render_template(
        "admin/homepage.html",
        categories=categories,
        sections=sections,
        cat_images=cat_images,
        active_admin="homepage",
    )


# ── صفحات المحتوى (فرونت إند) ──────────────────────────────────

@app.route("/blog")
def blog():
    db = get_db()
    posts = db.execute(
        "SELECT * FROM blog_posts WHERE is_published=1 ORDER BY created_at DESC"
    ).fetchall()
    db.close()
    return render_template("blog.html", posts=posts)


@app.route("/blog/<slug>")
def blog_post(slug):
    db = get_db()
    post = db.execute(
        "SELECT * FROM blog_posts WHERE slug=? AND is_published=1", (slug,)
    ).fetchone()
    db.close()
    if not post:
        return page_not_found(None)
    return render_template("blog_post.html", post=post)



@app.route("/returns")
def returns():
    return render_template("returns.html")

@app.route("/shipping")
def shipping_info():
    db = get_db()
    zones = db.execute("SELECT * FROM shipping_zones ORDER BY sort_order, name_ar").fetchall()
    db.close()
    return render_template("shipping_info.html", zones=zones)

@app.route("/pages/<slug>")
def content_page(slug):
    db = get_db()
    page = db.execute("SELECT * FROM content_pages WHERE slug=?", (slug,)).fetchone()
    db.close()
    if not page:
        return page_not_found(None)
    return render_template("content_page.html", page=page)


# ── أدمن صفحات المحتوى ──────────────────────────────────────────

@app.route("/admin/promotions", methods=["GET", "POST"])
@admin_required
def admin_promotions():
    db  = get_db()
    msg = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "save_promo":
            fields = {
                'offer_type':       request.form.get("offer_type","").strip(),
                'threshold_amount': request.form.get("threshold_amount","0").strip() or "0",
                'title_ar':         request.form.get("title_ar","").strip(),
                'title_en':         request.form.get("title_en","").strip(),
                'progress_ar':      request.form.get("progress_ar","").strip() or None,
                'progress_en':      request.form.get("progress_en","").strip() or None,
                'unlocked_ar':      request.form.get("unlocked_ar","").strip() or None,
                'unlocked_en':      request.form.get("unlocked_en","").strip() or None,
                'social_url':       request.form.get("social_url","").strip() or None,
                'sort_order':       request.form.get("sort_order","0").strip() or "0",
            }
            pid = request.form.get("promo_id")
            if pid:
                db.execute("""UPDATE cart_promotions SET offer_type=?,threshold_amount=?,title_ar=?,title_en=?,
                    progress_ar=?,progress_en=?,unlocked_ar=?,unlocked_en=?,social_url=?,sort_order=?
                    WHERE id=?""", (*fields.values(), pid))
            else:
                db.execute("""INSERT INTO cart_promotions (offer_type,threshold_amount,title_ar,title_en,
                    progress_ar,progress_en,unlocked_ar,unlocked_en,social_url,sort_order)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", tuple(fields.values()))
                # Push: أعلم المشتركين بالعرض الجديد
                promo_title = fields.get('title_ar') or fields.get('title_en') or 'عرض جديد'
                _push_broadcast(
                    f'🎁 {promo_title}',
                    'عرض حصري — تسوّق هلق!',
                    '/',
                    log_event='push_new_promo'
                )
            db.commit(); msg = "تم الحفظ"

        elif action == "toggle_promo":
            pid = request.form.get("promo_id")
            db.execute("UPDATE cart_promotions SET is_active = 1 - is_active WHERE id=?", (pid,))
            db.commit()

        elif action == "delete_promo":
            pid = request.form.get("promo_id")
            db.execute("DELETE FROM cart_promotions WHERE id=?", (pid,))
            db.commit(); msg = "تم الحذف"

        elif action == "save_gift":
            promo_id = request.form.get("promo_id")
            name_ar  = request.form.get("gift_name_ar","").strip()
            name_en  = request.form.get("gift_name_en","").strip()
            if promo_id and name_ar:
                db.execute("INSERT INTO promo_gift_options (promo_id,name_ar,name_en) VALUES (?,?,?)",
                           (promo_id, name_ar, name_en))
                db.commit(); msg = "تمت إضافة الهدية"

        elif action == "delete_gift":
            gid = request.form.get("gift_id")
            db.execute("DELETE FROM promo_gift_options WHERE id=?", (gid,))
            db.commit()

    promos = db.execute(
        "SELECT * FROM cart_promotions ORDER BY sort_order, id"
    ).fetchall()
    promo_gifts = {}
    for pr in promos:
        promo_gifts[pr['id']] = db.execute(
            "SELECT * FROM promo_gift_options WHERE promo_id=? ORDER BY sort_order", (pr['id'],)
        ).fetchall()
    db.close()
    return render_template("admin/promotions.html",
                           promos=promos, promo_gifts=promo_gifts,
                           msg=msg, active_admin="promotions")


@app.route("/admin/loyalty", methods=["GET", "POST"])
@admin_required
def admin_loyalty():
    db = get_db()
    msg = None

    if request.method == "POST":
        action = request.form.get("action")

        if action == "save":
            phone         = request.form.get("phone", "").strip()
            customer_name = request.form.get("customer_name", "").strip() or None
            perk_type     = request.form.get("perk_type", "").strip()
            perk_value    = request.form.get("perk_value", "").strip() or None
            note          = request.form.get("note", "").strip() or None
            expires_at    = request.form.get("expires_at", "").strip() or None
            condition_type  = request.form.get("condition_type", "").strip() or None
            condition_value = request.form.get("condition_value", "").strip() or None
            if phone and perk_type:
                db.execute("""
                    INSERT INTO customer_perks (phone, customer_name, perk_type, perk_value, note, expires_at, condition_type, condition_value)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(phone) DO UPDATE SET
                        customer_name=excluded.customer_name,
                        perk_type=excluded.perk_type,
                        perk_value=excluded.perk_value,
                        note=excluded.note,
                        expires_at=excluded.expires_at,
                        condition_type=excluded.condition_type,
                        condition_value=excluded.condition_value
                """, (phone, customer_name, perk_type, perk_value, note, expires_at, condition_type, condition_value))
                db.commit()
                msg = "تم الحفظ"

        elif action == "delete":
            pid = request.form.get("perk_id")
            if pid:
                db.execute("DELETE FROM customer_perks WHERE id=?", (pid,))
                db.commit()
                msg = "تم الحذف"

    perks = db.execute(
        "SELECT * FROM customer_perks ORDER BY created_at DESC"
    ).fetchall()
    db.close()
    return render_template("admin/loyalty.html",
                           perks=perks, msg=msg, active_admin="loyalty")


@app.route("/admin/loyalty/check")
@admin_required
def admin_loyalty_check():
    """API: ابحث عن زبون بالرقم وارجع معلوماته للـ WhatsApp preview."""
    phone = request.args.get("phone", "").strip()
    db = get_db()
    perk = db.execute("SELECT * FROM customer_perks WHERE phone=?", (phone,)).fetchone()
    # آخر طلب للزبون
    last_order = db.execute(
        "SELECT customer_name, created_at FROM orders WHERE phone=? ORDER BY created_at DESC LIMIT 1",
        (phone,)
    ).fetchone()
    db.close()
    return jsonify({
        "perk": dict(perk) if perk else None,
        "name": last_order["customer_name"] if last_order else "",
    })


# ── Admin: Telegram Monitor Settings ────────────────────────────

@app.route("/admin/monitor")
@admin_required
def admin_monitor():
    db = get_db()

    # ── Telegram config ──
    tg_rows = db.execute(
        "SELECT key, value FROM integration_settings WHERE key IN (?,?)",
        ("telegram_token", "telegram_chat_id")
    ).fetchall()
    cfg = {r["key"]: r["value"] for r in tg_rows}

    # ── Products: no image ──
    no_img = db.execute("""
        SELECT p.id, p.name_ar, p.slug
        FROM products p
        WHERE p.is_active=1
          AND (SELECT COUNT(*) FROM product_images WHERE product_id=p.id) = 0
        ORDER BY p.name_ar
    """).fetchall()

    # ── Products: no description ──
    no_desc = db.execute("""
        SELECT id, name_ar, slug FROM products
        WHERE is_active=1 AND (description_ar IS NULL OR description_ar='')
        ORDER BY name_ar
    """).fetchall()

    # ── Products: out of stock ──
    out_of_stock = db.execute("""
        SELECT id, name_ar, slug FROM products
        WHERE is_active=1 AND stock_qty=0
        ORDER BY name_ar
    """).fetchall()

    # ── Products: low stock (1-3) ──
    low_stock = db.execute("""
        SELECT id, name_ar, slug, stock_qty FROM products
        WHERE is_active=1 AND stock_qty > 0 AND stock_qty <= 3
        ORDER BY stock_qty
    """).fetchall()

    # ── Recent 404s (top repeated) ──
    top_404 = db.execute("""
        SELECT path, COUNT(*) as hits, MAX(hit_at) as last_hit
        FROM not_found_log
        WHERE hit_at >= datetime('now','-7 days')
        GROUP BY path ORDER BY hits DESC LIMIT 10
    """).fetchall()

    # ── Orders today ──
    orders_today = db.execute("""
        SELECT COUNT(*) as cnt, COALESCE(SUM(total),0) as rev
        FROM orders WHERE date(created_at)=date('now')
    """).fetchone()

    # ── Pending orders ──
    pending_orders = db.execute(
        "SELECT COUNT(*) as cnt FROM orders WHERE status='new'"
    ).fetchone()["cnt"]

    # ── Large images (>500KB on disk) ──
    large_imgs = []
    try:
        img_dir = config.UPLOAD_FOLDER
        for fname in os.listdir(img_dir):
            # skip srcset variants
            if any(fname.endswith(f'_{w}.jpg') for w in (400, 800)):
                continue
            fpath = os.path.join(img_dir, fname)
            try:
                size = os.path.getsize(fpath)
                if size > 500 * 1024:
                    large_imgs.append({'filename': fname, 'size_kb': size // 1024})
            except OSError:
                pass
        large_imgs.sort(key=lambda x: -x['size_kb'])
        large_imgs = large_imgs[:10]
    except Exception:
        pass

    # ── Products: no SEO meta ──
    total_products = db.execute("SELECT COUNT(*) FROM products WHERE is_active=1").fetchone()[0]
    seo_covered = db.execute("""
        SELECT COUNT(DISTINCT page_id) FROM seo_meta WHERE page_type='product'
    """).fetchone()[0]

    db.close()

    return render_template("admin/monitor.html",
                           telegram_token=cfg.get("telegram_token",""),
                           telegram_chat_id=cfg.get("telegram_chat_id",""),
                           no_img=no_img,
                           no_desc=no_desc,
                           out_of_stock=out_of_stock,
                           low_stock=low_stock,
                           top_404=top_404,
                           orders_today=orders_today,
                           pending_orders=pending_orders,
                           large_imgs=large_imgs,
                           total_products=total_products,
                           seo_covered=seo_covered,
                           active_admin="monitor")

@app.route("/admin/monitor/save", methods=["POST"])
@admin_required
def admin_monitor_save():
    token   = request.form.get("telegram_token","").strip()
    chat_id = request.form.get("telegram_chat_id","").strip()
    db = get_db()
    for key, val in [("telegram_token", token), ("telegram_chat_id", chat_id)]:
        db.execute("INSERT OR REPLACE INTO integration_settings (key,value) VALUES (?,?)", (key, val))
    db.commit()
    db.close()
    from flask import flash
    flash("تم الحفظ ✓", "success")
    return redirect(url_for("admin_monitor"))

@app.route("/admin/monitor/test")
@admin_required
def admin_monitor_test():
    monitor_mod.send_alert(
        "🧪 <b>اختبار</b>\nالمراقبة شغالة ✅\n<i>Bella Pet Monitor 🐾</i>"
    )
    from flask import flash
    flash("تم إرسال رسالة اختبار على تيليغرام ✓", "success")
    return redirect(url_for("admin_monitor"))

@app.route("/admin/monitor/run-now")
@admin_required
def admin_monitor_run_now():
    """شغّل كل الفحوصات فوراً."""
    import threading
    def _run():
        alerts = []
        for fn in [monitor_mod.check_recent_errors,
                   monitor_mod.check_zero_stock,
                   monitor_mod.check_products_missing,
                   monitor_mod.check_no_orders,
                   monitor_mod.check_traffic_spike]:
            try:
                r = fn()
                if r: alerts.append(r)
            except Exception:
                pass
        if alerts:
            monitor_mod.send_alert("🔍 <b>فحص يدوي:</b>\n\n" + "\n\n──────────\n\n".join(alerts))
        else:
            monitor_mod.send_alert("✅ <b>فحص يدوي:</b> كل شي تمام!")
    threading.Thread(target=_run, daemon=True).start()
    from flask import flash
    flash("جاري الفحص — النتيجة رح توصل على تيليغرام خلال ثوان ✓", "success")
    return redirect(url_for("admin_monitor"))


# ─────────────────────────────────────────────
# Subscriptions — customer + admin
# ─────────────────────────────────────────────

@app.route('/subscribe', methods=['POST'])
def subscribe_create():
    import uuid
    from datetime import date, timedelta

    name  = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    area  = request.form.get('area', '').strip()
    notes = request.form.get('notes', '').strip()

    if not name or not phone:
        return jsonify({'ok': False, 'msg': 'بيانات ناقصة'}), 400

    # Multi-item batch format: items[0][product_id], items[0][qty], items[0][interval_days]
    items = []
    i = 0
    while True:
        pid = request.form.get(f'items[{i}][product_id]', type=int)
        if pid is None:
            break
        interval = request.form.get(f'items[{i}][interval_days]', 30, type=int)
        qty      = max(1, request.form.get(f'items[{i}][qty]', 1, type=int))
        items.append({'pid': pid, 'qty': qty, 'interval': interval})
        i += 1

    # Fallback: single-product format
    if not items:
        pid      = request.form.get('product_id', type=int)
        interval = request.form.get('interval_days', type=int)
        qty      = max(1, request.form.get('qty', 1, type=int))
        if not pid or not interval:
            return jsonify({'ok': False, 'msg': 'بيانات ناقصة'}), 400
        if interval < 1 or interval > 365:
            return jsonify({'ok': False, 'msg': 'فترة غير صالحة'}), 400
        items = [{'pid': pid, 'qty': qty, 'interval': interval}]

    batch_id = str(uuid.uuid4())[:8] if len(items) > 1 else None
    db = get_db()
    for it in items:
        if it['interval'] < 1 or it['interval'] > 365:
            continue
        next_date = (date.today() + timedelta(days=it['interval'])).isoformat()
        db.execute(
            """INSERT INTO subscriptions
               (product_id, qty, interval_days, customer_name, phone, area, notes, next_renewal_date, batch_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (it['pid'], it['qty'], it['interval'], name, phone, area, notes, next_date, batch_id)
        )
    db.commit()
    db.close()
    pids = ','.join(str(it['pid']) for it in items)
    _auto_log('subscription_new', 'ok', f'pids={pids} phone={phone} batch={batch_id}')
    return jsonify({'ok': True})


def _sub_settings(db):
    """Load subscription settings from integration_settings."""
    rows = db.execute(
        "SELECT key, value FROM integration_settings WHERE key IN ('sub_free_delivery_min','sub_delivery_days_min','sub_delivery_days_max')"
    ).fetchall()
    cfg = {r['key']: r['value'] for r in rows}
    return {
        'free_min':   float(cfg.get('sub_free_delivery_min', 0) or 0),
        'days_min':   int(cfg.get('sub_delivery_days_min', 2) or 2),
        'days_max':   int(cfg.get('sub_delivery_days_max', 4) or 4),
    }


@app.route('/admin/subscriptions')
@admin_required
def admin_subscriptions():
    from datetime import date, timedelta
    db     = get_db()
    today  = date.today().isoformat()
    tom    = (date.today() + timedelta(days=1)).isoformat()
    week   = (date.today() + timedelta(days=7)).isoformat()
    sub_cfg = _sub_settings(db)

    _q = """
        SELECT s.*,
               p.name_ar, p.name_en, p.slug AS prod_slug, p.price, p.discount_price,
               (SELECT filename FROM product_images
                WHERE product_id=p.id ORDER BY sort_order LIMIT 1) AS img
        FROM subscriptions s JOIN products p ON s.product_id=p.id
        WHERE s.status='active' AND {cond}
        ORDER BY s.next_renewal_date, s.id
    """
    overdue  = db.execute(_q.format(cond="s.next_renewal_date < ?"),  (today,)).fetchall()
    due_today= db.execute(_q.format(cond="s.next_renewal_date = ?"),  (today,)).fetchall()
    due_tom  = db.execute(_q.format(cond="s.next_renewal_date = ?"),  (tom,)).fetchall()
    upcoming = db.execute(_q.format(cond="s.next_renewal_date > ? AND s.next_renewal_date <= ?"), (tom, week)).fetchall()

    all_subs = db.execute("""
        SELECT s.*, p.name_ar, p.name_en, p.price, p.discount_price
        FROM subscriptions s JOIN products p ON s.product_id=p.id
        ORDER BY s.created_at DESC LIMIT 60
    """).fetchall()

    active_count = db.execute("SELECT COUNT(*) FROM subscriptions WHERE status='active'").fetchone()[0]
    paused_count = db.execute("SELECT COUNT(*) FROM subscriptions WHERE status='paused'").fetchone()[0]
    due_count    = db.execute("SELECT COUNT(*) FROM subscriptions WHERE status='active' AND next_renewal_date <= ?", (today,)).fetchone()[0]
    db.close()

    return render_template('admin/subscriptions.html',
        overdue=overdue, due_today=due_today, due_tom=due_tom,
        upcoming=upcoming, all_subs=all_subs,
        active_count=active_count, paused_count=paused_count, due_count=due_count,
        today=today, tom=tom, sub_cfg=sub_cfg,
        active_admin='subscriptions')


@app.route('/admin/subscriptions/settings', methods=['POST'])
@admin_required
def admin_sub_settings_save():
    db = get_db()
    for key in ('sub_free_delivery_min', 'sub_delivery_days_min', 'sub_delivery_days_max'):
        val = request.form.get(key, '').strip()
        db.execute("INSERT OR REPLACE INTO integration_settings (key, value) VALUES (?,?)", (key, val))
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/admin/subscriptions/<int:sid>/status', methods=['POST'])
@admin_required
def admin_sub_status(sid):
    from datetime import date, timedelta
    action = request.form.get('action', '')
    db     = get_db()
    sub    = db.execute('SELECT * FROM subscriptions WHERE id=?', (sid,)).fetchone()
    if not sub:
        db.close()
        return jsonify({'ok': False}), 404

    today = date.today().isoformat()
    if action in ('delivered', 'skipped'):
        next_date = (date.today() + timedelta(days=sub['interval_days'])).isoformat()
        db.execute('UPDATE subscriptions SET current_status=?, next_renewal_date=? WHERE id=?',
                   ('pending', next_date, sid))
        db.execute('INSERT INTO subscription_logs (subscription_id, renewal_date, action) VALUES (?,?,?)',
                   (sid, today, action))
    elif action == 'confirmed':
        db.execute('UPDATE subscriptions SET current_status=? WHERE id=?', ('confirmed', sid))
        db.execute('INSERT INTO subscription_logs (subscription_id, renewal_date, action) VALUES (?,?,?)',
                   (sid, today, 'confirmed'))
    elif action == 'pause':
        db.execute('UPDATE subscriptions SET status=? WHERE id=?', ('paused', sid))
    elif action == 'cancel':
        db.execute('UPDATE subscriptions SET status=? WHERE id=?', ('cancelled', sid))
    elif action == 'reactivate':
        db.execute('UPDATE subscriptions SET status=? WHERE id=?', ('active', sid))

    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/admin/subscriptions/<int:sid>/notify', methods=['POST'])
@admin_required
def admin_sub_notify(sid):
    db  = get_db()
    sub = db.execute('SELECT * FROM subscriptions WHERE id=?', (sid,)).fetchone()
    if not sub:
        db.close()
        return jsonify({'ok': False}), 404

    prod = db.execute('SELECT name_ar, name_en, slug FROM products WHERE id=?', (sub['product_id'],)).fetchone()
    push_subs = db.execute(
        'SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE phone=? AND product_id IS NULL',
        (sub['phone'],)
    ).fetchall()
    db.close()

    if not push_subs:
        return jsonify({'ok': False, 'msg': 'لا يوجد اشتراك push لهذا الزبون'})

    title = 'تجديد طلبيتك 🔄'
    body  = f"طلبيتك من {prod['name_ar']} جاهزة للتجديد، وافق عبر التطبيق"
    url   = f'/product/{prod["slug"]}'
    sent  = 0
    for ps in push_subs:
        info = {'endpoint': ps['endpoint'], 'keys': {'p256dh': ps['p256dh'], 'auth': ps['auth']}}
        if _push_send(info, title, body, url):
            sent += 1
    if sent:
        db2 = get_db()
        db2.execute('UPDATE subscriptions SET current_status=? WHERE id=?', ('notified', sid))
        db2.execute('INSERT INTO subscription_logs (subscription_id, renewal_date, action) VALUES (?,?,?)',
                    (sid, __import__('datetime').date.today().isoformat(), 'push_sent'))
        db2.commit()
        db2.close()
    return jsonify({'ok': True, 'sent': sent})


# ── Admin: Redirect Manager ──────────────────────────────────────
@app.route("/admin/redirects", methods=["GET", "POST"])
@admin_required
def admin_redirects():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            from_path = '/' + request.form.get("from_path", "").lstrip('/')
            to_path   = request.form.get("to_path", "").strip()
            if from_path and to_path:
                db.execute(
                    "INSERT OR REPLACE INTO redirects (from_path, to_path, is_active, created_at) VALUES (?,?,1,datetime('now'))",
                    (from_path, to_path)
                )
                db.commit()
        elif action == "delete":
            db.execute("DELETE FROM redirects WHERE id=?", (request.form.get("id"),))
            db.commit()
        elif action == "toggle":
            db.execute(
                "UPDATE redirects SET is_active = 1 - is_active WHERE id=?",
                (request.form.get("id"),)
            )
            db.commit()
    rows = db.execute("SELECT * FROM redirects ORDER BY created_at DESC").fetchall()
    db.close()
    return render_template("admin/redirects.html", rows=rows, active_admin="redirects")


# ── Admin: 404 Log ───────────────────────────────────────────────
@app.route("/admin/404-log", methods=["GET", "POST"])
@admin_required
def admin_404_log():
    db = get_db()
    if request.method == "POST" and request.form.get("action") == "clear":
        db.execute("DELETE FROM not_found_log")
        db.commit()
    rows = db.execute(
        "SELECT * FROM not_found_log ORDER BY hit_at DESC LIMIT 200"
    ).fetchall()
    db.close()
    return render_template("admin/404_log.html", rows=rows, active_admin="404log")


if __name__ == "__main__":
    init_db()
    # load API keys from DB into config (overrides env only if DB has a value)
    _boot_db = get_db()
    for _k, _attr in [('gemini_api_key','GEMINI_API_KEY'),('anthropic_api_key','ANTHROPIC_API_KEY'),
                       ('whatsapp_number','WHATSAPP_NUMBER')]:
        _row = _boot_db.execute("SELECT value FROM integration_settings WHERE key=?", (_k,)).fetchone()
        if _row and _row['value']:
            setattr(config, _attr, _row['value'])
    _boot_db.close()
    # احفظ الـ Telegram config بالـ DB إذا ما مسجّل بعد
    _db = get_db()
    _db.execute("INSERT OR IGNORE INTO integration_settings (key,value) VALUES (?,?)",
                ("telegram_token", "8949008033:AAFAUwNSHXbAAXHKGgFKR9gyh4crEp43rEk"))
    _db.execute("INSERT OR IGNORE INTO integration_settings (key,value) VALUES (?,?)",
                ("telegram_chat_id", "991560539"))
    _db.commit()
    _db.close()
    monitor_mod.start_monitor()
    port = int(os.environ.get("FLASK_RUN_PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
