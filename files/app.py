"""
TradingView -> Telegram webhook bot
- يستقبل تنبيه JSON من TradingView على /webhook
- يتحقق من السر والبيانات
- يرسل بطاقة توصية منسقة لقناة تيليغرام
- [جديد] يتابع كل صفقة ويرسل للقناة نتيجتها (ربح/خسارة + عدد النقاط) والحصيلة الكلية
"""
import hmac
import html
import json
import logging
import os
import threading
import time

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webhook")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")  # كلمة سر تضعها داخل JSON في TradingView

SIGNALS_FILE = "signals.json"
EXPIRE_HOURS = 24
_lock = threading.Lock()


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
        f"🎯 الهدف 1: <code>{fmt(s['tp1'])}</code> (1:{s['rr1']:.1f})",
    ]
    if s["tp2"]:
        lines.append(f"🎯 الهدف 2: <code>{fmt(s['tp2'])}</code> (1:{s['rr2']:.1f})")
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
            log.info("Message sent")
    except requests.RequestException as e:
        log.error("Telegram request failed: %s", e)


# ───────────────────────── تتبع النتائج ─────────────────────────

def yf_ticker(symbol):
    """يحول اسم الرمز من TradingView إلى رمز yfinance. يرجع None إذا غير معروف."""
    s = symbol.upper().split(":")[-1].replace("/", "").strip()
    if s in ("XAUUSD", "GOLD"):
        return "GC=F"
    if len(s) == 6 and s.isalpha():
        return f"{s}=X"
    return None


def unit(symbol):
    """(مضاعف، اسم الوحدة): الذهب بالنقاط (دولار)، الفوركس بالبيبس."""
    s = symbol.upper().split(":")[-1].replace("/", "")
    if s in ("XAUUSD", "GOLD"):
        return 1, "نقطة"
    return (100 if s.endswith("JPY") else 10000), "بيب"


def load_signals():
    try:
        with open(SIGNALS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_signals(data):
    with open(SIGNALS_FILE, "w") as f:
        json.dump(data, f)


def add_signal(s):
    t = yf_ticker(s["symbol"])
    if not t:
        log.info("Not tracking unknown symbol %s", s["symbol"])
        return
    with _lock:
        d = load_signals()
        d.append(dict(symbol=s["symbol"], ticker=t, action=s["action"],
                      entry=s["entry"], sl=s["sl"], tp1=s["tp1"],
                      t=time.time(), status="open", pts=0.0))
        save_signals(d)


def stats_text():
    closed = [x for x in load_signals() if x["status"] == "closed"]
    wins = sum(1 for x in closed if x["pts"] > 0)
    lines = ["📊 <b>الحصيلة الكلية</b>",
             f"✅ رابحة: {wins}",
             f"❌ خاسرة: {len(closed) - wins}"]
    # مجموع النقاط لكل رمز على حدة (الذهب بالنقاط، الفوركس بالبيبس)
    totals = {}
    for x in closed:
        totals.setdefault(x["symbol"], 0.0)
        totals[x["symbol"]] += x["pts"]
    for sym, tot in totals.items():
        lines.append(f"📈 {html.escape(sym)}: {tot:+.1f} {unit(sym)[1]}")
    return "\n".join(lines)


def check_signals():
    """يفحص الصفقات المفتوحة ويرسل نتيجة كل صفقة انتهت."""
    import yfinance as yf  # استيراد متأخر كي لا يتعطل التطبيق إن لم يكن مثبتاً

    with _lock:
        d = load_signals()
        open_ = [x for x in d if x["status"] == "open"]
        if not open_:
            return 0
        msgs = []
        cache = {}
        for x in open_:
            tk = x["ticker"]
            if tk not in cache:
                try:
                    cache[tk] = yf.Ticker(tk).history(period="5d", interval="1m")
                except Exception as e:
                    log.error("yfinance failed for %s: %s", tk, e)
                    cache[tk] = None
            df = cache[tk]
            if df is None or df.empty:
                continue

            sell = x["action"] == "SELL"
            mult, uname = unit(x["symbol"])
            mask = [i.timestamp() > x["t"] for i in df.index]
            head = None
            for _, r in df[mask].iterrows():
                sl_hit = r["High"] >= x["sl"] if sell else r["Low"] <= x["sl"]
                tp_hit = r["Low"] <= x["tp1"] if sell else r["High"] >= x["tp1"]
                if sl_hit:  # إذا لمس الاثنين بنفس الشمعة نعتبرها خسارة (حذر)
                    x["pts"] = -abs(x["sl"] - x["entry"]) * mult
                    head = "❌ <b>خسرنا الصفقة</b>"
                    break
                if tp_hit:
                    x["pts"] = abs(x["tp1"] - x["entry"]) * mult
                    head = "✅ <b>ربحنا الصفقة</b>"
                    break
            else:
                if time.time() - x["t"] < EXPIRE_HOURS * 3600:
                    continue
                last = float(df["Close"].iloc[-1])
                x["pts"] = ((x["entry"] - last) if sell else (last - x["entry"])) * mult
                head = f"⏱ <b>انتهت الصفقة ({EXPIRE_HOURS} ساعة)</b>"

            x["status"] = "closed"
            icon = "💰" if x["pts"] > 0 else "🔻"
            side = "بيع" if sell else "شراء"
            msgs.append(f"{head}\n{html.escape(x['symbol'])} | {side} | دخول {fmt(x['entry'])}\n"
                        f"{icon} {x['pts']:+.1f} {uname}")
        save_signals(d)

    for m in msgs:
        send_telegram(m + "\n\n" + stats_text())
    return len(msgs)


# ───────────────────────── المسارات ─────────────────────────

@app.get("/")
def health():
    return "ok", 200


@app.get("/check")
def check():
    # رابط يستدعيه cron-job.org كل 5 دقائق
    try:
        n = check_signals()
    except Exception as e:
        log.exception("check failed")
        return jsonify(error=str(e)), 500
    return jsonify(closed=n), 200


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

    add_signal(signal)  # [جديد] تسجيل الصفقة للمتابعة

    # TradingView يقطع الاتصال بعد ~3 ثوانٍ، فنرد فوراً ونرسل للتلغرام في الخلفية
    threading.Thread(target=send_telegram, args=(build_message(signal),), daemon=True).start()
    return jsonify(status="ok"), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
