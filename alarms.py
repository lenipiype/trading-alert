import json
import logging
import time

from history import exposure, price_context


IPHONE_CLOCK = "iphone_clock"


def alert_text(row, now, quote=None):
    signal = json.loads(row["classification"])
    values = [
        f"{signal['type'].upper()} {(signal['direction'] or '?').upper()} {signal['symbol'] or '?'}",
        " | ".join(
            f"{label} {signal[key] if signal[key] is not None else 'NOT GIVEN'}"
            for label, key in (("Entry", "entry"), ("SL", "sl"), ("TP", "tp"))
        ),
        f"Age {max(0, now - row['sent']) / 60:.1f} min | {quote or 'Price comparison unavailable'}",
        "Check the Telegram group for voice details.",
        row["link"],
    ]
    return "\n".join(values) + "\n\n" + row["body"]


class AlarmWorker:
    def __init__(self, store, providers, config):
        self.store, self.providers, self.config = store, providers, config

    def tick(self, now=None):
        now = time.time() if now is None else now
        rows = self.store.rows(
            """SELECT alerts.*, messages.sent, messages.body, messages.link, messages.classification
               FROM alerts JOIN messages ON messages.id=alerts.message_id
               WHERE state IN ('pending', 'active', 'stopping') AND alerts.retry_at<=?
               ORDER BY message_id""", now,
        )
        for row in rows:
            try:
                self.advance(row, now)
            except Exception as error:
                logging.warning("Alarm %s provider step failed (%s)", row["message_id"], type(error).__name__)
                self.store.write(
                    "UPDATE alerts SET attempts=attempts+1, retry_at=? WHERE message_id=?",
                    now + min(60, 2 ** min(6, row["attempts"] + 1)), row["message_id"],
                )

    def advance(self, row, now):
        message_id = row["message_id"]
        iphone_clock = self.config.get("ALARM_MODE") == IPHONE_CLOCK
        if row["state"] == "pending":
            quote = None
            if self.config.get("QUOTE_DIR"):
                quote = price_context(row, now, self.config)
            if quote is None:
                quote = "Price comparison unavailable\n" + exposure(
                    json.loads(row["classification"]), None, self.config,
                )
            full_text = alert_text(row, now, quote)
            text = full_text[:995] + ("…" if len(full_text) > 995 else "")
            wake = bool(
                now - row["sent"] <= self.config["MAX_WAKE_AGE"]
                and self.store.rows(
                    """SELECT 1 FROM alerts WHERE message_id=? AND emergency=1 AND state='pending'
                       AND EXISTS (SELECT 1 FROM settings WHERE key='alarm' AND value='on')""",
                    message_id,
                )
            )
            emergency = wake and not iphone_clock
            if wake and iphone_clock:
                receipt = self.providers.push(text, 0, title=f"Trading Wake-Up #{message_id}")
            else:
                receipt = self.providers.push(text, 2 if emergency else 0)
            self.store.write(
                """UPDATE alerts SET receipt=?, started=?, state=?, attempts=0,
                   retry_at=0 WHERE message_id=? AND state='pending'""",
                receipt if emergency else None, now, "active" if emergency else "sent", message_id,
            )
            if emergency:
                still_armed = self.store.rows(
                    "SELECT emergency FROM alerts WHERE message_id=?", message_id,
                )[0]["emergency"]
                if not still_armed or self.store.get("alarm") != "on":
                    self.stop(message_id)
            return
        if row["state"] == "stopping" or iphone_clock or self.store.get("alarm") != "on":
            self.stop(message_id)
            return
        receipt = self.providers.receipt(row["receipt"])
        if receipt.get("acknowledged") or receipt.get("expired") or now - row["started"] >= 600:
            self.stop(message_id)
            return
        elapsed = now - row["started"]
        if elapsed >= 120 and row["owner_at"] is None and self.config.get("OWNER_PHONE"):
            self.place_call(message_id, "owner", self.config["OWNER_PHONE"], now, row["receipt"])
        elif (
            row["owner_at"] is not None and row["backup_at"] is None
            and now - row["owner_at"] >= 180 and self.config.get("BACKUP_PHONE")
        ):
            self.place_call(message_id, "backup", self.config["BACKUP_PHONE"], now, row["receipt"])

    def place_call(self, message_id, stage, target, now, receipt):
        status = self.providers.receipt(receipt)
        state = self.store.rows("SELECT state FROM alerts WHERE message_id=?", message_id)[0]["state"]
        if state != "active" or self.store.get("alarm") != "on" or status.get("acknowledged") or status.get("expired"):
            self.stop(message_id)
            return
        # Record the attempt first. A timed-out create may already have placed a call.
        self.store.write(f"UPDATE alerts SET {stage}_at=? WHERE message_id=?", now, message_id)
        sid = self.providers.call(target)
        self.store.write(f"UPDATE alerts SET {stage}_call=? WHERE message_id=?", sid, message_id)
        if self.store.get("alarm") != "on":
            self.stop(message_id)

    def stop(self, message_id):
        self.store.write("UPDATE alerts SET state='stopping' WHERE message_id=?", message_id)
        row = self.store.rows("SELECT * FROM alerts WHERE message_id=?", message_id)[0]
        failures = []
        for column, cancel in (
            ("owner_call", self.providers.stop_call),
            ("backup_call", self.providers.stop_call),
            ("receipt", self.providers.cancel_push),
        ):
            if row[column]:
                try:
                    cancel(row[column])
                except Exception as error:
                    failures.append(error)
                else:
                    self.store.write(f"UPDATE alerts SET {column}=NULL WHERE message_id=?", message_id)
        if failures:
            raise failures[0]
        self.store.write("UPDATE alerts SET state='stopped' WHERE message_id=?", message_id)
