"""ASOBI TICKET（アソビストア）抽票提醒。

偶像大师各场 live 的抽选什么时候开始、什么时候截止，以前只能自己上网站翻。
这个模块拉 ASOBI TICKET 的公开接口，把日期结构化出来，做三类提醒。

**数据源是官方 API，不是爬页面**（2026-10-18 用 rea 实测确认）：

* `asobiticket2.asobistore.jp` 是 Angular SPA，列表页 DOM 里**只有活动名和
  `受付中` 状态，没有日期** —— 日期是前端调接口拿的（`main-JERHSTGS.js` 里
  写着 `apiEndpoint`）。
* `GET /v1/public/receptions` **完全公开、不需要登录**。群晖直连 1.4 秒，
  **不用挂代理**（YouTube / X 才需要）。
* 整个响应 814 KB / 31 条受付。带 `If-None-Match` 返回 **304、0 字节、0.47 秒**，
  所以轮询很便宜，5 分钟一次完全无所谓。
* 接口**不支持**按品牌过滤（`?brand=` 之类的参数全被忽略，照样返回 31 条），
  只能客户端筛，见下面的 `is_imas`。

**品牌判据：booth 的 `slug` 前缀。** 站点自己打的标，实测 17 个 booth 与品牌
完全一致。**不能改成按活动名匹配** —— 有三个名字里根本没有 IDOLM@STER 的：

    xR LIVE REVIVAL                       → idolmaster-261212    (IM@S)
    ATHOME xR LIVE　Apple of my アイ       → cinderellagirls-...  (灰姑娘)
    shiny, the first REFRAC7IONS ...      → shinycolors-...      (闪耀色彩)

反过来 `tales-` / `denonbu-` / `zerokyori-` 是别的作品，要剔掉。

**判定用 JST，显示用北京时间（CST）。** 接口给的是 `+09:00`，服务器 TZ 是
`Asia/Shanghai`，直接拿本地时间比会差一小时 —— 抽选截止的 `23:59` 差一小时
就是错过，所以**所有比较**都走 `now()`（JST）。显示是给群里国内的人看的，
`_date` / `_at` / `_stamp` 统一转 CST。两者比的是同一个绝对时刻，**别把
`now()` 也改成 CST**（smoke 里有一条钉这个）。

**首轮只建 baseline、一条都不推。** 存量有 31 条受付（14 条 IM@S），直接跑会
当场倒灌十几条 —— 和 `rss.py` 里 `sub.initialized` 是同一个坑。
但**締切提醒不 baseline**：那本来就是按时间窗口算的，该响就响。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Mapping, Sequence

from . import features, multiimage

if TYPE_CHECKING:
    import httpx

_log = logging.getLogger("qq_rss_bot.tickets")

# 一律按日本时间。见模块 docstring 里的时区说明。
JST = timezone(timedelta(hours=9))

DEFAULT_API = "https://asobi-ticket.api.app.t-riple.com/api/v1/public/receptions"

# booth 列表接口。**每场 live 的封面在这里，不在 receptions 里。**
# receptions 的 `booth.relationships.cover_image` 是 `{}` 而且从不下发
# （`?include=` 传了会被静默忽略，响应还是 814 KB、booth 照样没封面），
# 而本条接口 17 个 booth **全部**带封面、`included` 里就是那 17 张图。
# 只有 16.8 KB，而且同样支持 ETag 304（2026-10-18 实测）。
DEFAULT_BOOTHS = "https://asobi-ticket.api.app.t-riple.com/api/v1/public/booths"


def booths_url(api_url: str) -> str:
    """从 receptions 的地址推出 booth 列表的地址。

    接口是同一套 JSON:API，两条路径只差最后一段，所以跟着 `TICKET_URL` 走
    比再开一个环境变量省事；推不出来就退回默认地址。
    """
    base, _, last = (api_url or "").rpartition("/")
    if base and last == "receptions":
        return f"{base}/booths"
    return DEFAULT_BOOTHS

# booth slug 的品牌前缀白名单。认不出来的前缀**一律不推**（fail-closed），
# 但会打一条 INFO 日志 —— 将来出新品牌时日志里能看见，不会静默漏掉。
DEFAULT_BRANDS: tuple[str, ...] = (
    "va-liv",
    "cinderellagirls",
    "idolmaster",
    "sidem",
    "shinycolors",
    "765pro",
    "gkmas",
    "millionlive",
)

BOOTH_URL = "https://asobiticket2.asobistore.jp/booths/{slug}"

# 封面图的尺寸档位 → 接口里 `reading_urls` 的键。实测体积（都是 16:9）：
#
#     small     267×150     11.8 KB
#     medium    800×450    143.8 KB   ← 默认
#     large    1200×675    270.9 KB
#     original 1920×1080   1864.0 KB
#
# 选 medium 是因为**图片冷链接失败和体积强相关**：实测 1.6 MB 不炸、
# 3.4 MB 全炸（见 CLAUDE.md）。original 那 1.8 MB 正是危险量级，而 small
# 只有 150 px 高，放大到渲染宽度 208 会糊。
IMAGE_SIZES = ("small", "medium", "large", "original")
DEFAULT_IMAGE_SIZE = "medium"

# 封面渲染宽度（px）。**QQ 不会把小于气泡的图放大**，定多少渲染多少 ——
# `multiimage.COVER_W`（208）那个值是给 bilibili 封面用的，在列表里看着
# 「没占满」。这个数对不上设备就白搭，所以可配（`TICKET_IMAGE_W`），
# 用 `scripts/probe_mdfmt.py` 在真机上挑。
DEFAULT_IMAGE_W = 320

# 締切提醒的提前量：(小时, 用来说明的那几个字)。**从紧到松排** ——
# 判定时先看 2 小时的窗口，进了就不再补发 24 小时那条。
LEADS: tuple[tuple[int, str], ...] = ((2, "2"), (24, "24"))

# 三条提醒各自的标题前缀
KIND_LABELS = {"entry": "申込締切", "deposit": "入金締切"}

_FETCH_TIMEOUT = 25.0


def now() -> datetime:
    """当前日本时间。**所有时间比较都从这里取。**

    服务器 TZ 是 `Asia/Shanghai`，`datetime.now()` 拿到的是北京时间 —— 拿它
    去比 JST 的締切会差一小时，抽选截止的 `23:59` 差一小时就是错过。
    """
    return datetime.now(JST)


def _dt(raw: object) -> datetime | None:
    """解析接口给的 ISO8601。全是 `+09:00`，但**不假设** —— 没带时区的按 JST 补。"""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=JST)


@dataclass(frozen=True)
class Reception:
    """一条「受付」＝ 一轮报名（活动 × 会员档位）。同场活动会有好几条。"""

    id: str
    booth_slug: str
    booth_name: str
    name: str
    entry_type: str
    status: str
    entry_start: datetime | None = None
    entry_end: datetime | None = None
    result_at: datetime | None = None
    deposit_start: datetime | None = None
    deposit_end: datetime | None = None
    cover: Mapping[str, str] = field(default_factory=dict)

    @property
    def lottery(self) -> bool:
        """抽選（`lottery`）还是先着（`fcfs`）。"""
        return self.entry_type == "lottery"

    @property
    def kind_label(self) -> str:
        return "抽選" if self.lottery else "先着"

    @property
    def url(self) -> str:
        return BOOTH_URL.format(slug=self.booth_slug)

    @property
    def title(self) -> str:
        """活动名 + 受付名，日志和消息都用它。"""
        return f"{self.booth_name} / {self.name}"


def is_imas(slug: str | None, brands: Sequence[str] = DEFAULT_BRANDS) -> bool:
    """booth slug 是不是偶像大师的品牌。

    **按 `前缀-` 匹配，不是裸 `startswith`** —— 裸匹配时 `millionlivex-…`
    会被 `millionlive` 误收。带横杠才算一个完整的品牌段。
    """
    if not slug:
        return False
    return any(slug == b or slug.startswith(b + "-") for b in brands)


def brand_of(slug: str | None) -> str:
    """取 slug 的品牌段（第一个 `-` 之前），只用于日志。"""
    return (slug or "").split("-", 1)[0]


def parse(payload: Mapping) -> list[Reception]:
    """解 JSON:API。`tour → booth` 和 `cover_image` 都在 `included` 里。"""
    records = payload.get("data") or []
    included = payload.get("included") or []

    by: dict[tuple[str, str], Mapping] = {}
    for item in included:
        if isinstance(item, Mapping) and item.get("id"):
            by[(str(item.get("type")), str(item["id"]))] = item

    def rel(obj: Mapping | None, name: str) -> Mapping | None:
        data = ((obj or {}).get("relationships") or {}).get(name) or {}
        return data.get("data") if isinstance(data, Mapping) else None

    out: list[Reception] = []
    for record in records:
        if not isinstance(record, Mapping) or not record.get("id"):
            continue
        attrs = record.get("attributes") or {}

        # 受付 --tour--> tour --booth--> booth。活动名和 slug 都在 booth 上，
        # 但**两跳都要防一手**：缺任何一跳都不能让整条炸掉。
        tour_ref = rel(record, "tour") or {}
        tour = by.get(("tour", str(tour_ref.get("id")))) or {}
        booth_ref = rel(tour, "booth") or {}
        booth = by.get(("booth", str(booth_ref.get("id")))) or {}
        booth_attrs = booth.get("attributes") or {}

        slug = str(booth_attrs.get("slug") or booth_ref.get("id") or "")
        cover_ref = rel(record, "cover_image") or {}
        image = by.get(("image", str(cover_ref.get("id")))) or {}
        urls = (image.get("attributes") or {}).get("reading_urls") or {}

        out.append(
            Reception(
                id=str(record["id"]),
                booth_slug=slug,
                booth_name=str(
                    booth_attrs.get("name") or tour.get("attributes", {}).get("name") or slug
                ),
                name=str(attrs.get("name") or "（无受付名）"),
                entry_type=str(attrs.get("entry_type") or ""),
                status=str(attrs.get("entry_period_status") or ""),
                entry_start=_dt(attrs.get("entry_period_starts_at")),
                entry_end=_dt(attrs.get("entry_period_ends_at")),
                result_at=_dt(attrs.get("result_announcement_scheduled_at")),
                deposit_start=_dt(attrs.get("deposit_period_starts_at")),
                deposit_end=_dt(attrs.get("deposit_period_ends_at")),
                cover={k: str(v) for k, v in urls.items() if isinstance(v, str)},
            )
        )
    return out


def parse_booths(payload: Mapping) -> dict[str, dict[str, str]]:
    """解 booth 列表 → `{slug: {档位: URL}}`。这就是**每场 live 的主视觉**。

    别拿 receptions 里的 `cover_image` 当封面：那是**单条受付**自己的横幅
    （同样 16:9、同样 800×450，但是每条受付一张，31 条里只有 17 条有）。
    按场一行地配图时用它，同一场挂哪张图取决于哪条受付排在最前面 ——
    既不稳定，也有一半的场一张都拿不到。
    """
    by: dict[tuple[str, str], Mapping] = {}
    for item in payload.get("included") or []:
        if isinstance(item, Mapping) and item.get("id"):
            by[(str(item.get("type")), str(item["id"]))] = item

    out: dict[str, dict[str, str]] = {}
    for booth in payload.get("data") or []:
        if not isinstance(booth, Mapping) or not booth.get("id"):
            continue
        attrs = booth.get("attributes") or {}
        slug = str(attrs.get("slug") or booth["id"])
        ref = ((booth.get("relationships") or {}).get("cover_image") or {}).get("data") or {}
        image = by.get(("image", str(ref.get("id")))) or {}
        urls = (image.get("attributes") or {}).get("reading_urls") or {}
        if urls:
            out[slug] = {k: str(v) for k, v in urls.items() if isinstance(v, str)}
    return out


def filter_imas(recs: Sequence[Reception], brands: Sequence[str] = DEFAULT_BRANDS) -> list[Reception]:
    """只留偶像大师的。**被剔掉的前缀打一条 INFO**，方便发现新品牌。"""
    kept, dropped = [], set()
    for rec in recs:
        if is_imas(rec.booth_slug, brands):
            kept.append(rec)
        else:
            dropped.add(brand_of(rec.booth_slug))
    if dropped:
        _log.info("抽票：剔掉非偶像大师的 %d 个品牌 %s", len(dropped), sorted(dropped))
    return kept


async def fetch(
    http: "httpx.AsyncClient",
    url: str = DEFAULT_API,
    *,
    etag: str | None = None,
    timeout: float = _FETCH_TIMEOUT,
) -> tuple[list[Reception], str | None, bool]:
    """拉一次。返回 `(受付列表, 新 etag, 是不是 304)`。

    **带 ETag 条件请求**：接口 814 KB，但没变时是 304 + 0 字节 + 0.47 秒，
    所以轮询频率可以给得很松。
    """
    headers = {"Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    resp = await http.get(url, headers=headers, timeout=timeout)
    if resp.status_code == 304:
        return [], etag, True
    resp.raise_for_status()
    return parse(resp.json()), resp.headers.get("etag"), False


async def fetch_booths(
    http: "httpx.AsyncClient",
    url: str = DEFAULT_BOOTHS,
    *,
    etag: str | None = None,
    timeout: float = _FETCH_TIMEOUT,
) -> tuple[dict[str, dict[str, str]], str | None, bool]:
    """拉一次 booth 列表（每场 live 的封面）。16.8 KB，**同样支持 ETag 304**。"""
    headers = {"Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    resp = await http.get(url, headers=headers, timeout=timeout)
    if resp.status_code == 304:
        return {}, etag, True
    resp.raise_for_status()
    return parse_booths(resp.json()), resp.headers.get("etag"), False


class TicketFeed:
    """带缓存的抓取器。轮询器和指令共用。

    * 轮询器用 `max_age=0` —— 每次都拿 ETag 问一次，便宜且能第一时间发现变化
    * 指令用默认的 60 秒 —— 群里有人连点 `/抽选` 时不会把接口刷爆
    """

    def __init__(self, cfg, http: "httpx.AsyncClient") -> None:
        self.cfg = cfg
        self.http = http
        self._etag: str | None = None
        self._cache: list[Reception] = []
        self._at: float = 0.0
        self._booths: dict[str, dict[str, str]] = {}
        self._booths_etag: str | None = None
        self._booths_at: float = 0.0  # 0 = 从没拉过，**空结果也算拉过**

    async def receptions(self, *, max_age: float = 60.0) -> list[Reception]:
        now = time.monotonic()
        if self._cache and now - self._at < max_age:
            return self._cache
        recs, etag, not_modified = await fetch(self.http, self.cfg.ticket_url, etag=self._etag)
        if not not_modified:
            self._etag = etag
            self._cache = recs
        self._at = now
        return self._cache

    async def imas_receptions(self, *, max_age: float = 60.0) -> list[Reception]:
        return filter_imas(await self.receptions(max_age=max_age), self.cfg.ticket_brands)

    async def covers(self, *, max_age: float = 60.0) -> dict[str, dict[str, str]]:
        """`{slug: {档位: URL}}` —— 每场 live 的封面。

        **拉失败不抛，返回上一次的结果（可能是空的）。** 封面只是加分项，
        不该因为它把整条列表指令搞挂；`cover_url` 会退到受付级封面、
        再退到纯文字。拉成功但结果为空也算「拉过了」，否则每次指令都会
        白跑一趟 16.8 KB。
        """
        mono = time.monotonic()
        if self._booths_at and mono - self._booths_at < max_age:
            return self._booths
        try:
            covers, etag, not_modified = await fetch_booths(
                self.http, booths_url(self.cfg.ticket_url), etag=self._booths_etag
            )
        except Exception as exc:  # noqa: BLE001 —— 封面拉不到不该影响列表
            _log.warning(
                "抽票：booth 封面表拉取失败，退到受付级封面 %s: %s", type(exc).__name__, exc
            )
            self._booths_at = mono
            return self._booths
        if not not_modified:
            self._booths_etag = etag
            self._booths = covers
        self._booths_at = mono
        return self._booths


# ---------- 提醒判定 ----------


def due_alerts(
    rec: Reception, now: datetime, seen: set[str]
) -> list[str]:
    """这条受付现在该发哪些提醒。返回 kind 列表，可能为空。

    kind ∈ `new` / `entry24` / `entry2` / `deposit24` / `deposit2`。

    两个刻意的设计：

    * `new` 用**服务端返回的状态**判定，不是「第一次见到这个 id」——
      受付可以先以 `before_entry_period` 露面，那时推等于推了个还没开的东西。
    * 两个提前量**从紧到松**看，进了 2 小时窗口就不再补发 24 小时那条。
      否则机器人重启或首次见到时，同一条会「24 小时 + 2 小时」连响两声。
    """
    out: list[str] = []

    # 「新受付」看**服务端返回的状态**。但状态和日期偶尔会打架（接口缓存、
    # 或我们这边的钟快了）—— 那就不推：推一条「刚开始受付」却已经截止的
    # 通知，比不推更糟。
    still_open = rec.entry_end is None or now < rec.entry_end
    if "new" not in seen and rec.status == "within_entry_period" and still_open:
        out.append("new")

    for prefix, end in (("entry", rec.entry_end), ("deposit", rec.deposit_end)):
        if end is None or now >= end:
            continue
        for hours, suffix in LEADS:
            if now >= end - timedelta(hours=hours):
                kind = f"{prefix}{suffix}"
                if kind not in seen:
                    out.append(kind)
                break
    return out


# ---------- 渲染 ----------
#
# 排版照着群友那个工具的观感来（2026-10-18 用户给的样例）：
#
#     🎫 更新于 10月18日 18:55（北京时间）
#     抽选中（8 场）
#     ![图1 #208px #117px](…)
#     1. ATHOME xR LIVE　Apple of my アイ
#     アソビストアプレミアム会員先行
#     受付期間：2026年9月28日(月) 19:30 〜 2026年10月18日(日) 22:59
#     当落发表：2026年10月31日 12:00
#     　
#     【S席・A席】アソビストアプレミアム会員先行
#     当落发表：2026年10月31日 12:00
#
# 下面几处是**样例里的写法**，别自作主张统一：
#   · 月/日**不补零**（`9月28日` 而不是 `09月28日`），时分补零
#   · 受付期間**带星期**，当落发表**不带**
#   · 同一场的多个受付之间隔一个全角空格行（`SEP`）
#   · 每场一张封面，图在该场序号**上面**
#
# ⚠️ **显示用北京时间，判定用日本时间。**
# 样例里 `2026年10月18日(日) 22:59`（北京）就是接口的 `23:59+09:00` ——
# 同一个时刻，两个都对，只是读的人在国内。所以：
#   · `now()` / `due_alerts()` / 所有比较 → **JST，别动**
#   · 只有 `_date` / `_at` / `_stamp` 这几个格式化函数走 CST
# 改这里最容易犯的错是把 `now()` 也换成 CST —— 那会让所有判定整体平移
# 一小时。smoke 里钉了「同一时刻两种时区判定一致」。

WD = "月火水木金土日"

# 显示时区。**只影响显示**，见上面那段。
CST = timezone(timedelta(hours=8))

# 同一场的多个受付之间的分隔行。样例里就是一个全角空格。
SEP = "　"

# 列表按「场」数截断，不是按受付条数 —— 一场下面可能挂好几个受付。
LIST_LIMIT = 10

# 列表里最多配几张封面 = 最多几场，也就是**每场都有图**。
#
# **不要改成 `multiimage.MAX_IMAGES`（6）** —— 那个数是「一篇资讯最多 6 张图」，
# 和 `rss.MAX_IMAGES` 对齐，是另一回事。用它卡列表的症状正是用户报的那个：
# 前 6 场有图、第 7 场往后没有，看起来就像「有些图片根本没写进文本里」。
#
# 长度由 `multiimage.MAX_CHARS` 那条安全网兜着（超了从尾部丢图，不截断语法）。
# 实测每张一行约 118 字符：/抽选 8 场 ≈ 2160 字符，/当落 4 场 ≈ 1120，
# 离 3400 的余量还远。
MAX_COVERS = LIST_LIMIT

# 每种指令要哪几节，顺序就是渲染顺序（照样例：先待当落，再抽选中）。
SECTIONS = {
    "pending": "待当落",
    "open": "抽选中",
    "deposit": "入金中",
}

# 一节都没有时的兜底话术。**按 wanted 精确匹配**，别让 /抽选 说「待当落」。
_EMPTY = {
    ("open",): "现在没有正在受付中的偶像大师抽选。",
    ("pending", "deposit"): "现在没有待当落或入金中的抽选。",
}


# 进 `**…**` 之前要剥掉的字符 —— 和 `bilibili._MD_STRIP_RE` 同一套。
# 活动名里真的出现过 `*` / `_`，不剥会把外面那对星号的配对搞乱，整行渲染成
# 原文或者半个粗体。
_MD_STRIP = re.compile(r"[*_`~\\]")


def _md(text: str) -> str:
    """清洗成可以安全放进 `**…**` 的样子。"""
    return _MD_STRIP.sub("", text or "").strip()


def _title_line(index: int, rec: Reception) -> str:
    """列表里那行场名。加粗，**链接在下一行**（见 `_link_line`）。"""
    return f"**{index}. {_md(rec.booth_name)}**"


def _link_line(rec: Reception) -> str | None:
    """场名下面那行链接，和提醒那两条用的是同一个写法。

    **没有 slug 就不加。** `tour` 关系为 null 的存量数据 slug 是空的，
    拼出来是 `…/booths/`，点了是个 404 —— 不如干脆不加。

    ⚠️ **为什么链接不挂在场名上（写成 `**[场名](url)**`）**：
    项目里**没有任何地方**在用它 —— bilibili 卡片和提醒那两条都是
    「加粗一行 + 链接单独一行」。`**加粗**` 和 `[文字](url)` 各自都实测过，
    但**套在一起没验过**，而 markdown 渲染失败在这个平台上是**静默**的
    （用户只会看到裸的 `**1. [名字](url)**`）。要改成挂在场名上，
    **先跑 `scripts/probe_mdfmt.py` 的链接 A/B 确认 ①**，再动这里。
    """
    if not rec.booth_slug:
        return None
    return f"[在 ASOBI TICKET 打开]({rec.url})"


def _hm(dt: datetime) -> str:
    return f"{dt.hour:02d}:{dt.minute:02d}"


def _date(dt: datetime, *, weekday: bool = True) -> str:
    """`2026年10月18日(日)`。月/日**不补零** —— 和站点自己的写法一致。"""
    d = dt.astimezone(CST)
    tail = f"({WD[d.weekday()]})" if weekday else ""
    return f"{d.year}年{d.month}月{d.day}日{tail}"


def _at(dt: datetime | None, *, weekday: bool = True) -> str:
    """`2026年10月18日(日) 22:59`。None 给 `?`（接口真有字段缺失的）。"""
    if dt is None:
        return "?"
    return f"{_date(dt, weekday=weekday)} {_hm(dt.astimezone(CST))}"


def _stamp(dt: datetime) -> str:
    """`10月18日 18:55` —— 只给「更新于」那一行用。"""
    d = dt.astimezone(CST)
    return f"{d.month}月{d.day}日 {_hm(d)}"


def _span(start: datetime | None, end: datetime | None) -> str:
    """`2026年9月28日(月) 19:30 〜 2026年10月18日(日) 22:59`。"""
    if start and end:
        return f"{_at(start)} 〜 {_at(end)}"
    # 接口偶尔只给一头。别硬凑成区间，会把「未知」写成「现在」。
    return _at(start or end)


def _remain(end: datetime | None, now: datetime) -> str:
    """还剩多久，给締切提醒用。"""
    if end is None:
        return "?"
    secs = (end - now).total_seconds()
    if secs <= 0:
        return "まもなく"
    minutes = int(secs // 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        # 超过 6 小时就不报分钟了，「あと 23 時間」比「あと 23 時間 47 分」干净
        return f"{hours} 時間" if hours >= 6 or not minutes else f"{hours} 時間 {minutes} 分"
    return f"{minutes} 分"


# ---------- 单条提醒（推送用）----------


def render_new(rec: Reception) -> str:
    """新受付。带全部日期，一条看完。"""
    lines = [f"🎫 **新受付 · {rec.kind_label}**", f"**{_md(rec.booth_name)}**", rec.name, ""]
    if rec.entry_start or rec.entry_end:
        lines.append(f"受付期間：{_span(rec.entry_start, rec.entry_end)}")
    if rec.result_at:
        lines.append(f"当落发表：{_at(rec.result_at, weekday=False)}")
    if rec.deposit_start or rec.deposit_end:
        lines.append(f"入金期間：{_span(rec.deposit_start, rec.deposit_end)}")
    lines.append(f"[在 ASOBI TICKET 打开]({rec.url})")
    return "\n".join(lines)


def render_deadline(rec: Reception, kind: str, now: datetime) -> str:
    """締切提醒。`kind` 形如 `entry24` / `entry2` / `deposit24` / `deposit2`。

    **尾数是 24 或 2，位数不一样** —— 所以只能把尾部的数字整体剥掉，
    不能写 `kind[:-2]`（那样 `entry2` 会解析成前缀 `entr`，静默退到入金日期，
    推出去的是一条日期错误的締切）。smoke 里有一条专门钉这个。
    """
    prefix = kind.rstrip("0123456789")
    end = rec.entry_end if prefix == "entry" else rec.deposit_end
    label = KIND_LABELS.get(prefix, "締切")
    return "\n".join(
        [
            f"⏰ **{label}まで あと {_remain(end, now)}**",
            f"**{_md(rec.booth_name)}**",
            f"{rec.name}（{rec.kind_label}）",
            f"{_at(end, weekday=False)}（北京时间）",
            f"[在 ASOBI TICKET 打开]({rec.url})",
        ]
    )


def render_alert(rec: Reception, kind: str, now: datetime) -> str:
    return render_new(rec) if kind == "new" else render_deadline(rec, kind, now)


# ---------- 列表（/抽选、/当落）----------


def _in_section(rec: Reception, which: str, now: datetime) -> bool:
    if which == "open":
        return rec.status == "within_entry_period"
    if which == "pending":
        # 申込已经关了，但结果还没出
        return bool(
            rec.result_at
            and rec.result_at > now
            and (rec.entry_end is None or rec.entry_end <= now)
        )
    # 付钱窗口开着（中了不付直接作废）
    return bool(
        rec.deposit_start and rec.deposit_end and rec.deposit_start <= now <= rec.deposit_end
    )


def _section_key(rec: Reception, which: str) -> datetime:
    when = {
        "open": rec.entry_end,
        "pending": rec.result_at,
        "deposit": rec.deposit_end,
    }[which]
    return when or datetime.max.replace(tzinfo=JST)


def _receipt_lines(rec: Reception, which: str) -> list[str]:
    """一条受付下面那几行。"""
    out = [rec.name]
    if which == "open":
        if rec.entry_start or rec.entry_end:
            out.append(f"受付期間：{_span(rec.entry_start, rec.entry_end)}")
        if rec.result_at:
            out.append(f"当落发表：{_at(rec.result_at, weekday=False)}")
    elif which == "pending":
        out.append(f"当落发表：{_at(rec.result_at, weekday=False)}")
    else:
        out.append(f"入金期間：{_span(rec.deposit_start, rec.deposit_end)}")
    return out


def _booth_groups(recs: Sequence[Reception]) -> list[list[Reception]]:
    """按场分组，保持第一次出现的次序。"""
    order: list[str] = []
    by: dict[str, list[Reception]] = {}
    for rec in recs:
        key = rec.booth_slug or rec.booth_name
        if key not in by:
            by[key] = []
            order.append(key)
        by[key].append(rec)
    return [by[k] for k in order]


def plan(
    recs: Sequence[Reception], now: datetime, wanted: Sequence[str]
) -> list[tuple[str, int, list[tuple[Reception, list[str]]]]]:
    """纯函数：每一节的（标题, 总场数, [(该场的第一条, 行)]）。

    **刻意不碰网络。** 封面尺寸要联网读，所以这里只把「代表这条」带出去，
    真正插图在 `render_digest` 里做。这样「哪场进哪节、怎么排序、日期怎么排、
    同一场的多个受付怎么合并」全都能离线断言。
    """
    out: list[tuple[str, int, list[tuple[Reception, list[str]]]]] = []
    for which in wanted:
        picked = [r for r in recs if _in_section(r, which, now)]
        if not picked:
            continue
        picked.sort(key=lambda r: _section_key(r, which))
        groups = _booth_groups(picked)
        blocks: list[tuple[Reception, list[str]]] = []
        for group in groups[:LIST_LIMIT]:
            lines: list[str] = []
            for i, rec in enumerate(group):
                if i:
                    lines.append(SEP)
                lines += _receipt_lines(rec, which)
            blocks.append((group[0], lines))
        out.append((SECTIONS[which], len(groups), blocks))
    return out


async def _size_of(
    http: "httpx.AsyncClient", url: str, cache: dict | None
) -> "tuple[int, int] | None":
    """读图片宽高，带进程内缓存。

    **只缓存成功的结果。** 曾经把 `None` 也缓存住（理由是「读不到的别每轮重试」），
    代价是一次网络抖动就让那张图在**整个进程生命周期**里永远不出现，而且没有
    任何日志。指令是用户手动触发的，多探一次的成本可以忽略。
    """
    if cache is not None and url in cache:
        return cache[url]
    wh = await multiimage.measure(http, url)
    if cache is not None and wh:
        cache[url] = wh
    return wh


async def render_digest(
    http: "httpx.AsyncClient",
    recs: Sequence[Reception],
    now: datetime,
    *,
    wanted: Sequence[str],
    size: str = DEFAULT_IMAGE_SIZE,
    images: bool = True,
    cache: dict | None = None,
    width: int = multiimage.COVER_W,
    booths: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """列表输出。`wanted` 是要哪几节，见 `SECTIONS`。

    `booths` 是 `TicketFeed.covers()` 那份 `{slug: {档位: URL}}`，每场 live
    的封面都在里面。**不传也能跑**，只是退到受付级封面（一半的场没有）。
    """
    sections = plan(recs, now, wanted)
    if not sections:
        return _EMPTY.get(tuple(wanted), "现在没有可看的抽选。")

    lines = [f"🎫 更新于 {_stamp(now)}（北京时间）"]
    used = 0  # 已经插了几张图，全局计数（`![图N …]` 的 N）
    for title, total, blocks in sections:
        if len(lines) > 1:
            lines.append("")  # 两节之间空一行，不然三节糊成一坨
        lines.append(f"**{title}（{total} 场）**")
        for i, (head, body) in enumerate(blocks, 1):
            # 图在该场序号**上面**。超出张数或正文快超长就不再插 ——
            # 图是加分项，把整条消息挤到渲染失败才是灾难。
            if (
                images
                and used < MAX_COVERS
                and len("\n".join(lines)) < multiimage.MAX_CHARS - 600
            ):
                url = cover_url(head, size, booths)
                wh = await _size_of(http, url, cache) if url else None
                if wh:
                    used += 1
                    lines.append(multiimage.image_line(used, url, wh, width))
                elif url:
                    # **必须记日志。** 之前这里是静默的，症状是「这场没图」，
                    # 和「接口本来就没给封面」长得一模一样，排查只能靠猜。
                    _log.warning("抽票：封面读不到尺寸，这场不配图 %s", url)
                else:
                    # booth 表 17/17 全有封面，所以走到这里只有两种可能：
                    # 表没拉到，或者这场是站点新加的、封面还没配。
                    _log.info(
                        "抽票：这场两个来源都没封面，不配图（%s）", head.booth_name[:40]
                    )
            lines.append(_title_line(i, head))
            lines += body
            link = _link_line(head)
            if link:
                lines.append(link)
        if total > len(blocks):
            lines.append(f"…还有 {total - len(blocks)} 场，去 ASOBI TICKET 看全部")
    return "\n".join(lines)


_IMAGE_LINE = re.compile(r"^!\[[^\]]*\]\([^)]*\)$")


def strip_images(text: str) -> str:
    """把图片行去掉。markdown 回复失败退回纯文本时用 —— 纯文本里
    `![图1 #208px #117px](…)` 是一坨没人看得懂的东西。"""
    return "\n".join(
        line for line in text.splitlines() if not _IMAGE_LINE.match(line.strip())
    )


# ---------- 封面图 ----------


def cover_url(
    rec: Reception,
    size: str,
    booths: Mapping[str, Mapping[str, str]] | None = None,
) -> str | None:
    """按档位取封面 URL。**返回的 URL 原样用，绝不加 query 参数。**

    2026-10-18 实测：给图 URL 加 `&_cb=<时间戳>` 想造一个 QQ 没见过的冷链接，
    结果**带参数的一张都不渲染、不带的全好**。QQ 的图片代理碰到多余的 query
    参数就抓不到。这个假象当时让我得出了一串错误结论，绕了十几轮。

    **优先 booth 级**（每场 live 的主视觉，17/17 全有）；`rec.cover` 是单条
    受付的横幅，只在 booth 表拉不到时兜底。
    """
    for table in ((booths or {}).get(rec.booth_slug) or {}, rec.cover):
        url = table.get(size) or table.get(DEFAULT_IMAGE_SIZE) or table.get("original")
        if url:
            return url
    return None


async def with_cover(
    http: "httpx.AsyncClient",
    rec: Reception,
    text: str,
    size: str,
    cache: dict[str, tuple[int, int] | None] | None = None,
    width: int = multiimage.COVER_W,
    booths: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """把封面图那行拼到正文**最上面**（和 `preview.py` 的 bilibili 卡片一致）。

    读不到尺寸就**丢图发纯文字** —— 不猜比例。比例猜错图会被拉伸，用户一眼
    看得出来；而且 markdown 的尺寸提示在手机端是必填的，缺了整条都不渲染。
    """
    url = cover_url(rec, size, booths)
    if not url:
        return text
    wh = await _size_of(http, url, cache)
    if not wh:
        _log.info("抽票：封面读不到尺寸，这条不发图（%s）", url)
        return text
    return "\n".join([multiimage.image_line(1, url, wh, width), text])


# ---------- 轮询器 ----------


class TicketPoller:
    """定期检查抽票变化并推送。

    和 `FeedPoller` 同形（`run` / `stop` / `poll_once`）但**不塞进那个类**：
    数据模型、轮询节奏、失败语义都不一样，而且 `FeedPoller` 已经很长了。
    """

    def __init__(self, cfg, db, http: "httpx.AsyncClient", pusher) -> None:
        self.cfg = cfg
        self.db = db
        self.http = http
        self.pusher = pusher
        self.feed = TicketFeed(cfg, http)
        self._stopped = asyncio.Event()
        self._size_cache: dict[str, tuple[int, int] | None] = {}
        self._sent_total = 0
        # 预热 / 预览 / 人工否决。语义和 `FeedPoller` 那套**完全一致**，
        # 复用同一组配置（`WARMUP_GROUP` / `PREVIEW_GROUP` / 两个秒数）。
        self._veto = asyncio.Event()
        self._preview_open = False

    @property
    def groups(self) -> tuple[str, ...]:
        """这个功能该发到哪些群。**每次重查库** —— `/set` 改完下一拍就生效。

        群开关存在库里（见 `features.py`），`.env` 里**一个群参数都没有**。
        """
        return features.groups_for(self.db, "tickets")

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.ticket_alert and self.groups)

    def stop(self) -> None:
        self._stopped.set()

    def veto_current(self) -> bool:
        """人工喊「不发」。**只在预览窗口内有效**，和 `FeedPoller.veto_current` 同义。

        窗口外一律返回 False —— 否则你随口一句「不发」会把下一条提醒吞掉，
        而且日志上完全看不出原因。
        """
        if not self._preview_open:
            return False
        self._veto.set()
        return True

    async def _wait(self, sec: float) -> None:
        """等 `sec` 秒，但**能被 `stop()` 打断** —— 不然停机要干等满 45 秒。"""
        if sec <= 0:
            await asyncio.sleep(0)
            return
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=sec)
        except asyncio.TimeoutError:
            pass

    async def _try_send(self, group: str, text: str, label: str) -> bool:
        """发一条，失败只记日志不抛 —— 和 RSS 那边「一条挂了别中断整轮」一致。"""
        try:
            await self.pusher.send_markdown(group, text)
            return True
        except Exception as exc:  # noqa: BLE001
            _log.warning("抽票：发送失败 %s → %s: %s", label, group[:8], exc)
            return False

    async def _deliver(self, text: str, groups: Sequence[str], label: str) -> list[str] | None:
        """预热群 → 预览群（+ 喊停窗口）→ 正式群。**三段必须串行。**

        和 RSS 那条路的唯一区别是组织单位：那边按「订阅」，这里按
        **一条提醒消息**（每条自带一张封面，只能各自预热）。

        返回**发成功的正式群**；被人工否决时返回 None。
        """
        cfg = self.cfg
        warm, prev = cfg.warmup_group, cfg.preview_group

        # ① 预热群。两个群配成同一个时跳过 —— 那样预览那条本身就是预热。
        if warm and warm not in groups and warm != prev:
            if await self._try_send(warm, text, f"预热/{label}"):
                _log.info("抽票：预热已发，等 %d 秒让 QQ 把封面抓完", cfg.preview_warmup_sec)
                await self._wait(cfg.preview_warmup_sec)
                if self._stopped.is_set():
                    return []

        # ② 预览群 + 人工否决窗口
        if prev and prev not in groups:
            self._veto.clear()
            self._preview_open = True
            try:
                if await self._try_send(prev, text, f"预览/{label}"):
                    _log.info("抽票：预览已发，等 %d 秒看你喊不喊停", cfg.preview_veto_sec)
                    try:
                        await asyncio.wait_for(
                            self._veto.wait(), timeout=cfg.preview_veto_sec
                        )
                    except asyncio.TimeoutError:
                        pass
                    else:
                        _log.info("抽票：收到「不发」，这条不进正式群（%s）", label)
                        return None
            finally:
                self._preview_open = False
            if self._stopped.is_set():
                return []

        # ③ 正式群。**逐个发、逐个记**：一个群失败不该连累其它群。
        return [g for g in groups if await self._try_send(g, text, label)]

    async def run(self) -> None:
        # ⚠️ **这里不能 return。** 群开关在库里（`features.py`），`/set` 的语义是
        # 「改完下一拍就生效」—— 启动时库里一个群都没开的话，一 return 这个 task
        # 就**永久结束**了：之后再怎么 `/set` 开群都不会跑，只能重启容器。
        # 留着轮询不花什么钱 —— `poll_once` 每拍开头 `if not self.enabled: return 0`。
        if not self.enabled:
            _log.info("抽票提醒：库里没有开着的群，功能关闭（**仍然轮询**，/set 开了下一拍生效）")
        else:
            _log.info(
                "抽票提醒：已启用，推送到 %d 个群，每 %ss 检查一次",
                len(self.groups),
                self.cfg.ticket_check_sec,
            )
        while not self._stopped.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 接口挂了只写日志。**不往群里报错** —— 和抓 RSS 失败的语义一致，
                # 而且抽票是锦上添花，不该因为它把群搅得不安宁。
                _log.exception("抽票：本轮检查异常，跳过")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.cfg.ticket_check_sec)
            except asyncio.TimeoutError:
                pass
        _log.info("抽票提醒已停止")

    async def poll_once(self) -> int:
        """跑一轮，返回这轮发出去的条数。"""
        if not self.enabled:
            return 0

        recs = await self.feed.imas_receptions(max_age=0.0)
        if not recs:
            return 0
        # 封面表每轮拉一次（同样走 ETag，没变就是 304 + 0 字节）。
        # **只在这轮真有东西要发的时候才用得上**，但先拿好省得在循环里 await。
        booths = await self.feed.covers(max_age=0.0) if self.cfg.ticket_images else {}

        now = datetime.now(JST)

        # **先算完整轮要发什么，再逐条发。** 不能像原来那样在「群 × 受付 × kind」
        # 的三层循环里直接发 —— 预热/预览是按**一条消息**组织的，同一条提醒
        # 会被预热好几次（每个正式群一次），而且三阶段会被拆散成并发的。
        seen = {g: self.db.ticket_alert_kinds(g) for g in self.groups}

        # ⚠️⚠️ **baseline 判据必须按群，不能按全表。**
        #
        # 「这个群一行记录都没有」= 它还没建过 baseline → 把当前正在受付的全部
        # 记成「已推过」，**一条 `new` 都不发**。存量 31 条受付（14 条 IM@S），
        # 直接跑会当场倒灌十几条进群。
        #
        # 以前判的是 `ticket_alerts_any()`（**整个表**有没有行）。那个判据在
        # 「只有第一个群」时看起来对，但线上踩到了：正式群 10-18 就有了记录，
        # 2026-10-21 用 `/set` 把抽票在新群打开 → 那个群的 `seen` 是空的 →
        # **几天前就推过的 5 条旧受付当成「新受付」连发进群**。
        # 表里只要有别的群的记录，这个群就永远建不了 baseline。
        #
        # 按群判之后：新开的群**不补发存量**，但締切提醒（24h / 2h）照常 ——
        # 那些是按截止时刻算的，不受 baseline 影响，所以新群不会「一片空白」。
        #
        # ⚠️ **这里不 return**（同上）：留在 24h / 2h 窗口里的締切该推就推。
        seeded_groups = [g for g in self.groups if not seen[g]]
        if seeded_groups:
            seeded = 0
            for group in seeded_groups:
                for rec in recs:
                    if rec.status == "within_entry_period":
                        self.db.mark_ticket_alert(rec.id, group, "new")
                        seen[group].setdefault(rec.id, set()).add("new")
                        seeded += 1
            _log.info(
                "抽票：%d 个群首次启用，存量 %d 条受付标记为已见（不补发 new）",
                len(seeded_groups),
                seeded,
            )
        pending: list[tuple[Reception, str, list[str]]] = []
        for rec in recs:
            groups_for: dict[str, list[str]] = {}
            for group in self.groups:
                for kind in due_alerts(rec, now, seen[group].get(rec.id, set())):
                    groups_for.setdefault(kind, []).append(group)
            for kind, groups in groups_for.items():
                pending.append((rec, kind, groups))

        sent = 0
        for rec, kind, groups in pending:
            if self._stopped.is_set():
                break
            text = render_alert(rec, kind, now)
            if self.cfg.ticket_images:
                text = await with_cover(
                    self.http,
                    rec,
                    text,
                    self.cfg.ticket_image_size,
                    self._size_cache,
                    self.cfg.ticket_image_w,
                    booths,
                )
            label = f"{kind}/{rec.title[:40]}"
            delivered = await self._deliver(text, groups, label)

            if delivered is None:
                # 被「不发」挡下：**照着已推处理**。不记的话下一轮又捞出来，
                # 你得每轮否一次。和 RSS 那边同一条规矩。
                for group in groups:
                    self.db.mark_ticket_alert(rec.id, group, kind)
                continue

            # 发失败就**不记账**，下一轮会重试 —— 和 RSS「先发再标记已见」一致。
            for group in delivered:
                self.db.mark_ticket_alert(rec.id, group, kind)
                _log.info("抽票：已推送 %s → %s（%s）", kind, group[:8], rec.title[:50])
            sent += len(delivered)

        if sent:
            self._sent_total += sent
        return sent
