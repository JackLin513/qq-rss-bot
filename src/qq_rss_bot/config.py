"""从环境变量 / .env 读取运行配置。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 默认值只在 ai.py 里写一份，免得两边漂
from . import ai as ai_module

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


def _env_str(name: str, default: str) -> str:
    """字符串环境变量。空串按「没配」处理 —— `.env.example` 里留空的那些
    复制过来就是空串，不该被当成一个有效的值。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


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
    # 群里有人分享 bilibili 链接时，自动回一张带封面和数据的卡片。
    # 走**被动回复**（不占主动消息配额）；关掉它不影响 RSS 推送。
    # 四种分享形态和实现见 bilibili.py 的模块说明。
    bilibili_preview: bool = True
    # 群里有人分享 YouTube / X(Twitter) 链接时，同样自动回卡片。
    # 这两个站点**容器直连不通**，必须配 media_proxy —— 没配或代理不通时
    # 解析必然失败，调用方静默不发，不会连累 bilibili 和 RSS 推送。
    youtube_preview: bool = True
    twitter_preview: bool = True
    # 一条分享带**多张图**。关掉就只发第一张，回到之前的行为。
    # 两个场景的代价**完全不同，所以分成两个开关**：
    #   · preview 那条走被动回复 —— 多图只多占几次回复额度，**不占主动配额**；
    #   · RSS 那条是主动消息 —— **每张图各占一条配额**（20 条/分钟 + 每日上限），
    #     一条带 4 张图的资讯就是 4 条。
    # 觉得刷屏就关掉对应的那个，另一个不受影响。
    preview_multi_image: bool = True
    rss_multi_image: bool = True
    # 推送文案里带不带 RSS 摘要。关掉就退回「源名 / 标题 / 链接」三行。
    push_summary: bool = True
    # 摘要显示多少字。**不是平台限制**（实测 40000 字符 QQ 照样收），
    # 纯粹是可读性。
    summary_chars: int = 400

    # ===== AI 摘要（抓正文 + DeepSeek 翻译/精简）=====
    # 按源开关，默认所有源都是关的（`subscriptions.ai_enabled`）。
    # key 留空 = 整个功能关掉，不报错。
    ai_api_key: str = ""
    ai_base_url: str = ai_module.DEFAULT_BASE_URL
    ai_model: str = ai_module.DEFAULT_MODEL
    # **全局**日调用上限，不是每群。花的是我们自己的钱，所以要有个总数兜底。
    # 缓存命中不计数（同一篇文章只付一次）。
    ai_daily_limit: int = 200
    # **默认不抓文章页。** 很多源的 `<description>` 本身就是全文
    # （实测少数派 3914 字），再抓一遍纯属浪费一次请求和几秒延迟。
    # 只有当源的导语明显短于正文（V2EX / Hacker News 那种只给标题的）才值得开。
    ai_fetch_article: bool = False
    # DeepSeek 的 thinking 默认开着且档位是 high。翻译+摘要不需要推理链，
    # 开着白白多花 output token 和多等几秒。非 DeepSeek 端点若不认这个字段
    # （OpenAI 会 400），设成 false。
    ai_disable_thinking: bool = True
    # 抓正文的超时。付费墙 / JS 渲染的站点会拖满这个时间，所以别设太长 ——
    # 它在推送主循环里，等的是所有人的消息。
    ai_fetch_timeout_sec: int = 15
    # 调模型的超时。
    ai_timeout_sec: int = 30
    # 访问被墙站点（YouTube / X）用的出站代理，如 http://192.168.0.63:6152。
    # **留空 = 直连**（那两个功能自然失效，其余一切照常）。
    # 只给 media_http 用 —— rss_http 保持直连：RSSHub 是容器内部服务名
    # （http://rsshub:1200），代理解析不了；QQ 的接口在国内直连更快。
    media_proxy: str = ""
    # ⚠️ Surge 这类**会做 MITM 的代理**必须配下面两个之一，否则 YouTube 全线
    # `CERTIFICATE_VERIFY_FAILED: self-signed certificate`（实测：`www.youtube.com`
    # 被 Surge 重签了，而 `i.ytimg.com` / `api.fxtwitter.com` 没有 —— 所以症状是
    # 「图能下、接口全挂」，很有迷惑性）。
    #
    # `MEDIA_CA_BUNDLE` = 把代理的根证书导出成 PEM 放进来（**推荐**）。
    # 容器里路径要写挂载后的（如 /app/data/surge-ca.pem）。
    media_ca_bundle: str = ""
    # `MEDIA_INSECURE_TLS=true` = 干脆不校验证书。**逃生舱，不是首选** ——
    # 它让这两个站点的流量完全裸奔。只在拿不到代理根证书时用。
    media_insecure_tls: bool = False


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
        bilibili_preview=_env_bool("BILIBILI_PREVIEW", True),
        youtube_preview=_env_bool("YOUTUBE_PREVIEW", True),
        twitter_preview=_env_bool("TWITTER_PREVIEW", True),
        preview_multi_image=_env_bool("PREVIEW_MULTI_IMAGE", True),
        rss_multi_image=_env_bool("RSS_MULTI_IMAGE", True),
        push_summary=_env_bool("PUSH_SUMMARY", True),
        summary_chars=_env_int("SUMMARY_CHARS", 400),
        ai_api_key=_env_str("DEEPSEEK_API_KEY", ""),
        ai_base_url=_env_str("DEEPSEEK_BASE_URL", ai_module.DEFAULT_BASE_URL),
        ai_model=_env_str("DEEPSEEK_MODEL", ai_module.DEFAULT_MODEL),
        ai_daily_limit=_env_int("AI_DAILY_LIMIT", 200),
        ai_fetch_article=_env_bool("AI_FETCH_ARTICLE", False),
        ai_disable_thinking=_env_bool("AI_DISABLE_THINKING", True),
        ai_fetch_timeout_sec=_env_int("AI_FETCH_TIMEOUT_SEC", 15),
        ai_timeout_sec=_env_int("AI_TIMEOUT_SEC", 30),
        media_proxy=(os.environ.get("MEDIA_PROXY") or "").strip(),
        media_ca_bundle=(os.environ.get("MEDIA_CA_BUNDLE") or "").strip(),
        media_insecure_tls=_env_bool("MEDIA_INSECURE_TLS", False),
    )
