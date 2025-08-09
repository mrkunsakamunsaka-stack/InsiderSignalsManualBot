import os
import logging
from datetime import datetime, timedelta, time as dtime
import pytz
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackContext, JobQueue

# ============== BASIC SETUP ==============
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TELEGRAM_MANUAL_TOKEN")  # <-- put this in Render (do NOT paste here)
TIMEZONE = os.getenv("TIMEZONE", "Europe/Dublin")
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "20"))

# Optional: force-send to a specific chat ID (e.g., your private chat or a channel ID)
# If not set, the bot will send to the last chat that used /start
FORCED_CHAT_ID = os.getenv("CHAT_ID")  # like "123456789" or "-100123456789" for channels

tz = pytz.timezone(TIMEZONE)

# In-memory signals store (simple demo)
signals = []
last_chat_id = None  # we remember who pressed /start (if CHAT_ID not set)

# ============== HELPERS ==============
def target_chat_id(update: Update = None) -> int:
    """
    Decide where to send messages:
    1) If FORCED_CHAT_ID is set, use it.
    2) Else use where the user typed /start most recently.
    """
    global last_chat_id
    if FORCED_CHAT_ID:
        return int(FORCED_CHAT_ID)
    if update:
        last_chat_id = update.effective_chat.id
        return last_chat_id
    return last_chat_id

def format_signal(sig) -> str:
    t = sig["time"].strftime("%Y-%m-%d %H:%M:%S")
    tps = ", ".join([str(x) for x in sig["tp"]])
    return (
        f"📢 <b>New Signal</b>\n"
        f"Coin: <b>{sig['coin']}</b>\n"
        f"Entry: <b>{sig['entry']}</b>\n"
        f"TP: <b>{tps}</b>\n"
        f"SL: <b>{sig['sl']}</b>\n"
        f"Time: <code>{t}</code>"
    )

# ============== COMMANDS ==============
def start(update: Update, context: CallbackContext):
    chat_id = target_chat_id(update)
    context.bot.send_message(
        chat_id=chat_id,
        text=(
            "👋 Welcome to <b>InsiderSignals_Manual</b>.\n\n"
            "You’ll receive watchlist alerts, confirmed trade signals (Entry / TP / SL), "
            "Ripple legal/banking news summaries, and a daily report at 20:00.\n\n"
            "Commands:\n"
            "/signals – show active signals\n"
            "/ping – check bot is alive\n"
            "/help – show help"
        ),
        parse_mode=ParseMode.HTML
    )

def help_cmd(update: Update, context: CallbackContext):
    chat_id = target_chat_id(update)
    context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🆘 <b>Help</b>\n\n"
            "/start – register this chat for alerts\n"
            "/signals – list current active demo signals\n"
            "/ping – bot health check\n\n"
            "Daily performance report is sent every day at 20:00 (your timezone setting)."
        ),
        parse_mode=ParseMode.HTML
    )

def ping(update: Update, context: CallbackContext):
    chat_id = target_chat_id(update)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    context.bot.send_message(chat_id=chat_id, text=f"✅ Bot alive: {now} {TIMEZONE}")

def list_signals(update: Update, context: CallbackContext):
    chat_id = target_chat_id(update)
    if not signals:
        context.bot.send_message(chat_id=chat_id, text="No active signals right now.")
        return
    lines = ["📋 <b>Active Signals</b>"]
    for s in signals:
        lines.append(
            f"• {s['coin']}: Entry {s['entry']} | TP {', '.join(map(str, s['tp']))} | SL {s['sl']}"
        )
    context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML)

# ============== JOBS (BACKGROUND TASKS) ==============
def generate_demo_signal(context: CallbackContext):
    """Demo signal every 6 hours. Replace with real scanner later."""
    chat_id = target_chat_id()
    if not chat_id:
        return  # nobody pressed /start yet and no CHAT_ID set
    demo = {
        "coin": "XRP",
        "entry": 0.55,
        "tp": [0.60, 0.65],
        "sl": 0.52,
        "time": datetime.now(tz),
    }
    signals.append(demo)
    context.bot.send_message(chat_id=chat_id, text=format_signal(demo), parse_mode=ParseMode.HTML)

def send_daily_report(context: CallbackContext):
    """Daily 20:00 report of last 24h signals (demo)."""
    chat_id = target_chat_id()
    if not chat_id:
        return
    cutoff = datetime.now(tz) - timedelta(days=1)
    recent = [s for s in signals if s["time"] >= cutoff]
    if not recent:
        msg = "📊 Daily Report (last 24h): No signals in the last 24 hours."
    else:
        lines = ["📊 <b>Daily Report</b> (last 24h)"]
        for s in recent:
            t = s["time"].strftime("%H:%M")
            lines.append(f"• {t} {s['coin']} – Entry {s['entry']} | TP {', '.join(map(str, s['tp']))} | SL {s['sl']}")
        msg = "\n".join(lines)
    context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

# ============== MAIN ==============
def main():
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_MANUAL_TOKEN environment variable.")

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("ping", ping))
    dp.add_handler(CommandHandler("signals", list_signals))

    jq: JobQueue = updater.job_queue

    # Demo: generate a signal every 6 hours
    jq.run_repeating(generate_demo_signal, interval=6*60*60, first=10)

    # Daily report at 20:00 in your timezone
    hour = DAILY_REPORT_HOUR
    send_time = dtime(hour=hour, minute=0, tzinfo=tz)
    jq.run_daily(send_daily_report, time=send_time)

    logging.info("InsiderSignals_Manual started.")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
