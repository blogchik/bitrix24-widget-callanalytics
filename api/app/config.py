"""Process configuration.

WHY: §10 fixes the environment contract, and §4.1/§6 make several of these values
security boundaries rather than tunables (the OAuth host allowlist, the token
encryption key ring, the REST-log retention floor). A deployment that gets one of
them wrong must fail at import time with a loud error instead of starting and
silently weakening a guarantee, so every rule below is a hard validator - never a
warning, never a silent fallback.
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# §4.1: the only OAuth hosts a portal-supplied SERVER_ENDPOINT may resolve to.
_DEFAULT_OAUTH_HOSTS: frozenset[str] = frozenset({"oauth.bitrix.info", "oauth.bitrix24.tech"})

# §6 / decision 20: the moderation trail must survive a weekend plus a support round trip.
_MIN_REST_LOG_RETENTION_DAYS = 3

# AES-256 => exactly 32 bytes; the envelope stores the key id in ONE byte (§3, decision 19).
_TOKEN_KEY_BYTES = 32
_MIN_KEY_ID = 1
_MAX_KEY_ID = 255

# HS256 (security/session_token.py) is only as strong as its secret.
_MIN_SESSION_SECRET_CHARS = 32

_LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})

_LOG_LEVELS: frozenset[str] = frozenset(
    {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
)


class ConfigError(ValueError):
    """A configuration value that is structurally unusable.

    WHY: pydantic wraps this into a ValidationError naming the offending field, which
    is exactly what an operator needs to see in the logs of a container that refused
    to start.
    """


class Settings(BaseSettings):
    """Environment-only settings; each field name is the lowercase of its env var (§10).

    NOTE: `NoDecode` (pydantic-settings >= 2.7) is required on the two fields whose env
    format is our own (comma list, key ring). Without it the env source would try to
    JSON-decode the raw string before any validator of ours could run.
    """

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # ---- Bitrix24 application credentials (no default exists that is safe) ----
    b24_client_id: str = Field(min_length=1)
    b24_client_secret: str = Field(min_length=1)

    # ---- OAuth trust root (§4.1) ----
    oauth_host_allowlist: Annotated[frozenset[str], NoDecode] = _DEFAULT_OAUTH_HOSTS
    oauth_exchange_limit: int = Field(default=5, ge=1)

    # ---- Public origin: handler URLs, placement HANDLERs, CSP (§4.3, §4.10) ----
    app_base_url: str

    # ---- Database ----
    database_url: str = Field(min_length=1)             # runtime role ca_app
    database_url_migrations: str = Field(min_length=1)  # ca_owner, Alembic only

    # ---- Secrets at rest and in transit (decision 19, §4.6) ----
    token_enc_keys: Annotated[dict[int, bytes], NoDecode]
    token_enc_active_key_id: int = Field(default=1, ge=_MIN_KEY_ID, le=_MAX_KEY_ID)
    session_secret: str = Field(min_length=_MIN_SESSION_SECRET_CHARS)

    # ---- Sync cadence and budgets (§5) ----
    sync_interval_sec: int = Field(default=300, ge=10)
    rescan_interval_sec: int = Field(default=3600, ge=60)
    rescan_window_hours: int = Field(default=72, ge=1)
    employee_ttl_hours: int = Field(default=4, ge=1)
    global_portal_concurrency: int = Field(default=4, ge=1)
    backfill_batches_per_visit: int = Field(default=20, ge=1)
    operating_soft_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    operating_limit_floor: int = Field(default=300, ge=1)
    sync_rate_per_sec: float = Field(default=2.0, gt=0.0)

    # ---- REST log (§6) ----
    rest_log_retention_days: int = Field(default=7)
    rest_log_body_limit: int = Field(default=262_144, ge=1024)

    # ---- Feature budgets ----
    crm_activity_cap: int = Field(default=250, ge=1)
    token_reseed_after_days: int = Field(default=120, ge=1)
    uninstall_grace_days: int = Field(default=30, ge=1)
    max_period_days: int = Field(default=366, ge=1)

    # ---- Modes ----
    recording_mode: Literal["off", "proxy"] = "off"
    # §5.9 keeps the Celery swap open; an unknown backend name is a startup error,
    # not a runtime surprise inside jobs/__init__.py.
    job_backend: Literal["apscheduler", "celery"] = "apscheduler"
    scheduler_inline: bool = False
    log_level: str = "INFO"

    # ------------------------------------------------------------------ validators

    @field_validator("oauth_host_allowlist", mode="before")
    @classmethod
    def _parse_oauth_hosts(cls, value: Any) -> Any:
        """§4.1: a comma-separated list of bare hosts, lowercased, never empty."""
        if isinstance(value, str):
            hosts = {part.strip().lower() for part in value.split(",")}
        elif isinstance(value, (list, tuple, set, frozenset)):
            hosts = {str(part).strip().lower() for part in value}
        else:
            raise ConfigError("OAUTH_HOST_ALLOWLIST must be a comma-separated host list")
        hosts.discard("")
        if not hosts:
            raise ConfigError("OAUTH_HOST_ALLOWLIST must not be empty")
        for host in hosts:
            # A scheme, port or path here would make the allowlist comparison in
            # bitrix/oauth.py (which compares a parsed hostname) silently never match.
            if "/" in host or ":" in host or " " in host:
                raise ConfigError(f"OAUTH_HOST_ALLOWLIST entry is not a bare host: {host!r}")
        return frozenset(hosts)

    @field_validator("token_enc_keys", mode="before")
    @classmethod
    def _parse_token_enc_keys(cls, value: Any) -> Any:
        """Parse `<id>:<base64 32 bytes>[,<id>:<b64>]` into the key ring (decision 19)."""
        raw_items: list[tuple[Any, Any]]
        if isinstance(value, dict):
            raw_items = list(value.items())
        elif isinstance(value, str):
            raw_items = []
            for entry in value.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                raw_id, sep, material = entry.partition(":")
                if not sep:
                    raise ConfigError("TOKEN_ENC_KEYS entries must be '<id>:<base64 key>'")
                raw_items.append((raw_id.strip(), material.strip()))
        else:
            raise ConfigError("TOKEN_ENC_KEYS must be a '<id>:<base64 key>' comma list")

        ring: dict[int, bytes] = {}
        for raw_id, raw_material in raw_items:
            try:
                key_id = int(raw_id)
            except (TypeError, ValueError):
                raise ConfigError(f"TOKEN_ENC_KEYS key id is not an integer: {raw_id!r}") from None
            if not _MIN_KEY_ID <= key_id <= _MAX_KEY_ID:
                # The envelope carries the id in a single byte; 0 stays reserved as "unset".
                raise ConfigError(
                    f"TOKEN_ENC_KEYS key id {key_id} outside {_MIN_KEY_ID}..{_MAX_KEY_ID}"
                )
            if key_id in ring:
                raise ConfigError(f"TOKEN_ENC_KEYS contains duplicate key id {key_id}")
            if isinstance(raw_material, (bytes, bytearray)):
                material = bytes(raw_material)
            else:
                try:
                    material = base64.b64decode(str(raw_material), validate=True)
                except (binascii.Error, ValueError):
                    raise ConfigError(f"TOKEN_ENC_KEYS key {key_id} is not valid base64") from None
            if len(material) != _TOKEN_KEY_BYTES:
                raise ConfigError(
                    f"TOKEN_ENC_KEYS key {key_id} must decode to {_TOKEN_KEY_BYTES} bytes, "
                    f"got {len(material)}"
                )
            ring[key_id] = material

        if not ring:
            raise ConfigError("TOKEN_ENC_KEYS must contain at least one key")
        return ring

    @field_validator("rest_log_retention_days")
    @classmethod
    def _check_retention_floor(cls, value: int) -> int:
        """§6 / decision 20: configuration REFUSES a retention below the moderation floor."""
        if value < _MIN_REST_LOG_RETENTION_DAYS:
            raise ConfigError(
                f"REST_LOG_RETENTION_DAYS must be >= {_MIN_REST_LOG_RETENTION_DAYS} "
                "(moderation trail floor)"
            )
        return value

    @field_validator("app_base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        """§4.10: the app is only ever framed over TLS; localhost is the dev exception."""
        # Stored without a trailing slash so callers can concatenate "/app/" safely.
        url = value.strip().rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ConfigError("APP_BASE_URL must be an absolute http(s) URL")
        if parts.path or parts.query or parts.fragment:
            raise ConfigError("APP_BASE_URL must be an origin without path, query or fragment")
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" and host not in _LOCAL_HOSTS:
            raise ConfigError("APP_BASE_URL must be https except for localhost")
        return url

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in _LOG_LEVELS:
            raise ConfigError(f"LOG_LEVEL is not a logging level: {value!r}")
        return level

    @model_validator(mode="after")
    def _check_active_key_present(self) -> Settings:
        """Encryption must be provably possible at startup, not on the first token write."""
        if self.token_enc_active_key_id not in self.token_enc_keys:
            raise ConfigError(
                f"TOKEN_ENC_ACTIVE_KEY_ID {self.token_enc_active_key_id} "
                "is not present in TOKEN_ENC_KEYS"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """One parsed instance per process; cached so validation runs exactly once."""
    return Settings()  # type: ignore[call-arg]  # every field is supplied by the environment


# Import-time construction is deliberate (§10): a misconfigured container must die on
# start, not on the first request that happens to touch the bad value.
settings: Settings = get_settings()
