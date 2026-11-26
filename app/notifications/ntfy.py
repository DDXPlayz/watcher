"""ntfy JSON publishing: https://docs.ntfy.sh/publish/#publish-as-json"""

from __future__ import annotations

from typing import Any

import httpx

from app.notifications.base import Message, Provider


class NtfyProvider(Provider):
    name = "ntfy"

    def __init__(self, client: httpx.AsyncClient, server: str, topic: str, token: str = "") -> None:
        super().__init__(client)
        self._server, self._topic, self._token = server.rstrip("/"), topic, token

    async def send(self, message: Message) -> None:
        payload: dict[str, Any] = {
            "topic": self._topic,
            "title": message.title,
            "message": message.body,
            "priority": 5 if message.urgent else 3,
            "tags": ["rotating_light"] if message.kind == "alarm" else ["tickets"] if message.kind in ("ticket", "update") else ["gear"],
        }
        actions: list[dict[str, str]] = []
        if message.url:
            payload["click"] = message.url
            actions.append({"action": "view", "label": message.link_label, "url": message.url})
        if message.ack_url:
            actions.append({"action": "view", "label": "Stop alerts", "url": message.ack_url})
        if actions:
            payload["actions"] = actions
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        await self._post(self._server, json=payload, headers=headers)
