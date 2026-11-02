#!/bin/bash
# Copy this file to start.sh and fill in your keys.
export BOT_TOKEN="paste-telegram-token-here"
export GMAIL_ADDRESS="your.bot.mail@gmail.com"
export GMAIL_APP_PASSWORD="abcd efgh ijkl mnop"
export ANTHROPIC_API_KEY="paste-anthropic-key-here"
export TIMETABLE_URL="paste-sirius-icalendar-link-here"
export CLASS_REMIND_MINUTES="15"   # 0 = no reminders before classes
cd "$(dirname "$0")"
python3 bot.py
