# Polymarket alert bot

Sends you a Telegram message when trading on Polymarket (crypto, politics, sports and
company news) looks like
someone might know something. It only reads public Polymarket data. An alert is a
signal worth a look, not proof of anything.

## What triggers an alert

| Alert | Default | Plain meaning |
|---|---|---|
| Big trade | one trade of **$10,000+** (**$50,000+** in sports, where big bets are routine) | Someone put a lot of money down at once. |
| Fresh wallet | a **$2,000+** buy from a wallet with **5 or fewer** earlier trades | A new account showing up with real money is a classic insider pattern. |
| Price jump | price moves **10+ points** (e.g. 30¢ to 40¢) within **60 min** on a market that traded **between $1k and $100k** in the last day | A quiet market suddenly moving hard. |

**Proven winners.** The bot looks up each sizeable trader's public results on
Polymarket's leaderboard. A **$2,000+** bet from someone with **$10,000+ all-time
profit, a 5%+ return on everything they've traded, and a profitable last month**
triggers an alert on its own, and every trade alert shows the trader's record.

**Noise filters.** No alerts for bets on a side already priced above 95¢ (near-certain,
little to learn), for trades more than 15 minutes old, or for the same trader on the
same event more than once every 12 hours.

Every alert ends with a **Signal** line saying which side to bet if you follow it,
and how strong it is: 🟢 strong (proven winner, or big bet *and* new wallet),
🟡 medium (big bet or new wallet), 🟠 weak (price jump only). It's a hint from public
trades, not a guarantee.

**Split bets.** 3+ brand-new wallets each buying $1,000+ of the same side within 30
minutes ($5,000+ in total) trigger an alert: often one person hiding a big bet.

**Late alerts are skipped** when the price has already moved 10+ points past what the
trader paid. Price-jump alerts are off for sports, where live odds swing all game.

**Scorecard.** Every alert is logged in `scorecard.json` and marked won or lost when
its market settles. Send your bot **/score** in Telegram any time to see the results
(and what $100 on each signal would have made), broken down by category so you can
see which is most accurate. It also sends this once a week.

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
