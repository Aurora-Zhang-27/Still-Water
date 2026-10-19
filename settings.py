"""Read local settings from .env.

Only what read-only data access needs; nothing order-related.
Environment variables win over .env; a value still set to a your_..._here placeholder counts as empty.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parent

_PLACEHOLDER = re.compile(r"^your_.*_here$")
_KEYS = (
    "IBKR_FLEX_TOKEN",
    "IBKR_FLEX_QUERY_ID_ACTIVITY",
    "IBKR_FLEX_QUERY_ID_TRADE_CONFIRM",
    "IBKR_ACCOUNT_ID",
    "IB_GATEWAY_HOST",
    "IB_GATEWAY_PORT",
    "IB_CLIENT_ID",
    "DB_PATH",
    "RULES_PATH",
)


class SettingsError(ValueError):
    """.env is invalid."""


@dataclass(frozen=True)
class Settings:
    # secrets and the account number stay out of repr, so they never show up in logs or errors
    flex_token: str | None = field(repr=False)
    flex_query_id_activity: str | None
    flex_query_id_trade_confirm: str | None
    account_id: str | None = field(repr=False)
    gateway_host: str | None
    gateway_port: int | None
    gateway_client_id: int | None
    db_path: Path
    rules_path: Path


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines; skip blank lines and # comments, strip matching quotes."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load_settings(
    env_file: Path | None = None, environ: Mapping[str, str] | None = None
) -> Settings:
    merged = read_env_file(env_file or PROJECT_ROOT / ".env")
    env = os.environ if environ is None else environ
    merged.update({k: env[k] for k in _KEYS if k in env})

    def get(key: str) -> str | None:
        value = merged.get(key, "").strip()
        if not value or _PLACEHOLDER.match(value):
            return None
        return value

    def get_int(key: str) -> int | None:
        value = get(key)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            raise SettingsError(f"{key} must be an integer") from None

    def get_path(key: str, default: str) -> Path:
        path = Path(get(key) or default).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    return Settings(
        flex_token=get("IBKR_FLEX_TOKEN"),
        flex_query_id_activity=get("IBKR_FLEX_QUERY_ID_ACTIVITY"),
        flex_query_id_trade_confirm=get("IBKR_FLEX_QUERY_ID_TRADE_CONFIRM"),
        account_id=get("IBKR_ACCOUNT_ID"),
        gateway_host=get("IB_GATEWAY_HOST"),
        gateway_port=get_int("IB_GATEWAY_PORT"),
        gateway_client_id=get_int("IB_CLIENT_ID"),
        db_path=get_path("DB_PATH", "data/trading.db"),
        rules_path=get_path("RULES_PATH", "rules.yaml"),
    )
