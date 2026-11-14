"""Telegram Bot API channel: send one message to one chat.

Setup: create a bot with @BotFather (gives TELEGRAM_BOT_TOKEN), send the bot any message, then read your
chat id from https://api.telegram.org/bot<token>/getUpdates (TELEGRAM_CHAT_ID). Both live in .env only.
"""

from __future__ import annotations

import time

import httpx

API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None, timeout: float = 10.0):
        self.token, self.chat_id = token, chat_id
        self.client = client or httpx.Client(timeout=timeout)

    def send(self, text: str, max_wait: float = 10.0) -> None:
        """Send an HTML-formatted message; one retry after a rate limit (as long as Telegram asks, capped)."""
        for attempt in (1, 2):
            r = self.client.post(f"{API}/bot{self.token}/sendMessage", json={
                "chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
            if r.status_code == 200 and r.json().get("ok"):
                return
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            wait = (body.get("parameters") or {}).get("retry_after")
            if attempt == 1 and (r.status_code == 429 or r.status_code >= 500):
                time.sleep(min(float(wait or 1), max_wait))
                continue
            # the description never contains the token; the URL (which does) is not included
            raise TelegramError(f"HTTP {r.status_code}: {body.get('description') or r.text[:200]}")
