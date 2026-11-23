"""Request bodies."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(Body):
    password: str = Field(max_length=256)


class TargetBody(Body):
    """`confirm` must be typed out, so a stray click can never change what is being watched."""

    confirm: Literal["CHANGE TARGET"]
    date: dt.date
    language: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z ]+$")
    earliest_time: dt.time
    latest_time: dt.time
