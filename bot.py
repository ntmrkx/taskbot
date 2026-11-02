"""
Personal task & reminder bot for Telegram  (v3: + timetable, repeating tasks, buttons)

Commands:
  /add <когда> [ЧЧ:ММ] <задача>   add a task
  /list                           all open tasks
  /today                          overdue + today + tomorrow
  /every <правило> [ЧЧ:ММ] <задача>  repeating task (день / пн..вс / 15)
  /done <id>                      mark a task as done (repeating: moves to next time)
  /edit <id> <когда> [ЧЧ:ММ]      move a task to another date/time
  /rename <id> <новое название>   rename a task
  /delete <id>                    delete a task
  /checkmail                      check email right now
  /schedule [когда]               classes for a day (also /rozvrh)
  /help                           show help

<когда>: сегодня/today, завтра/tomorrow, пн..вс / mon..sun, 20.11., 20.11.2026, 2026-11-20

Reminders:
  - tasks with a time:    1 day before and 1 hour before
  - tasks without a time: 2 days before (at 9:00)
  - every morning at 8:00: summary of overdue, today and tomorrow
  - reminders have buttons: done / remind in 1 hour / remind tomorrow
  - every Sunday at 19:00: weekly summary

Email (optional): every 5 minutes the bot reads new mail in Gmail, asks Claude
for a short summary and sends it to Telegram with "add task" buttons.

Environment variables:
  BOT_TOKEN            Telegram token from @BotFather          (required)
  GMAIL_ADDRESS        Gmail that receives your forwarded mail  (needed for email)
  GMAIL_APP_PASSWORD   16-letter Google app password            (needed for email)
  ANTHROPIC_API_KEY    API key from console.anthropic.com       (needed for email)
  CLAUDE_MODEL         optional, default "claude-haiku-5-5"
  TIMETABLE_URL        Sirius iCalendar export link (optional, keep it secret)
  CLASS_REMIND_MINUTES remind N minutes before each class (optional, 0 = off)
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
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, time as dtime, timedelta
from email import policy
from zoneinfo import ZoneInfo

import anthropic
import icalendar
import recurring_ical_events
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, Defaults

# ---------- settings ----------

TOKEN = os.environ.get("BOT_TOKEN")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
MAIL_ENABLED = bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD and ANTHROPIC_API_KEY)
TIMETABLE_URL = os.environ.get("TIMETABLE_URL", "").strip()
CLASS_REMIND_MINUTES = int(os.environ.get("CLASS_REMIND_MINUTES", "0") or 0)

TZ = ZoneInfo("Europe/Prague")
DB_PATH = "tasks.db"
SUMMARY_TIME = dtime(8, 0, tzinfo=TZ)  # daily morning summary
WEEKLY_TIME = dtime(19, 0, tzinfo=TZ)  # weekly summary (on Sundays)
CHECK_EVERY = 60                       # seconds between reminder checks
MAIL_CHECK_EVERY = 300                 # seconds between email checks
MAX_EMAILS_PER_CHECK = 10
MAX_BODY_CHARS = 8000                  # longer emails are cut (saves API money)
TIMETABLE_FILE = "timetable.ics"       # local copy, so the bot works even if Sirius is down
TIMETABLE_REFRESH = 6 * 3600           # seconds between timetable downloads

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
ON_DAYS = ["по понедельникам", "по вторникам", "по средам", "по четвергам",
           "по пятницам", "по субботам", "по воскресеньям"]
DAILY_WORDS = {"день", "ежедневно", "day", "daily"}
EVERY_WORDS = {"каждый", "каждую", "каждое", "every"}  # optional, "/every каждый вт ..." also works

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
    "/every <правило> [ЧЧ:ММ] <задача> — повторяющаяся задача\n"
    "   правило: день, пн–вс или число месяца (15)\n"
    "/done <id> — отметить выполненной\n"
    "/edit <id> <когда> [ЧЧ:ММ] — перенести задачу\n"
    "/rename <id> <название> — переименовать\n"
    "/delete <id> — удалить задачу\n"
    "/checkmail — проверить почту сейчас\n"
    "/schedule [когда] — пары на день (по умолчанию сегодня)\n\n"
    "<когда>: сегодня, завтра, пн–вс, 20.11., 20.11.2026\n\n"
    + EXAMPLES
    + "\n/every вт Вынести мусор\n/every 1 10:00 Оплатить телефон"
)
USAGE_EVERY = (
    "Формат: /every <правило> [ЧЧ:ММ] <задача>\n"
    "Правило: день (каждый день), пн–вс (каждую неделю) или число месяца.\n"
    "Примеры:\n/every вт Вынести мусор\n/every день 22:00 Выпить таблетку\n"
    "/every 1 10:00 Оплатить телефон"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)        # hide every poll request
logging.getLogger("apscheduler").setLevel(logging.WARNING)  # hide "job executed" lines

ai = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if MAIL_ENABLED else None
MAIL_LOCK = asyncio.Lock()
last_mail_error = None
last_timetable_error = None
_calendar = None           # parsed timetable, cached in memory
_class_reminded = set()    # classes already reminded about (since the bot started)

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
        # columns added in v3 (older databases get them added automatically)
        cols = {row["name"] for row in con.execute("PRAGMA table_info(tasks)")}
        if "repeat" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN repeat TEXT")        # e.g. "weekly:1"
        if "snooze_until" not in cols:
            con.execute("ALTER TABLE tasks ADD COLUMN snooze_until TEXT")  # extra reminder time
        con.execute("""
            CREATE TABLE IF NOT EXISTS completions (  -- history for the weekly summary
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                title   TEXT NOT NULL,
                done_at TEXT NOT NULL
            )""")
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


def initial_sent(due, has_time):
    # Reminders whose moment has already passed are marked as sent,
    # so adding a task for tomorrow doesn't fire the "1 day before" reminder instantly.
    return ",".join(k for k, when in reminder_times(due, has_time).items() if when <= now())


def insert_task(title, due, has_time, repeat=None):
    with db() as con:
        cur = con.execute(
            "INSERT INTO tasks (title, due, has_time, sent, repeat) VALUES (?, ?, ?, ?, ?)",
            (title, due.isoformat(), int(has_time), initial_sent(due, has_time), repeat),
        )
        return cur.lastrowid


def complete_task(task_id):
    """Mark a task done. A repeating task moves to its next time instead.
    Returns (row, next_due) or None if there is no such open task."""
    with db() as con:
        row = con.execute("SELECT * FROM tasks WHERE id = ? AND done = 0", (task_id,)).fetchone()
        if row is None:
            return None
        con.execute("INSERT INTO completions (title, done_at) VALUES (?, ?)", (row["title"], now().isoformat()))
        if not row["repeat"]:
            con.execute("UPDATE tasks SET done = 1, snooze_until = NULL WHERE id = ?", (task_id,))
            return row, None
        old = due_of(row)
        t = old.time() if row["has_time"] else None
        nxt = next_due(row["repeat"], max(now(), old), t)
        con.execute(
            "UPDATE tasks SET due = ?, sent = ?, snooze_until = NULL WHERE id = ?",
            (nxt.isoformat(), initial_sent(nxt, row["has_time"]), task_id),
        )
        return row, nxt


def snooze_task(task_id, until):
    with db() as con:
        row = con.execute("SELECT title FROM tasks WHERE id = ? AND done = 0", (task_id,)).fetchone()
        if row:
            con.execute("UPDATE tasks SET snooze_until = ? WHERE id = ?", (until.isoformat(), task_id))
    return row


# ---------- repeating tasks ----------


def parse_rule(args):
    """Reads the repeat rule from the start of /every arguments.
    Returns (rule, number_of_words_used) or (None, 0)."""
    i = 1 if args and args[0].lower() in EVERY_WORDS else 0
    if i >= len(args):
        return None, 0
    w = args[i].lower()
    if w in DAILY_WORDS:
        return "daily", i + 1
    if w in WEEKDAYS:
        return f"weekly:{WEEKDAYS[w]}", i + 1
    m = re.fullmatch(r"(\d{1,2})(?:-?е|-?го|\.)?", w)
    if m and 1 <= int(m[1]) <= 31:
        used = i + 1
        if used < len(args) and args[used].lower() in ("число", "числа"):
            used += 1
        return f"monthly:{int(m[1])}", used
    return None, 0


def last_day_of_month(day):
    first_next = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (first_next - timedelta(days=1)).day


def rule_matches(rule, day):
    kind, _, value = rule.partition(":")
    if kind == "daily":
        return True
    if kind == "weekly":
        return day.weekday() == int(value)
    if kind == "monthly":  # "31" means the last day in shorter months
        return day.day == min(int(value), last_day_of_month(day))
    return False


def next_due(rule, after, t):
    """First time matching the rule that is strictly later than `after`."""
    for i in range(0, 400):
        day = after.date() + timedelta(days=i)
        if rule_matches(rule, day):
            due = datetime.combine(day, t or dtime(23, 59), tzinfo=TZ)
            if due > after:
                return due
    raise ValueError(f"bad repeat rule: {rule}")


def fmt_rule(rule):
    kind, _, value = rule.partition(":")
    if kind == "daily":
        return "каждый день"
    if kind == "weekly":
        return ON_DAYS[int(value)]
    if kind == "monthly":
        return f"каждое {value}-е число"
    return rule


def reminder_buttons(task_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Готово", callback_data=f"done:{task_id}"),
        InlineKeyboardButton("⏰ Через час", callback_data=f"snz:{task_id}:1h"),
        InlineKeyboardButton("📅 Завтра", callback_data=f"snz:{task_id}:tmr"),
    ]])


def done_text(row, nxt):
    if nxt is None:
        return f"🎉 Выполнено: {row['title']}"
    return f"🎉 Выполнено: {row['title']}\n🔁 Следующий раз: {fmt_due(nxt, row['has_time'])}"


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
    classes = safe_classes_on(today)
    if classes:
        parts.append("🎓 Пары сегодня:\n" + "\n".join(fmt_class(c) for c in classes))
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


# ---------- timetable (Sirius .ics) ----------


def download_timetable():
    """Runs in a separate thread. Downloads the .ics, checks that it parses, saves it."""
    global _calendar
    req = urllib.request.Request(TIMETABLE_URL, headers={"User-Agent": "taskbot"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    cal = icalendar.Calendar.from_ical(data)  # raises if it's not a valid calendar
    tmp = TIMETABLE_FILE + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, TIMETABLE_FILE)  # replace the old copy only after a good download
    _calendar = cal


def load_calendar():
    global _calendar
    if _calendar is None and os.path.exists(TIMETABLE_FILE):
        with open(TIMETABLE_FILE, "rb") as f:
            _calendar = icalendar.Calendar.from_ical(f.read())
    return _calendar


def to_local(value):
    """iCal times can be dates (all-day events), naive or timezone-aware datetimes."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)
    return datetime.combine(value, dtime(0, 0), tzinfo=TZ)


def classes_between(start, end):
    cal = load_calendar()
    if cal is None:
        return []
    result = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        dtstart = ev.get("DTSTART")
        if dtstart is None:
            continue
        s = to_local(dtstart.dt)
        dtend = ev.get("DTEND")
        result.append({
            "start": s,
            "end": to_local(dtend.dt) if dtend is not None else s,
            "all_day": not isinstance(dtstart.dt, datetime),
            "title": str(ev.get("SUMMARY", "")).strip() or "(без названия)",
            "location": str(ev.get("LOCATION", "") or "").strip(),
        })
    result.sort(key=lambda c: c["start"])
    return result


def safe_classes_on(day):
    """Classes on one day. Never crashes the caller (e.g. the morning summary)."""
    try:
        start = datetime.combine(day, dtime(0, 0), tzinfo=TZ)
        return classes_between(start, start + timedelta(days=1))
    except Exception:
        logging.exception("Could not read timetable")
        return []


def fmt_class(c):
    where = f" · {c['location']}" if c["location"] else ""
    if c["all_day"]:
        return f"весь день: {c['title']}{where}"
    return f"{c['start']:%H:%M}–{c['end']:%H:%M} {c['title']}{where}"


async def timetable_job(context: ContextTypes.DEFAULT_TYPE):
    global last_timetable_error
    try:
        await asyncio.to_thread(download_timetable)
        logging.info("Timetable updated.")
        last_timetable_error = None
    except Exception as e:
        logging.warning("Timetable download failed: %s", e)
        owner = get_owner()
        if owner and str(e) != last_timetable_error:  # tell you once, not every time
            await context.bot.send_message(owner, f"⚠️ Не удалось обновить расписание:\n{e}")
        last_timetable_error = str(e)


async def class_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    owner = get_owner()
    if owner is None:
        return
    n = now()
    lead = timedelta(minutes=CLASS_REMIND_MINUTES)
    try:
        upcoming = classes_between(n, n + lead + timedelta(minutes=1))
    except Exception:
        logging.exception("Could not read timetable")
        return
    for c in upcoming:
        if c["all_day"] or not (c["start"] - lead <= n < c["start"]):
            continue
        key = (c["title"], c["start"].isoformat())
        if key in _class_reminded:
            continue
        _class_reminded.add(key)
        await context.bot.send_message(owner, f"🎓 Скоро пара:\n{fmt_class(c)}")


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
        rep = f" 🔁 {fmt_rule(r['repeat'])}" if r["repeat"] else ""
        lines.append(f"{mark}#{r['id']} {r['title']} — {fmt_due(due, r['has_time'])}{rep}")
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
    result = complete_task(task_id)
    await update.message.reply_text(done_text(*result) if result else f"Нет открытой задачи #{task_id}.")


async def every(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update):
        return
    args = context.args
    rule, used = parse_rule(args)
    if rule is None:
        await update.message.reply_text(USAGE_EVERY)
        return
    rest = args[used:]
    t = parse_time(rest[0]) if rest else None
    title = " ".join(rest[1:] if t else rest).strip()
    if not title:
        await update.message.reply_text("А какая задача? 🙂\n\n" + USAGE_EVERY)
        return
    has_time = t is not None
    due = next_due(rule, now(), t)
    task_id = insert_task(title, due, has_time, repeat=rule)
    await update.message.reply_text(
        f"🔁 Добавлено #{task_id}: {title}\n{fmt_rule(rule).capitalize()}, ближайший раз: {fmt_due(due, has_time)}"
    )


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update):
        return
    args = context.args
    usage = "Формат: /edit <id> <когда> [ЧЧ:ММ]\nПример: /edit 5 пт 18:00"
    task_id = parse_id(context)
    d = parse_date(args[1]) if task_id is not None and len(args) > 1 else None
    if d is None:
        await update.message.reply_text(usage)
        return
    t = parse_time(args[2]) if len(args) > 2 else None
    has_time = t is not None
    due = datetime.combine(d, t or dtime(23, 59), tzinfo=TZ)
    if due < now():
        await update.message.reply_text("Это время уже прошло 🤔")
        return
    with db() as con:
        row = con.execute("SELECT title FROM tasks WHERE id = ? AND done = 0", (task_id,)).fetchone()
        if row:
            con.execute(
                "UPDATE tasks SET due = ?, has_time = ?, sent = ?, snooze_until = NULL WHERE id = ?",
                (due.isoformat(), int(has_time), initial_sent(due, has_time), task_id),
            )
    if row is None:
        await update.message.reply_text(f"Нет открытой задачи #{task_id}.")
        return
    await update.message.reply_text(f"✏️ Перенесено #{task_id}: {row['title']}\n📅 {fmt_due(due, has_time)}")


async def rename_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update):
        return
    task_id = parse_id(context)
    title = " ".join(context.args[1:]).strip() if task_id is not None else ""
    if not title:
        await update.message.reply_text("Формат: /rename <id> <новое название>")
        return
    with db() as con:
        cur = con.execute("UPDATE tasks SET title = ? WHERE id = ? AND done = 0", (title[:200], task_id))
    await update.message.reply_text(
        f"✏️ Переименовано #{task_id}: {title}" if cur.rowcount else f"Нет открытой задачи #{task_id}."
    )


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


async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update):
        return
    if not TIMETABLE_URL:
        await update.message.reply_text("Расписание не настроено: нужен TIMETABLE_URL.")
        return
    day = parse_date(context.args[0]) if context.args else now().date()
    if day is None:
        await update.message.reply_text("Не понимаю дату. Пример: /schedule завтра")
        return
    if load_calendar() is None:
        await update.message.reply_text("Расписание ещё не загружено, попробуй через минуту.")
        return
    classes = safe_classes_on(day)
    header = f"🎓 {DAY_NAMES[day.weekday()]} {day:%d.%m.}"
    if classes:
        await update.message.reply_text(header + ":\n" + "\n".join(fmt_class(c) for c in classes))
    else:
        await update.message.reply_text(header + ": пар нет 🎉")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """All inline buttons: "➕ add task" under emails, "✅ / ⏰ / 📅" under reminders."""
    query = update.callback_query
    if query.message.chat.id != get_owner():
        await query.answer()
        return
    action, _, sid = query.data.partition(":")
    if action == "done":
        await on_done_button(query, sid)
        return
    if action == "snz":
        await on_snooze_button(query, sid)
        return
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


async def close_buttons(query, note):
    """Remove the buttons and add a short note under the original message."""
    try:
        await query.edit_message_text(f"{query.message.text}\n\n{note}")
    except Exception:  # e.g. message too old to edit
        await query.message.reply_text(note)


async def on_done_button(query, sid):
    result = complete_task(int(sid)) if sid.isdigit() else None
    if result is None:
        await query.answer("Задача уже закрыта или удалена")
        await close_buttons(query, "ℹ️ Задача уже закрыта")
        return
    await query.answer("Готово ✅")
    await close_buttons(query, done_text(*result))


async def on_snooze_button(query, data):
    sid, _, how = data.partition(":")
    if not sid.isdigit():
        await query.answer()
        return
    if how == "tmr":
        until = datetime.combine(now().date() + timedelta(days=1), dtime(9, 0), tzinfo=TZ)
        note = "📅 Напомню завтра в 9:00"
    else:
        until = now() + timedelta(hours=1)
        note = f"⏰ Напомню в {until:%H:%M}"
    if snooze_task(int(sid), until) is None:
        await query.answer("Задача уже закрыта или удалена")
        return
    await query.answer("Ок 👍")
    await close_buttons(query, note)


# ---------- scheduled jobs ----------


async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    owner = get_owner()
    if owner is None:
        return
    n = now()
    for r in open_tasks():
        due = due_of(r)
        rep = "  🔁" if r["repeat"] else ""
        text = f"⏰ Напоминание: {r['title']}\n📅 {fmt_due(due, r['has_time'])}  (#{r['id']}){rep}"

        # 1) a snoozed reminder ("⏰ через час" / "📅 завтра") is due
        if r["snooze_until"] and datetime.fromisoformat(r["snooze_until"]) <= n:
            with db() as con:
                con.execute("UPDATE tasks SET snooze_until = NULL WHERE id = ?", (r["id"],))
            await context.bot.send_message(owner, text, reply_markup=reminder_buttons(r["id"]))

        # 2) the normal scheduled reminders
        sent = set(filter(None, r["sent"].split(",")))
        changed = False
        for key, when in reminder_times(due, r["has_time"]).items():
            if key in sent or when > n:
                continue
            sent.add(key)
            changed = True
            if due > n:  # don't send "upcoming" reminders for things already past
                await context.bot.send_message(owner, text, reply_markup=reminder_buttons(r["id"]))
        if changed:
            with db() as con:
                con.execute("UPDATE tasks SET sent = ? WHERE id = ?", (",".join(sorted(sent)), r["id"]))


async def weekly_summary(context: ContextTypes.DEFAULT_TYPE):
    n = now()
    owner = get_owner()
    if owner is None or n.weekday() != 6:  # runs every evening, but only speaks on Sundays
        return
    week_ago, week_ahead = n - timedelta(days=7), n + timedelta(days=7)
    with db() as con:
        done_rows = con.execute(
            "SELECT title, done_at FROM completions ORDER BY done_at"
        ).fetchall()
    done_titles = [r["title"] for r in done_rows if datetime.fromisoformat(r["done_at"]) >= week_ago]

    tasks = open_tasks()
    overdue = [r for r in tasks if due_of(r) < n]
    upcoming = [r for r in tasks if n <= due_of(r) < week_ahead]

    parts = [f"📊 Итоги недели\n\n✅ Выполнено задач: {len(done_titles)}"]
    if done_titles:
        shown = done_titles[-10:]
        parts[0] += "\n" + "\n".join(f"• {t}" for t in shown)
        if len(done_titles) > len(shown):
            parts[0] += f"\n…и ещё {len(done_titles) - len(shown)}"
    if overdue:
        parts.append("⚠️ Просрочено:\n" + "\n".join(
            f"#{r['id']} {r['title']} ({fmt_due(due_of(r), r['has_time'])})" for r in overdue))
    if upcoming:
        parts.append("📅 На следующей неделе:\n" + "\n".join(
            f"#{r['id']} {r['title']} — {fmt_due(due_of(r), r['has_time'])}" for r in upcoming))
    else:
        parts.append("📅 На следующей неделе задач пока нет.")
    await context.bot.send_message(owner, "\n\n".join(parts))


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
    app.add_handler(CommandHandler("every", every))
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CommandHandler("rename", rename_cmd))
    app.add_handler(CommandHandler("delete", delete))
    app.add_handler(CommandHandler("checkmail", checkmail_cmd))
    app.add_handler(CommandHandler(["schedule", "rozvrh"], schedule_cmd))
    app.add_handler(CallbackQueryHandler(on_button))

    app.job_queue.run_repeating(check_reminders, interval=CHECK_EVERY, first=5)
    app.job_queue.run_daily(daily_summary, time=SUMMARY_TIME)
    app.job_queue.run_daily(weekly_summary, time=WEEKLY_TIME)
    if MAIL_ENABLED:
        app.job_queue.run_repeating(mail_job, interval=MAIL_CHECK_EVERY, first=15)
        logging.info("Email checking is ON (%s, model %s).", GMAIL_ADDRESS, CLAUDE_MODEL)
    else:
        logging.info("Email checking is OFF (set GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ANTHROPIC_API_KEY).")

    if TIMETABLE_URL:
        app.job_queue.run_repeating(timetable_job, interval=TIMETABLE_REFRESH, first=10)
        if CLASS_REMIND_MINUTES > 0:
            app.job_queue.run_repeating(class_reminder_job, interval=60, first=20)
        logging.info("Timetable is ON (class reminders: %s min before).", CLASS_REMIND_MINUTES or "off")
    else:
        logging.info("Timetable is OFF (set TIMETABLE_URL).")

    logging.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
