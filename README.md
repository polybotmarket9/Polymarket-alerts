# Polymarket crypto alert bot

Sends you a Telegram message when trading on a Polymarket crypto market looks like
someone might know something. It only reads public Polymarket data. An alert is a
signal worth a look, not proof of anything.

## What triggers an alert

| Alert | Default | Plain meaning |
|---|---|---|
| Big trade | one trade of **$10,000+** | Someone put a lot of money down at once. |
| Fresh wallet | a **$2,000+** buy from a wallet with **5 or fewer** earlier trades | A new account showing up with real money is a classic insider pattern. |
| Price jump | price moves **10+ points** (e.g. 30¢ to 40¢) within **60 min** on a market that traded **between $1k and $100k** in the last day | A quiet market suddenly moving hard. |

Every alert ends with a **Signal** line saying which side to bet if you follow it,
and how strong it is: 🟢 strong (big bet *and* new wallet), 🟡 medium (one of the two),
🟠 weak (price jump only). It's a hint from public trades, not a guarantee.

Very short-term "Bitcoin Up or Down" markets are skipped because bots trade them
nonstop. Every number above can be changed (see `.env.example`). If you get too many
alerts, raise the dollar amounts; too few, lower them.

## Running on GitHub (no computer needed)

This repository checks Polymarket every 2 minutes by itself (see the **Actions** tab).
It needs one secret:

1. In Telegram, send any message to your bot.
2. Open **Settings > Secrets and variables > Actions > New repository secret**
   ([direct link](https://github.com/polybotmarket9/Polymarket-alerts/settings/secrets/actions/new)),
   name it `TELEGRAM_BOT_TOKEN` and paste the token from BotFather.

The bot finds your chat on its own and sends a "connected" message on its first run.
If it ever stops messaging you, send your bot a message again.

To change a threshold, add a repository **variable** (same page, Variables tab), e.g.
`BIG_TRADE_USD` = `25000`. GitHub pauses scheduled runs if the repository has no
activity for 60 days and emails you a button to turn them back on.

## Setup on a computer instead

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

`python3 alert_bot.py --once` does one check and exits (`--minutes 60` keeps checking for an hour), for running from cron or a
scheduler. The bot remembers what it already alerted on in `state.json`.

## Tests

`python3 -m unittest test_alert_bot` runs offline tests with made-up trades.
