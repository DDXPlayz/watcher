"""Telegram Bot API: https://core.telegram.org/bots/api#sendmessage"""

from __future__ import annotations

from html import escape
from typing import Any

import httpx

from app.notifications.base import DeliveryError, Message, Provider


class TelegramProvider(Provider):
    name = "telegram"

    def __init__(self, client: httpx.AsyncClient, token: str, chat_id: str, api: str = "https://api.telegram.org") -> None:
        super().__init__(client)
        self._url = f"{api}/bot{token}/sendMessage"
        self._chat_id = chat_id

    async def send(self, message: Message) -> None:
        text = f"<b>{escape(message.title)}</b>\n\n{escape(message.body)}"
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "parse_mode": "HTML",
            "disable_notification": message.kind == "reminder",
            "link_preview_options": {"is_disabled": True},
        }
        if message.url:
            text += f'\n\n<b>{message.link_label.upper()}:</b> <a href="{escape(message.url, quote=True)}">{escape(message.url)}</a>'
            buttons = [{"text": message.link_label, "url": message.url}]
            if message.ack_url:
                buttons.append({"text": "Stop alerts", "url": message.ack_url})
            payload["reply_markup"] = {"inline_keyboard": [buttons]}
        payload["text"] = text
        response = await self._post(self._url, json=payload)
        if not response.json().get("ok"):
            raise DeliveryError("telegram rejected the message")
