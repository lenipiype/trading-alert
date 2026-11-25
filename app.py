import argparse
import asyncio
import errno
import hashlib
import json
import logging
import os
import re
import sys
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from alarms import AlarmWorker
from core import PROMPT, Store, candidates, validate
from history import chunks, digest, labeled, weekly
from providers import APIError, Providers, send_owner


class SessionInUseError(Exception):
    pass


@contextmanager
def service_lock(data):
    with (data / "service.lock").open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise SessionInUseError(
                "Another process is using this data folder. Stop it before starting the bot or logging in."
            ) from None
        yield


def configuration():
    from dotenv import load_dotenv

    load_dotenv()
    config = dict(os.environ)
    config.setdefault("DATA_DIR", "data")
    config.setdefault("OPENAI_MODEL", "gpt-4o-mini")
    config["ZONE"] = ZoneInfo(config.get("TIMEZONE", "Europe/Vienna"))
    config["DIGEST_TIME"] = datetime.strptime(config.get("DIGEST_TIME", "18:00"), "%H:%M").time()
    config["ALIASES"] = json.loads(config.get("ALIASES_JSON", '{"XAUUSD":["gold","XAU","XAUUSD"]}'))
    if not config["ALIASES"] or any(
        not re.fullmatch(r"[A-Z0-9._-]+", symbol) or symbol in (".", "..")
        or not isinstance(names, list) or not names
        or any(not isinstance(name, str) or not name.strip() for name in names)
        for symbol, names in config["ALIASES"].items()
    ):
        raise ValueError("Invalid ALIASES_JSON")
    config["EXPOSURE_UNITS"] = json.loads(config.get("EXPOSURE_UNITS_JSON", "{}"))
    if any(
        symbol not in config["ALIASES"] or type(units) not in (int, float)
        or not 0 < units < float("inf") for symbol, units in config["EXPOSURE_UNITS"].items()
    ):
        raise ValueError("Invalid EXPOSURE_UNITS_JSON")
    config["COOLDOWN"] = int(config.get("COOLDOWN_SECONDS", "300"))
    config["MAX_WAKE_AGE"] = int(config.get("MAX_WAKE_AGE_SECONDS", "300"))
    return config


def require(config, keys):
    missing = [key for key in keys if not config.get(key)]
    if missing:
        raise ValueError("Missing configuration: " + ", ".join(missing))


def classify_pending(store, providers, config):
    now = time.time()
    messages = store.rows(
        "SELECT * FROM messages WHERE decision IS NULL AND retry_at<=? ORDER BY id LIMIT 20", now,
    )
    for row in messages:
        try:
            if row["media"]:
                store.decide(row, None, "ignored " + row["media"], now, 0, 0)
                continue
            symbols, direction = candidates(row["body"], config["ALIASES"])
            fresh = bool(symbols and direction)
            parents = []
            reply_id = row["reply_to"]
            seen = set()
            while reply_id and reply_id not in seen and len(parents) < 4:
                seen.add(reply_id)
                matches = store.rows(
                    """SELECT telegram_id, reply_to, body, classification FROM messages
                       WHERE telegram_id=? AND media IS NULL ORDER BY edited DESC,id DESC LIMIT 1""",
                    reply_id,
                )
                if not matches:
                    break
                parents.append(dict(matches[0]))
                reply_id = matches[0]["reply_to"]
            if not fresh and not any(parent["classification"] for parent in parents):
                if row["reply_to"] and row["attempts"] == 0 and (not parents or parents[0]["classification"] is None):
                    store.write("UPDATE messages SET attempts=1,retry_at=? WHERE id=?", now + 5, row["id"])
                    continue
                store.decide(row, None, "outside candidate filter", now, 0, 0)
                continue
            result = providers.llm(PROMPT, {
                "allowed_aliases": config["ALIASES"], "message": row["body"],
                "reply_to": row["reply_to"], "reply_context": parents,
            })
            result = validate(result, row["body"], row["reply_to"], config["ALIASES"], fresh)
            store.decide(
                row, result, result["type"], time.time(), config["COOLDOWN"], config["MAX_WAKE_AGE"],
            )
        except Exception as error:
            logging.warning("Classification %s failed (%s)", row["id"], type(error).__name__)
            if row["attempts"] >= 5:
                store.decide(row, None, "classification failed after retries", now, 0, 0)
            else:
                store.write(
                    "UPDATE messages SET attempts=attempts+1,retry_at=? WHERE id=?",
                    now + min(120, 5 * 2 ** row["attempts"]), row["id"],
                )


def send(providers, config, text, **options):
    for part in chunks(text, 3500):
        send_owner(providers, config, part, **options)


def alarm_menu(store):
    on = store.get("alarm") == "on"
    return f"Alarm is {'ON' if on else 'OFF'} · iPhone Clock via Telegram.", {
        "inline_keyboard": [[
            {"text": "Turn alarm off" if on else "Turn alarm on", "callback_data": "off" if on else "on"}
        ]]
    }


def set_alarm(store, mode):
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('alarm',?)", (mode,))
        if mode == "off":
            db.execute("UPDATE alerts SET state='canceled' WHERE state='pending'")


def handle_update(store, providers, config, update):
    callback = update.get("callback_query")
    message = (callback or {}).get("message") or update.get("message") or {}
    owner = int(config["BOT_OWNER_ID"])
    if message.get("chat", {}).get("type") != "private" or message["chat"]["id"] != owner:
        return
    if ((callback or message).get("from") or {}).get("id") != owner:
        return
    if callback:
        mode = callback.get("data")
        if mode in ("on", "off"):
            set_alarm(store, mode)
            text, keyboard = alarm_menu(store)
            providers.bot("answerCallbackQuery", callback_query_id=callback["id"], text=text)
            send(providers, config, text, reply_markup=keyboard)
        return
    text = message.get("text", "").strip()
    if text in ("/start", "/alarm"):
        status, keyboard = alarm_menu(store)
        send(providers, config, status + "\n/dashboard · /history [before ID] · /review ID false|missed|ok\nSend a WhatsApp .txt export here.", reply_markup=keyboard)
    elif text == "/dashboard":
        stats = store.rows(
            """SELECT count(*) AS captured,
               count(CASE WHEN json_extract(classification,'$.type')='new_signal' THEN 1 END) AS signals,
               count(CASE WHEN review='false' THEN 1 END) AS false_alarms,
               count(CASE WHEN review='missed' THEN 1 END) AS missed
               FROM messages"""
        )[0]
        status, keyboard = alarm_menu(store)
        send(
            providers, config,
            f"{status}\nCaptured revisions: {stats['captured']}\n"
            f"New signal classifications: {stats['signals']}\n"
            f"Marked false: {stats['false_alarms']} · Marked missed: {stats['missed']}\n"
            "Browse messages and parsed fields with /history.",
            reply_markup=keyboard,
        )
    elif text == "/history" or text.startswith("/history "):
        _, _, before = text.partition(" ")
        if before and not re.fullmatch(r"[0-9]{1,18}", before):
            send(providers, config, "Usage: /history [before ID]")
            return
        rows = store.rows(
            "SELECT * FROM messages WHERE id<? ORDER BY id DESC LIMIT 5",
            int(before) if before else 2**63 - 1,
        )
        send(providers, config, (
            "\n\n".join(labeled(row, config["ZONE"]) for row in rows)
            + f"\n\nOlder messages: /history {rows[-1]['id']}" if rows else "No messages on this page."
        ))
    elif text.startswith("/review"):
        match = re.fullmatch(r"/review ([0-9]{1,18}) (false|missed|ok)", text)
        if not match:
            send(providers, config, "Usage: /review ID false|missed|ok")
            return
        row_id = int(match[1])
        if not store.rows("SELECT id FROM messages WHERE id=?", row_id):
            send(providers, config, "Message ID not found.")
            return
        store.write("UPDATE messages SET review=? WHERE id=?", match[2], row_id)
        send(providers, config, f"Marked #{row_id} as {match[2]}.")
    elif document := message.get("document"):
        if not document.get("file_name", "").lower().endswith(".txt") or document.get("file_size", 0) > 2_000_000:
            send(providers, config, "Send a UTF-8 WhatsApp .txt export under 2 MB.")
            return
        try:
            body = providers.download_export(document["file_id"])
        except UnicodeError:
            send(providers, config, "The export must be UTF-8 text.")
            return
        if not body.strip():
            send(providers, config, "The export is empty.")
            return
        now = datetime.fromtimestamp(message["date"], config["ZONE"])
        cutoff = now.date() + timedelta(days=now.time() >= config["DIGEST_TIME"])
        store.write(
            "INSERT OR IGNORE INTO imports VALUES (?,?,?,?)",
            hashlib.sha256(body.encode()).hexdigest(), cutoff.isoformat(), now.timestamp(), body,
        )
        send(providers, config, f"WhatsApp export saved for the {cutoff.isoformat()} digest.")


def bot_poll(store, providers, config):
    updates = providers.bot(
        "getUpdates", offset=int(store.get("bot_offset", "0")), timeout=15,
        allowed_updates=["message", "callback_query"],
    )
    for update in updates:
        handle_update(store, providers, config, update)
        store.set("bot_offset", update["update_id"] + 1)


def report(store, providers, config, kind, day, render):
    key = day.isoformat()
    rows = store.rows("SELECT * FROM reports WHERE kind=? AND day=?", kind, key)
    if not rows:
        store.write("INSERT INTO reports(kind,day,body) VALUES (?,?,?)", kind, key, render())
        rows = store.rows("SELECT * FROM reports WHERE kind=? AND day=?", kind, key)
    row = rows[0]
    for cursor, part in enumerate(chunks(row["body"], 3500), 1):
        if cursor <= row["cursor"]:
            continue
        send(providers, config, part)
        store.write("UPDATE reports SET cursor=? WHERE kind=? AND day=?", cursor, kind, key)


def run_reports(store, providers, config):
    now = datetime.now(config["ZONE"])
    day = now.date()
    if now.hour >= 9 and time.time() - float(store.get("telegram_health", "0")) < 150:
        report(store, providers, config, "heartbeat", day, lambda: "System alive. Telegram session is connected.")
    latest_cutoff = day if now.time() >= config["DIGEST_TIME"] else day - timedelta(days=1)
    last_digest = datetime.fromisoformat(store.get("last_digest", latest_cutoff.isoformat())).date()
    if last_digest >= latest_cutoff:
        return
    cutoff_day = last_digest + timedelta(days=1)
    end = datetime.combine(cutoff_day, config["DIGEST_TIME"], config["ZONE"])
    report(store, providers, config, "digest", cutoff_day, lambda: digest(store, providers, end, config["ZONE"]))
    if cutoff_day.weekday() == 6:
        report(store, providers, config, "weekly", cutoff_day, lambda: weekly(store, end, config["ZONE"]))
    store.set("last_digest", cutoff_day.isoformat())


async def loop(job, interval):
    while True:
        try:
            await asyncio.to_thread(job)
        except Exception as error:
            detail = str(error) if isinstance(error, APIError) else type(error).__name__
            logging.warning("%s failed (%s)", job.__name__, detail)
            await asyncio.sleep(max(5, interval))
        else:
            await asyncio.sleep(interval)


async def telegram(config, store=None, providers=None, login=False):
    from telethon import TelegramClient, events, errors, utils

    session = str(Path(config["DATA_DIR"]) / "telegram")
    client = TelegramClient(session, int(config["TELEGRAM_API_ID"]), config["TELEGRAM_API_HASH"])
    if login:
        async with client:
            async for dialog in client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    print(f"{dialog.id}  {dialog.name}")
        return
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise errors.AuthKeyUnregisteredError(None)
        group_setting = config["TELEGRAM_GROUP"]
        if re.fullmatch(r"-?[0-9]+", group_setting):
            peer = int(group_setting)
            async for dialog in client.iter_dialogs():
                if dialog.id == peer:
                    group = dialog.entity
                    break
            else:
                raise ValueError("Configured Telegram group is absent from dialogs")
        else:
            group = await client.get_entity(group_setting)
        chat_id = utils.get_peer_id(group)
        bound_chat = store.get("telegram_chat_id")
        if bound_chat and bound_chat != str(chat_id):
            raise ValueError("Use a new data directory for a different Telegram group")
        store.set("telegram_chat_id", chat_id)
        username = getattr(group, "username", None)

        def ingest(message):
            if username:
                link = f"https://t.me/{username}/{message.id}"
            elif str(chat_id).startswith("-100"):
                link = f"https://t.me/c/{str(chat_id)[4:]}/{message.id}"
            else:
                link = f"tg://openmessage?chat_id={abs(chat_id)}&message_id={message.id}"
            store.ingest(
                message.id, message.date.timestamp(), (message.edit_date or message.date).timestamp(),
                message.reply_to_msg_id, message.raw_text or "", link,
                "voice" if message.voice else ("non-text message" if not message.raw_text else None),
            )

        async def received(event):
            if event.chat_id == chat_id:
                ingest(event.message)

        client.add_event_handler(received, events.NewMessage())
        client.add_event_handler(received, events.MessageEdited())
        cursor_key = f"telegram_cursor:{chat_id}"
        current = store.get(cursor_key)
        if not current:
            latest = await client.get_messages(group, limit=1)
            store.set(cursor_key, latest[0].id if latest else 0)
        else:
            await client.catch_up()
        logging.info("Telegram signal listener is ready. Send /alarm to the private bot.")
        while True:
            if not client.is_connected():
                raise ConnectionError("Telegram disconnected")
            if not await client.is_user_authorized():
                raise errors.AuthKeyUnregisteredError(None)
            async for message in client.iter_messages(group, min_id=int(store.get(cursor_key)), reverse=True):
                ingest(message)
                store.set(cursor_key, message.id)
            store.set("telegram_health", time.time())
            store.set("session_warning", "")
            await asyncio.sleep(30)
    except (errors.UnauthorizedError, errors.AuthKeyError):
        if not store.get("session_warning"):
            await asyncio.to_thread(
                send_owner, providers, config, "Telegram login lost. Run the service login command again.",
            )
            store.set("session_warning", "sent")
        raise
    finally:
        await client.disconnect()


async def serve(config, store):
    store.prepare_telegram_wakeups()
    providers = Providers(config)
    worker = AlarmWorker(store, providers, config)
    now = datetime.now(config["ZONE"])
    if not store.get("last_digest"):
        store.set("last_digest", (now.date() - timedelta(days=1)).isoformat() if now.time() < config["DIGEST_TIME"] else now.date().isoformat())

    def classify():
        classify_pending(store, providers, config)

    def bot():
        bot_poll(store, providers, config)

    def reports():
        run_reports(store, providers, config)

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(telegram(config, store, providers))
        tasks.create_task(loop(classify, 0.5))
        tasks.create_task(loop(worker.tick, 2))
        tasks.create_task(loop(bot, 0))
        tasks.create_task(loop(reports, 30))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "login", "backup"], nargs="?", default="run")
    parser.add_argument("destination", nargs="?")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.CRITICAL)
    os.umask(0o077)
    try:
        config = configuration()
        data = Path(config["DATA_DIR"])
        data.mkdir(parents=True, exist_ok=True)
        if args.command == "login":
            require(config, ["TELEGRAM_API_ID", "TELEGRAM_API_HASH"])
            with service_lock(data):
                asyncio.run(telegram(config, login=True))
            return
        if args.command == "backup":
            import sqlite3

            if not args.destination:
                parser.error("backup requires a destination file")
            store = Store(data / "signals.sqlite")
            destination = Path(args.destination)
            with destination.open("xb"):
                pass
            with store.connect() as source, closing(sqlite3.connect(destination)) as target:
                source.backup(target)
            return
        require(config, [
            "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_GROUP", "BOT_TOKEN",
            "BOT_OWNER_ID", "OPENAI_API_KEY",
        ])
        if not re.fullmatch(r"[1-9][0-9]{0,15}", config["BOT_OWNER_ID"]):
            raise ValueError("BOT_OWNER_ID must be a positive Telegram user ID")
        with service_lock(data):
            asyncio.run(serve(config, Store(data / "signals.sqlite")))
    except KeyboardInterrupt:
        pass
    except SessionInUseError as error:
        logging.error("%s", error)
        sys.exit(1)
    except ExceptionGroup as error:
        logging.error("Service stopped (%s)", ", ".join(type(item).__name__ for item in error.exceptions))
        sys.exit(1)
    except ValueError as error:
        if str(error).startswith(("Missing configuration: ", "BOT_OWNER_ID must be ")):
            logging.error("%s", error)
        else:
            logging.error("Startup or command failed (invalid configuration)")
        sys.exit(1)
    except Exception as error:
        logging.error("Startup or command failed (%s)", type(error).__name__)
        sys.exit(1)


if __name__ == "__main__":
    main()
