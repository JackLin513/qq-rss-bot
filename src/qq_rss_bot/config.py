"""从环境变量 / .env 读取运行配置。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> tuple[str, ...]:
    """逗号或空白分隔的列表。空值 → 空元组（= 不启用该功能）。"""
    raw = os.environ.get(name) or ""
    return tuple(part for part in re.split(r"[,\s]+", raw.strip()) if part)


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
    restrict_admin_commands: bool
    # 主动消息节流：群维度官方频控是 20 条/分钟、1000 条/天，
    # 这里默认留余量给回执/提示这类消息。
    send_per_minute: int = 20
    daily_send_limit: int = 900
    # 每群订阅数上限。没有上限的话，管理员加 20 个源能瞬间打满每日配额。
    max_subs_per_group: int = 10
    # 连续失败多少次自动暂停该订阅
    max_fail_before_pause: int = 5
    # 出错私聊的收件人白名单。**留空 = 自动登记群主和管理员**（默认行为）；
    # 一旦填了就只发给名单上的人，数据库里登记的谁都不看 —— 收件人写死在
    # .env 里，换群主、加管理员、撤管理员都不影响。
    # 注意 openid 是「用户 × 机器人」维度的匿名 ID，不是 QQ 号，
    # 换 AppID 就全变（连 group_openid 一起失效）。
    notify_openids: tuple[str, ...] = ()
    # 机器人进新群时自动订阅的源（RSSHub 路由或完整 URL）。**留空 = 关掉这个功能**。
    # 启动时还会给「一个订阅都没有」的已有群补上（只补零订阅的，见
    # autosub.AutoSubscriber.backfill —— 一律补会把故意退订的群重新订回来）。
    auto_subscribe_feed: str = ""
    # 日志同时落一份到文件。**留空 = 只写 stdout**（本地开发就是这样，
    # botctl.sh 已经重定向了）。容器里必须开 —— `docker logs` 要 root，
    # 而群晖上普通账号连 `/volume1/@docker` 都进不去，出问题时完全看不到原因。
    # 指向挂载卷里的路径（如 /app/data/bot.log），这样就任何人都读得到。
    log_file: str = ""


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
        # 关掉就退回「谁都能订阅/退订」。留这个开关是为了角色识别万一失灵时
        # 有条退路，不至于把机器人锁死。
        restrict_admin_commands=_env_bool("RESTRICT_ADMIN_COMMANDS", True),
        send_per_minute=_env_int("SEND_PER_MINUTE", 20),
        daily_send_limit=_env_int("DAILY_SEND_LIMIT", 900),
        max_subs_per_group=_env_int("MAX_SUBS_PER_GROUP", 10),
        max_fail_before_pause=_env_int("MAX_FAIL_BEFORE_PAUSE", 5),
        notify_openids=_env_list("NOTIFY_OPENIDS"),
        auto_subscribe_feed=(os.environ.get("AUTO_SUBSCRIBE_FEED") or "").strip(),
        log_file=(os.environ.get("LOG_FILE") or "").strip(),
    )
