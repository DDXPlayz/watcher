"""Pushover: https://pushover.net/api. Ticket alerts use emergency priority, which repeats until acknowledged."""

from __future__ import annotations

from typing import Any

import httpx

from app.notifications.base import DeliveryError, Message, Provider


class PushoverProvider(Provider):
    name = "pushover"

    def __init__(
        self, client: httpx.AsyncClient, app_token: str, user_key: str, api: str = "https://api.pushover.net/1/messages.json"
    ) -> None:
        super().__init__(client)
        self._api, self._token, self._user = api, app_token, user_key

    async def send(self, message: Message) -> None:
        data: dict[str, Any] = {
            "token": self._token,
            "user": self._user,
            "title": message.title[:250],
            "message": message.body[:1024],
            "priority": 2 if message.urgent else 0,
        }
        if message.urgent:
            data |= {"retry": 30, "expire": 1800, "sound": "siren"}
        if message.url:
            data |= {"url": message.url[:512], "url_title": message.link_label}
        response = await self._post(self._api, data=data)
        if response.json().get("status") != 1:
            raise DeliveryError("pushover rejected the message")
