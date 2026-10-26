"""In-process fake of the Telegram Bot API, plugged into python-telegram-bot as its request backend.

It keeps the sent messages, turns the HTML we send into text + entities the way Telegram does
(so `Message.text_html` round-trips through the real library), and rejects malformed HTML.
"""

from __future__ import annotations

import itertools
import json
import re
import time
from html.parser import HTMLParser

from telegram import Update
from telegram.request import BaseRequest

BOT_USER = {"id": 999, "is_bot": True, "first_name": "AnkiBot", "username": "anki_test_bot"}
ALLOWED_USER = 42
OTHER_USER = 7


def user(uid: int) -> dict:
    return {"id": uid, "is_bot": False, "first_name": f"user{uid}"}


def u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class BadHtml(Exception):
    pass


class _Parser(HTMLParser):
    ALLOWED = {"b": "bold", "i": "italic", "u": "underline", "s": "strikethrough", "code": "code"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.entities: list[dict] = []
        self.stack: list[tuple[str, int]] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.ALLOWED:
            raise BadHtml(f"Unsupported start tag {tag!r}")
        self.stack.append((tag, u16(self.text)))

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            raise BadHtml(f"Unmatched end tag {tag!r}")
        _, start = self.stack.pop()
        length = u16(self.text) - start
        if length:
            self.entities.append({"type": self.ALLOWED[tag], "offset": start, "length": length})

    def handle_data(self, data):
        self.text += data


_TAG = re.compile(r"</?(?:b|i|u|s|code)>")
_ENTITY = re.compile(r"&(?:lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);")


def parse_html(source: str) -> tuple[str, list[dict]]:
    # Telegram rejects any <, > or & that is not part of a supported tag or an entity.
    bare = _ENTITY.sub("", _TAG.sub("", source))
    if any(c in bare for c in "<>&"):
        raise BadHtml("unescaped <, > or &")
    parser = _Parser()
    parser.feed(source)
    parser.close()
    if parser.stack:
        raise BadHtml("Unclosed tag")
    return parser.text, sorted(parser.entities, key=lambda e: e["offset"])


class FakeTelegram(BaseRequest):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.messages: dict[tuple[int, int], dict] = {}
        # method name -> list of error payloads ({"error_code": ..., "description": ..., ...}) to return first
        self.failures: dict[str, list[dict]] = {}
        self._ids = itertools.count(1000)
        self._update_ids = itertools.count(1)
        # Raw updates handed out by the next getUpdates call (used when the real Updater polls).
        self.pending_updates: list[dict] = []

    @property
    def read_timeout(self):
        return None

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def fail(self, method: str, *errors: dict) -> None:
        self.failures.setdefault(method, []).extend(errors)

    @staticmethod
    def retry_after(seconds: int = 1) -> dict:
        return {
            "error_code": 429,
            "description": f"Too Many Requests: retry after {seconds}",
            "parameters": {"retry_after": seconds},
        }

    @staticmethod
    def bad_request(description: str) -> dict:
        return {"error_code": 400, "description": f"Bad Request: {description}"}

    async def do_request(self, url, method, request_data=None, **kwargs):
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        self.calls.append((name, params))
        pending = self.failures.get(name)
        if pending:
            return self._error(pending.pop(0))
        try:
            result = self._handle(name, params)
        except BadHtml as exc:
            return self._error(self.bad_request(f"can't parse entities: {exc}"))
        except KeyError:
            return self._error(self.bad_request("message to edit not found"))
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def _error(self, payload: dict):
        body = {"ok": False, **payload}
        return payload["error_code"], json.dumps(body).encode()

    def _message(self, chat_id: int, message_id: int, params: dict) -> dict:
        text, entities = (
            parse_html(params["text"])
            if params.get("parse_mode") == "HTML"
            else (
                params["text"],
                [],
            )
        )
        msg = {
            "message_id": message_id,
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private"},
            "from": BOT_USER,
            "text": text,
        }
        if entities:
            msg["entities"] = entities
        if params.get("reply_markup"):
            msg["reply_markup"] = params["reply_markup"]
        if params.get("reply_parameters"):
            msg["reply_to"] = params["reply_parameters"]["message_id"]
        return msg

    def _handle(self, name: str, params: dict):
        if name == "getMe":
            return BOT_USER
        if name in ("answerCallbackQuery", "setMyCommands", "deleteWebhook"):
            return True
        if name == "sendMessage":
            chat_id = int(params["chat_id"])
            msg = self._message(chat_id, next(self._ids), params)
            self.messages[(chat_id, msg["message_id"])] = msg
            return msg
        if name == "editMessageText":
            key = (int(params["chat_id"]), int(params["message_id"]))
            self.messages[key]  # noqa: B018 - raises KeyError when the message is gone
            msg = self._message(*key, params)
            self.messages[key] = msg
            return msg
        if name == "editMessageReplyMarkup":
            key = (int(params["chat_id"]), int(params["message_id"]))
            msg = dict(self.messages[key])
            if params.get("reply_markup"):
                msg["reply_markup"] = params["reply_markup"]
            else:
                msg.pop("reply_markup", None)
            self.messages[key] = msg
            return msg
        if name == "getUpdates":
            updates, self.pending_updates = self.pending_updates, []
            return updates
        raise AssertionError(f"unexpected Bot API call {name}")

    # --- helpers for tests ---------------------------------------------------------

    def sent(self, chat_id: int = ALLOWED_USER) -> list[dict]:
        return [m for (c, _), m in sorted(self.messages.items()) if c == chat_id]

    def buttons(self, chat_id: int, message_id: int) -> list[str]:
        markup = self.messages[(chat_id, message_id)].get("reply_markup") or {}
        return [b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row]

    def answers(self) -> list[str | None]:
        return [p.get("text") for n, p in self.calls if n == "answerCallbackQuery"]

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

    def text_update(self, bot, text: str, uid: int = ALLOWED_USER) -> Update:
        return Update.de_json(self.raw_text_update(text, uid), bot)

    def raw_text_update(self, text: str, uid: int = ALLOWED_USER) -> dict:
        message = {
            "message_id": next(self._ids),
            "date": int(time.time()),
            "chat": {"id": uid, "type": "private"},
            "from": user(uid),
            "text": text,
        }
        if text.startswith("/"):
            command = text.split()[0]
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        return {"update_id": next(self._update_ids), "message": message}

    def callback_update(
        self, bot, message_id: int, data: str, uid: int = ALLOWED_USER, with_message: bool = True
    ) -> Update:
        return Update.de_json(self.raw_callback_update(message_id, data, uid, with_message), bot)

    def raw_callback_update(
        self, message_id: int, data: str, uid: int = ALLOWED_USER, with_message: bool = True
    ) -> dict:
        query = {
            "id": f"cq{next(self._update_ids)}",
            "from": user(uid),
            "chat_instance": "ci",
            "data": data,
        }
        if with_message:
            query["message"] = self.messages[(uid if uid == ALLOWED_USER else ALLOWED_USER, message_id)]
        else:
            query["inline_message_id"] = "inline"
        return {"update_id": next(self._update_ids), "callback_query": query}
