"""Stable keys for screenings and notifications."""

from __future__ import annotations

from app.domain import Observation
from app.monitor.matcher import classify_screenx, normalize


def screening_key(obs: Observation) -> str:
    """Platform-specific identity.

    District publishes a session id. PVR's seat-link token is not documented as stable, so PVR screenings
    are keyed on verified attributes; the venue has one ScreenX auditorium, so that is unambiguous.
    """
    if obs.platform == "district" and obs.session_id:
        return f"district|sid|{obs.cinema_id}|{obs.session_id}"
    return f"{obs.platform}|attr|{entity_key(obs)}"


def entity_key(obs: Observation) -> str:
    """Cross-platform identity. Built only from fields the matcher has already verified."""
    time_text = obs.showtime.strftime("%H:%M") if obs.showtime else "?"
    _, dimension = classify_screenx(obs.format)
    return "|".join(
        [
            normalize(obs.movie),
            # All confirmed spellings of the one target venue collapse to the same token.
            "inox pacific mall jasola" if "jasola" in normalize(obs.cinema) else normalize(obs.cinema),
            obs.date.isoformat() if obs.date else "?",
            time_text,
            normalize(obs.language),
            f"screenx{dimension.lower()}",
        ]
    )


def notification_key(screening: str, transition: str, generation: int) -> str:
    """One notification per screening, per kind of change, per period of availability."""
    return f"{screening}#{transition}#{generation}"
