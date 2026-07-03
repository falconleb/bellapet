"""
Bella Pet — نظام المراقبة الذكي
يشتغل في background thread، يبعت تنبيهات تيليغرام تلقائياً
ويستقبل أوامر ويرد بأزرار inline
"""
import threading
import time
import json
import urllib.request as _req
import urllib.parse as _parse
from datetime import datetime

from database import get_db


# ── Telegram API Helpers ─────────────────────────────────────────

def _tg_post(token: str, method: str, payload: dict):
    if not token:
        return None
    try:
        data = json.dumps(payload).encode()
        req  = _req.Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        resp = _req.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception:
        return None


def tg_send(token: str, chat_id: str, text: str):
    _tg_post(token, "sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML"
    })


def _tg_send_menu(token: str, chat_id: str, text: str):
    """بعت رسالة مع لوحة الأزرار الرئيسية."""
    _tg_post(token, "sendMessage", {
        "chat_id":      chat_id,
        "text":         text,
        "parse_mode":   "HTML",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "📊 الحالة الآن",     "callback_data": "cmd_status"},
                    {"text": "📦 آخر الطلبات",     "callback_data": "cmd_orders"},
                ],
                [
                    {"text": "⚠️ المخزون",          "callback_data": "cmd_stock"},
                    {"text": "🖼 محتوى ناقص",        "callback_data": "cmd_missing"},
                ],
                [
                    {"text": "🔍 فحص كل شي",        "callback_data": "cmd_checkall"},
                    {"text": "☀️ التقرير اليومي",   "callback_data": "cmd_report"},
                ],
            ]
        }
    })


def _tg_edit_menu(token: str, chat_id: str, message_id: int, text: str):
    """عدّل رسالة موجودة وحطّ الأزرار من جديد."""
    _tg_post(token, "editMessageText", {
        "chat_id":      chat_id,
        "message_id":   message_id,
        "text":         text,
        "parse_mode":   "HTML",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "📊 الحالة الآن",     "callback_data": "cmd_status"},
                    {"text": "📦 آخر الطلبات",     "callback_data": "cmd_orders"},
                ],
                [
                    {"text": "⚠️ المخزون",          "callback_data": "cmd_stock"},
                    {"text": "🖼 محتوى ناقص",        "callback_data": "cmd_missing"},
                ],
                [
                    {"text": "🔍 فحص كل شي",        "callback_data": "cmd_checkall"},
                    {"text": "☀️ التقرير اليومي",   "callback_data": "cmd_report"},
                ],
            ]
        }
    })


def _tg_answer_callback(token: str, callback_id: str):
    _tg_post(token, "answerCallbackQuery", {"callback_query_id": callback_id})


def _get_tg_config():
    db = get_db()
    rows = db.execute(
        "SELECT key, value FROM integration_settings WHERE key IN (?,?)",
        ("telegram_token", "telegram_chat_id")
    ).fetchall()
    db.close()
    cfg = {r["key"]: r["value"] for r in rows}
    return cfg.get("telegram_token", ""), cfg.get("telegram_chat_id", "")


def send_alert(text: str):
    token, chat_id = _get_tg_config()
    tg_send(token, chat_id, text)


# ── Commands / Checks ────────────────────────────────────────────

def cmd_status() -> str:
    db = get_db()
    today = db.execute("""
        SELECT COUNT(*) as cnt, COALESCE(SUM(total),0) as rev
        FROM orders WHERE date(created_at)=date('now')
    """).fetchone()
    pending = db.execute("""
        SELECT COUNT(*) as cnt FROM orders WHERE status='pending'
    """).fetchone()["cnt"]
    sessions = db.execute("""
        SELECT COUNT(*) as cnt FROM analytics_sessions
        WHERE date(started_at)=date('now')
    """).fetchone()["cnt"]
    products = db.execute(
        "SELECT COUNT(*) as cnt FROM products WHERE is_active=1"
    ).fetchone()["cnt"]
    db.close()
    return (
        f"📊 <b>الحالة الآن — {datetime.now().strftime('%H:%M')}</b>\n\n"
        f"📦 طلبات اليوم: <b>{today['cnt']}</b>  (${ today['rev']:.0f})\n"
        f"🕐 طلبات معلّقة: <b>{pending}</b>\n"
        f"👥 زيارات اليوم: <b>{sessions}</b>\n"
        f"🛍 منتجات نشطة: <b>{products}</b>"
    )


def cmd_orders() -> str:
    db = get_db()
    rows = db.execute("""
        SELECT id, customer_name, total, status, created_at
        FROM orders ORDER BY id DESC LIMIT 6
    """).fetchall()
    db.close()
    if not rows:
        return "📦 ما في طلبات بعد."
    status_emoji = {"pending":"🕐","processing":"⚙️","shipped":"🚚","delivered":"✅","cancelled":"❌"}
    lines = "\n".join(
        f"{status_emoji.get(r['status'],'•')} #{r['id']} — {r['customer_name']} — ${r['total']:.0f}"
        for r in rows
    )
    return f"📦 <b>آخر الطلبات:</b>\n\n{lines}"


def cmd_stock() -> str:
    db = get_db()
    zero = db.execute("""
        SELECT name_ar FROM products WHERE is_active=1 AND stock_qty=0
    """).fetchall()
    low = db.execute("""
        SELECT name_ar, stock_qty FROM products
        WHERE is_active=1 AND stock_qty>0 AND stock_qty<=3
        ORDER BY stock_qty
    """).fetchall()
    db.close()
    parts = []
    if zero:
        names = "\n".join(f"  • {r['name_ar']}" for r in zero[:6])
        parts.append(f"🔴 <b>نفد ({len(zero)}):</b>\n{names}")
    if low:
        names = "\n".join(f"  • {r['name_ar']} ({r['stock_qty']} قطعة)" for r in low[:6])
        parts.append(f"🟡 <b>مخزون قليل ({len(low)}):</b>\n{names}")
    return "\n\n".join(parts) if parts else "✅ المخزون كله تمام!"


def check_products_missing() -> str | None:
    db = get_db()
    rows = db.execute("""
        SELECT p.name_ar,
               (SELECT COUNT(*) FROM product_images pi WHERE pi.product_id=p.id) AS img_count,
               p.description_ar
        FROM products p WHERE p.is_active=1
    """).fetchall()
    db.close()
    no_img  = [r for r in rows if r["img_count"] == 0]
    no_desc = [r for r in rows if not r["description_ar"]]
    alerts = []
    if no_img:
        names = "\n".join(f"  • {r['name_ar']}" for r in no_img[:5])
        alerts.append(f"🖼 <b>{len(no_img)} منتج بدون صورة:</b>\n{names}")
    if no_desc:
        names = "\n".join(f"  • {r['name_ar']}" for r in no_desc[:5])
        alerts.append(f"✍️ <b>{len(no_desc)} منتج بدون وصف:</b>\n{names}")
    return "\n\n".join(alerts) if alerts else None


def cmd_missing() -> str:
    result = check_products_missing()
    return result if result else "✅ كل المنتجات عندها صور وأوصاف!"


def cmd_checkall() -> str:
    alerts = []
    checks = [
        check_recent_errors,
        check_zero_stock,
        check_products_missing,
        check_traffic_spike,
    ]
    for fn in checks:
        try:
            r = fn()
            if r:
                alerts.append(r)
        except Exception:
            pass
    return "🔍 <b>فحص شامل:</b>\n\n" + "\n\n──────\n\n".join(alerts) if alerts else "✅ <b>فحص شامل:</b> كل شي تمام!"


def check_zero_stock() -> str | None:
    db = get_db()
    rows = db.execute(
        "SELECT name_ar FROM products WHERE is_active=1 AND stock_qty=0"
    ).fetchall()
    db.close()
    if not rows:
        return None
    names = "\n".join(f"  • {r['name_ar']}" for r in rows[:8])
    return f"⚠️ <b>{len(rows)} منتج نفد من المخزون:</b>\n{names}"


def check_no_orders() -> str | None:
    hour = datetime.now().hour
    if hour < 10 or hour > 22:
        return None
    db = get_db()
    row = db.execute("""
        SELECT COUNT(*) as cnt FROM orders
        WHERE created_at >= datetime('now','-6 hours')
    """).fetchone()
    db.close()
    if row["cnt"] == 0:
        return "😴 <b>تنبيه:</b> ما في أي طلب من 6 ساعات!"
    return None


def check_traffic_spike() -> str | None:
    db = get_db()
    last_hour = db.execute("""
        SELECT COUNT(*) as cnt FROM analytics_events
        WHERE event_type='page_view' AND created_at >= datetime('now','-1 hour')
    """).fetchone()["cnt"]
    avg_hour = db.execute("""
        SELECT COALESCE(AVG(cnt),0) as avg FROM (
            SELECT COUNT(*) as cnt FROM analytics_events
            WHERE event_type='page_view'
              AND created_at BETWEEN datetime('now','-25 hours') AND datetime('now','-1 hour')
            GROUP BY strftime('%H', created_at)
        )
    """).fetchone()["avg"]
    db.close()
    if avg_hour > 0 and last_hour > avg_hour * 4 and last_hour > 20:
        return (f"🚀 <b>ضغط عالي!</b>\n"
                f"الزيارات بآخر ساعة: <b>{last_hour}</b>\n"
                f"المعدل العادي: {int(avg_hour)}/ساعة")
    return None


def check_recent_errors() -> str | None:
    db = get_db()
    try:
        cnt = db.execute("""
            SELECT COUNT(*) as cnt FROM error_log
            WHERE created_at >= datetime('now','-1 hour')
        """).fetchone()["cnt"]
        db.close()
        if cnt > 0:
            return f"🔴 <b>{cnt} خطأ 500</b> بالساعة الأخيرة!"
    except Exception:
        db.close()
    return None


def daily_report() -> str:
    db = get_db()
    today    = db.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(total),0) as rev FROM orders WHERE date(created_at)=date('now')").fetchone()
    yest     = db.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(total),0) as rev FROM orders WHERE date(created_at)=date('now','-1 day')").fetchone()
    sessions = db.execute("SELECT COUNT(*) as cnt FROM analytics_sessions WHERE date(started_at)=date('now')").fetchone()["cnt"]
    low      = db.execute("SELECT COUNT(*) as cnt FROM products WHERE is_active=1 AND stock_qty<=3 AND stock_qty>0").fetchone()["cnt"]
    db.close()
    arrow = "📈" if today["cnt"] >= yest["cnt"] else "📉"
    return (
        f"☀️ <b>تقرير الصباح — {datetime.now().strftime('%Y-%m-%d')}</b>\n\n"
        f"📦 طلبات اليوم: <b>{today['cnt']}</b> (${today['rev']:.0f})\n"
        f"{arrow} أمس: {yest['cnt']} (${yest['rev']:.0f})\n"
        f"👥 زيارات اليوم: <b>{sessions}</b>\n"
        f"⚠️ مخزون قليل (≤3): <b>{low}</b>\n\n"
        f"<i>Bella Pet Monitor 🐾</i>"
    )


# ── Command Dispatcher ───────────────────────────────────────────

_COMMANDS = {
    "cmd_status":   cmd_status,
    "cmd_orders":   cmd_orders,
    "cmd_stock":    cmd_stock,
    "cmd_missing":  cmd_missing,
    "cmd_checkall": cmd_checkall,
    "cmd_report":   daily_report,
}

_WELCOME = (
    "🐾 <b>Bella Pet Monitor</b>\n\n"
    "اختار شو بدك تشوف:"
)

def _handle_update(token: str, chat_id: str, update: dict):
    """معالجة رسالة أو callback من تيليغرام."""
    # Callback من زر
    if "callback_query" in update:
        cb   = update["callback_query"]
        data = cb.get("data", "")
        msg_id = cb["message"]["message_id"]
        _tg_answer_callback(token, cb["id"])
        fn = _COMMANDS.get(data)
        if fn:
            result = fn()
            _tg_edit_menu(token, chat_id, msg_id, result)
        return

    # رسالة نصية
    if "message" in update:
        text = update["message"].get("text", "").strip().lower()
        if text in ("/start", "/menu", "قائمة", "menu", "ابدا"):
            _tg_send_menu(token, chat_id, _WELCOME)
        else:
            _tg_send_menu(token, chat_id, _WELCOME)


# ── Polling Loop ─────────────────────────────────────────────────

_last_update_id = 0

def _polling_loop():
    global _last_update_id
    token, chat_id = _get_tg_config()
    if not token:
        return

    while True:
        try:
            token, chat_id = _get_tg_config()
            result = _tg_post(token, "getUpdates", {
                "offset":  _last_update_id + 1,
                "timeout": 20,
                "allowed_updates": ["message", "callback_query"]
            })
            if result and result.get("ok"):
                for upd in result.get("result", []):
                    _last_update_id = upd["update_id"]
                    try:
                        _handle_update(token, chat_id, upd)
                    except Exception:
                        pass
        except Exception:
            time.sleep(5)


# ── Monitor Loop (تنبيهات تلقائية) ──────────────────────────────

_last_daily          = None
_last_no_order_alert = None


def _monitor_loop():
    global _last_daily, _last_no_order_alert
    time.sleep(60)

    while True:
        try:
            now = datetime.now()

            if now.hour == 8 and now.minute < 5:
                if _last_daily != now.date():
                    _last_daily = now.date()
                    send_alert(daily_report())

            alerts = []
            for fn in [check_recent_errors, check_traffic_spike, check_zero_stock]:
                r = fn()
                if r:
                    alerts.append(r)

            no_ord = check_no_orders()
            if no_ord:
                if _last_no_order_alert is None or (now - _last_no_order_alert).seconds > 6 * 3600:
                    _last_no_order_alert = now
                    alerts.append(no_ord)

            if alerts:
                send_alert("\n\n──────────\n\n".join(alerts))

            if now.minute < 1:
                missing = check_products_missing()
                if missing:
                    send_alert("📋 <b>محتوى ناقص:</b>\n\n" + missing)

        except Exception:
            pass

        time.sleep(30 * 60)


# ── Entry Point ──────────────────────────────────────────────────

def start_monitor():
    """شغّل المراقبة والـ polling في background threads."""
    threading.Thread(target=_monitor_loop, daemon=True).start()
    threading.Thread(target=_polling_loop, daemon=True).start()
