import json
import logging
import time

from history import exposure, price_context
from providers import send_owner


def alert_text(row, now, quote=None):
    signal = json.loads(row["classification"])
    values = [
        " | ".join([
            f"{signal['direction'].upper()} {signal['symbol']}",
            *(
                f"{label}: {signal[key] if signal[key] is not None else 'NOT GIVEN'}"
                for label, key in (("Entry", "entry"), ("SL", "sl"), ("TP", "tp"))
            ),
        ]),
        f"Age {max(0, now - row['sent']) / 60:.1f} min | {quote or 'Price comparison unavailable'}",
        "Check the Telegram group for voice details.",
        row["link"],
    ]
    return "\n".join(values) + "\n\n" + row["body"]


class AlarmWorker:
    def __init__(self, store, providers, config):
        self.store, self.providers, self.config = store, providers, config

    def tick(self, now=None):
        rows = self.store.rows(
            """SELECT alerts.*, messages.sent, messages.body, messages.link, messages.classification
               FROM alerts JOIN messages ON messages.id=alerts.message_id
               WHERE state='pending' AND emergency=1 AND alerts.retry_at<=?
               ORDER BY message_id""", time.time() if now is None else now,
        )
        for row in rows:
            current = time.time() if now is None else now
            try:
                self.deliver(row, current)
            except Exception as error:
                logging.warning("Wake-up %s delivery failed (%s)", row["message_id"], type(error).__name__)
                self.store.write(
                    """UPDATE alerts SET attempts=attempts+1,retry_at=?
                       WHERE message_id=? AND state='pending'""",
                    current + min(60, 2 ** min(6, row["attempts"] + 1)), row["message_id"],
                )

    def deliver(self, row, now):
        message_id = row["message_id"]
        if now - row["sent"] > self.config["MAX_WAKE_AGE"]:
            self.store.write(
                "UPDATE alerts SET state='expired' WHERE message_id=? AND state='pending'", message_id,
            )
            return
        quote = price_context(row, now, self.config) if self.config.get("QUOTE_DIR") else None
        if quote is None:
            quote = "Price comparison unavailable\n" + exposure(
                json.loads(row["classification"]), None, self.config,
            )
        full_text = alert_text(row, now, quote)
        text = full_text[:995] + ("…" if len(full_text) > 995 else "")
        if self.store.rows(
            """SELECT 1 FROM alerts WHERE message_id=? AND state='pending' AND emergency=1
               AND EXISTS (SELECT 1 FROM settings WHERE key='alarm' AND value='on')""",
            message_id,
        ):
            send_owner(
                self.providers, self.config, text, wake_id=message_id,
                link_preview_options={"is_disabled": True},
            )
            self.store.write(
                "UPDATE alerts SET state='sent',started=?,attempts=0,retry_at=0 WHERE message_id=?",
                now, message_id,
            )
