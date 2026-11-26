"""Contract for notification providers."""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx
from pydantic import BaseModel


class Message(BaseModel):
    title: str
    body: str
    url: str | None = None
    kind: str = "ticket"  # ticket | alarm | update | system | reminder | test
    ack_url: str | None = None  # the dashboard, where a repeating alarm is stopped

    @property
    def urgent(self) -> bool:
        return self.kind in ("ticket", "alarm")

    @property
    def link_label(self) -> str:
        return "Book now" if self.kind in ("ticket", "alarm", "update") else "Open page"


class DeliveryError(Exception):
    """Delivery failed. `permanent` means retrying the same request cannot succeed (e.g. a bad token)."""

    def __init__(self, detail: str, *, permanent: bool = False) -> None:
        super().__init__(detail)
        self.permanent = permanent


class Provider(ABC):
    name: str

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    @abstractmethod
    async def send(self, message: Message) -> None:
        """Deliver or raise DeliveryError."""

    async def _post(self, url: str, **kwargs: object) -> httpx.Response:
        try:
            response = await self.client.post(url, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            # str(exc) can contain the URL, and Telegram's URL contains the token.
            raise DeliveryError(type(exc).__name__) from None
        if response.status_code >= 400:
            permanent = response.status_code in (400, 401, 403, 404)
            raise DeliveryError(f"{self.name} answered HTTP {response.status_code}", permanent=permanent)
        return response
