"""
التقويم الاقتصادي الأسبوعي -> قناة تيليغرام
- المصدر: Forex Factory (ملف JSON مجاني)
- يعرض الأخبار عالية التأثير فقط للعملات الرئيسية (تؤثر على الذهب والفوركس)
- الأوقات بتوقيت سوريا (UTC+3)

التشغيل:
    python calendar_post.py --dry-run     # يطبع الرسالة فقط بدون إرسال
    python calendar_post.py               # يرسل للقناة (يحتاج BOT_TOKEN و CHAT_ID)
"""
import argparse
import html
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# العملات المتابعة (USD أهمها للذهب)
CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"}
IMPACTS = {"High"}  # لإضافة المتوسط: {"High", "Medium"}

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Asia/Damascus")
except Exception:  # احتياطي إذا لم تتوفر قاعدة المناطق الزمنية
    LOCAL_TZ = timezone(timedelta(hours=3))

DAYS_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]

FLAGS = {
    "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵",
    "CHF": "🇨🇭", "AUD": "🇦🇺", "CAD": "🇨🇦", "NZD": "🇳🇿",
}

# ترجمة مختصرة لأهم الأحداث (بحث بالكلمة المفتاحية، والأطول أولاً)
AR_NAMES = [
    ("Non-Farm Employment Change", "الوظائف غير الزراعية NFP"),
    ("Federal Funds Rate", "قرار الفائدة الأمريكية"),
    ("FOMC Statement", "بيان الفيدرالي FOMC"),
    ("FOMC Meeting Minutes", "محضر اجتماع الفيدرالي"),
    ("FOMC Press Conference", "مؤتمر الفيدرالي الصحفي"),
    ("Core CPI", "التضخم الأساسي CPI"),
    ("CPI", "التضخم CPI"),
    ("Core PCE", "مؤشر PCE الأساسي"),
    ("PPI", "أسعار المنتجين PPI"),
    ("Unemployment Claims", "طلبات إعانة البطالة"),
    ("Unemployment Rate", "معدل البطالة"),
    ("ADP Non-Farm", "وظائف ADP"),
    ("Retail Sales", "مبيعات التجزئة"),
    ("GDP", "الناتج المحلي GDP"),
    ("ISM Manufacturing", "مؤشر ISM الصناعي"),
    ("ISM Services", "مؤشر ISM الخدمي"),
    ("Fed Chair", "تصريحات رئيس الفيدرالي"),
    ("Powell", "تصريحات باول"),
    ("Main Refinancing Rate", "قرار فائدة المركزي الأوروبي"),
    ("ECB Press Conference", "مؤتمر المركزي الأوروبي"),
    ("Official Bank Rate", "قرار فائدة بنك إنجلترا"),
    ("Monetary Policy Statement", "بيان السياسة النقدية"),
    ("BOJ Policy Rate", "قرار فائدة بنك اليابان"),
    ("Cash Rate", "قرار الفائدة"),
    ("Employment Change", "التغير بالوظائف"),
]

GOLD_NOTE = "⭐ = يؤثر مباشرة على الذهب"


def translate(title: str) -> str:
    for key, ar in AR_NAMES:
        if key.lower() in title.lower():
            return f"{ar} ({title})" if len(title) < 40 else ar
    return title


def fetch_events():
    r = requests.get(FEED_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def filter_events(events):
    out = []
    for e in events:
        if e.get("country") not in CURRENCIES:
            continue
        if e.get("impact") not in IMPACTS:
            continue
        try:
            dt = datetime.fromisoformat(e["date"]).astimezone(LOCAL_TZ)
        except (KeyError, ValueError):
            continue
        out.append({**e, "dt": dt})
    out.sort(key=lambda x: x["dt"])
    return out


def build_message(events):
    if not events:
        return "📅 لا توجد أخبار عالية التأثير هذا الأسبوع."

    by_day = defaultdict(list)
    for e in events:
        by_day[e["dt"].date()].append(e)

    first, last = events[0]["dt"], events[-1]["dt"]
    lines = [
        "📅 <b>التقويم الاقتصادي لهذا الأسبوع</b>",
        f"🗓 {first:%d/%m} → {last:%d/%m}  |  🕒 بتوقيت سوريا",
        "🔴 أخبار عالية التأثير فقط",
        GOLD_NOTE,
        "",
    ]
    for day in sorted(by_day):
        lines.append(f"━━━ <b>{DAYS_AR[day.weekday()]} {day:%d/%m}</b> ━━━")
        for e in by_day[day]:
            gold = "⭐ " if e["country"] == "USD" else ""
            lines.append(
                f"{e['dt']:%H:%M} {FLAGS.get(e['country'], '')} {gold}"
                f"{html.escape(translate(e.get('title', '')))}"
            )
            extra = []
            if e.get("forecast"):
                extra.append(f"المتوقع: {e['forecast']}")
            if e.get("previous"):
                extra.append(f"السابق: {e['previous']}")
            if extra:
                lines.append("    " + " | ".join(extra))
        lines.append("")
    lines += [
        "⚠️ وقت الأخبار الكبيرة الحركة بتكون عنيفة. خفّف حجم الصفقة أو ابتعد.",
        "ليست نصيحة مالية.",
    ]
    return "\n".join(lines)


def split_message(text, limit=4000):
    """تيليغرام يقبل 4096 حرفاً كحد أقصى؛ نقسم عند الأسطر الفارغة."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > limit:
            parts.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        parts.append(cur)
    return parts


def send_telegram(text):
    token = os.environ["BOT_TOKEN"]
    chat_id = os.environ["CHAT_ID"]
    for part in split_message(text):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": part, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if not r.ok:
            print("Telegram error:", r.status_code, r.text, file=sys.stderr)
            sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="اطبع الرسالة فقط")
    ap.add_argument("--file", help="اقرأ من ملف JSON محلي للاختبار")
    args = ap.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            raw = json.load(f)
    else:
        raw = fetch_events()

    msg = build_message(filter_events(raw))
    if args.dry_run:
        print(msg)
    else:
        send_telegram(msg)
        print("تم الإرسال ✅")


if __name__ == "__main__":
    main()
