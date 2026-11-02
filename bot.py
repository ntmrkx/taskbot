"""
Personal task & reminder bot for Telegram  (v2: + school email summaries)

Commands:
  /add <когда> [ЧЧ:ММ] <задача>   add a task
  /list                           all open tasks
  /today                          overdue + today + tomorrow
  /done <id>                      mark a task as done
  /delete <id>                    delete a task
  /checkmail                      check email right now
  /help                           show help

<когда>: сегодня/today, завтра/tomorrow, пн..вс / mon..sun, 20.11., 20.11.2026, 2026-11-20

Reminders:
  - tasks with a time:    1 day before and 1 hour before
  - tasks without a time: 2 days before (at 9:00)
  - every morning at 8:00: summary of overdue, today and tomorrow

Email (optional): every 5 minutes the bot reads new mail in Gmail, asks Claude
for a short summary and sends it to Telegram with "add task" buttons.

Environment variables:
  BOT_TOKEN            Telegram token from @BotFather          (required)
  GMAIL_ADDRESS        Gmail that receives your forwarded mail  (needed for email)
  GMAIL_APP_PASSWORD   16-letter Google app password            (needed for email)
  ANTHROPIC_API_KEY    API key from console.anthropic.com       (needed for email)
  CLAUDE_MODEL         optional, default "claude-haiku-5-5"
"""
import asyncio
import email
import html
import imaplib
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time as dtime, timedelta
from email import policy
from zoneinfo import ZoneInfo

import anthropic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, Defaults

# ---------- settings ----------

TOKEN = os.environ.get("BOT_TOKEN")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
MAIL_ENABLED = bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD and ANTHROPIC_API_KEY)

TZ = ZoneInfo("Europe/Prague")
DB_PATH = "tasks.db"
SUMMARY_TIME = dtime(8, 0, tzinfo=TZ)  # daily morning summary
CHECK_EVERY = 60                       # seconds between reminder checks
MAIL_CHECK_EVERY = 300                 # seconds between email checks
MAX_EMAILS_PER_CHECK = 10
MAX_BODY_CHARS = 8000                  # longer emails are cut (saves API money)

WEEKDAYS = {
    "mon": 0, "monday": 0, "пн": 0, "понедельник": 0,
    "tue": 1, "tuesday": 1, "вт": 1, "вторник": 1,
    "wed": 2, "wednesday": 2, "ср": 2, "среда": 2,
    "thu": 3, "thursday": 3, "чт": 3, "четверг": 3,
    "fri": 4, "friday": 4, "пт": 4, "пятница": 4,
    "sat": 5, "saturday": 5, "сб": 5, "суббота": 5,
    "sun": 6, "sunday": 6, "вс": 6, "воскресенье": 6,
}
TODAY_WORDS = {"today", "сегодня"}
TOMORROW_WORDS = {"tomorrow", "завтра"}
DAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

EXAMPLES = (
    "Примеры:\n"
    "/add завтра 14:30 Врач\n"
    "/add сб Убраться в комнате\n"
    "/add 20.11. Экзамен по линейной алгебре"
)
USAGE_ADD = "Формат: /add <когда> [ЧЧ:ММ] <задача>\n" + EXAMPLES
HELP = (
    "Команды:\n"
    "/add <когда> [ЧЧ:ММ] <задача> — добавить задачу\n"
    "/list — все открытые задачи\n"
    "/today — просроченные, сегодня и завтра\n"
    "/done <id> — отметить выполненной\n"
    "/delete <id> — удалить задачу\n"
    "/checkmail — проверить почту сейчас\n\n"
    "<когда>: сегодня, завтра, пн–вс, 20.11., 20.11.2026\n\n"
    + EXAMPLES
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)        # hide every poll request
logging.getLogger("apscheduler").setLevel(logging.WARNING)  # hide "job executed" lines

ai = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if MAIL_ENABLED else None
MAIL_LOCK = asyncio.Lock()
last_mail_error = None

# ---------- database ----------


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                title    TEXT NOT NULL,
                due      TEXT NOT NULL,             -- ISO datetime
                has_time INTEGER NOT NULL,          -- 1 if the user gave HH:MM
                done     INTEGER NOT NULL DEFAULT 0,
                sent     TEXT NOT NULL DEFAULT ''   -- reminders already sent, e.g. "1d,1h"
            )""")
        con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS suggestions (  -- deadlines found in emails, waiting for a button press
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                title    TEXT NOT NULL,
                due      TEXT NOT NULL,
                has_time INTEGER NOT NULL,
                added    INTEGER NOT NULL DEFAULT 0
            )""")


def get_setting(key):
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key, value):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))


def get_owner():
    value = get_setting("owner")
    return int(value) if value else None


def open_tasks():
    with db() as con:
        return con.execute("SELECT * FROM tasks WHERE done = 0 ORDER BY due").fetchall()


def reminder_times(due, has_time):
    """When to remind about a task, as {key: datetime}."""
    if has_time:
        return {"1d": due - timedelta(days=1), "1h": due - timedelta(hours=1)}
    two_days_before = datetime.combine(due.date() - timedelta(days=2), dtime(9, 0), tzinfo=TZ)
    return {"2d": two_days_before}  # the day itself is covered by the morning summary


def insert_task(title, due, has_time):
    # Reminders whose moment has already passed are marked as sent,
    # so adding a task for tomorrow doesn't fire the "1 day before" reminder instantly.
    sent = ",".join(k for k, when in reminder_times(due, has_time).items() if when <= now())
    with db() as con:
        cur = con.execute(
            "INSERT INTO tasks (title, due, has_time, sent) VALUES (?, ?, ?, ?)",
            (title, due.isoformat(), int(has_time), sent),
        )
        return cur.lastrowid


# ---------- helpers ----------


def now():
    return datetime.now(TZ)


def parse_date(word):
    w = word.lower()
    today = now().date()
    if w in TODAY_WORDS:
        return today
    if w in TOMORROW_WORDS:
        return today + timedelta(days=1)
    if w in WEEKDAYS:
        ahead = (WEEKDAYS[w] - today.weekday()) % 7 or 7  # always the NEXT such day
        return today + timedelta(days=ahead)
    try:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", w)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4})?)?", w)
        if m:
            d = date(int(m[3]) if m[3] else today.year, int(m[2]), int(m[1]))
            if not m[3] and d < today:  # "5.1." in November means next year
                d = d.replace(year=d.year + 1)
            return d
    except ValueError:  # impossible dates like 31.02.
        pass
    return None


def parse_time(word):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", word or "")
    if m and int(m[1]) < 24 and int(m[2]) < 60:
        return dtime(int(m[1]), int(m[2]))
    return None


def due_of(row):
    return datetime.fromisoformat(row["due"])


def fmt_due(due, has_time):
    text = f"{DAY_NAMES[due.weekday()]} {due:%d.%m.}"
    return text + f" {due:%H:%M}" if has_time else text


def summary_text():
    n = now()
    today, tomorrow = n.date(), n.date() + timedelta(days=1)
    overdue, due_today, due_tomorrow = [], [], []
    for r in open_tasks():
        due = due_of(r)
        at = f" в {due:%H:%M}" if r["has_time"] else ""
        if due < n:
            overdue.append(f"#{r['id']} {r['title']} ({fmt_due(due, r['has_time'])})")
        elif due.date() == today:
            due_today.append(f"#{r['id']} {r['title']}{at}")
        elif due.date() == tomorrow:
            due_tomorrow.append(f"#{r['id']} {r['title']}{at}")

    parts = []
    if overdue:
        parts.append("⚠️ Просрочено:\n" + "\n".join(overdue))
    if due_today:
        parts.append("📌 Сегодня:\n" + "\n".join(due_today))
    if due_tomorrow:
        parts.append("🔜 Завтра:\n" + "\n".join(due_tomorrow))
    return "\n\n".join(parts) or None


async def is_owner(update: Update):
    """The bot belongs to whoever sent /start first. Everyone else is ignored."""
    owner = get_owner()
    if owner is not None and update.effective_chat.id == owner:
        return True
    await update.message.reply_text("Это личный бот. 🔒" if owner else "Сначала отправь /start.")
    return False


def parse_id(context):
    try:
        return int(context.args[0])
    except (IndexError, ValueError):
        return None


# ---------- email ----------


def fetch_new_emails():
    """Runs in a separate thread (imaplib is blocking).
    Returns (list of (uid, raw_bytes), first_run). On the very first run it only
    remembers where the inbox is now, so your old emails are not summarized."""
    with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        imap.select("INBOX", readonly=True)  # readonly: emails stay unread in Gmail
        last = get_setting("last_uid")

        if last is None:
            _, data = imap.uid("search", None, "ALL")
            uids = [int(x) for x in data[0].split()]
            set_setting("last_uid", max(uids) if uids else 0)
            return [], True

        _, data = imap.uid("search", None, f"UID {int(last) + 1}:*")
        uids = sorted(u for u in (int(x) for x in data[0].split()) if u > int(last))
        emails = []
        for uid in uids[:MAX_EMAILS_PER_CHECK]:
            _, msg_data = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
            emails.append((uid, msg_data[0][1]))
        return emails, False


def html_to_text(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def parse_email(raw):
    msg = email.message_from_bytes(raw, policy=policy.default)
    subject = str(msg.get("Subject", "")) or "(без темы)"
    sender = str(msg.get("From", ""))
    text = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            text = part.get_content()
            if part.get_content_type() == "text/html":
                text = html_to_text(text)
    except Exception:  # weird encodings: fall back to just the subject
        logging.warning("Could not read body of: %s", subject)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return subject, sender, text[:MAX_BODY_CHARS]


PROMPT = """You help a forgetful university student (first year, CVUT FIT in Prague) keep track of email.
Today is {today}. The email may be forwarded, in that case the original sender is inside the text.
Emails may be in Czech, English or Russian.

Reply with ONLY a JSON object, no other text, with these keys:
- "skip": true only for ads, newsletters or automatic messages with nothing the student needs to know or do; otherwise false.
- "summary": 1-3 short sentences IN RUSSIAN: what the email is about and what the student has to do.
- "deadlines": a list of things with a specific date the student must do or attend (homework, exams, registrations, meetings).
  Each item: {{"title": "short task name in Russian", "date": "YYYY-MM-DD", "time": "HH:MM" or null}}.
  If a year is missing, use the next upcoming date. Use [] if there are none.

From: {sender}
Subject: {subject}

{body}"""


async def summarize(subject, sender, body):
    prompt = PROMPT.format(today=now().strftime("%Y-%m-%d (%A)"), sender=sender, subject=subject, body=body)
    resp = await ai.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"AI did not return JSON: {text[:200]}")
    return json.loads(m.group(0))


def save_suggestion(item):
    """Store one deadline from the AI. Returns (id, button label) or None if it's invalid."""
    try:
        d = date.fromisoformat(item["date"])
        t = parse_time(item.get("time"))
        title = str(item["title"]).strip()[:100]
    except (KeyError, ValueError, TypeError):
        return None
    if not title:
        return None
    has_time = t is not None
    due = datetime.combine(d, t or dtime(23, 59), tzinfo=TZ)
    if due < now():
        return None
    with db() as con:
        cur = con.execute(
            "INSERT INTO suggestions (title, due, has_time) VALUES (?, ?, ?)",
            (title, due.isoformat(), int(has_time)),
        )
        return cur.lastrowid, f"➕ {title} ({fmt_due(due, has_time)})"


async def handle_email(bot, owner, raw):
    subject, sender, body = parse_email(raw)
    try:
        data = await summarize(subject, sender, body)
    except Exception as e:
        logging.warning("AI summary failed: %s", e)
        await bot.send_message(owner, f"📧 {subject}\nОт: {sender}\n\n(не удалось сделать сводку)")
        return

    if data.get("skip"):
        logging.info("Skipped email: %s", subject)
        return

    text = f"📧 {subject}\nОт: {sender}\n\n{str(data.get('summary', ''))[:1000]}"
    buttons = []
    for item in (data.get("deadlines") or [])[:5]:
        saved = save_suggestion(item) if isinstance(item, dict) else None
        if saved:
            sid, label = saved
            buttons.append([InlineKeyboardButton(label, callback_data=f"add:{sid}")])
    if buttons:
        text += "\n\nДобавить в задачи?"
    await bot.send_message(owner, text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def process_new_mail(bot):
    """Check Gmail once. Returns how many new emails were processed."""
    owner = get_owner()
    if owner is None:
        return 0
    async with MAIL_LOCK:  # never run two checks at the same time
        emails, first_run = await asyncio.to_thread(fetch_new_emails)
        if first_run:
            await bot.send_message(owner, "📬 Почта подключена! С этого момента буду присылать сводки новых писем.")
        for uid, raw in emails:
            try:
                await handle_email(bot, owner, raw)
            except Exception:
                logging.exception("Could not process email %s", uid)
            set_setting("last_uid", uid)
        return len(emails)


async def mail_job(context: ContextTypes.DEFAULT_TYPE):
    global last_mail_error
    try:
        await process_new_mail(context.bot)
        last_mail_error = None
    except Exception as e:
        logging.warning("Mail check failed: %s", e)
        owner = get_owner()
        if owner and str(e) != last_mail_error:  # tell you once, not every 5 minutes
            await context.bot.send_message(owner, f"⚠️ Не удалось проверить почту:\n{e}")
        last_mail_error = str(e)


# ---------- commands ----------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    owner = get_owner()
    if owner is None:
        set_setting("owner", chat_id)
        owner = chat_id
    if chat_id != owner:
        await update.message.reply_text("Это личный бот. 🔒")
        return
    await update.message.reply_text("Привет! Я слежу за твоими задачами и напоминаю о них. 📝\n\n" + HELP)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_owner(update):
        await update.message.reply_text(HELP)


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text(USAGE_ADD)
        return

    d = parse_date(args[0])
    if d is None:
        await update.message.reply_text(f"Не понимаю дату «{args[0]}».\n\n{USAGE_ADD}")
        return
    t = parse_time(args[1]) if len(args) > 1 else None
    title = " ".join(args[2:] if t else args[1:]).strip()
    if not title:
        await update.message.reply_text("А какая задача? 🙂\n\n" + USAGE_ADD)
        return

    has_time = t is not None
    due = datetime.combine(d, t or dtime(23, 59), tzinfo=TZ)
    if due < now():
        await update.message.reply_text("Это время уже прошло 🤔")
        return
    task_id = insert_task(title, due, has_time)
    await update.message.reply_text(f"✅ Добавлено #{task_id}: {title}\n📅 {fmt_due(due, has_time)}")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update):
        return
    rows = open_tasks()
    if not rows:
        await update.message.reply_text("Открытых задач нет. 🎉")
        return
    n = now()
    lines = []
    for r in rows:
        due = due_of(r)
        mark = "⚠️ " if due < n else ""
        lines.append(f"{mark}#{r['id']} {r['title']} — {fmt_due(due, r['has_time'])}")
    await update.message.reply_text("Твои задачи:\n" + "\n".join(lines))


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_owner(update):
        await update.message.reply_text(summary_text() or "На сегодня и завтра ничего нет. 🎉")


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update):
        return
    task_id = parse_id(context)
    if task_id is None:
        await update.message.reply_text("Формат: /done <id>  (id есть в /list)")
        return
    with db() as con:
        row = con.execute("SELECT title FROM tasks WHERE id = ? AND done = 0", (task_id,)).fetchone()
        if row:
            con.execute("UPDATE tasks SET done = 1 WHERE id = ?", (task_id,))
    await update.message.reply_text(f"🎉 Выполнено: {row['title']}" if row else f"Нет открытой задачи #{task_id}.")


async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update):
        return
    task_id = parse_id(context)
    if task_id is None:
        await update.message.reply_text("Формат: /delete <id>  (id есть в /list)")
        return
    with db() as con:
        row = con.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row:
            con.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    await update.message.reply_text(f"🗑 Удалено: {row['title']}" if row else f"Нет задачи #{task_id}.")


async def checkmail_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update):
        return
    if not MAIL_ENABLED:
        await update.message.reply_text(
            "Почта не настроена: нужны GMAIL_ADDRESS, GMAIL_APP_PASSWORD и ANTHROPIC_API_KEY."
        )
        return
    await update.message.reply_text("📬 Проверяю почту…")
    try:
        count = await process_new_mail(context.bot)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Не удалось проверить почту:\n{e}")
        return
    if count == 0:
        await update.message.reply_text("Новых писем нет.")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Someone pressed a "➕ add task" button under an email summary."""
    query = update.callback_query
    if query.message.chat.id != get_owner():
        await query.answer()
        return
    action, _, sid = query.data.partition(":")
    if action != "add" or not sid.isdigit():
        await query.answer()
        return

    with db() as con:
        row = con.execute("SELECT * FROM suggestions WHERE id = ?", (int(sid),)).fetchone()
        if row and not row["added"]:
            con.execute("UPDATE suggestions SET added = 1 WHERE id = ?", (int(sid),))
    if row is None:
        await query.answer("Не найдено")
        return
    if row["added"]:
        await query.answer("Уже добавлено ✅")
        return

    due = due_of(row)
    task_id = insert_task(row["title"], due, row["has_time"])
    await query.answer("Добавлено ✅")
    await query.message.reply_text(f"✅ Добавлено #{task_id}: {row['title']}\n📅 {fmt_due(due, row['has_time'])}")


# ---------- scheduled jobs ----------


async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    owner = get_owner()
    if owner is None:
        return
    n = now()
    for r in open_tasks():
        due = due_of(r)
        sent = set(filter(None, r["sent"].split(",")))
        changed = False
        for key, when in reminder_times(due, r["has_time"]).items():
            if key in sent or when > n:
                continue
            sent.add(key)
            changed = True
            if due > n:  # don't send "upcoming" reminders for things already past
                await context.bot.send_message(
                    owner, f"⏰ Напоминание: {r['title']}\n📅 {fmt_due(due, r['has_time'])}  (#{r['id']})"
                )
        if changed:
            with db() as con:
                con.execute("UPDATE tasks SET sent = ? WHERE id = ?", (",".join(sorted(sent)), r["id"]))


async def daily_summary(context: ContextTypes.DEFAULT_TYPE):
    owner = get_owner()
    text = summary_text()
    if owner is not None and text:  # stay quiet on empty days
        await context.bot.send_message(owner, "☀️ Доброе утро!\n\n" + text)


# ---------- main ----------


def main():
    if not TOKEN:
        raise SystemExit("Set the BOT_TOKEN environment variable first (token from @BotFather).")
    init_db()

    app = Application.builder().token(TOKEN).defaults(Defaults(tzinfo=TZ)).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("done", done))
    app.add_handler(CommandHandler("delete", delete))
    app.add_handler(CommandHandler("checkmail", checkmail_cmd))
    app.add_handler(CallbackQueryHandler(on_button))

    app.job_queue.run_repeating(check_reminders, interval=CHECK_EVERY, first=5)
    app.job_queue.run_daily(daily_summary, time=SUMMARY_TIME)
    if MAIL_ENABLED:
        app.job_queue.run_repeating(mail_job, interval=MAIL_CHECK_EVERY, first=15)
        logging.info("Email checking is ON (%s, model %s).", GMAIL_ADDRESS, CLAUDE_MODEL)
    else:
        logging.info("Email checking is OFF (set GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ANTHROPIC_API_KEY).")

    logging.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
