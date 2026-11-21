import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from alarms import AlarmWorker
from app import classify_pending, handle_update, set_alarm
from core import Store, validate
from history import digest, exposure, price_context
from providers import APIError, Providers


class FakeProviders:
    def __init__(self):
        self.events = []
        self.acknowledged = False

    def push(self, message, priority, *, title=None):
        self.events.append(("push", priority) if title is None else ("push", priority, title))
        return "receipt"

    def receipt(self, code):
        return {"acknowledged": self.acknowledged}

    def cancel_push(self, code):
        self.events.append(("cancel", code))

    def call(self, target):
        self.events.append(("call", target))
        return target + "-sid"

    def stop_call(self, sid):
        self.events.append(("stop", sid))

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
        return {
            "type": kind, "symbol": "XAUUSD", "direction": "sell", "entry": "market",
            "sl": None, "tp": None, "scaling_plan": None, "confidence": "high",
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
            "MAX_WAKE_AGE": 300, "COOLDOWN": 300, "OWNER_PHONE": "owner",
            "BACKUP_PHONE": "backup", "BOT_OWNER_ID": "42", "ZONE": self.zone,
            "DIGEST_TIME": datetime.strptime("18:00", "%H:%M").time(), "EXPOSURE_UNITS": {},
        }
        self.alarm = AlarmWorker(self.store, self.providers, self.config)
        self.now = datetime.now().timestamp()

    def ingest(self, telegram_id, body, *, edited=None, reply=None, media=None):
        self.store.ingest(telegram_id, self.now, edited or self.now, reply, body, "https://t.me/test/1", media)

    def classify(self):
        classify_pending(self.store, self.providers, self.config)

    def test_phase_one_classification_and_acknowledgement(self):
        set_alarm(self.store, "on")
        self.ingest(1, "Selling gold now")
        self.ingest(1, "Selling gold now")
        self.ingest(2, "should we sell gold?")
        self.ingest(3, "Selling gold now", media="voice")
        self.ingest(4, "Move SL to 2380", reply=1)
        self.classify()
        self.alarm.tick(self.now + 1)
        self.assertEqual([("push", 2), ("push", 0)], self.providers.events)
        self.providers.acknowledged = True
        self.alarm.tick(self.now + 130)
        self.ingest(1, "Selling gold now, checking edits", edited=self.now + 10)
        self.classify()
        self.assertEqual(("cancel", "receipt"), self.providers.events[-1])
        self.assertEqual("stopped", self.store.rows("SELECT state FROM alerts WHERE message_id=1")[0]["state"])
        self.assertEqual(2, len(self.store.rows("SELECT * FROM alerts")))

    def test_phase_two_toggle_cooldown_and_escalation(self):
        self.assertEqual("", self.store.get("alarm"))
        self.ingest(1, "Selling gold now")
        self.classify()
        self.alarm.tick(self.now + 1)
        set_alarm(self.store, "on")
        self.ingest(2, "Selling gold now")
        self.ingest(3, "Selling XAUUSD now")
        self.classify()
        self.alarm.tick(self.now + 2)
        self.alarm.tick(self.now + 122)
        self.alarm.tick(self.now + 302)
        set_alarm(self.store, "off")
        set_alarm(self.store, "on")
        self.alarm.tick(self.now + 303)
        self.assertEqual(
            [("push", 0), ("push", 2), ("push", 0), ("call", "owner"), ("call", "backup"),
             ("stop", "owner-sid"), ("stop", "backup-sid"), ("cancel", "receipt")],
            self.providers.events,
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

    def test_clock_mode_sends_one_trigger_and_remains_armed_for_later_signals(self):
        self.config["ALARM_MODE"] = "iphone_clock"
        set_alarm(self.store, "on")
        self.ingest(1, "Selling gold now")
        self.ingest(2, "Selling XAUUSD now")
        self.ingest(3, "Move SL to 2380", reply=1)
        self.classify()
        self.alarm.tick(self.now + 1)
        self.alarm.tick(self.now + 125)
        self.alarm.tick(self.now + 310)
        self.assertEqual(
            [("push", 0, "Trading Wake-Up #1"), ("push", 0), ("push", 0)],
            self.providers.events,
        )
        alert = self.store.rows("SELECT * FROM alerts WHERE message_id=1")[0]
        self.assertEqual("sent", alert["state"])
        self.assertIsNone(alert["receipt"])
        self.assertEqual("on", self.store.get("alarm"))

        self.now += 320
        self.ingest(4, "Selling gold now")
        with patch("app.time.time", return_value=self.now):
            self.classify()
        self.alarm.tick(self.now + 1)
        self.assertEqual(("push", 0, "Trading Wake-Up #4"), self.providers.events[-1])

    def test_clock_toggle_off_downgrades_queued_trigger_and_preserves_ordinary_alert(self):
        self.config["ALARM_MODE"] = "iphone_clock"
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
        self.assertEqual([("push", 0), ("push", 0)], self.providers.events)

    def test_clock_push_retry_uses_the_same_title_and_expires_to_an_ordinary_push(self):
        self.config["ALARM_MODE"] = "iphone_clock"
        set_alarm(self.store, "on")
        self.ingest(1, "Selling gold now")
        self.classify()
        with patch.object(self.providers, "push", side_effect=APIError("timeout")) as failed:
            with self.assertLogs(level="WARNING"):
                self.alarm.tick(self.now + 1)
            self.assertEqual("Trading Wake-Up #1", failed.call_args.kwargs["title"])
        self.alarm.tick(self.now + 3)
        self.assertEqual([("push", 0, "Trading Wake-Up #1")], self.providers.events)

        self.now += 320
        self.ingest(2, "Selling gold now")
        with patch("app.time.time", return_value=self.now):
            self.classify()
        self.alarm.tick(self.now + 301)
        self.assertEqual(("push", 0), self.providers.events[-1])

    def test_clock_notification_payload_has_a_title_and_no_emergency_retry(self):
        providers = Providers({"PUSHOVER_TOKEN": "test-token", "PUSHOVER_USER": "test-user"})
        with patch.object(providers, "pushover", return_value={"request": "test-id"}) as request:
            providers.push("New signal", 0, title="Trading Wake-Up #1")
        request.assert_called_once_with(
            "messages.json",
            {
                "token": "test-token", "user": "test-user", "message": "New signal",
                "priority": 0, "title": "Trading Wake-Up #1",
            },
        )

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
