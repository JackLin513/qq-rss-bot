"""从环境变量 / .env 读取运行配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"环境变量 {name} 必须是整数，当前值：{raw!r}") from exc


@dataclass(frozen=True)
class Config:
    appid: str
    secret: str
    is_sandbox: bool
    rsshub_base: str
    default_interval_sec: int
    tick_sec: int
    fetch_timeout_sec: int
    max_items_per_push: int
    db_path: Path
    log_level: str
    attach_images: bool


def load_config() -> Config:
    load_dotenv(PROJECT_ROOT / ".env")

    appid = (os.environ.get("QQ_BOT_APPID") or "").strip()
    secret = (os.environ.get("QQ_BOT_SECRET") or "").strip()
    missing = [n for n, v in (("QQ_BOT_APPID", appid), ("QQ_BOT_SECRET", secret)) if not v]
    if missing:
        raise SystemExit(
            f"缺少环境变量：{', '.join(missing)}\n"
            f"请复制 {PROJECT_ROOT / '.env.example'} 为 .env 并填写。"
        )

    db_path = Path(os.environ.get("DB_PATH") or "data/bot.db")
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    return Config(
        appid=appid,
        secret=secret,
        is_sandbox=_env_bool("QQ_BOT_SANDBOX", False),
        rsshub_base=(os.environ.get("RSSHUB_BASE") or "https://rsshub.app").rstrip("/"),
        default_interval_sec=_env_int("DEFAULT_INTERVAL_SEC", 900),
        tick_sec=_env_int("TICK_SEC", 30),
        fetch_timeout_sec=_env_int("FETCH_TIMEOUT_SEC", 20),
        max_items_per_push=_env_int("MAX_ITEMS_PER_PUSH", 5),
        db_path=db_path,
        # DEBUG 能看到「非 @ 消息被忽略」这类细节，排查协议问题很有用
        log_level=(os.environ.get("LOG_LEVEL") or "INFO").strip().upper(),
        attach_images=_env_bool("ATTACH_IMAGES", True),
    )
