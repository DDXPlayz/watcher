"""Deterministic matching. A screening qualifies only when every mandatory field is verified.

Each field gets PASS, FAIL or UNKNOWN. One FAIL makes the screening irrelevant. One UNKNOWN makes it
UNVERIFIED, which never alerts. There is no scoring and no fuzzy matching.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

from pydantic import BaseModel

from app.config import Aliases
from app.domain import Observation, State, Target


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


IDENTITY_FIELDS = ("movie", "cinema", "city", "date", "language", "format", "showtime")


class Decision(BaseModel):
    fields: dict[str, Verdict]
    state: State | None  # None: not our screening
    qualified: bool
    reasons: list[str]

    @property
    def identity_match(self) -> bool:
        return all(self.fields[name] is Verdict.PASS for name in IDENTITY_FIELDS)


def normalize(text: str | None) -> str:
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(re.sub(r"[^0-9a-z]+", " ", folded).split())


# Tokens that may sit next to ScreenX without naming another auditorium format.
_SCREENX_COMPANIONS = {"2D", "3D", "MS", "INFINITY", "VISION", "ATMOS", "LASER", "RECLINER", "RECLINERS"}


def classify_screenx(raw: str | None) -> tuple[Verdict, str]:
    """Return (verdict, dimension). 'SCREEN X', 'ScreenX' and '3D SCREEN X' pass.

    ScreenX combined with another premium format ('4DX SCREEN X') is a different product: UNKNOWN.
    """
    if not raw or not raw.strip():
        return Verdict.UNKNOWN, ""
    text = re.sub(r"[^0-9A-Z]+", " ", unicodedata.normalize("NFKC", raw).upper())
    text = re.sub(r"\bSCREEN X\b", "SCREENX", text)
    tokens = set(text.split())
    dimension = "3D" if "3D" in tokens else "2D" if "2D" in tokens else ""
    if "SCREENX" not in tokens:
        return Verdict.FAIL, dimension
    if tokens - {"SCREENX"} - _SCREENX_COMPANIONS or {"2D", "3D"} <= tokens:
        return Verdict.UNKNOWN, dimension
    return Verdict.PASS, dimension


class Matcher:
    def __init__(self, target: Target, aliases: Aliases) -> None:
        self.target = target
        self._movies = {normalize(a) for a in aliases.movie} | {normalize(target.movie)}
        self._cinemas = {normalize(a) for a in aliases.cinema} | {normalize(target.cinema)}
        self._cities = {normalize(a) for a in aliases.city} | {normalize(target.city)}

    def is_target_cinema(self, name: str | None) -> bool:
        return self._cinema(name) is Verdict.PASS

    def _movie(self, value: str | None) -> Verdict:
        name = normalize(value)
        if not name:
            return Verdict.UNKNOWN
        if name in self._movies:
            return Verdict.PASS
        # "Avengers: Doomsday (something)" may be a variant or an event; do not guess which.
        return Verdict.UNKNOWN if any(name.startswith(m + " ") for m in self._movies) else Verdict.FAIL

    def _cinema(self, value: str | None) -> Verdict:
        name = normalize(value)
        if not name:
            return Verdict.UNKNOWN
        if name in self._cinemas:
            return Verdict.PASS
        # Jasola by name but not a confirmed spelling, or the mall without its locality: ambiguous.
        ambiguous = "jasola" in name or name in ("inox pacific mall", "pacific mall")
        return Verdict.UNKNOWN if ambiguous else Verdict.FAIL

    def _city(self, value: str | None) -> Verdict:
        name = normalize(value)
        if not name:
            return Verdict.UNKNOWN
        return Verdict.PASS if name in self._cities else Verdict.FAIL

    def _language(self, value: str | None) -> Verdict:
        name = normalize(value)
        if not name:
            return Verdict.UNKNOWN
        wanted = normalize(self.target.language)
        if name == wanted:
            return Verdict.PASS
        return Verdict.UNKNOWN if wanted in name.split() else Verdict.FAIL

    def _format(self, value: str | None) -> Verdict:
        if normalize(self.target.format).replace(" ", "") != "screenx":
            raise ValueError("only the ScreenX target format is implemented")
        return classify_screenx(value)[0]

    def _showtime(self, obs: Observation) -> Verdict:
        if obs.showtime is None or obs.timezone != self.target.timezone:
            return Verdict.UNKNOWN
        local = obs.showtime.replace(tzinfo=None)
        return Verdict.PASS if self.target.earliest_time <= local <= self.target.latest_time else Verdict.FAIL

    @staticmethod
    def _bookability(obs: Observation) -> Verdict:
        if obs.sold_out or obs.lapsed or obs.bookable is False:
            return Verdict.FAIL
        if obs.bookable is True and obs.booking_url:
            return Verdict.PASS
        return Verdict.UNKNOWN

    def evaluate(self, obs: Observation) -> Decision:
        fields = {
            "movie": self._movie(obs.movie),
            "cinema": self._cinema(obs.cinema),
            "city": self._city(obs.city),
            "date": Verdict.UNKNOWN
            if obs.date is None
            else (Verdict.PASS if obs.date == self.target.date else Verdict.FAIL),
            "language": self._language(obs.language),
            "format": self._format(obs.format),
            "showtime": self._showtime(obs),
            "bookability": self._bookability(obs),
        }
        reasons = [f"{name}={verdict.value}" for name, verdict in fields.items() if verdict is not Verdict.PASS]
        identity = [fields[name] for name in IDENTITY_FIELDS]
        state: State | None
        if Verdict.FAIL in identity:
            state = None
        elif Verdict.UNKNOWN in identity:
            state = State.UNVERIFIED
        elif fields["bookability"] is Verdict.PASS:
            state = State.AVAILABLE
        elif obs.sold_out:
            state = State.SOLD_OUT
        elif fields["bookability"] is Verdict.FAIL:
            state = State.UNAVAILABLE
        else:
            state = State.UNVERIFIED
        return Decision(fields=fields, state=state, qualified=state is State.AVAILABLE, reasons=reasons)
