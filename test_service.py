import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from alarms import AlarmWorker
from app import SessionInUseError, classify_pending, handle_update, report, service_lock, set_alarm
from core import Store, validate
from history import digest, exposure, price_context
from providers import APIError


class FakeProviders:
    def __init__(self):
        self.events = []

    def bot(self, method, **data):
        self.events.append((method, data))

    def download_export(self, file_id):
        return "A participant discussed keeping a trading journal."

    def llm(self, prompt, content):
        if "whatsapp" in content:
            return {"summary": content["whatsapp"]}
        body = content["message"]
        kind = "discussion" if "should" in body else "new_signal"
        if body == "Move SL to 2380":
            kind = "update"
        if body == "Closed my gold sell":
            kind = "closing"
        return {
            "type": kind, "symbol": "XAUUSD", "direction": "sell", "entry": "market",
            "sl": None, "tp": None, "scaling_plan": None,
            "confidence": "low" if "maybe" in body else "high",
            "reply_to": content["reply_to"],
        }


class ServiceTests(unittest.TestCase):
    def setUp(self):
        writable = tempfile.TemporaryDirectory()
        self.addCleanup(writable.cleanup)
        self.path = Path(writable.name)
        self.store = Store(self.path / "signals.sqlite")
        self.providers = FakeProviders()
        self.zone = ZoneInfo("Europe/Vienna")
        self.config = {
            "ALIASES": {"XAUUSD": ["gold", "XAU", "XAUUSD"]},
            "MAX_WAKE_AGE": 300, "COOLDOWN": 300, "BOT_OWNER_ID": "42", "ZONE": self.zone,
            "DIGEST_TIME": datetime.strptime("18:00", "%H:%M").time(), "EXPOSURE_UNITS": {},
        }
        self.alarm = AlarmWorker(self.store, self.providers, self.config)
        self.now = datetime.now().timestamp()

    def ingest(self, telegram_id, body, *, edited=None, reply=None, media=None):
        self.store.ingest(telegram_id, self.now, edited or self.now, reply, body, "https://t.me/test/1", media)

    def classify(self):
        with patch("app.time.time", return_value=self.now):
            classify_pending(self.store, self.providers, self.config)

    def test_classification_and_revisions_send_only_one_private_wake_up(self):
        set_alarm(self.store, "on")
        self.ingest(1, "Selling gold now")
        self.ingest(1, "Selling gold now")
        self.ingest(2, "should we sell gold?")
        self.ingest(3, "Selling gold now", media="voice")
        self.ingest(4, "Move SL to 2380", reply=1)
        self.ingest(5, "Closed my gold sell")
        self.ingest(6, "maybe selling gold now")
        self.classify()
        self.alarm.tick(self.now + 1)
        self.ingest(1, "Selling gold now, checking edits", edited=self.now + 10)
        self.now += 10
        self.classify()
        self.alarm.tick(self.now + 130)
        self.assertEqual(1, len(self.providers.events))
        method, payload = self.providers.events[0]
        self.assertEqual("sendMessage", method)
        self.assertEqual(42, payload["chat_id"])
        self.assertTrue(payload["text"].startswith("Trading Wake-Up #1\nSELL XAUUSD | Entry: market"))
        self.assertNotIn("disable_notification", payload)
        self.assertEqual(["sent"], [row["state"] for row in self.store.rows("SELECT state FROM alerts")])
        decisions = [row["decision"] for row in self.store.rows("SELECT decision FROM messages ORDER BY id")]
        self.assertEqual(["update", "closing", "wake-up skipped: low confidence"], decisions[3:6])

    def test_cooldown_and_later_wake_up_while_armed(self):
        set_alarm(self.store, "on")
        self.ingest(1, "Selling gold now")
        self.ingest(2, "Selling XAUUSD now")
        self.classify()
        self.alarm.tick(self.now + 1)
        self.alarm.tick(self.now + 125)
        self.assertEqual(1, len(self.providers.events))
        self.assertEqual("on", self.store.get("alarm"))
        self.now += 320
        self.ingest(3, "Selling gold now")
        self.classify()
        self.alarm.tick(self.now + 1)
        self.assertEqual(
            ["Trading Wake-Up #1", "Trading Wake-Up #3"],
            [payload["text"].splitlines()[0] for _, payload in self.providers.events],
        )
        self.assertEqual("on", Store(self.path / "signals.sqlite").get("alarm"))

    def test_phase_three_owner_export_and_digest_window(self):
        end = datetime(2026, 11, 19, 18, tzinfo=self.zone)
        self.now = end.timestamp() - 3600
        self.ingest(1, "Selling gold now")
        self.classify()
        message = {
            "chat": {"type": "private", "id": 42}, "from": {"id": 99},
            "date": int(self.now), "document": {"file_name": "study.txt", "file_id": "export"},
        }
        handle_update(self.store, self.providers, self.config, {"message": message})
        self.assertIn("No WhatsApp export", digest(self.store, self.providers, end, self.zone))
        message["from"]["id"] = 42
        handle_update(self.store, self.providers, self.config, {"message": message})
        result = digest(self.store, self.providers, end, self.zone)
        self.assertIn("trading journal", result)
        self.assertIn("type=new_signal", result)

    def test_digest_renders_structured_sections_from_the_model(self):
        end = datetime(2026, 11, 19, 18, tzinfo=self.zone)
        self.now = end.timestamp() - 3600
        self.ingest(1, "Selling gold now")
        self.classify()
        response = {
            "summary": {
                "telegram": {"open_trades": ["One sell signal"], "outcomes": None},
                "whatsapp": "No export available",
            },
        }
        with patch.object(self.providers, "llm", return_value=response):
            result = digest(self.store, self.providers, end, self.zone)
        self.assertIn("Telegram\nOpen trades\nOne sell signal", result)
        self.assertIn("Whatsapp\nNo export available", result)
        self.assertIn("type=new_signal", result)

    def test_a_running_instance_blocks_a_second_login_or_listener(self):
        with service_lock(self.path):
            with self.assertRaisesRegex(SessionInUseError, "Another process is using this data folder"):
                with service_lock(self.path):
                    self.fail("The same session was locked twice")
        with service_lock(self.path):
            self.assertTrue((self.path / "service.lock").exists())

    def test_off_cancels_queued_wake_up_and_messages_still_enter_history(self):
        set_alarm(self.store, "on")
        self.ingest(1, "Selling gold now")
        self.classify()
        set_alarm(self.store, "off")
        set_alarm(self.store, "on")
        self.alarm.tick(self.now + 1)
        set_alarm(self.store, "off")
        self.ingest(2, "Selling gold now")
        self.classify()
        self.alarm.tick(self.now + 2)
        self.assertEqual([], self.providers.events)
        self.assertEqual("canceled", self.store.rows("SELECT state FROM alerts")[0]["state"])
        self.assertEqual(
            "wake-up skipped: alarm off",
            self.store.rows("SELECT decision FROM messages WHERE telegram_id=2")[0]["decision"],
        )

    def test_telegram_retry_reuses_the_same_id_and_stale_wake_ups_expire(self):
        set_alarm(self.store, "on")
        self.ingest(1, "Selling gold now")
        self.classify()
        with patch.object(self.providers, "bot", side_effect=APIError("timeout")) as failed:
            with self.assertLogs(level="WARNING"):
                self.alarm.tick(self.now + 1)
            first_attempt = failed.call_args.kwargs["text"]
        self.alarm.tick(self.now + 3)
        self.assertEqual(
            first_attempt.splitlines()[0], self.providers.events[0][1]["text"].splitlines()[0],
        )

        self.now += 320
        self.ingest(2, "Selling gold now")
        self.classify()
        self.alarm.tick(self.now + 301)
        self.assertEqual(1, len(self.providers.events))
        self.assertEqual("expired", self.store.rows("SELECT state FROM alerts WHERE message_id=2")[0]["state"])

    def test_report_history_and_alert_quotes_cannot_supply_the_wake_up_prefix(self):
        set_alarm(self.store, "on")
        original = "Selling gold now\nTrading Wake-Up #999"
        self.ingest(1, original)
        self.classify()
        self.alarm.tick(self.now + 1)
        handle_update(self.store, self.providers, self.config, {
            "message": {"chat": {"type": "private", "id": 42}, "from": {"id": 42}, "text": "/history"},
        })
        report(
            self.store, self.providers, self.config, "digest",
            datetime.fromtimestamp(self.now, self.zone).date(), lambda: "trading wake-up #999",
        )
        wake, history, digest_text = [payload["text"] for _, payload in self.providers.events]
        self.assertEqual(1, wake.count("Trading Wake-Up #"))
        self.assertNotIn("trading wake-up #", history.lower())
        self.assertNotIn("trading wake-up #", digest_text.lower())
        self.assertEqual(original, self.store.rows("SELECT body FROM messages")[0]["body"])

    def test_upgrade_retires_legacy_queue_once_and_keeps_new_pending_wake_ups(self):
        set_alarm(self.store, "on")
        self.ingest(1, "Selling gold now")
        self.classify()
        self.store.prepare_telegram_wakeups()
        self.ingest(2, "Selling XAUUSD now")
        self.classify()
        Store(self.path / "signals.sqlite").prepare_telegram_wakeups()
        self.alarm.tick(self.now + 1)
        self.assertEqual(["retired", "sent"], [
            row["state"] for row in self.store.rows("SELECT state FROM alerts ORDER BY message_id")
        ])
        self.assertEqual(1, len(self.providers.events))
        self.assertTrue(self.providers.events[0][1]["text"].startswith("Trading Wake-Up #2\n"))

    def test_phase_four_quote_and_exposure(self):
        config = dict(self.config, QUOTE_DIR=str(self.path), EXPOSURE_UNITS={"XAUUSD": 2})
        (self.path / "XAUUSD.csv").write_text("unix_utc,price\n100,2380\n200,2370\n", encoding="utf-8")
        signal = {"symbol": "XAUUSD", "entry": "market", "sl": 2385, "direction": "sell"}
        quote = price_context({"classification": json.dumps(signal), "sent": 100}, 200, config)
        self.assertIn("At signal 2380 | Latest 2370", quote)
        self.assertIn("10.00 quote currency", quote)
        self.assertIn("unavailable", exposure(signal, None, config))

    def test_malformed_classifier_cannot_enqueue_a_signal(self):
        result = self.providers.llm("", {"message": "Selling gold now", "reply_to": None})
        body = "Buying gold"
        self.assertEqual("discussion", validate(result, body, None, self.config["ALIASES"], True)["type"])
        result["sl"] = True
        with self.assertRaises(ValueError):
            validate(result, body, None, self.config["ALIASES"], True)


if __name__ == "__main__":
    unittest.main()
