"""
TradingView -> Telegram webhook bot
- يستقبل تنبيه JSON من TradingView على /webhook
- يتحقق من السر والبيانات
- يرسل بطاقة توصية منسقة لقناة تيليغرام
"""
import hmac
import html
import logging
import os
import threading

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webhook")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")  # كلمة سر تضعها داخل JSON في TradingView


def fmt(x):
    """تنسيق الأرقام: 2600.0 -> 2600 ، 1.08500 -> 1.085"""
    return f"{float(x):.5f}".rstrip("0").rstrip(".")


def parse_signal(data):
    """يتحقق من البيانات ويرجع قاموساً نظيفاً، أو يرمي ValueError."""
    action = str(data.get("action", "")).upper().strip()
    action = {"LONG": "BUY", "SHORT": "SELL"}.get(action, action)
    if action not in ("BUY", "SELL"):
        raise ValueError("action يجب أن يكون BUY أو SELL")

    symbol = str(data.get("symbol", "")).strip()
    if not symbol:
        raise ValueError("symbol مفقود")

    entry = float(data["entry"])
    sl = float(data["sl"])
    tp1 = float(data["tp1"])
    tp2 = float(data["tp2"]) if data.get("tp2") not in (None, "", 0) else None

    # فحص منطقي كي لا تُنشر صفقة بأرقام معكوسة بالخطأ
    if action == "BUY" and not (sl < entry < tp1):
        raise ValueError("BUY: يجب أن يكون sl < entry < tp1")
    if action == "SELL" and not (tp1 < entry < sl):
        raise ValueError("SELL: يجب أن يكون tp1 < entry < sl")

    risk = abs(entry - sl)
    rr1 = abs(tp1 - entry) / risk if risk else 0
    rr2 = abs(tp2 - entry) / risk if (tp2 and risk) else None

    return dict(symbol=symbol, action=action, entry=entry, sl=sl,
                tp1=tp1, tp2=tp2, rr1=rr1, rr2=rr2)


def build_message(s):
    is_buy = s["action"] == "BUY"
    head = "🟢 شراء BUY" if is_buy else "🔴 بيع SELL"
    lines = [
        f"{head} | <b>{html.escape(s['symbol'])}</b>",
        "━━━━━━━━━━━━━━",
        f"📍 الدخول: <code>{fmt(s['entry'])}</code>",
        f"🎯 الهدف 1: <code>{fmt(s['tp1'])}</code>  (1:{s['rr1']:.1f})",
    ]
    if s["tp2"]:
        lines.append(f"🎯 الهدف 2: <code>{fmt(s['tp2'])}</code>  (1:{s['rr2']:.1f})")
    lines += [
        f"🛑 وقف الخسارة: <code>{fmt(s['sl'])}</code>",
        "━━━━━━━━━━━━━━",
        "⚠️ إدارة المخاطرة مسؤوليتك — ليست نصيحة مالية",
    ]
    return "\n".join(lines)


def send_telegram(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if not r.ok:
            log.error("Telegram error %s: %s", r.status_code, r.text)
        else:
            log.info("Signal sent")
    except requests.RequestException as e:
        log.error("Telegram request failed: %s", e)


@app.get("/")
def health():
    return "ok", 200


@app.post("/webhook")
def webhook():
    # TradingView يرسل أحياناً text/plain، لذلك force=True
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify(error="JSON غير صالح"), 400

    if WEBHOOK_SECRET and not hmac.compare_digest(
        str(data.get("secret", "")), WEBHOOK_SECRET
    ):
        log.warning("Rejected: wrong secret")
        return jsonify(error="unauthorized"), 401

    try:
        signal = parse_signal(data)
    except (ValueError, KeyError, TypeError) as e:
        log.warning("Bad payload: %s | %s", e, data)
        return jsonify(error=str(e)), 400

    # TradingView يقطع الاتصال بعد ~3 ثوانٍ، فنرد فوراً ونرسل للتلغرام في الخلفية
    threading.Thread(target=send_telegram, args=(build_message(signal),), daemon=True).start()
    return jsonify(status="ok"), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
