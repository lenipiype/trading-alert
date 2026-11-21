# Trading Signal Alarm and Daily Summary

This service reads one Telegram signal group through your Telegram user session, sends Pushover alerts, and delivers a daily summary through your private Telegram bot. You place any trades yourself. WhatsApp input comes from a chat export you send to the bot.

## Alarm controls

Send `/alarm` to the bot. **On** arms wake-ups for qualifying fresh signals. **Off** sends ordinary notifications and downgrades queued wake-ups. The bot shows the current setting and delivery mode, and SQLite preserves the On/Off state across restarts. A fresh database starts Off. Monitoring, history, and digests continue in either state.

- **`ALARM_MODE=iphone_clock`** sends a single Pushover notification titled `Trading Wake-Up #<message ID>`. A Shortcut on your iPhone creates a one-time Clock alarm. Use the Clock screen's Stop control for the current alarm. Telegram stays armed for the next signal. Turning the bot Off prevents future wake-up notifications. Manage alarms already created on the iPhone in Clock. This mode uses a standard-priority push without Pushover repeats, acknowledgement, or Twilio escalation.
- **`ALARM_MODE=pushover`** is the default. An emergency push repeats every 30 seconds for up to 10 minutes. A call to the owner is attempted after two minutes without Pushover acknowledgement, followed by an optional backup call three minutes after the first call attempt. Acknowledge in Pushover. Turning the bot Off requests cancellation of active repeats and calls, which can take several seconds.

The cooldown is five minutes per symbol and direction in both modes. Low-confidence signals and messages older than five minutes get ordinary notifications. New installs capture from the point of first connection.

## Configure accounts

1. Copy `.env.example` to `.env`. Keep the file private and enter credentials directly there.
2. Create a Telegram API application and set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`. Create a separate bot with BotFather, set `BOT_TOKEN`, and start a private chat with it. Set `BOT_OWNER_ID` to your numeric Telegram user ID. The bot is for controls and reports and does not need group membership. [Telegram API credentials](https://core.telegram.org/api/obtaining_api_id) · [Telegram Bot API](https://core.telegram.org/bots/api)
3. Set `OPENAI_API_KEY` and keep `OPENAI_MODEL=gpt-4o-mini` or select a model that supports Chat Completions JSON mode. Candidate Telegram messages, reply context, and materials for the digest are sent to the model provider. Check both groups' rules and any permission requirements before using real messages.
4. Register a Pushover application and configure `PUSHOVER_TOKEN` and `PUSHOVER_USER`. Install Pushover on your phone and sign in to the account that owns the user key. [Pushover receipts API](https://pushover.net/api/receipts)
5. To enable calls in `pushover` mode, configure `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`, and `OWNER_PHONE`. Use a Twilio voice-capable number and E.164 format, including the country code. Leave `BACKUP_PHONE` blank until the backup recipient agrees. Confirm destination permissions, trial verification requirements if applicable, and pricing in Twilio before testing. [Twilio Call resource](https://www.twilio.com/docs/voice/api/call-resource)

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

## Set up iPhone Clock mode

1. If the service is already running in Pushover mode, turn it Off in Telegram and wait for active pushes and calls to stop. Set `ALARM_MODE=iphone_clock` in your `.env`. Restart the local process or run `docker compose up -d --build` on the VPS with the updated project. Open `/alarm` and confirm the bot displays **iPhone Clock**.
2. On an iPhone with iOS 27, create a Shortcut with a **Notification** automation trigger. Choose **Pushover** and add a **Title** filter for titles containing `Trading Wake-Up #`. Set the trigger to run without confirmation and enable **Allow Running When Locked** in the Shortcut's privacy settings. Allow immediate Pushover notifications in the Focus you use while sleeping. [Apple notification triggers](https://support.apple.com/en-by/guide/shortcuts/apd932ff833f/10.0/ios/27) · [Apple automation settings](https://support.apple.com/en-ae/guide/shortcuts/apdfbdbd7123/10.0/ios/27)
3. Add **Current Date**, then **Adjust Date** to add one minute, then **Create Alarm** using that adjusted time. Leave Repeat empty and use the incoming notification's full title as the alarm label. To handle a delivery retry, add a check before **Create Alarm** that looks up Clock alarms and ends the Shortcut if one already has that exact label. Retries for the same notification reuse its title. Keep recent alarms in Clock while testing the duplicate check.
4. Turn `/alarm` On and send a qualifying test signal with the phone locked, Silent Mode on, and your usual Focus enabled. Confirm exactly one Clock alarm rings. Stop it on the Clock screen and check that Telegram still says On. After the cooldown, send another test signal and confirm it rings too. Turn `/alarm` Off and send a final test signal. It should arrive as an ordinary push without scheduling a Clock alarm. [Apple Clock alarm settings](https://support.apple.com/en-us/118444)

## Verify Pushover mode

1. Open the bot and send `/start`. With the alarm Off, post an example such as “Selling gold now” in your test group. Confirm you get an ordinary push and a recorded decision under `/history`.
2. Turn the alarm On. Enable Pushover Critical Alerts if available on iOS, or allow the Pushover notification channel through Do Not Disturb on Android. Allow the Twilio caller through your phone's Focus or Do Not Disturb settings. Choose a distinctive sound and test with the phone in its actual overnight configuration.
3. Send another qualifying test signal after the cooldown. Acknowledge the emergency notification and confirm the repeats stop and no call is placed. In another test, wait for the configured owner call, then acknowledge and confirm ringing or the connected call ends. Test **Off** separately with an active alarm. Use consenting recipients for backup-call tests.
4. Send a study-group export without media to the bot. Confirm the bot gives a digest date and check that the next digest includes it. Test one cutoff without an export, and confirm the morning heartbeat arrives.
5. Use an authorized test group for a one-to-two-week trial. Compare at least five to ten examples, including questions, negations, replies, closures, and edits. Use `/dashboard`, `/history [before ID]`, and `/review ID false|missed|ok` to review results. Retest the phone setup weekly.

## Price comparison and exposure

The service reads quotes from a directory you supply. Set `QUOTE_DIR` to that directory, for example `/data/quotes` in Docker. Provide one CSV per normalized symbol, such as `XAUUSD.csv`, with columns `unix_utc,price`. Feed it from a market-data source you are authorized to use and update the file atomically. An at-message or current quote must be within two minutes of its target timestamp. Alerts show unavailable for missing or stale quotes. A feed connector is deployment-specific.

Optionally set `EXPOSURE_UNITS_JSON='{"XAUUSD":2}'` to your own planned total position units across all layers. Exposure is estimated in the quote currency from entry-to-stop distance and total units. An explicit entry can be used without a quote feed. Market entry requires an at-message quote. The estimate needs an explicit stop and does not account for gaps, slippage, fees, or unpriced layers. Points in the original message are preserved as written.

## Back up history

Run `python app.py backup data/signals-backup.sqlite` locally, or:

```sh
docker compose exec signals python app.py backup /data/signals-backup.sqlite
```

The destination must be new. Schedule timestamped backups and copy them to private storage. To restore, stop the service, archive the current database and any WAL and SHM files, install the backup as `data/signals.sqlite`, and restart. After a restore, check the alarm setting and avoid resuming any old pending alerts until their state has been reviewed. Keep the Telegram session file separate and protected.

The service retries provider reads and delivery failures with backoff. A provider timeout can leave uncertainty about whether a push or digest part was accepted, so retries can occasionally duplicate delivery. Twilio call creation is attempted once per stage to avoid redialing on an ambiguous response. The service cannot cancel a call whose ID was never returned by Twilio.

See [the build log](CHANGELOG.md) for implementation changes and follow-ups.
