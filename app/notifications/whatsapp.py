"""WhatsApp, two ways.

CallMeBot: a free third-party bot for personal notifications. Quick to set up, no business account.
WhatsApp Cloud API: Meta's official API. Messages you did not reply to in the last 24 hours must use a
template that Meta has approved, so the alert is sent as template parameters.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.notifications.base import DeliveryError, Message, Provider


def one_line(text: str, limit: int) -> str:
    """WhatsApp template parameters may not contain new lines, tabs or runs of spaces."""
    return re.sub(r"\s+", " ", text).strip()[:limit]


class CallMeBotProvider(Provider):
    name = "whatsapp"
    api = "https://api.callmebot.com/whatsapp.php"

    def __init__(self, client: httpx.AsyncClient, phone: str, apikey: str) -> None:
        super().__init__(client)
        self._phone, self._apikey = phone, apikey

    async def send(self, message: Message) -> None:
        text = f"*{message.title}*\n\n{message.body}"
        if message.url:
            text += f"\n\n{message.link_label}: {message.url}"
        params = {"phone": self._phone, "text": text[:1500], "apikey": self._apikey}
        try:
            response = await self.client.get(self.api, params=params)
        except httpx.HTTPError as exc:
            raise DeliveryError(type(exc).__name__) from None  # never the URL: it contains the key
        if response.status_code >= 400:
            raise DeliveryError(f"whatsapp answered HTTP {response.status_code}", permanent=response.status_code in (401, 403))
        # CallMeBot answers 200 with an HTML page even for some failures.
        lowered = response.text.casefold()
        if "apikey is invalid" in lowered or "invalid apikey" in lowered:
            raise DeliveryError("whatsapp rejected the API key", permanent=True)
        if "error" in lowered and "queued" not in lowered and "sent" not in lowered:
            raise DeliveryError("whatsapp reported an error")


class WhatsAppCloudProvider(Provider):
    name = "whatsapp_cloud"

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        phone_number_id: str,
        to: str,
        template: str,
        language: str,
        api_version: str,
    ) -> None:
        super().__init__(client)
        self._url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
        self._token, self._to, self._template, self._language = token, to.lstrip("+"), template, language

    def payload(self, message: Message) -> dict[str, Any]:
        """Template with two body variables: {{1}} the alert, {{2}} the link."""
        summary = one_line(f"{message.title}. {message.body}", 900)
        return {
            "messaging_product": "whatsapp",
            "to": self._to,
            "type": "template",
            "template": {
                "name": self._template,
                "language": {"code": self._language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": summary},
                            {"type": "text", "text": message.url or "-"},
                        ],
                    }
                ],
            },
        }

    async def send(self, message: Message) -> None:
        response = await self._post(self._url, json=self.payload(message), headers={"Authorization": f"Bearer {self._token}"})
        if not response.json().get("messages"):
            raise DeliveryError("whatsapp cloud api did not accept the message")
