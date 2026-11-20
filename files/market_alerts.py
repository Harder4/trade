"""
tracker.py — يتابع الصفقات المفتوحة ويرسل نتيجتها (ربح/خسارة) على القناة.

الاستخدام في app.py:
    from tracker import add_trade, start_tracker
    start_tracker(send_message)          # مرة وحدة عند تشغيل التطبيق
    add_trade(symbol, side, entry, tp1, tp2, sl)   # بعد إرسال كل إشارة

send_message(text): دالتك الحالية اللي بترسل رسالة للقناة.
"""
import json, os, threading, time
from datetime import datetime, timezone
import yfinance as yf

FILE = "trades.json"
LOCK = threading.Lock()
CHECK_EVERY = 300          # كل 5 دقايق
MAX_AGE_HOURS = 48         # الصفقة بتنغلق بدون نتيجة بعد 48 ساعة

# رمز yfinance لكل أداة (عدّل حسب أدواتك)
YF = {"XAU/USD": "GC=F", "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X",
      "USD/JPY": "JPY=X"}


def _load():
    if os.path.exists(FILE):
        with open(FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save(trades):
    with open(FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False)


def add_trade(symbol, side, entry, tp1, tp2, sl):
    with LOCK:
        trades = _load()
        trades.append({
            "symbol": symbol, "side": side.upper(), "entry": float(entry),
            "tp1": float(tp1), "tp2": float(tp2), "sl": float(sl),
            "opened": time.time(), "tp1_hit": False, "closed": False,
        })
        _save(trades)


def _candles(symbol, since_ts):
    ticker = YF.get(symbol)
    if not ticker:
        return []
    df = yf.download(ticker, period="5d", interval="5m", progress=False)
    if df.empty:
        return []
    df = df[df.index.tz_convert("UTC").astype("int64") // 10**9 >= since_ts]
    return [(float(r["High"].iloc[0] if hasattr(r["High"], "iloc") else r["High"]),
             float(r["Low"].iloc[0] if hasattr(r["Low"], "iloc") else r["Low"]))
            for _, r in df.iterrows()]


def _evaluate(t):
    """يرجع (نتيجة, رسالة) أو None إذا ما زالت مفتوحة."""
    buy = t["side"] == "BUY"
    for high, low in _candles(t["symbol"], t["opened"]):
        sl_hit = low <= t["sl"] if buy else high >= t["sl"]
        tp1 = high >= t["tp1"] if buy else low <= t["tp1"]
        tp2 = high >= t["tp2"] if buy else low <= t["tp2"]
        # إذا SL و TP بنفس الشمعة نحسبها خسارة (حذر)
        if sl_hit and not t["tp1_hit"]:
            return "LOSS", "❌ خسارة — ضرب وقف الخسارة"
        if sl_hit and t["tp1_hit"]:
            return "BE", "⚪️ هدف 1 تحقق ثم رجع السعر لوقف الخسارة"
        if tp2:
            return "WIN2", "✅✅ ربح — تحقق الهدف 2"
        if tp1:
            t["tp1_hit"] = True
    return None


def _risk(t):
    return abs(t["entry"] - t["sl"])


def _r_value(result, t):
    r = _risk(t)
    reward1 = abs(t["tp1"] - t["entry"]) / r
    reward2 = abs(t["tp2"] - t["entry"]) / r
    return {"LOSS": -1.0, "BE": reward1 * 0.0, "WIN2": reward2}.get(result, 0.0)


def _loop(send):
    while True:
        try:
            with LOCK:
                trades = _load()
                for t in trades:
                    if t["closed"]:
                        continue
                    res = _evaluate(t)
                    if res:
                        code, msg = res
                        t["closed"], t["result"] = True, code
                        t["r"] = _r_value(code, t)
                        send(f"📊 نتيجة الصفقة\n{t['symbol']} {t['side']} @ {t['entry']}\n"
                             f"{msg}\n📐 النتيجة: {t['r']:+.2f}R")
                    elif time.time() - t["opened"] > MAX_AGE_HOURS * 3600:
                        t["closed"], t["result"], t["r"] = True, "EXPIRED", 0.0
                        send(f"⏳ انتهت الصفقة بدون نتيجة\n{t['symbol']} {t['side']} @ {t['entry']}")
                _save(trades)
        except Exception as e:
            print("tracker error:", e)
        time.sleep(CHECK_EVERY)


def stats():
    """ملخص الأداء — ممكن تنشره أسبوعياً."""
    done = [t for t in _load() if t["closed"] and t.get("result") != "EXPIRED"]
    if not done:
        return "لا توجد صفقات مغلقة بعد."
    wins = sum(1 for t in done if t["result"] == "WIN2")
    total_r = sum(t["r"] for t in done)
    return (f"📈 الأداء: {len(done)} صفقة\n✅ رابحة: {wins}\n"
            f"❌ خاسرة: {sum(1 for t in done if t['result']=='LOSS')}\n"
            f"📐 المجموع: {total_r:+.2f}R")


def start_tracker(send):
    threading.Thread(target=_loop, args=(send,), daemon=True).start()
