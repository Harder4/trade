import os
import requests
import yfinance as yf
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timezone

# ================= الإعدادات المعزولة =================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# إذا أضفت قناة ثانية في Secrets باسم GOLD_BREAKOUT_CHAT_ID سيرسل لها، وإلا سيرسل للقناة الأساسية
CHAT_ID = os.getenv("GOLD_BREAKOUT_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

BOT_TAG = "⚡ [بوت الاختراق الذهبي - V2]"
SYMBOL = "GC=F"
INTERVAL = "1h"
PERIOD = "30d"
ATR_SL_MULT = 2.0
ATR_TP_MULT = 4.0
# ======================================================

def fetch_and_prepare_data():
    df = yf.download(SYMBOL, interval=INTERVAL, period=PERIOD, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.dropna(inplace=True)

    df['EMA_200'] = df['Close'].ewm(span=200, adjust=False).mean()
    df['High_20'] = df['High'].rolling(window=20).max()
    df['Low_20'] = df['Low'].rolling(window=20).min()

    tr = pd.concat([
        df['High'] - df['Low'],
        (df['High'] - df['Close'].shift()).abs(),
        (df['Low'] - df['Close'].shift()).abs()
    ], axis=1).max(axis=1)
    df['ATR'] = tr.rolling(window=14).mean()

    plus_dm = df['High'].diff()
    minus_dm = df['Low'].diff().abs()
    plus_dm = np.where((plus_dm > minus_dm) & (plus_dm > 0), plus_dm, 0.0)
    minus_dm = np.where((minus_dm > plus_dm) & (minus_dm > 0), minus_dm, 0.0)
    plus_di = 100 * (pd.Series(plus_dm, index=df.index).rolling(window=14).mean() / df['ATR'])
    minus_di = 100 * (pd.Series(minus_dm, index=df.index).rolling(window=14).mean() / df['ATR'])
    dx = (np.abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
    df['ADX'] = dx.rolling(window=14).mean()

    df.dropna(inplace=True)
    return df

def generate_chart(df, signal_type, entry, sl, tp, filename="gold_breakout_chart.png"):
    recent = df.tail(60)
    plt.figure(figsize=(10, 5))
    plt.plot(recent.index, recent['Close'], label='Gold (Close)', color='#1a73e8', linewidth=1.8)
    plt.plot(recent.index, recent['EMA_200'], label='EMA 200', color='orange', linestyle='--', linewidth=1.2)

    plt.axhline(entry, color='black', linestyle='-', linewidth=1.2, label=f'Entry: {entry:.2f}')
    plt.axhline(sl, color='red', linestyle='--', linewidth=1.2, label=f'SL: {sl:.2f}')
    plt.axhline(tp, color='green', linestyle='--', linewidth=1.2, label=f'TP: {tp:.2f}')

    plt.title(f"GOLD BREAKOUT V2 ({SYMBOL}) - {signal_type} (1H)", fontsize=13, fontweight='bold')
    plt.legend(loc='upper left')
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(filename, dpi=150)
    plt.close()

def send_telegram_photo(caption, image_path):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(image_path, "rb") as photo:
        response = requests.post(
            url,
            data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": photo}
        )
    print("Telegram Response:", response.status_code, response.text)

def check_signal():
    is_test = os.getenv("TEST_MODE", "false").lower() == "true"
    now_utc = datetime.now(timezone.utc)

    if not is_test and not (7 <= now_utc.hour <= 17):
        print(f"[V2] خارج أوقات التداول ({now_utc.hour}:00 UTC).")
        return

    df = fetch_and_prepare_data()
    if len(df) < 5:
        print("[V2] البيانات غير كافية.")
        return

    current = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]

    signal = None
    entry = current['Open']
    atr = prev['ATR']

    if is_test:
        signal = "BUY"
        sl = entry - (atr * ATR_SL_MULT)
        tp = entry + (atr * ATR_TP_MULT)
    elif prev['Close'] > prev['EMA_200'] and prev['Close'] > prev2['High_20'] and prev['ADX'] > 20:
        signal = "BUY"
        sl = entry - (atr * ATR_SL_MULT)
        tp = entry + (atr * ATR_TP_MULT)
    elif prev['Close'] < prev['EMA_200'] and prev['Close'] < prev2['Low_20'] and prev['ADX'] > 20:
        signal = "SELL"
        sl = entry + (atr * ATR_SL_MULT)
        tp = entry - (atr * ATR_TP_MULT)

    if signal:
        emoji = "🟢" if signal == "BUY" else "🔴"
        direction_ar = "شراء (BUY)" if signal == "BUY" else "بيع (SELL)"
        test_banner = "🧪 <b>[رسالة تجريبية - TEST]</b>\n" if is_test else ""
        caption = (
            f"{test_banner}"
            f"<b>{BOT_TAG}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} <b>إشارة ذهب جديدة (Donchian Breakout)</b>\n"
            f"📌 <b>النوع:</b> {direction_ar} | ⏱ <b>الفريم:</b> 1H\n\n"
            f"🎯 <b>الدخول:</b> <code>{entry:.2f}</code>\n"
            f"🛑 <b>الوقف (SL):</b> <code>{sl:.2f}</code>\n"
            f"💰 <b>الهدف (TP):</b> <code>{tp:.2f}</code>\n\n"
            f"📊 ADX: {prev['ADX']:.1f} | ATR: {atr:.2f} | R:R 1:2\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"#Gold_Breakout_V2"
        )
        chart_file = "gold_breakout_chart.png"
        generate_chart(df, signal, entry, sl, tp, chart_file)
        send_telegram_photo(caption, chart_file)
        print(f"[V2] تم إرسال إشارة {signal} بنجاح (Test={is_test}).")
    else:
        print(f"[V2] [{now_utc.strftime('%Y-%m-%d %H:%M')} UTC] لا توجد إشارة. السعر: {current['Close']:.2f} | ADX: {prev['ADX']:.1f}")
if __name__ == "__main__":
    check_signal()
