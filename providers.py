import base64
import json
import urllib.error
import urllib.parse
import urllib.request


class APIError(Exception):
    """A provider error with no credentials or message bodies in its text."""


def request(url, *, data=None, form=False, headers=None, timeout=20, raw=False):
    headers = dict(headers or {})
    if data is not None:
        headers["Content-Type"] = (
            "application/x-www-form-urlencoded" if form else "application/json"
        )
        data = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data, headers), timeout=timeout,
        ) as response:
            body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise APIError("Response exceeds size limit")
        return body if raw else json.loads(body)
    except urllib.error.HTTPError as error:
        raise APIError(f"HTTP {error.code}") from None
    except (OSError, ValueError) as error:
        raise APIError(type(error).__name__) from None


class Providers:
    def __init__(self, config):
        self.config = config

    def llm(self, instructions, content):
        data = request(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": "Bearer " + self.config["OPENAI_API_KEY"]},
            data={
                "model": self.config["OPENAI_MODEL"],
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": json.dumps(content, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
            },
        )
        return json.loads(data["choices"][0]["message"]["content"])

    def push(self, message, priority=0, *, title=None):
        data = {
            "token": self.config["PUSHOVER_TOKEN"], "user": self.config["PUSHOVER_USER"],
            "message": message, "priority": priority,
        }
        if title is not None:
            data["title"] = title
        if priority == 2:
            data.update(retry=30, expire=600)
        result = self.pushover("messages.json", data)
        return result["receipt"] if priority == 2 else result.get("request")

    def pushover(self, path, data=None):
        result = request("https://api.pushover.net/1/" + path, data=data, form=True)
        if result.get("status") != 1:
            raise APIError("Pushover rejected request")
        return result

    def receipt(self, code):
        token = urllib.parse.urlencode({"token": self.config["PUSHOVER_TOKEN"]})
        return self.pushover(f"receipts/{code}.json?{token}")

    def cancel_push(self, code):
        self.pushover(f"receipts/{code}/cancel.json", {"token": self.config["PUSHOVER_TOKEN"]})

    def bot(self, method, **data):
        result = request(
            f"https://api.telegram.org/bot{self.config['BOT_TOKEN']}/{method}",
            data=data, timeout=30,
        )
        if not result.get("ok"):
            raise APIError("Telegram rejected request")
        return result["result"]

    def download_export(self, file_id):
        info = self.bot("getFile", file_id=file_id)
        path = urllib.parse.quote(info["file_path"], safe="/")
        url = f"https://api.telegram.org/file/bot{self.config['BOT_TOKEN']}/{path}"
        return request(url, raw=True).decode("utf-8-sig")

    def twilio(self, suffix="", data=None):
        sid = self.config["TWILIO_ACCOUNT_SID"]
        auth = base64.b64encode(f"{sid}:{self.config['TWILIO_AUTH_TOKEN']}".encode()).decode()
        return request(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls{suffix}.json",
            headers={"Authorization": "Basic " + auth}, data=data, form=True,
        )

    def call(self, target):
        return self.twilio(data={
            "To": target, "From": self.config["TWILIO_FROM"], "Timeout": 25,
            "Twiml": "<Response><Say>Trading signal alert. Check your Telegram group and acknowledge the alarm.</Say></Response>",
        })["sid"]

    def stop_call(self, sid):
        status = self.twilio("/" + sid)["status"]
        if status in ("queued", "ringing", "in-progress"):
            self.twilio("/" + sid, {"Status": "completed" if status == "in-progress" else "canceled"})
