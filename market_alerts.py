#!/usr/bin/env python3
"""تنبيهات الفوركس والذهب والنفط إلى تيليغرام.

ما يفعله كل تشغيلة:
  1) يجلب أسعار الرموز في symbols.txt من Twelve Data (الخطة المجانية).
  2) يرسل تنبيهاً عند حركة حادة خلال حوالي ساعة (النسبة المئوية لكل رمز في الملف).
  3) يرسل أي خبر جديد من المصادر الرسمية في feeds.txt (الفيدرالي، المركزي الأوروبي...).

لا يعطي توصيات شراء أو بيع. بدون مكتبات خارجية.

المتغيرات البيئية (Secrets في GitHub):
  TWELVE_DATA_API_KEY   مفتاح مجاني من twelvedata.com
  TELEGRAM_BOT_TOKEN    توكن البوت
  TELEGRAM_CHAT_ID      مثل @اسم_القناة
  DRY_RUN=1             للتجربة المحلية: يطبع بدل الإرسال ولا يحفظ الحالة

وضع الاستكشاف (يستهلك رصيداً واحداً): لمعرفة أسماء رموز السلع الصحيحة
  python market_alerts.py --list commodities
  python market_alerts.py --list commodities oil      (تصفية بكلمة)
"""
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

TD_KEY = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"

ROOT = Path(__file__).resolve().parent
SYMBOLS = ROOT / "symbols.txt"
FEEDS = ROOT / "feeds.txt"
STATE = ROOT / "state" / "market.json"

WINDOW_MIN = 60          # نقارن السعر الحالي بسعر قبل حوالي ساعة
MIN_AGE_MIN = 30         # أقل عمر للمرجع حتى نسمح بالتنبيه
COOLDOWN_MIN = 120       # لا نكرر تنبيه نفس الرمز قبل ساعتين
HISTORY_KEEP_MIN = 26 * 60
MAX_FEED_MSGS = 5        # حد أقصى لرسائل الأخبار في التشغيلة
SEEN_CAP = 2000
UA = "market-alerts-bot/1.0"


def now():
    return time.time()


def fetch_text(url, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            raise


def get_json(url):
    return json.loads(fetch_text(url))


# ---------- قراءة الإعدادات ----------
def read_symbols():
    """سطر: الرمز  النسبة%  [الاسم بالعربية]   مثال: EUR/USD 0.30 اليورو/الدولار"""
    out = []
    for line in SYMBOLS.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = line.split(None, 2)
        sym = parts[0].upper()
        thr = float(parts[1]) if len(parts) > 1 else 0.5
        name = parts[2] if len(parts) > 2 else sym
        out.append((sym, thr, name))
    return out


def read_feeds():
    """سطر: الاسم | الرابط"""
    out = []
    if not FEEDS.exists():
        return out
    for line in FEEDS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        name, url = [x.strip() for x in line.split("|", 1)]
        out.append((name, url))
    return out


# ---------- الأسعار ----------
def fetch_prices(symbols):
    """يرجع {الرمز: السعر}. الرموز التي فشلت تُطبع كتحذير ولا توقف التشغيل."""
    syms = [s for s, _, _ in symbols]
    q = urllib.parse.urlencode({"symbol": ",".join(syms), "apikey": TD_KEY})
    data = get_json(f"https://api.twelvedata.com/price?{q}")
    if isinstance(data, dict) and data.get("status") == "error" and "price" not in data:
        # خطأ عام (مفتاح خاطئ، تجاوز الحد...) أو خطأ رمز وحيد
        msg = data.get("message", data)
        if len(syms) > 1 or data.get("code") in (401, 429):
            raise RuntimeError(f"Twelve Data: {msg}")
        print(f"تحذير {syms[0]}: {msg}")
        return {}
    items = {syms[0]: data} if len(syms) == 1 else data
    prices = {}
    for s in syms:
        item = items.get(s)
        if isinstance(item, dict) and "price" in item:
            try:
                prices[s] = float(item["price"])
            except (TypeError, ValueError):
                print(f"تحذير {s}: سعر غير مفهوم: {item['price']}")
        else:
            msg = item.get("message") if isinstance(item, dict) else "لا توجد بيانات"
            print(f"تحذير {s}: {msg}")
    return prices


def fmt_price(p):
    return f"{p:.5f}" if p < 10 else f"{p:.2f}"


def reference_point(history, t):
    """أقرب نقطة قبل (t - WINDOW_MIN)؛ وإلا أقدم نقطة إن كان عمرها كافياً."""
    target = t - WINDOW_MIN * 60
    older = [h for h in history if h[0] <= target]
    if older:
        return older[-1]
    if history and (t - history[0][0]) >= MIN_AGE_MIN * 60:
        return history[0]
    return None


def format_move(name, sym, price, ref_price, change, minutes):
    arrow = "🔼" if change > 0 else "🔽"
    return "\n".join(
        [
            f"⚡ <b>{html.escape(name)} ({html.escape(sym)})</b>",
            f"حركة حادة {arrow} {change:+.2f}% خلال حوالي {minutes} دقيقة",
            f"السعر الآن: {fmt_price(price)} (كان {fmt_price(ref_price)})",
            "⚠️ معلومات تعليمية وليست نصيحة مالية.",
        ]
    )


def format_feed_item(feed_name, title, link):
    lines = [f"🏛️ <b>{html.escape(feed_name)}</b>", html.escape(title)]
    if link:
        lines.append(f'📎 <a href="{html.escape(link)}">الرابط</a>')
    lines.append("⚠️ معلومات تعليمية وليست نصيحة مالية.")
    return "\n".join(lines)


# ---------- الأخبار الرسمية ----------
def parse_feed(xml_text):
    root = ET.fromstring(xml_text)
    items = []
    for it in root.iter("item"):
        items.append((it.findtext("title", "").strip(), it.findtext("link", "").strip()))
    atom = "{http://www.w3.org/2005/Atom}"
    for en in root.iter(atom + "entry"):
        link_el = en.find(atom + "link")
        link = link_el.get("href", "") if link_el is not None else ""
        items.append((en.findtext(atom + "title", "").strip(), link.strip()))
    return [(t, l) for t, l in items if t]


# ---------- تيليغرام ----------
def send_telegram(text):
    if DRY_RUN or not TG_TOKEN or not TG_CHAT:
        print("---- [لن يُرسل: وضع تجريبي أو بيانات تيليغرام ناقصة] ----")
        print(text)
        return
    body = urllib.parse.urlencode(
        {
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=body)
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


# ---------- الحالة ----------
def load_state():
    if STATE.exists():
        s = json.loads(STATE.read_text(encoding="utf-8"))
    else:
        s = {}
    s.setdefault("history", {})
    s.setdefault("last_alert", {})
    s.setdefault("feeds_seeded", [])
    s.setdefault("feed_seen", [])
    return s


def save_state(s):
    if DRY_RUN:
        return
    STATE.parent.mkdir(parents=True, exist_ok=True)
    s["feed_seen"] = s["feed_seen"][-SEEN_CAP:]
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- وضع الاستكشاف ----------
def list_mode(args):
    if not TD_KEY:
        sys.exit("ضع TWELVE_DATA_API_KEY أولاً.")
    kind = args[0] if args else "commodities"
    needle = args[1].lower() if len(args) > 1 else ""
    data = get_json(f"https://api.twelvedata.com/{kind}?apikey={TD_KEY}")
    rows = data.get("data", data) if isinstance(data, dict) else data
    n = 0
    for row in rows if isinstance(rows, list) else []:
        text = " | ".join(str(v) for v in row.values()) if isinstance(row, dict) else str(row)
        if needle in text.lower():
            print(text)
            n += 1
    print(f"-- {n} نتيجة --")


# ---------- التشغيل الرئيسي ----------
def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--list":
        return list_mode(sys.argv[2:])
    if len(sys.argv) == 2 and sys.argv[1] == "--list":
        return list_mode([])

    if not TD_KEY:
        sys.exit("ضع TWELVE_DATA_API_KEY (مفتاح مجاني من twelvedata.com).")
    symbols = read_symbols()
    if not symbols:
        sys.exit("symbols.txt فارغ.")
    if len(symbols) > 8:
        print("تنبيه: أكثر من 8 رموز مع فحص كل 15 دقيقة قد يتجاوز 800 طلب يومياً (الحد المجاني).")

    state = load_state()
    t = now()

    # 1) الأسعار
    try:
        prices = fetch_prices(symbols)
    except Exception as e:
        print(f"تعذر جلب الأسعار: {e}")
        prices = {}

    for sym, thr, name in symbols:
        if sym not in prices:
            continue
        p = prices[sym]
        hist = state["history"].setdefault(sym, [])
        ref = reference_point(hist, t)
        hist.append([t, p])
        state["history"][sym] = [h for h in hist if h[0] >= t - HISTORY_KEEP_MIN * 60]
        if ref is None or ref[1] == 0:
            continue
        change = (p - ref[1]) / ref[1] * 100
        last = state["last_alert"].get(sym, 0)
        if abs(change) >= thr and (t - last) >= COOLDOWN_MIN * 60:
            minutes = int((t - ref[0]) / 60)
            try:
                send_telegram(format_move(name, sym, p, ref[1], change, minutes))
                state["last_alert"][sym] = t
            except Exception as e:
                print(f"فشل إرسال تنبيه {sym}: {e}")

    # 2) الأخبار الرسمية
    sent = 0
    for fname, url in read_feeds():
        try:
            items = parse_feed(fetch_text(url))
        except Exception as e:
            print(f"تعذر قراءة المصدر {fname}: {e}")
            continue
        if url not in state["feeds_seeded"]:
            for title, link in items:
                key = link or title
                if key not in state["feed_seen"]:
                    state["feed_seen"].append(key)
            state["feeds_seeded"].append(url)
            print(f"{fname}: تم تسجيل {len(items)} عنصراً قديماً بصمت.")
            continue
        for title, link in reversed(items):  # الأقدم أولاً
            key = link or title
            if key in state["feed_seen"]:
                continue
            if sent >= MAX_FEED_MSGS:
                state["feed_seen"].append(key)
                continue
            try:
                send_telegram(format_feed_item(fname, title, link))
            except Exception as e:
                print(f"فشل إرسال خبر ({e}). سنعيد المحاولة لاحقاً.")
                break
            state["feed_seen"].append(key)
            sent += 1
            time.sleep(1)

    save_state(state)
    print(f"انتهى: {len(prices)} سعراً، {sent} خبراً.")


if __name__ == "__main__":
    main()
