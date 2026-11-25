# Trading Signal Alarm and Daily Summary

This service reads one Telegram signal group through your Telegram user session. Your private Telegram bot delivers wake-up messages and the daily summary. An iPhone Shortcut creates the Clock alarm. You place any trades yourself. WhatsApp input comes from a chat export you send to the bot.

## Alarm controls

Send `/alarm` to the bot. **On** arms wake-ups for fresh new signals classified with medium or high confidence. **Off** cancels wake-ups still queued for delivery. SQLite preserves the switch across restarts, and a fresh database starts Off. Monitoring, history, and digests continue in either state.

The bot sends each qualifying wake-up to the private chat identified by `BOT_OWNER_ID`. It starts with a project-generated header and the signal details. [Telegram Bot API](https://core.telegram.org/bots/api#sendmessage)

```text
Trading Wake-Up #123
SELL XAUUSD | Entry: market | SL: NOT GIVEN | TP: NOT GIVEN
```

The cooldown is five minutes per symbol and direction. Nonqualifying messages, updates, closures, and discussion are available in history and the digest. New installs capture from the point of first connection. Use **Stop** or **Snooze** in Clock for the current alarm. Those controls are local to the iPhone. The bot switch applies to future delivery, and a message already sent or in flight can still produce its Clock alarm.

## Configure accounts

1. Copy `.env.example` to `.env`. Keep the file private and enter credentials directly there.
2. Create a Telegram API application and set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`. Create a separate bot with BotFather, set `BOT_TOKEN`, and start a private chat with it. Set `BOT_OWNER_ID` to your positive numeric Telegram user ID. The bot delivers wake-ups and reports and handles the controls. Group access comes from the user session. [Telegram API credentials](https://core.telegram.org/api/obtaining_api_id) · [Telegram Bot API](https://core.telegram.org/bots/api)
3. Set `OPENAI_API_KEY` and keep `OPENAI_MODEL=gpt-4o-mini` or select a model that supports Chat Completions JSON mode. Candidate Telegram messages, reply context, and materials for the digest are sent to the model provider. Check both groups' rules and any permission requirements before using real messages.

Defaults are `Europe/Vienna`, a daily digest at 18:00 local, a morning heartbeat from 09:00 local, and XAUUSD aliases `gold`, `XAU`, and `XAUUSD`. The digest window runs from the previous scheduled cutoff to the current cutoff. Exports received after cutoff go into the next digest. The bot accepts UTF-8 text exports up to 2 MB and deduplicates identical imports within each digest window. Sunday digests are followed by a weekly alarm review.

## Run on Windows

From PowerShell in the project directory:

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest -v
.\.venv\Scripts\python.exe app.py login
```

Fill in the credentials before running `login`. Complete Telegram authentication in your own terminal. The command lists group and channel IDs. Put your test group's exact ID into `TELEGRAM_GROUP`, then run:

```powershell
.\.venv\Scripts\python.exe app.py run
```

The PowerShell window must stay open for a local run. Use the VPS setup for continuous operation.

## Deploy on a Linux VPS

Install Docker Engine with the Compose plugin on the VPS. Transfer the project files and configure `.env` on the VPS. Create the data directory as the deploying account and restrict access:

```sh
mkdir -p data
chmod 700 data
chmod 600 .env
export SERVICE_UID="$(id -u)" SERVICE_GID="$(id -g)"
docker compose build
docker compose run --rm signals login
```

Set `TELEGRAM_GROUP` in `.env` from the login output. Save the numeric `SERVICE_UID` and `SERVICE_GID` in the VPS `.env` so later Compose invocations use the same identity. Run one service instance and keep `data/` mounted across deployments:

```sh
docker compose up -d
docker compose ps
docker compose logs --tail=100 signals
```

The container uses outbound connections and publishes no ports. After changing source, use `docker compose up -d --build`. For a lost Telegram login, stop the service with `docker compose stop signals`, rerun the interactive login, then bring it back up. Use a new data directory and separate bot for a different signal group or for a parallel test service.

## Upgrade an existing installation

On the old version, turn `/alarm` Off and wait for active Pushover alerts and calls to finish. Back up SQLite. Keep your Telegram and OpenAI settings and the existing `data/` directory. The updated service no longer uses the old `ALARM_MODE`, Pushover, or Twilio settings. Install the updated files and restart the local process or run `docker compose up -d --build` on the VPS. On its first start, the new worker retires unfinished alerts from the previous version and keeps the history and alarm switch.

## Set up the iPhone Shortcut

1. On iOS 27, add a **Notification** automation trigger in Shortcuts. Choose **Telegram** and add **Message contains** `Trading Wake-Up #`. Enable Telegram notifications and allow them through your usual Focus. Keep the private bot chat unmuted. [Apple notification triggers](https://support.apple.com/en-by/guide/shortcuts/apd932ff833f/10.0/ios/27)
2. Send a test alert and check the title of the notification that actually arrives on your iPhone. Add a **Title** filter for the private bot's observed title before relying on the automation. The message filter alone can also match text posted in other Telegram chats. If the lock screen hides the message body, check Telegram's preview settings and confirm the test still reaches Shortcuts under your chosen lock-screen settings.
3. Set the Shortcut to run without confirmation and allow execution when locked. From the incoming notification message, match the header using `(?m)^Trading Wake-Up #[0-9]+$`. End the Shortcut if there is no match. Use the matched header as the Clock alarm label. End the Shortcut if Clock already contains that exact label, so a retry with the same signal ID cannot schedule another alarm. [Apple automation settings](https://support.apple.com/en-ae/guide/shortcuts/apdfbdbd7123/10.0/ios/27)
4. Add the current date, adjust it by **two minutes**, and pass that time to **Create Alarm**. Leave **Repeat** unset. Choose the alarm sound you want in Clock. [Apple Clock alarm settings](https://support.apple.com/en-us/118444)

## Verify delivery and reporting

1. Send `/alarm` to the private bot and confirm it shows **iPhone Clock via Telegram**. While Off, post “Selling gold now” in the test group and confirm `/history` records it without a wake-up message.
2. Turn On and post a fresh qualifying signal with the phone locked, Silent Mode on, and your usual Focus enabled. Confirm the private bot notification schedules one Clock alarm. Stop it in Clock, check that `/alarm` still shows On, and send another test signal after the cooldown.
3. Send a WhatsApp text export without media to the bot and check the next digest. Test one cutoff without an export and verify the morning heartbeat arrives.
4. Use an authorized test group for a one-to-two-week trial. Compare five to ten examples, including questions, negations, replies, closures, and edits. Use `/dashboard`, `/history [before ID]`, and `/review ID false|missed|ok` to review results.

## Price comparison and exposure

The service reads quotes from a directory you supply. Set `QUOTE_DIR` to that directory, for example `/data/quotes` in Docker. Provide one CSV per normalized symbol, such as `XAUUSD.csv`, with columns `unix_utc,price`. Feed it from a market-data source you are authorized to use and update the file atomically. An at-message or current quote must be within two minutes of its target timestamp. Alerts show unavailable for missing or stale quotes. A feed connector is deployment-specific.

Optionally set `EXPOSURE_UNITS_JSON='{"XAUUSD":2}'` to your own planned total position units across all layers. Exposure is estimated in the quote currency from entry-to-stop distance and total units. An explicit entry can be used without a quote feed. Market entry requires an at-message quote. The estimate needs an explicit stop and does not account for gaps, slippage, fees, or unpriced layers. Points in the original message are preserved as written.

## Back up history

Run `python app.py backup data/signals-backup.sqlite` locally, or:

```sh
docker compose exec signals python app.py backup /data/signals-backup.sqlite
```

The destination must be new. Schedule timestamped backups and copy them to private storage. To restore, stop the service, archive the current database and any WAL and SHM files, install the backup as `data/signals.sqlite`, and restart. After a restore, check the alarm setting and avoid resuming any old pending alerts until their state has been reviewed. Keep the Telegram session file separate and protected.

The service retries delivery failures with backoff while signals are fresh. A timeout can leave uncertainty about whether Telegram accepted a message. A retry keeps the same wake-up ID so the Shortcut can avoid creating another alarm for that signal.

See [the build log](CHANGELOG.md) for implementation changes and follow-ups.
