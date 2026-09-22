"""Configuration management for Capital.com MCP Server."""

import logging
import time
from enum import Enum
from pathlib import Path

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError, ErrorCode


class CapEnv(str, Enum):
    """Capital.com environment."""

    DEMO = "demo"
    LIVE = "live"


# The MCPB manifest renders Environment as a boolean toggle (on = Live), and Claude
# Desktop passes it through as the literal string "true"/"false". Only those two
# spellings are aliased — nothing else produces them.
_ENV_TOGGLE_ALIASES = {"true": CapEnv.LIVE.value, "false": CapEnv.DEMO.value}


# Get project root directory (parent of capital_mcp package)
_PROJECT_ROOT = Path(__file__).parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


# ============================================================
# Credential validation
# ============================================================

# Env var name for each required credential, keyed by Config field name.
CREDENTIAL_FIELDS: dict[str, str] = {
    "cap_api_key": "CAP_API_KEY",
    "cap_identifier": "CAP_IDENTIFIER",
    "cap_api_password": "CAP_API_PASSWORD",
}

CREDENTIALS_HELP = (
    "Generate a Demo API key in the Capital.com web app under "
    "Settings > API integrations > Generate new key, note the custom password you set "
    "there, and put both values plus your login email in .env (or in the env block of "
    "your MCP client config)."
)

# Every placeholder this repo ships, listed per source file. The set is closed on
# purpose: heuristics such as "starts with your_" also reject real credentials, e.g.
# the address your-name@gmail.com.
_SHIPPED_PLACEHOLDERS = frozenset(
    {
        # .env.example
        "enter_generated_api_key",
        "enter_your_email",
        "enter_your_api_password",
        # install.sh, install.ps1, README.md, USAGE.md
        "your_api_key_here",
        "your_email@example.com",
        "your_custom_password",
        # README.md
        "your_generated_api_key_here",
        "your_custom_api_password",
        # IMPLEMENTATION_SUMMARY.md
        "your_key",
        "your_email",
        "your_password",
    }
)

# A value wrapped entirely in template brackets, e.g. INSTALL.md's {api_key} or a
# hand-written <api-key>. No real credential takes this shape.
_TEMPLATE_WRAPPERS = (("<", ">"), ("{", "}"), ("[", "]"))

# RFC 2606 reserved domains — never a real Capital.com login.
_RESERVED_EMAIL_DOMAINS = frozenset({"example.com", "example.net", "example.org", "example.edu"})


def is_placeholder_credential(value: str) -> bool:
    """Check whether a credential is a shipped example value or an unfilled template."""
    candidate = value.strip().lower()
    if not candidate:
        return False
    if candidate in _SHIPPED_PLACEHOLDERS:
        return True
    return any(
        candidate.startswith(opening) and candidate.endswith(closing)
        for opening, closing in _TEMPLATE_WRAPPERS
    )


def _has_reserved_email_domain(identifier: str) -> bool:
    """Check whether an identifier points at an RFC 2606 example domain."""
    return identifier.rpartition("@")[2].lower() in _RESERVED_EMAIL_DOMAINS


class Config(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,  # Allow CAP_API_KEY to match cap_api_key
        extra="ignore",
    )

    # ============================================================
    # REQUIRED: API Credentials
    # ============================================================
    cap_env: CapEnv = Field(default=CapEnv.DEMO, description="Environment: demo or live")
    cap_api_key: str = Field(..., description="API Key from Capital.com")
    cap_identifier: str = Field(..., description="Login email")
    cap_api_password: str = Field(..., description="API Key custom password")

    # ============================================================
    # SAFETY: Trading Controls
    # ============================================================
    cap_allow_trading: bool = Field(default=True, description="Allow trading operations")
    cap_allowed_epics: str = Field(
        default="ALL", description="Comma-separated allowlist of EPICs, or ALL for every instrument"
    )
    cap_max_position_size: float = Field(default=1.0, gt=0, description="Max position size")
    cap_max_working_order_size: float = Field(
        default=1.0, gt=0, description="Max working order size"
    )
    cap_max_open_positions: int = Field(
        default=3, ge=0, description="Max open positions at any time"
    )
    cap_max_orders_per_day: int = Field(default=20, ge=0, description="Max orders per day")
    cap_require_explicit_confirm: bool = Field(
        default=True, description="Require confirm=true for trade operations"
    )
    cap_dry_run: bool = Field(default=False, description="Dry-run mode: refuse all executions")

    # ============================================================
    # OPTIONAL: Account & Session
    # ============================================================
    cap_default_account_id: str | None = Field(
        default=None, description="Default account ID after login"
    )
    cap_http_timeout_s: float = Field(default=15.0, gt=0, description="HTTP timeout in seconds")
    cap_log_level: str = Field(default="INFO", description="Log level")
    cap_ws_enabled: bool = Field(default=False, description="Enable WebSocket streaming")

    # Internal defaults (not configurable via env)
    cap_preview_cache_ttl_s: int = Field(default=120, description="Preview cache TTL (seconds)")
    cap_ping_interval_s: int = Field(default=480, description="Session ping interval (8 minutes)")

    @field_validator("cap_api_key", "cap_identifier")
    @classmethod
    def strip_credential(cls, v: str) -> str:
        """Trim copy/paste whitespace. The API password is kept byte-exact."""
        return v.strip()

    @field_validator("cap_env", mode="before")
    @classmethod
    def coerce_env_toggle(cls, v: object) -> object:
        """Accept the boolean toggle the MCPB manifest renders for Environment.

        Claude Desktop interpolates a boolean `user_config` value into the env var as
        the literal string "true"/"false", so CAP_ENV arrives that way from the bundle;
        the Docker, Cursor and .env channels still pass "demo"/"live". Unknown values
        fall through to the enum, which reports "Input should be 'demo' or 'live'".
        """
        if not isinstance(v, str):
            return v
        candidate = v.strip().lower()
        return _ENV_TOGGLE_ALIASES.get(candidate, candidate)

    @field_validator("cap_log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper_v = v.upper()
        if upper_v not in valid_levels:
            raise ValueError(f"Invalid log level: {v}. Must be one of {valid_levels}")
        return upper_v

    @model_validator(mode="after")
    def validate_credentials(self) -> "Config":
        """Reject empty or placeholder credentials so no API call is ever made with them.

        Problem descriptions stay value-free: they end up in server logs and in MCP
        client error output. The offending variable names travel in the error context,
        because a model validator reports no field location of its own.
        """
        problems: list[str] = []
        env_vars: list[str] = []
        for field_name, env_var in CREDENTIAL_FIELDS.items():
            value: str = getattr(self, field_name)
            if not value.strip():
                problems.append(f"{env_var} is empty")
            elif is_placeholder_credential(value):
                problems.append(f"{env_var} still contains a placeholder value")
            elif field_name == "cap_identifier" and _has_reserved_email_domain(value):
                problems.append(f"{env_var} uses a reserved example domain")
            else:
                continue
            env_vars.append(env_var)

        if problems:
            raise PydanticCustomError(
                "capital_credentials",
                "{summary}",
                {"summary": f"{'; '.join(problems)}. {CREDENTIALS_HELP}", "env_vars": env_vars},
            )
        return self

    @model_validator(mode="after")
    def validate_trading_config(self) -> "Config":
        """Validate trading configuration consistency."""
        if self.cap_allow_trading and not self.cap_allowed_epics.strip():
            raise ValueError(
                "CAP_ALLOW_TRADING is true but CAP_ALLOWED_EPICS is empty. "
                "You must specify allowed EPICs for trading (or use 'ALL' for unrestricted)."
            )
        return self

    @property
    def base_url(self) -> str:
        """Get base URL based on environment."""
        if self.cap_env == CapEnv.DEMO:
            return "https://demo-api-capital.backend-capital.com"
        return "https://api-capital.backend-capital.com"

    @property
    def api_base_url(self) -> str:
        """Get full API base URL."""
        return f"{self.base_url}/api/v1"

    @property
    def ws_url(self) -> str:
        """Get WebSocket URL."""
        return "wss://api-streaming-capital.backend-capital.com/connect"

    @property
    def allowed_epics_list(self) -> list[str]:
        """Get allowed EPICs as a list."""
        if not self.cap_allowed_epics.strip():
            return []
        return [epic.strip() for epic in self.cap_allowed_epics.split(",") if epic.strip()]

    @property
    def allowlist_is_wildcard(self) -> bool:
        """Whether the allowlist grants every instrument.

        'ALL' counts in any position and any casing. Enforcement and the mode reported
        by cap://status, cap://risk-policy and cap://allowed-epics all read this, so a
        list can never be enforced one way and reported the other.
        """
        return any(epic.upper() == "ALL" for epic in self.allowed_epics_list)

    @property
    def wildcard_shadowed_epics(self) -> list[str]:
        """Specific epics listed alongside ALL, which the wildcard makes redundant.

        A stray ALL widens the allowlist to the whole market, so the entries it
        overrode are surfaced on startup and in cap://allowed-epics rather than
        being silently dropped.
        """
        if not self.allowlist_is_wildcard:
            return []
        return [epic for epic in self.allowed_epics_list if epic.upper() != "ALL"]

    def is_epic_allowed(self, epic: str) -> bool:
        """Check if an epic is in the allowlist."""
        if not self.cap_allow_trading:
            return False
        allowed = self.allowed_epics_list
        if not allowed:
            return False
        if self.allowlist_is_wildcard:
            return True
        return epic.upper() in [e.upper() for e in allowed]

    def setup_logging(self) -> None:
        """Configure logging based on settings."""
        logging.basicConfig(
            level=getattr(logging, self.cap_log_level),
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        # Use UTC timestamps to match MCP transport logs
        logging.Formatter.converter = time.gmtime

        # Set httpx logging to WARNING to reduce noise
        logging.getLogger("httpx").setLevel(logging.WARNING)


# Global config instance
_config: Config | None = None

_VALUE_ERROR_PREFIX = "Value error, "


def _config_error_from_validation(exc: ValidationError) -> ConfigError:
    """Build a redacted ConfigError from a pydantic ValidationError.

    pydantic embeds the rejected input in its own error text, which for credentials
    would mean secrets in logs, so only field locations and validator messages are used.
    """
    missing_vars: list[str] = []
    messages: list[str] = []
    env_vars: list[str] = []

    for error in exc.errors():
        field_name = str(error["loc"][0]) if error["loc"] else ""
        env_var = CREDENTIAL_FIELDS.get(field_name, field_name.upper())
        if env_var:
            env_vars.append(env_var)
        # Model validators report no field location, so they name the variables in ctx.
        env_vars.extend(str(name) for name in (error.get("ctx") or {}).get("env_vars", ()))
        if error["type"] == "missing":
            missing_vars.append(env_var)
            continue
        message = str(error["msg"])
        if message.startswith(_VALUE_ERROR_PREFIX):
            message = message[len(_VALUE_ERROR_PREFIX) :]
        messages.append(f"{env_var}: {message}" if env_var else message)

    if missing_vars:
        messages.insert(
            0,
            f"Missing required environment variable(s): {', '.join(sorted(missing_vars))}. "
            f"{CREDENTIALS_HELP}",
        )

    return ConfigError(
        "; ".join(messages),
        details={"env_vars": sorted(set(env_vars))},
        code=ErrorCode.CONFIG_MISSING if missing_vars else ErrorCode.CONFIG_INVALID,
    )


def get_config() -> Config:
    """Get or create the global config instance.

    Raises:
        ConfigError: credentials are missing, empty or still placeholders. Raised before
            any Capital.com API call is attempted.
    """
    global _config
    if _config is None:
        try:
            config = Config()  # type: ignore
        except ValidationError as exc:
            # `from None`: the pydantic error carries the rejected secret values.
            raise _config_error_from_validation(exc) from None
        config.setup_logging()
        _config = config
    return _config


def reset_config() -> None:
    """Reset the global config instance (mainly for testing)."""
    global _config
    _config = None
