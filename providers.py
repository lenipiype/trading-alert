import json
import re
import urllib.error
import urllib.parse
import urllib.request


class APIError(Exception):
    """A provider error with no credentials or message bodies in its text."""


WAKE_PREFIX = "Trading Wake-Up #"


def send_owner(providers, config, text, *, wake_id=None, **options):
    text = re.sub(re.escape(WAKE_PREFIX), "[wake-up] #", text, flags=re.IGNORECASE)
    if wake_id is not None:
        text = f"{WAKE_PREFIX}{wake_id}\n{text}"
    return providers.bot("sendMessage", chat_id=int(config["BOT_OWNER_ID"]), text=text, **options)


def request(url, *, data=None, headers=None, timeout=20, raw=False):
    headers = dict(headers or {})
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
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

    def bot(self, method, **data):
        try:
            result = request(
                f"https://api.telegram.org/bot{self.config['BOT_TOKEN']}/{method}",
                data=data, timeout=30,
            )
        except APIError as error:
            raise APIError(f"Telegram {method}: {error}") from None
        if not result.get("ok"):
            raise APIError("Telegram rejected request")
        return result["result"]

    def download_export(self, file_id):
        info = self.bot("getFile", file_id=file_id)
        path = urllib.parse.quote(info["file_path"], safe="/")
        url = f"https://api.telegram.org/file/bot{self.config['BOT_TOKEN']}/{path}"
        return request(url, raw=True).decode("utf-8-sig")

