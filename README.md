# Polymarket crypto alert bot

Sends you a Telegram message when trading on a Polymarket crypto market looks like
someone might know something. It only reads public Polymarket data. An alert is a
signal worth a look, not proof of anything.

## What triggers an alert

| Alert | Default | Plain meaning |
|---|---|---|
| Big trade | one trade of **$10,000+** | Someone put a lot of money down at once. |
| Fresh wallet | a **$2,000+** buy from a wallet with **5 or fewer** earlier trades | A new account showing up with real money is a classic insider pattern. |
| Price jump | price moves **10+ points** (e.g. 30¢ to 40¢) within **60 min** on a market that traded **under $100k** in the last day | A quiet market suddenly moving hard. |

Very short-term "Bitcoin Up or Down" markets are skipped because bots trade them
nonstop. Every number above can be changed (see `.env.example`). If you get too many
alerts, raise the dollar amounts; too few, lower them.

## Setup (about 5 minutes)

1. **Make a Telegram bot.** In Telegram, message `@BotFather`, send `/newbot`, pick a
   name. It gives you a token. Keep it private; don't paste it into chats.
2. **Send your new bot any message** (e.g. "hi") so it can find you.
3. **Set the token and find your chat id** in a terminal on your computer
   (needs Python 3, nothing else to install):
   ```
   cd alert-bot
   cp .env.example .env        # then put your token in .env
   set -a; . ./.env; set +a
   python3 alert_bot.py --find-chat-id
   ```
   Copy the `TELEGRAM_CHAT_ID=...` line it prints into `.env`, and load it again
   with `set -a; . ./.env; set +a`.
4. **Run it:**
   ```
   python3 alert_bot.py
   ```
   It checks every 60 seconds and prints a line each round. Without Telegram
   settings it just prints alerts to the screen (handy for trying it out).

`python3 alert_bot.py --once` does one check and exits, for running from cron or a
scheduler. The bot remembers what it already alerted on in `state.json`.

## Tests

`python3 -m unittest test_alert_bot` runs offline tests with made-up trades.
