"""Central configuration: config.yaml for behaviour, environment variables for secrets."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain import Target

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS = ("pushover", "ntfy", "telegram", "whatsapp", "whatsapp_cloud", "email")
ProviderName = Literal["pushover", "ntfy", "telegram", "whatsapp", "whatsapp_cloud", "email"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MonitoringConfig(Strict):
    enabled: bool = True
    default_interval_seconds: int = Field(60, ge=30, le=3600)
    adaptive_polling: bool = True
    respect_rate_limits: Literal[True] = True
    jitter_ratio: float = Field(0.15, ge=0, le=0.5)
    check_timeout_seconds: int = Field(90, ge=5, le=300)
    max_backoff_seconds: int = Field(900, ge=60, le=3600)
    breaker_threshold: int = Field(5, ge=2, le=50)
    breaker_cooldown_seconds: int = Field(300, ge=30, le=3600)
    stale_after_seconds: int = Field(300, ge=60, le=3600)
    check_now_min_gap_seconds: int = Field(30, ge=30, le=600)


class PlatformConfig(Strict):
    enabled: bool = True
    priority: int = 1
    interval_seconds: int | None = Field(None, ge=30, le=86400)


class PvrConfig(PlatformConfig):
    movie_url: str = "https://www.pvrcinemas.com/moviesessions/avengers--doomsday/35312"
    movie_id: str = "35312"
    region: str = "Delhi-NCR"
    sub_region: str = "All"
    seat_reverify_seconds: int = Field(300, ge=60, le=3600)
    few_seats_threshold: int = Field(20, ge=1, le=200)
    recycle_browser_after_checks: int = Field(100, ge=5, le=2000)


class BmsConfig(PlatformConfig):
    movie_url: str = "https://in.bookmyshow.com/movies/national-capital-region-ncr/avengers-doomsday/ET00439706"
    reminder_times_ist: list[str] = Field(default_factory=list)

    @field_validator("reminder_times_ist")
    @classmethod
    def _times(cls, value: list[str]) -> list[str]:
        for item in value:
            hour, _, minute = item.partition(":")
            if not (len(item) == 5 and hour.isdigit() and minute.isdigit() and int(hour) < 24 and int(minute) < 60):
                raise ValueError(f"reminder time {item!r} must be HH:MM")
        return value


class DistrictConfig(PlatformConfig):
    cinema_url: str = "https://www.district.in/movies/inox-pacific-mall-jasola-new-delhi-in-new-delhi-CD1018963"
    cinema_id: str = "1018963"
    movie_id: str = "176735"
    movie_url: str = "https://www.district.in/movies/avengers-doomsday-movie-tickets-in-new-delhi-MV176735"
    terms_ack: bool = False

    @field_validator("cinema_url", "movie_url")
    @classmethod
    def _robots(cls, value: str) -> str:
        if "?" in value:
            raise ValueError("district.in/robots.txt disallows URLs with a query string")
        return value


class PlatformsConfig(Strict):
    pvr: PvrConfig = Field(default_factory=PvrConfig)
    bookmyshow: BmsConfig = Field(default_factory=BmsConfig)
    district: DistrictConfig = Field(default_factory=DistrictConfig)


class ProviderToggle(Strict):
    enabled: bool = False


class AlarmConfig(Strict):
    """After a ticket alert, repeat it until someone presses "I've seen it" on the dashboard."""

    enabled: bool = True
    every_seconds: int = Field(60, ge=30, le=3600)
    # Push channels by default: a message a minute by email or WhatsApp would get those accounts limited.
    channels: list[ProviderName] = Field(default_factory=lambda: ["ntfy", "pushover", "telegram"])  # type: ignore[arg-type]


class NotificationsConfig(Strict):
    telegram: ProviderToggle = Field(default_factory=ProviderToggle)
    pushover: ProviderToggle = Field(default_factory=ProviderToggle)
    ntfy: ProviderToggle = Field(default_factory=ProviderToggle)
    email: ProviderToggle = Field(default_factory=ProviderToggle)
    whatsapp: ProviderToggle = Field(default_factory=ProviderToggle)
    whatsapp_cloud: ProviderToggle = Field(default_factory=ProviderToggle)
    # Every enabled channel receives every alert; this is only the order they are tried in.
    order: list[ProviderName] = Field(default_factory=lambda: list(PROVIDERS))  # type: ignore[arg-type]
    max_attempts: int = Field(60, ge=3, le=500)
    system_alerts: bool = True
    alarm: AlarmConfig = Field(default_factory=AlarmConfig)


class CheckoutConfig(Strict):
    """Checkout assistance and payment automation do not exist. Turning either on is a startup error."""

    enabled: Literal[False] = False
    payment_automation: Literal[False] = False


class Aliases(Strict):
    """Spellings seen on the platforms and confirmed to be the target. Anything else is not a match."""

    movie: list[str] = Field(default_factory=lambda: ["Avengers: Doomsday"])
    cinema: list[str] = Field(
        default_factory=lambda: [
            "INOX Pacific Mall, Jasola",
            "INOX Pacific Mall, Jasola Delhi",
            "INOX Pacific Mall, Jasola, New Delhi",
        ]
    )
    city: list[str] = Field(default_factory=lambda: ["New Delhi", "Delhi"])


class Secrets(BaseModel):
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    pushover_app_token: str = ""
    pushover_user_key: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: Literal["ssl", "starttls", "none"] = "ssl"
    smtp_username: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: str = ""
    callmebot_phone: str = ""
    callmebot_apikey: str = ""
    wa_cloud_token: str = ""
    wa_cloud_phone_number_id: str = ""
    wa_cloud_to: str = ""
    wa_cloud_template: str = ""
    wa_cloud_language: str = "en"
    wa_cloud_api_version: str = "v23.0"
    dashboard_password: str = ""
    session_secret: str = ""
    heartbeat_url: str = ""

    def __repr__(self) -> str:
        return "Secrets(***)"

    __str__ = __repr__


class AppConfig(Strict):
    target: Target
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    platforms: PlatformsConfig = Field(default_factory=PlatformsConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    checkout_assistance: CheckoutConfig = Field(default_factory=CheckoutConfig)
    aliases: Aliases = Field(default_factory=Aliases)
    host: str = "127.0.0.1"
    port: int = 8080
    cookie_secure: bool = False
    data_dir: Path = ROOT / "data"
    log_dir: Path = ROOT / "logs"
    database_url: str = ""
    dashboard_url: str = ""  # where the dashboard is reached from a phone; put in alarm messages
    runner: str = "main"  # shown in alerts from a second, backup watcher
    secrets: Secrets = Field(default_factory=Secrets, exclude=True, repr=False)

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite+aiosqlite:///{(self.data_dir / 'watcher.db').as_posix()}"

    @property
    def loopback_only(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")

    @model_validator(mode="after")
    def _check(self) -> AppConfig:
        secrets = self.secrets
        if not self.loopback_only and len(secrets.dashboard_password) < 12:
            raise ValueError("DASHBOARD_PASSWORD (12+ characters) is required when HOST is not loopback")
        needs = {
            "telegram": (secrets.telegram_bot_token, secrets.telegram_chat_id),
            "pushover": (secrets.pushover_app_token, secrets.pushover_user_key),
            "ntfy": (secrets.ntfy_topic,),
            "email": (secrets.smtp_host, secrets.email_to),
            "whatsapp": (secrets.callmebot_phone, secrets.callmebot_apikey),
            "whatsapp_cloud": (
                secrets.wa_cloud_token, secrets.wa_cloud_phone_number_id, secrets.wa_cloud_to, secrets.wa_cloud_template,
            ),
        }
        for name, values in needs.items():
            if getattr(self.notifications, name).enabled and not all(values):
                raise ValueError(f"notifications.{name} is enabled but its credentials are missing from the environment")
        if self.notifications.ntfy.enabled and len(secrets.ntfy_topic) < 12:
            raise ValueError("NTFY_TOPIC must be 12+ characters: anyone who guesses a topic name can read it")
        return self


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_config(path: Path | None = None, env: dict[str, str] | None = None) -> AppConfig:
    if env is None:
        load_dotenv(ROOT / ".env")
        env = dict(os.environ)
    path = path or Path(env.get("CONFIG_FILE") or ROOT / "config.yaml")
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    for field in ("host", "port", "database_url", "data_dir", "log_dir", "runner", "dashboard_url"):
        if env.get(field.upper()):
            raw[field] = env[field.upper()]
    if env.get("COOKIE_SECURE"):
        raw["cookie_secure"] = _bool(env["COOKIE_SECURE"])
    if env.get("DISTRICT_TERMS_ACK"):
        raw.setdefault("platforms", {}).setdefault("district", {})["terms_ack"] = _bool(env["DISTRICT_TERMS_ACK"])
    if env.get("NOTIFY_PROVIDERS"):
        chosen = [p.strip().lower() for p in env["NOTIFY_PROVIDERS"].split(",") if p.strip()]
        if unknown := [p for p in chosen if p not in PROVIDERS]:
            raise ValueError(f"NOTIFY_PROVIDERS contains unknown providers: {unknown}")
        section = raw.setdefault("notifications", {})
        for provider in PROVIDERS:
            section[provider] = {"enabled": provider in chosen}
        section["order"] = chosen
    raw["secrets"] = {name: env[name.upper()] for name in Secrets.model_fields if env.get(name.upper())}
    return AppConfig.model_validate(raw)
