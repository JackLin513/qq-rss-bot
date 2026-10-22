"""从环境变量 / .env 读取运行配置。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 默认值只在 ai.py 里写一份，免得两边漂
from . import ai as ai_module
# 同上：生日推送的时刻 / 窗口只在 birthday.py 里写一份
from . import birthday as birthday_module
# 同上：诞生祭贺图的时刻 / 窗口 / 张数 / 下限只在 birthday_art.py 里写一份
from . import birthday_art as birthday_art_module
# 同上：「月曜が近いよ」的时段 / 上限只在 monday.py 里写一份
from . import monday as monday_module
from . import morning as morning_module
# 同上：复读机的长度上限和冷却只在 repeat.py 里写一份
from . import repeat as repeat_module
# 同上：抽票的品牌白名单和接口地址只在 tickets.py 里写一份
from . import tickets as tickets_module

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


def _env_float(name: str, default: float) -> float:
    """浮点环境变量。**空串按「没配」处理** —— 和 `_env_str` 同一个口径，
    `.env.example` 里留空的那行复制过来不该变成 `float("")` 崩掉。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise SystemExit(f"环境变量 {name} 必须是数字，当前值：{raw!r}") from exc


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
    # 多图**合并成一条 markdown**（`msg_type=2`），而不是连发 N 条富媒体。
    # 一条带 4 张图的资讯从 4 条消息变成 1 条，省 3 份主动配额，也不刷屏。
    #
    # 代价：markdown 的图由 **QQ 的服务器自己去下载**（富媒体那条路其实也是，
    # `send_rich` 传的就是 url），所以图必须 QQ 够得到。发之前会先用
    # `upload_image` 探一次，抓不到的图直接剔掉 —— markdown 的图裂了是静默的，
    # 事后无从发现。
    #
    # 2026-10-18 实测有效（手机端 + 电脑端都确认过）。语法和坑见
    # `multiimage.py` 的模块说明。**关掉它只发第一张**（不连发）。
    multi_image_markdown: bool = True
    # 第三方图床的图（bilibili 封面 / 抽票封面 / RSS 配图）**下载后收进图库**，
    # 消息里用我们自己的地址。图留在自己手里，第三方删图 / 防盗链 / 换 CDN
    # 都不再影响历史消息。
    #
    # 代价：每条推送多下载、多存几 MB。**单张失败只降级那一张**（退回原 URL），
    # 消息照发。发现带宽或磁盘吃不消就关掉这个开关（不用回滚代码）。
    mirror_images: bool = True
    # ---- markdown 多图预热：正式群推送前的两道预热 + 一道人工预览 ----
    #
    # ⚠️ **预热机制已于 2026-10-22 删除。** 当年「冷链接第一次渲染必炸」
    # （3.4 MB 六张全裂、隔 20 秒发到另一个群就全好）是在 `pbs.twimg.com` 上
    # 量到的；换成自建图床后复现不出来 —— 一张 3.5 MB、从未被访问过的图
    # **不预热直接发就能显示**（`docs/imgsrv.md` 有实测表）。
    # 真遇到图不显示，先看图床访问日志有没有 200，别再盲目加预热。
    #
    # 保留下来的只有**人工预览**：先发到预览群，等 `preview_veto_sec`，你没喊
    # 「不发」才发正式群。空 = 关闭，行为退回「一把 gather」。
    preview_group: str = ""
    # 预览发出去后等多久算「你没喊停」。这段时间正式群一条都不发。
    preview_veto_sec: int = 30
    # 推送文案里带不带 RSS 摘要。关掉就退回「源名 / 标题 / 链接」三行。
    push_summary: bool = True
    # 摘要显示多少字。**不是平台限制**（实测 40000 字符 QQ 照样收），
    # 纯粹是可读性。
    summary_chars: int = 800
    # 偶像大师官网 NEWS 的「企划 / 分类」标签（`【灰姑娘】【演出/谷子】`）。
    # 这两个属性 RSS 里都没有 —— 企划 CMS 压根不输出、分类给的是日文原名 ——
    # 所以开了它**每次轮询会多调一次 CMS API**（见 idolmaster.py）。
    # 任何一步失败都静默降级成不带标签，不影响推送。
    imas_tags: bool = True

    # ===== ASOBI TICKET 抽票提醒（见 tickets.py 的模块说明）=====
    #
    # 偶像大师各场 live 的抽选什么时候开始 / 截止，以前只能自己上网站翻。
    # 这个功能拉 ASOBI TICKET 的**公开接口**（不用登录、群晖直连 1.4 秒、
    # **不用挂代理**），做三类提醒：新受付 / 申込締切 / 入金締切。
    #
    # ⚠️ **首轮只把 `new` 建成 baseline，一条 `new` 都不推** —— 存量有 31 条
    # 受付，直接跑会当场倒灌十几条。但締切提醒故意不 baseline，该响就响。
    ticket_alert: bool = True
    # 群列表**不在配置里** —— 在数据库的 group_features 表（见 features.py）
    # 多久检查一次。接口带 ETag，没变时是 304 + 0 字节，所以可以给得很松。
    ticket_check_sec: int = 300
    # booth slug 的品牌前缀白名单。**认不出来的前缀一律不推**（fail-closed），
    # 但会打一条 INFO 日志，将来出新品牌时能看见。
    # 判据是站点自己打在 slug 上的品牌标 —— **不能改成按活动名匹配**，
    # 有三个名字里根本没有 IDOLM@STER 的（见 tickets.py 的模块说明）。
    ticket_brands: tuple[str, ...] = tickets_module.DEFAULT_BRANDS
    ticket_url: str = tickets_module.DEFAULT_API
    # 封面图开关。**2026-10-18 加**：markdown 的图片冷链接第一次渲染会炸
    # （见 CLAUDE.md 那节），所以留个开关方便实测到底是不是也会不显示。
    # 关掉只影响有没有图，提醒照发。
    ticket_images: bool = True
    # 封面图档位。接口给 4 个尺寸，实测体积：
    #   small 12 KB / medium 144 KB（默认）/ large 271 KB / original 1.8 MB
    # **别用 original** —— 1.8 MB 正是会把冷链接搞炸的量级；small 只有
    # 150px 高，放大到渲染宽度 208 会糊。
    ticket_image_size: str = tickets_module.DEFAULT_IMAGE_SIZE
    # 封面渲染宽度（px）。**QQ 不会把小图放大** —— 定多少就渲染多少，
    # 所以 208（bilibili 封面那个值）在手机上是「没占满气泡」的样子。
    # 这个数对不上设备就白搭，所以做成可调的：
    # `scripts/probe_mdfmt.py` 会把同一张图按几个宽度发到测试群，挑一个刚好
    # 占满的填进来。
    ticket_image_w: int = tickets_module.DEFAULT_IMAGE_W

    # ===== 月曜が近いよ（每周定时的视频播报，见 monday.py 的模块说明）=====
    #
    # 原型是 X 上的 @ChocodateMonday，**那个号 2026-09-27 已停止活动**，所以
    # 这里是让机器人自己每周播一次：周日 22:00（北京时间）发一条
    # 「月曜が近いよ」+ 一个本地视频，素材从 MONDAY_VIDEO_DIR 里随机挑。
    monday_enabled: bool = True
    # 群列表**不在配置里** —— 在数据库的 group_features 表（见 features.py）
    # 素材目录。**留空就跟着 DB_PATH 走**（本地 `data/monday`、容器
    # `/app/data/monday`），所以正常情况下不用配。
    monday_video_dir: Path = Path("data/monday")
    # 星期几。**口径同 `datetime.weekday()`：0=周一 … 6=周日。**
    monday_weekday: int = monday_module.DEFAULT_WEEKDAY
    # 几点发，`HH:MM`，**本地时间**（不是 JST —— 见 monday.py 里的说明）。
    monday_time: str = monday_module.DEFAULT_TIME
    # 容忍多久的调度抖动。**这不是补发窗口** —— 用户明确选了「不补发」，
    # 所以它只该覆盖 tick 间隔（60 秒），别调成几小时。
    monday_window_sec: int = monday_module.DEFAULT_WINDOW_SEC

    # ===== 群复读机（见 repeat.py 的模块说明）=====
    #
    # 连着**两个人**发同样的话，机器人跟着复读一句。用户 2026-10-20 定的规则。
    # ⚠️ 这个功能和别的不同：**每条群消息都可能触发**，不是定时任务。
    repeat_enabled: bool = True
    # 群列表**不在配置里** —— 在数据库的 group_features 表（见 features.py）
    # 超过这个长度就不跟（复读长文像刷屏）。
    repeat_max_len: int = repeat_module.DEFAULT_MAX_LEN
    # 同一个群两次复读之间的最小间隔（秒）。防的是「一群人反复刷同一句」。
    repeat_gap_sec: float = repeat_module.DEFAULT_GAP_SEC

    # ===== 小偶像的每日早安语音（见 morning.py 的模块说明）=====
    #
    # 每天早上 07:30（北京时间）随机挑一位偶像，用她的声线说一句日语早上好。
    # 语音**离线预生成**（`scripts/make_morning.py`），运行时只扫
    # `data/morning/audio/` 上传，**零 LLM、零 TTS、零新依赖**。
    morning_enabled: bool = True
    # 群列表**不在配置里** —— 在数据库的 group_features 表（见 features.py）
    # 素材目录。**留空就跟着 DB_PATH 走**（本地 `data/morning`、容器
    # `/app/data/morning`），正常情况下不用配。运行时只用里面的 `audio/`。
    morning_dir: Path = Path("data/morning")
    # 几点发，`HH:MM`，**本地时间**。用户定的是 07:30。
    morning_time: str = morning_module.DEFAULT_TIME
    # 容忍多久的调度抖动。**不是补发窗口** —— 九点补一条早安比不补更怪。
    morning_window_sec: int = morning_module.DEFAULT_WINDOW_SEC
    # 多久之内不重复同一位偶像。28 位轮一圈约一个月，7 天足够。
    morning_recent_days: int = morning_module.DEFAULT_RECENT_DAYS

    # ===== 偶像生日推送（见 birthday.py 的模块说明）=====
    #
    # 每天 23:05（北京时间）推**第二天**的生日 —— 那正好是日本时间的当天 0 点，
    # 也是 imas_ch 发出当天祝贺图之后。所以 10-18 晚上推的是 10-19 的生日。
    #
    # 名单是**冷数据**（`data/birthday/birthdays.toml`，见
    # scripts/fetch_birthday_source.py），配图每天实时抓 —— 两条链路互相独立，
    # **抓不到图照样发名单**。
    birthday_enabled: bool = True
    # 群列表**不在配置里** —— 在数据库的 group_features 表（见 features.py）
    # 冷数据目录。**留空就跟着 DB_PATH 走**（本地 `data/birthday`、容器
    # `/app/data/birthday`），正常情况下不用配。
    birthday_dir: Path = Path("data/birthday")
    # 几点发，`HH:MM`，**本地时间**。用户定的是 23:05。
    birthday_time: str = birthday_module.DEFAULT_TIME
    # 窗口。⚠️ **和早安那个 180 秒不是一个意思**：早安是纯本地文件，窗口只覆盖
    # 调度抖动；这里要实时抓推文，所以窗口同时是**抓图的补发窗口** ——
    # 窗口内每拍重试，到窗口末尾还没抓到就发纯文字。
    birthday_window_sec: int = birthday_module.DEFAULT_WINDOW_SEC

    # ===== 誕生祭 贺图聚合（见 birthday_art.py 的模块说明）=====
    #
    # 每天 22:30（北京时间）搜推特上带生日 tag 的贺图，**一张一条**发到群里
    # （最多 6 条）。冷数据和上面那条生日推送**共用一份** birthdays.toml。
    #
    # ⚠️ 收的是**当天**的贺图 —— 和上面那条生日推送**差一天**（那个 23:05 推的是
    # **第二天**的名单）。见 birthday_art.art_date() 的说明，写错是整体错位一天。
    #
    # ⚠️ 用户原本定的是 12:00，改成了 22:30：贺图的发布高峰是**日本时间当天的
    # 傍晚到深夜**（= 北京 17:00–23:00），中午去搜只捞得到当天上午那批。
    # 22:30 北京 = 23:30 JST，当天基本收尾。想改回去改这一个变量就行。
    birthday_art_enabled: bool = True
    # 群列表**不在配置里** —— 在数据库的 group_features 表（见 features.py）
    # 几点收，`HH:MM`，本地时间。
    birthday_art_time: str = birthday_art_module.DEFAULT_TIME
    # 重试窗口 —— 搜索失败（网络抖 / 被限流）时窗口内每拍重试。
    # **不是补发窗口**：贺图早就躺在那儿了，纯粹是重试。
    birthday_art_window_sec: int = birthday_art_module.DEFAULT_WINDOW_SEC
    # 一天最多发几张（一条一张）。
    birthday_art_max: int = birthday_art_module.DEFAULT_MAX
    # 点赞软下限。用户原话是「点赞超过 2k」，但实测当天最高也就 464、
    # **按 2k 筛一条都不剩**，所以真正起作用的是「按点赞取前 N」，这个数只是
    # 「别发一堆 0 赞垃圾」的兜底。想让冷门偶像一张都不发就往上调。
    birthday_art_min_likes: int = birthday_art_module.DEFAULT_MIN_LIKES
    birthday_art_exclude: tuple[str, ...] = ()
    # ⚠️ 2026-10-22 起默认 **False**：官方号不再整个丢掉，而是**单列一格**
    # （官号 1 + 本人/声优 1 + 画师 4）。设成 True 是逃生门 = 官方号一条都不要。
    birthday_art_skip_official: bool = False

    # ===== 图库 + 图床（见 imagelib.py / imgsrv.py）=====
    #
    # ⚠️ 图床**不是可选项**。markdown 的图是 **QQ 的服务器去抓**，而它在大陆机房
    # 够不到 `pbs.twimg.com` —— 图得我们自己托管，给 QQ 一个它抓得到的 URL。
    #
    # `imgsrv_base` 是**对外的**根 URL（要带端口），图片地址是
    # `<imgsrv_base>/i/<sha256>.<ext>`。空 = 图床关闭（图照样入库，但组里看不到）。
    imgsrv_base: str = ""
    imgsrv_port: int = 18080          # 容器内监听端口，compose 里要发布同一个
    # 图库落盘位置（内容寻址，见 docs/adr/0002）。空 = 和 bot.db 同级的 library/
    imagelib_dir: Path | None = None

    # ===== X 搜索凭据（誕生祭 贺图用）=====
    # ⚠️ **真实凭据**：只从 `.env` / 环境变量读，**绝不打印**（日志里只出现长度）、
    # **绝不提交**（仓库是公开的）。拿账号 cookie 做自动化访问**违反 X 的条款**，
    # 有被封的风险 —— **用小号**。
    # 拿法：浏览器登录 x.com → DevTools → Application → Cookies → x.com。
    x_auth_token: str = ""
    x_ct0: str = ""

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

    # 月曜的素材目录：留空就跟着 DB_PATH 走。容器里 DB_PATH=/app/data/bot.db，
    # 所以默认是 /app/data/monday —— 正好在挂载卷里，不用额外配一项。
    monday_dir_raw = _env_str("MONDAY_VIDEO_DIR", "")
    monday_video_dir = (
        Path(monday_dir_raw) if monday_dir_raw else db_path.parent / "monday"
    )
    if not monday_video_dir.is_absolute():
        monday_video_dir = PROJECT_ROOT / monday_video_dir
    # 星期几的口径校验放这儿：写错 7 或 -1 的话 `slot_for` 会静默算出
    # 一个永远不匹配的时刻，功能看着「开着」但一周都不响。
    monday_weekday = _env_int("MONDAY_WEEKDAY", monday_module.DEFAULT_WEEKDAY)
    if not 0 <= monday_weekday <= 6:
        raise SystemExit(
            f"MONDAY_WEEKDAY 必须是 0（周一）~ 6（周日），当前值：{monday_weekday}"
        )
    # 时间也在这儿校验。只在轮询器里解析的话，写错要等到周日 22:00 那一拍
    # 才炸 —— 而那正好是唯一该发消息的时刻。启动就退出，别沉默一周。
    monday_time = _env_str("MONDAY_TIME", monday_module.DEFAULT_TIME)
    monday_module.parse_time(monday_time)

    # 早安素材目录：和月曜一字不差的推导 —— 留空就跟着 DB_PATH 走，
    # 容器里 DB_PATH=/app/data/bot.db，所以默认 /app/data/morning。
    morning_dir_raw = _env_str("MORNING_DIR", "")
    morning_dir = (
        Path(morning_dir_raw) if morning_dir_raw else db_path.parent / "morning"
    )
    if not morning_dir.is_absolute():
        morning_dir = PROJECT_ROOT / morning_dir
    # 时间同样在启动时校验。只在轮询器里解析的话，写错要等到 07:30 那一拍
    # 才炸 —— 而那正好是唯一该发消息的时刻。
    morning_time = _env_str("MORNING_TIME", morning_module.DEFAULT_TIME)
    morning_module.parse_time(morning_time)
    morning_recent_days = _env_int(
        "MORNING_RECENT_DAYS", morning_module.DEFAULT_RECENT_DAYS
    )
    if morning_recent_days < 0:
        raise SystemExit(f"MORNING_RECENT_DAYS 不能是负数：{morning_recent_days}")

    # 生日冷数据目录：和上面两条一字不差的推导 —— 留空就跟着 DB_PATH 走，
    # 容器里 DB_PATH=/app/data/bot.db，所以默认 /app/data/birthday。
    birthday_dir_raw = _env_str("BIRTHDAY_DIR", "")
    birthday_dir = (
        Path(birthday_dir_raw) if birthday_dir_raw else db_path.parent / "birthday"
    )
    if not birthday_dir.is_absolute():
        birthday_dir = PROJECT_ROOT / birthday_dir
    # 时间同样在启动时校验。写错要等到 23:05 那一拍才炸 —— 而那正好是唯一
    # 该发消息的时刻。**注意这里只校验格式，文件存不存在在轮询器里判**
    # （冷数据是 gitignore 的，全新部署本来就没有，不该连累机器人起不来）。
    birthday_time = _env_str("BIRTHDAY_TIME", birthday_module.DEFAULT_TIME)
    birthday_module.parse_time(birthday_time)

    # 贺图的时刻同样在启动时校验（理由同上：写错要等到该发的那一刻才炸）。
    birthday_art_time = _env_str("BIRTHDAY_ART_TIME", birthday_art_module.DEFAULT_TIME)
    birthday_art_module.parse_time(birthday_art_time)

    # 图库目录：同样跟着 DB_PATH 走（容器里就是 /app/data/library）。
    imagelib_dir_raw = _env_str("IMAGELIB_DIR", "")
    imagelib_dir = (
        Path(imagelib_dir_raw) if imagelib_dir_raw else db_path.parent / "library"
    )
    if not imagelib_dir.is_absolute():
        imagelib_dir = PROJECT_ROOT / imagelib_dir

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
        multi_image_markdown=_env_bool("MULTI_IMAGE_MARKDOWN", True),
        mirror_images=_env_bool("MIRROR_IMAGES", True),
        preview_group=_env_str("PREVIEW_GROUP", ""),
        preview_veto_sec=_env_int("PREVIEW_VETO_SEC", 30),
        push_summary=_env_bool("PUSH_SUMMARY", True),
        summary_chars=_env_int("SUMMARY_CHARS", 800),
        imas_tags=_env_bool("IMAS_TAGS", True),
        ticket_alert=_env_bool("TICKET_ALERT", True),
        ticket_check_sec=_env_int("TICKET_CHECK_SEC", 300),
        ticket_brands=_env_list("TICKET_BRANDS") or tickets_module.DEFAULT_BRANDS,
        ticket_url=_env_str("TICKET_URL", tickets_module.DEFAULT_API),
        ticket_images=_env_bool("TICKET_IMAGES", True),
        ticket_image_size=_env_str("TICKET_IMAGE_SIZE", tickets_module.DEFAULT_IMAGE_SIZE),
        ticket_image_w=_env_int("TICKET_IMAGE_W", tickets_module.DEFAULT_IMAGE_W),
        monday_enabled=_env_bool("MONDAY_ENABLED", True),
        monday_video_dir=monday_video_dir,
        monday_weekday=monday_weekday,
        monday_time=monday_time,
        monday_window_sec=_env_int("MONDAY_WINDOW_SEC", monday_module.DEFAULT_WINDOW_SEC),
        repeat_enabled=_env_bool("REPEAT_ENABLED", True),
        repeat_max_len=_env_int("REPEAT_MAX_LEN", repeat_module.DEFAULT_MAX_LEN),
        repeat_gap_sec=_env_float("REPEAT_GAP_SEC", repeat_module.DEFAULT_GAP_SEC),
        morning_enabled=_env_bool("MORNING_ENABLED", True),
        morning_dir=morning_dir,
        morning_time=morning_time,
        morning_window_sec=_env_int(
            "MORNING_WINDOW_SEC", morning_module.DEFAULT_WINDOW_SEC
        ),
        morning_recent_days=morning_recent_days,
        birthday_enabled=_env_bool("BIRTHDAY_ENABLED", True),
        birthday_dir=birthday_dir,
        birthday_time=birthday_time,
        birthday_window_sec=_env_int(
            "BIRTHDAY_WINDOW_SEC", birthday_module.DEFAULT_WINDOW_SEC
        ),
        birthday_art_enabled=_env_bool("BIRTHDAY_ART_ENABLED", True),
        birthday_art_time=birthday_art_time,
        birthday_art_window_sec=_env_int(
            "BIRTHDAY_ART_WINDOW_SEC", birthday_art_module.DEFAULT_WINDOW_SEC
        ),
        birthday_art_max=_env_int("BIRTHDAY_ART_MAX", birthday_art_module.DEFAULT_MAX),
        birthday_art_min_likes=_env_int(
            "BIRTHDAY_ART_MIN_LIKES", birthday_art_module.DEFAULT_MIN_LIKES
        ),
        birthday_art_exclude=_env_list("BIRTHDAY_ART_EXCLUDE"),
        birthday_art_skip_official=_env_bool("BIRTHDAY_ART_SKIP_OFFICIAL", False),
        imgsrv_base=_env_str("IMGSRV_BASE", "").rstrip("/"),
        imgsrv_port=_env_int("IMGSRV_PORT", 18080),
        imagelib_dir=imagelib_dir,
        x_auth_token=_env_str("auth_token", ""),
        x_ct0=_env_str("ct0", ""),
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
