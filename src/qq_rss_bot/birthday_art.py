"""誕生祭 贺图聚合 —— **每天定时把推特上带生日 tag 的高赞贺图收进群**。

偶像生日那天，粉丝和画师会带 `#杜野凛世誕生祭2026` 这种 tag 发贺图。
每天定时搜这个话题，把点赞高的挑出来，**一张一条**发到群里（最多 6 条）。

## ⚠️ 数据源：带 cookie 打 X 的 GraphQL —— 四条路里只有一条通

2026-10-19 实测（本机直连）：

| 传输 | 结果 |
|---|---|
| `GET x.com/search?q=…&f=live`（**未登录**） | ❌ 307 → 登录引导页，0 条推文 |
| `GET x.com/search?q=…&f=live`（**带 cookie**） | ⚠️ 200 / 307 KB，但**HTML 里一条推文都没有** —— 登录态只有 JS 壳，推文全靠客户端拉 |
| `GET x.com/i/api/graphql/<id>/SearchTimeline`（带 cookie） | ❌ **404 空响应** |
| **`POST` 同一个地址**（带 cookie） | ✅ **200**，20 条推文 + 点赞数 + 图 |

⚠️ **决定成败的是 POST，不是那几个头。** GET 无论加什么头（Referer /
Origin / language / 甚至补一个假的 `x-client-transaction-id`）**全是 404**；
POST 用最朴素的头就通。网上流传的示例几乎清一色是 GET，所以这一条
值得单独记一笔 —— 当时为此做了一整轮对照实验。

⚠️ **`features` 传 `{}` 就行。** X 自己的 JS 里每个操作都挂着一长串
`featureSwitches`，但实测传空对象**照样 200**。所以不用去抠那 40 个开关，
要跟着发版变的只有 `queryId` 一个东西。

⚠️ **`queryId` 随前端发版轮换，绝不能写死。** 从 `/home` 引用的
`main.<hash>.js` 里**自愈式发现**（`discover_query_id`），进程内缓存；
万一 bundle 里找不到，还有 `X_SEARCH_QUERY_ID` 手工兜底。

⚠️ **`queryId` 的匹配必须写成 `queryId:"…",operationName:"SearchTimeline"`
这个紧邻形态。** 松匹配会抓错：`SearchTimeline` 前面紧挨着的是
`BookmarkSearchTimeline`，把它的 id 拿来用会得到同样的 **404 空响应** ——
和「方法不对」长得一模一样，很难分辨。

## ⚠️ 两种 tag 写法是**两批人**，必须都搜

实测 `杜野凛世` 生日当天：

| tag | 唯一推文 | 与前者重叠 |
|---|---|---|
| `#杜野凛世誕生祭2026` | 40 | —— |
| `#杜野凛世生誕祭2026` | 40 | **只有 8 条** |

只搜一种会漏掉整整一半。

## ⚠️⚠️ `product` 必须是 **`"Top"`** —— 这是这个功能最大的一个坑

GraphQL 的 `variables.product` 就是网页上那两栏。**两栏的结果差一个数量级：**

    2026-10-19 杜野凛世，同一个 cookie、同一个 queryId：

      #杜野凛世誕生祭2026   Top     39 条  中位 459   max 8591   ≥2000 的 6 条
      #杜野凛世誕生祭2026   Latest  34 条  中位  11   max 1119   ≥2000 的 0 条
      #杜野凛世生誕祭2026   Top     38 条  中位 782   max 8591   ≥2000 的 8 条
      #杜野凛世生誕祭2026   Latest  35 条  中位   5   max  240   ≥2000 的 0 条

**「最新」是按时间倒序，不是按热度。** 生日当天有成百上千条推，最新那 60 条
全是深夜发的小号；而点赞最高的那批大多发在 **00:00 JST**（生日刚开始那一刻，
官推和头部画师一起发），按时间倒序要翻一整天的量才够得着 —— `count=20` 翻
3 页根本翻不到。**我第一版就是用 Latest，于是得出「当天最高才 464、2k 筛成空」
的错误结论，把用户「2k 以上」的判断当成了错的。** 用户是对的。

Top 那一栏**翻两页就到底**（实测第 3 页没有游标），所以 `PAGE_PLAN` 是
`("Top", "Top", "Latest")` —— Top 出主力，Latest 补最近一小时的漏网。

⚠️ **两种栏的游标不能混用**，得各存各的（见 `search_tag`）。

## 挑选规则：不是「过门槛」，是「排序取前 N」

即便走 Top，一个冷门偶像的池子也可能只有几十赞。所以真正起作用的是
「**按点赞排序取前 6**」，`BIRTHDAY_ART_MIN_LIKES`（默认 50）只是
「别发一堆 0 赞垃圾」的兜底 —— Top 第二页实测能到 min=0。

想让冷门偶像一张都不发就往上调（调 2000 就是用户最初说的那个门槛，
但**只有热门 idol 撑得住**）；想「有多少发多少」就调到 0。

## ⚠️ 「被庆祝的那天」= slot **当天**，不是 +1

`birthday.py` 那条最要命的规则在这里**正好相反**：

| | 取哪天 |
|---|---|
| `birthday.py`（23:05 推**第二天**的名单） | `slot.date() + 1` |
| **这里**（22:30 收**当天**的贺图） | **`slot.date()`** |

**绝不要 import `birthday.birthday_date` 来用** —— 两个函数长得像、差一天，
而日志上完全看不出来（tag 里的年份也对得上，只是整体错位一天）。

## ⚠️ 不带年份的 tag 会把**往年**的贺图搜出来

`#杜野凛世誕生祭` 没有年份，2021 年的作品照样命中。所以结果**统一按推文时间
过滤**到 `[D-1, D+1]`（JST），带年份的也一起过 —— 一条规则比两条好维护，
而且「提前画好的」和「迟到补的」都能收进来。

## ⚠️ 代理与凭据

容器直连 `x.com` / `pbs.twimg.com` 全不通，**必须走 `MEDIA_PROXY`**，
客户端复用 `bot.py` 里那个 `media_http`（`preview.make_client` 建的），
别另起一个。图片也一样：**下载字节再 base64 上传**，不能把
`pbs.twimg.com` 的 URL 交给 QQ 去抓（QQ 的机房在国内，够不到那个图床）。

`auth_token` / `ct0` 是**真实凭据**：只从 `.env` / 环境变量读，**绝不打印**
（日志里只出现长度）、**绝不提交**（仓库是公开的）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

import httpx

from . import birthday as birthday_module
from . import features
from . import imagelib
from . import multiimage
from . import preview
from . import seiyuu
from .tickets import JST

_log = logging.getLogger("qq_rss_bot.birthday_art")

# 用户原本定的是 12:00，改了：贺图的发布高峰是**日本时间当天的傍晚到深夜**
# （= 北京 17:00–23:00），北京中午去搜只捞得到当天上午那批，画师还没画完。
# 22:30 北京 = **23:30 JST**，生日当天基本收尾。改回去只要改这一个变量。
DEFAULT_TIME = "22:30"
# 搜索失败（网络抖、被限流）的**重试窗口**，窗口内每拍重试。
# 和生日那条不同：那边是等 imas_ch 发推，这边内容早就躺在那儿了，纯粹是重试。
DEFAULT_WINDOW_SEC = 300
CHECK_SEC = 60
DEFAULT_MAX = 6
# 「别发一堆 0 赞垃圾」的兜底 —— **真正起作用的是「按点赞取前 6」**。
# 见模块说明：调高到 2000 就是用户最初说的门槛，但只有热门 idol 撑得住。
DEFAULT_MIN_LIKES = 50
# ⚠️ **`"Top"` 是热度栏，`"Latest"` 是时间栏** —— 差一个数量级，别写反。
# 每个 tag 要抓的页序。Top 两页就到底（第 3 页没游标），Latest 只补最近一小时的漏网。
# 两种栏的游标不能混用，`search_tag` 里是各存各的。
PAGE_PLAN_YEARFUL: tuple[str, ...] = ("Top", "Top", "Latest")
# 不带年份的 tag 基本是白翻 —— 实测 `#杜野凛世誕生祭` 抓 37 条、**窗口内 0 条**
# （Top 全是往年作品）。留一页 Latest 只为兜住「不加年份」的那批人。
PAGE_PLAN_BARE: tuple[str, ...] = ("Latest",)
# 一次运行的请求数上限（6 位偶像 × 4 tag × 3 页 = 72 是极端情况，只有 11-11 会接近）。
MAX_REQUESTS = 80
SEARCH_GAP_SEC = 1.2
SEARCH_TIMEOUT = 25.0
# 时间窗前后各留一天，容纳提前画好 / 迟到补发的作品。
ART_DAY_SLACK = 1
# ⚠️ **官方号不是粉丝贺图，别占名额。** 两条规则，都能配：
#
# 1. 显示名里含 `OFFICIAL_NAME_MARKS` —— **这是主力**。官方号会新增、会改
#    handle，但名字里那句「公式」跑不掉。实测 2026-10-19 排第一的
#    `@imassc_official` 显示名就是「アイドルマスター シャイニーカラーズ**公式**」。
# 2. handle 在这张表里 —— 兜底。名字里不带「公式」的官方号只能靠这个。
#
# 排除掉的那个不占「前 6」的名额，后面按点赞顺位补上来。
OFFICIAL_NAME_MARKS: tuple[str, ...] = ("公式",)
OFFICIAL_HANDLES: tuple[str, ...] = (
    "imassc_official",  # シャイニーカラーズ公式（实测撞到过）
    "imas_official",    # アイドルマスター公式
    "imas_ch",          # アイマスch（生日推送那条链路用的也是它）
)
# 名额分配：**官号 1 + 本人/声优 1 + 画师 4 = 6**（总数仍由 BIRTHDAY_ART_MAX 控制）。
# 某一类当天没有合格作品，空出来的名额**回流给画师** —— 总数不会因此变少。
# 「本人」和「声优」是同一格（vα-liv 那种角色自己开号，没有另一个配音的人）。
OFFICIAL_QUOTA = 1
PERSON_QUOTA = 1

# 每位画师最多几张。实测 79 条里 77 个不同作者，这个上限平时咬不到，
# 但热门 idol 会出现一个高产画师刷屏的情况。
MAX_PER_AUTHOR = 1
# 一条推文只取第一张图（多图推文实测占 30/79）。6 条 = 最多 6 位不同画师，
# 比 1 位画师占 4 条有价值。
IMAGES_PER_TWEET = 1
# 下载候选的倍数：排前 6 的图可能有下不动的，往后多试几张补位。
DOWNLOAD_LOOKAHEAD = 3

TAG_SUFFIXES = ("誕生祭", "生誕祭")
X_HOME_URL = "https://x.com/home"
X_SEARCH_OP = "SearchTimeline"
GRAPHQL_URL = "https://x.com/i/api/graphql/{qid}/" + X_SEARCH_OP
# X 网页版自己 JS 里写死的公开 bearer —— **不是我们的凭据**，网上到处都是。
WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
BUNDLE_RE = re.compile(r"https://abs\.twimg\.com/responsive-web/client-web[^\"'\\ ]+\.js")
# ⚠️ 紧邻形态，见模块说明 —— 松匹配会抓到 BookmarkSearchTimeline 的 id。
_OP_RE = re.compile(
    r'queryId:"([A-Za-z0-9_-]{18,30})",operationName:"%s"' % X_SEARCH_OP
)
# 未登录 / cookie 失效时会被踢到这里。
ONBOARD_MARK = "i/jf/onboarding"
_TWEET_TIME_FMT = "%a %b %d %H:%M:%S %z %Y"


# ---------- 时间 ----------


def parse_time(raw: str) -> time:
    """`HH:MM` → `time`。写错就 `SystemExit`（启动时就该炸，别等到晚上）。"""
    try:
        hh, mm = raw.strip().split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError) as exc:
        raise SystemExit(f"BIRTHDAY_ART_TIME 必须是 HH:MM，当前值：{raw!r}") from exc


def slot_for(now: datetime, at: time | None = None) -> datetime:
    """`now` 之前最近的一次 `at`。和 `birthday.slot_for` 同构。"""
    at = at or parse_time(DEFAULT_TIME)
    slot = datetime.combine(now.date(), at)
    if slot > now:
        slot -= timedelta(days=1)
    return slot


def art_date(slot: datetime) -> Date:
    """推送时刻 → **被庆祝的那天**。

    ⚠️ **就是 `slot.date()`，不是 +1。** `birthday.py` 那边要 +1（23:05 推的是
    第二天的名单），这边 22:30 收的**就是当天**的贺图。两者差一天，而且写错了
    从日志上完全看不出来 —— 名单和推文取自同一份数据，只是整体错位。
    """
    return slot.date()


def window_bounds(d: Date) -> tuple[datetime, datetime]:
    """`[D-1 00:00 JST, D+2 00:00 JST)` —— 判定用 JST（生日本来就是日本日历的事）。"""
    lo = datetime.combine(d - timedelta(days=ART_DAY_SLACK), time.min, tzinfo=JST)
    hi = datetime.combine(d + timedelta(days=ART_DAY_SLACK + 1), time.min, tzinfo=JST)
    return lo, hi


def in_window(posted_at: datetime, d: Date) -> bool:
    lo, hi = window_bounds(d)
    return lo <= posted_at < hi


# ---------- 标签 ----------


def tags_for(name: str, year: int) -> list[str]:
    """一位偶像的四个 tag。带年份的排前面（结果更干净、翻页更值）。"""
    return [f"#{name}{s}{year}" for s in TAG_SUFFIXES] + [
        f"#{name}{s}" for s in TAG_SUFFIXES
    ]


def is_yearful(tag: str, year: int) -> bool:
    return tag.endswith(str(year))


# ---------- 推文 ----------


@dataclass(frozen=True)
class Art:
    """一条候选贺图。`image` 是**原图** URL（`?name=orig`）。"""

    id: str
    idol: str
    author: str
    author_name: str
    likes: int
    posted_at: datetime
    image: str
    sensitive: bool = False

    @property
    def url(self) -> str:
        return f"https://x.com/{self.author}/status/{self.id}"


def _with_name(url: str, name: str) -> str:
    """把 `?name=orig` 换成 `?name=large` 之类。"""
    return re.sub(r"([?&])name=[a-z]+", rf"\g<1>name={name}", url)


def _orig(url: str) -> str:
    """强制原图。和 `preview.py` 那条产品要求一致（推文的图必须无损）。"""
    if not url:
        return url
    if "name=" in url:
        return url
    return f"{url}{'&' if '?' in url else '?'}name=orig"


def _author_of(tweet: dict[str, Any]) -> tuple[str, str]:
    """屏幕名 / 显示名。**新版在 `core` 下，老版在 `legacy` 下**，两边都试。"""
    user = (
        tweet.get("core", {}).get("user_results", {}).get("result", {})
        or tweet.get("user_results", {}).get("result", {})
        or {}
    )
    core = user.get("core") or {}
    legacy = user.get("legacy") or {}
    return (
        core.get("screen_name") or legacy.get("screen_name") or "",
        core.get("name") or legacy.get("name") or "",
    )


def parse_tweet(tweet: dict[str, Any], idol: str) -> Art | None:
    """一条 GraphQL 结果 → `Art`。不合格返回 `None`（**不是抛**）。"""
    if not tweet:
        return None
    # 可见性受限的推文外面套了一层，得拆开；tombstone / unavailable 直接跳。
    if tweet.get("__typename") == "TweetWithVisibilityResults":
        tweet = tweet.get("tweet") or {}
    legacy = tweet.get("legacy") or {}
    raw_time = legacy.get("created_at")
    if not raw_time:
        return None
    try:
        posted = datetime.strptime(raw_time, _TWEET_TIME_FMT)
    except ValueError:
        return None
    media = (legacy.get("extended_entities") or {}).get("media") or []
    photos = [m for m in media if m.get("type") == "photo" and m.get("media_url_https")]
    if not photos:
        return None
    author, author_name = _author_of(tweet)
    return Art(
        id=str(legacy.get("id_str") or tweet.get("rest_id") or ""),
        idol=idol,
        author=author,
        author_name=author_name,
        likes=int(legacy.get("favorite_count") or 0),
        posted_at=posted,
        image=_orig(photos[0]["media_url_https"]),
        sensitive=bool(legacy.get("possibly_sensitive")),
    )


def parse_search(payload: dict[str, Any], idol: str) -> tuple[list[Art], str | None]:
    """一页搜索结果 → `(推文列表, 下一页游标)`。"""
    instructions = (
        payload.get("data", {})
        .get("search_by_raw_query", {})
        .get("search_timeline", {})
        .get("timeline", {})
        .get("instructions", [])
    )
    arts: list[Art] = []
    cursor: str | None = None
    for inst in instructions:
        for entry in inst.get("entries", []):
            content = entry.get("content", {})
            if content.get("cursorType") == "Bottom":
                cursor = content.get("value")
            result = content.get("itemContent", {}).get("tweet_results", {}).get("result")
            art = parse_tweet(result or {}, idol)
            if art is not None:
                arts.append(art)
    return arts, cursor


# ---------- 挑选 ----------


def is_official(
    art: Art, *, exclude: Iterable[str] = (), marks: tuple[str, ...] = OFFICIAL_NAME_MARKS
) -> bool:
    """是不是官方号。**官方号发的是告知，不是贺图，不给它占名额。**

    handle 一律小写比较（X 的 handle 大小写不敏感，但各家写法不一）。
    """
    handle = art.author.lower()
    if handle in OFFICIAL_HANDLES:
        return True
    if handle in {e.strip().lower().lstrip("@") for e in exclude if e.strip()}:
        return True
    return any(m in art.author_name for m in marks)


def make_classifier(
    book: seiyuu.Book | None, *, exclude: Iterable[str] = ()
):
    """→ `Art -> (类别, 标签)`。**整个改动唯一的新接缝**（测试里换成假实现）。

    库优先；库不认的再退回「显示名含『公式』」这个关键词兜底 —— 新官号随时
    会出现，库不可能永远最新。库读不到时（`book is None`）只剩关键词这一条。
    """

    def classify(art: Art) -> tuple[str, str]:
        category, label = seiyuu.classify(book, art.author, art.idol)
        if category != seiyuu.CATEGORY_ARTIST:
            return category, label
        if is_official(art, exclude=exclude):
            return seiyuu.CATEGORY_OFFICIAL, seiyuu.LABEL_OFFICIAL
        return seiyuu.CATEGORY_ARTIST, seiyuu.LABEL_ARTIST

    return classify


def select(
    arts: Iterable[Art],
    idols: list[str],
    *,
    min_likes: int,
    max_n: int = DEFAULT_MAX,
    per_author: int = MAX_PER_AUTHOR,
    exclude: Iterable[str] = (),
    skip_official: bool = False,
    classify: Any = None,
) -> tuple[list[Art], list[Art]]:
    """→ (每个偶像的前 `max_n` 条, 其余补位用的候选)。

    名额**按偶像各算一份**：每个偶像自己 1 官号 + 1 本人/声优 + 4 画师，
    某类不够就让给画师。一天两位偶像 = 两条消息，互不抢名额。
    顺序 = 官号 → 本人/声优 → 画师；同类别内点赞降序、同分按 id。

    `skip_official=True` 是配置里的逃生门：官方号**整个**不要（不是「不占名额」）。
    """
    def _classify(a: Art) -> tuple[str, str]:
        if classify is not None:
            return classify(a)
        if is_official(a, exclude=exclude):
            return seiyuu.CATEGORY_OFFICIAL, seiyuu.LABEL_OFFICIAL
        return seiyuu.CATEGORY_ARTIST, seiyuu.LABEL_ARTIST

    keep: list[Art] = []
    seen: set[str] = set()
    for a in arts:
        if a.id in seen:
            continue
        seen.add(a.id)
        if a.sensitive or a.likes < min_likes or not a.image:
            continue
        if skip_official and is_official(a, exclude=exclude):
            _log.info("贺图：@%s（%s）是官方号，按配置跳过", a.author, a.author_name)
            continue
        keep.append(a)
    keep.sort(key=lambda a: (-a.likes, a.id))  # 同分按 id → 结果确定，可复现

    # ⚠️ 名额是**按偶像各算一份**（每个偶像自己 1+1+4）。
    # 一天两位偶像 → 两条消息，各自独立，不互相抢名额。
    budget: dict[str, int] = {}  # 每位作者全天只出现一次（跨偶像也成立）

    def pick(candidates: list[Art]) -> tuple[list[Art], list[Art]]:
        buckets: dict[str, list[Art]] = {
            seiyuu.CATEGORY_OFFICIAL: [],
            seiyuu.CATEGORY_PERSON: [],
            seiyuu.CATEGORY_ARTIST: [],
        }
        for a in candidates:
            if budget.get(a.author, 0) >= per_author:
                continue
            budget[a.author] = budget.get(a.author, 0) + 1
            buckets[_classify(a)[0]].append(a)

        # ⚠️ **本人优先于声优**：两者抢同一格，本人才是过生日的那个人，
        # 哪怕赞少很多。
        buckets[seiyuu.CATEGORY_PERSON].sort(
            key=lambda a: (0 if _classify(a)[1] == seiyuu.LABEL_CHARACTER else 1,
                           -a.likes, a.id))

        off = buckets[seiyuu.CATEGORY_OFFICIAL]
        per = buckets[seiyuu.CATEGORY_PERSON]
        art = buckets[seiyuu.CATEGORY_ARTIST]
        off_n = min(len(off), OFFICIAL_QUOTA)
        per_n = min(len(per), PERSON_QUOTA)
        # ⚠️ 回流就发生在这行：某一类空出来的名额自动落到画师头上。
        art_n = max(0, max_n - off_n - per_n)
        head = off[:off_n] + per[:per_n] + art[:art_n]
        tail = art[art_n:] + off[off_n:] + per[per_n:]
        return head, tail

    head_all: list[Art] = []
    tail_all: list[Art] = []
    for idol in idols:
        group = [a for a in keep if a.idol == idol]
        if not group:
            continue
        head, tail = pick(group)
        head_all += head
        tail_all += tail
    # 只把**不属于任何偶像**的候选放到最后 —— 被「每位作者一张」挡掉的那些
    # 不能因为没进 buckets 就又被捡回来（那样上限形同虚设）。
    known = set(idols)
    return head_all, tail_all + [a for a in keep if a.idol not in known]


def rank_art(arts: Iterable[Art], idols: list[str], **kwargs: Any) -> list[Art]:
    """平铺版（探针和测试用）：前段是要发的，后段是补位。"""
    head, tail = select(arts, idols, **kwargs)
    return head + tail


def _likes(n: int) -> str:
    """`3200` → `3.2k`。小于 1000 原样。"""
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k".replace(".0k", "k")


_STATUS_ID_RE = re.compile(r"/status/(\d+)")


def _id_of(text: str) -> str:
    """从文案 / URL 里取回推文 id —— 记账需要知道**到底哪几条发出去了**。

    不锚定行尾：同一个函数既要能吃纯 URL，也要能吃 markdown 里的链接
    （`[…](https://x.com/…/status/123)`，后面还跟着括号）。
    """
    m = _STATUS_ID_RE.search(text)
    return m.group(1) if m else ""


# markdown 里一张图的显示宽度。6 张挤在一条里，320 差不多一屏半。
MARKDOWN_IMAGE_W = 320
# 长条图（高度超过这个）缩小**显示宽度**，但比例一个字都不动。
MARKDOWN_IMAGE_MAX_H = 640


def display_size(w: int, h: int) -> tuple[int, int]:
    """→ (显示宽, 显示高)，**比例和原图严格一致**。

    ⚠️ 别拿 `multiimage.MAX_H`（400）去夹高度。那个常数是给 208 宽的资讯多图
    定的，320 宽下 2:3 的插图画出来是 480，一夹就被拉扁 ——
    2026-10-22 实测就是这样：6 张里 2 张变成 `#320px #400px`，肉眼一看比例就不对。
    **比例永远不动，只动缩放**：太长的图缩窄显示宽度。
    """
    if w <= 0 or h <= 0:
        return MARKDOWN_IMAGE_W, MARKDOWN_IMAGE_MAX_H
    width = MARKDOWN_IMAGE_W
    height = max(1, round(width * h / w))
    if height > MARKDOWN_IMAGE_MAX_H:
        height = MARKDOWN_IMAGE_MAX_H
        width = max(1, round(height * w / h))
    return width, height


def render_markdown(
    idol: str, items: list[tuple[Art, bytes, str, str]], year: int
) -> str:
    """**一个偶像一条 markdown**：正文里放 N 张图，每张下面是署名。

    `items` 是 `(Art, 图片字节, 标签, 图片 URL)`。宽高必须逐张读真实值 ——
    QQ 的尺寸提示**必填**（手机端不带就整条不渲染），而且**比例错了图会被拉伸**。
    读不出尺寸的那张直接跳过（宁可不放，也不放一张被压扁的）。

    ⚠️ `url` **必须是 QQ 抓得到的地址** —— 也就是自建图床那条（见 `imgsrv.py`）。
    推特的 `pbs.twimg.com` 在大陆机房够不到，预热多久都不会显示。

    ⚠️ 图片 URL 原样给，**绝不加 `_cb` 之类的参数** —— 加了 QQ 的图片代理抓不到。
    """
    lines = [f"🎨 **{idol}** 誕生祭 {year}"]
    for art, raw, label, url in items:
        size = multiimage.image_size(raw)
        if size is None:
            _log.info("贺图：%s 的图读不出宽高，markdown 里跳过", art.id)
            continue
        width, height = display_size(*size)
        lines.append(f"![{label} #{width}px #{height}px]({url})")
        lines.append(f"[♥ {_likes(art.likes)} · {label} @{art.author}]({art.url})")
    return "\n".join(lines)


def render(art: Art, index: int, total: int, year: int, *,
           label: str = seiyuu.LABEL_ARTIST) -> str:
    """一条贺图的文案。**署名和原推链接是必须的** —— 既是礼节，
    也让群里看到喜欢的那张能直接找到人。

    `label` 是类别词：`公式` / `本人` / `声優` / `画师`（见 seiyuu.py）。
    """
    return (
        f"🎨 {art.idol} 誕生祭 {year}（{index}/{total}）\n"
        f"♥ {_likes(art.likes)} · {label} @{art.author}\n"
        f"{art.url}"
    )


# ---------- GraphQL 传输 ----------


def _api_headers(auth: str, ct0: str) -> dict[str, str]:
    """⚠️ **这套头只发给 `/i/api/*`。** 往 `/home` / `/search` 这类 HTML 页面上
    发 `authorization: Bearer` + `x-twitter-auth-type` 会吃 **401 空响应**。
    """
    return {
        "Cookie": f"auth_token={auth}; ct0={ct0}",
        "x-csrf-token": ct0,
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "authorization": f"Bearer {WEB_BEARER}",
        "content-type": "application/json",
    }


def _page_headers(auth: str, ct0: str) -> dict[str, str]:
    return {"Cookie": f"auth_token={auth}; ct0={ct0}"}


def extract_query_id(js: str) -> str | None:
    """从 bundle 文本里抠 `SearchTimeline` 的 queryId。见模块说明的匹配要求。"""
    m = _OP_RE.search(js)
    return m.group(1) if m else None


class XSearch:
    """带 cookie 的 X 话题搜索。**唯一出口**，别在别处再拼 GraphQL 请求。"""

    def __init__(self, client: httpx.AsyncClient, auth: str, ct0: str) -> None:
        self._http = client
        self._auth = auth
        self._ct0 = ct0
        self._query_id: str | None = None
        self._requests = 0
        # 识别到登录墙时置位，让轮询器去私聊告警（**别在这里发通知**）。
        self.cookie_dead = False

    @property
    def configured(self) -> bool:
        return bool(self._auth and self._ct0)

    @property
    def requests_made(self) -> int:
        return self._requests

    def reset(self) -> None:
        """**每轮开始时清零。**

        ⚠️ `MAX_REQUESTS` 是**每轮**的上限，不是进程生命周期的 ——
        `XSearch` 是在 `__init__` 里建一次、一直复用的，不清零的话跑几天就会
        撞到上限，然后**永远不再搜**（而日志上只是「已达请求上限」，
        看起来像今天流量特别大）。
        """
        self._requests = 0
        self.cookie_dead = False

    async def _discover_query_id(self) -> str | None:
        """⚠️ `queryId` 随前端发版轮换，所以是**发现**出来的，不是写死的。"""
        if self._query_id:
            return self._query_id
        try:
            resp = await self._http.get(
                X_HOME_URL, headers=_page_headers(self._auth, self._ct0)
            )
        except httpx.HTTPError as exc:
            _log.warning("贺图：取 /home 失败，拿不到 queryId：%s: %s", type(exc).__name__, exc)
            return None
        if ONBOARD_MARK in str(resp.headers.get("location", "")):
            self.cookie_dead = True
            _log.error("贺图：X cookie 已失效（/home 被踢到登录引导页）")
            return None
        bundles = list(dict.fromkeys(BUNDLE_RE.findall(resp.text)))
        for url in bundles:
            try:
                js = (await self._http.get(url)).text
            except httpx.HTTPError:
                continue
            qid = extract_query_id(js)
            if qid:
                self._query_id = qid
                _log.info("贺图：queryId 从 %s 里发现：%s", url.rsplit("/", 1)[-1], qid)
                return qid
        _log.error("贺图：bundle 里没找到 %s 的 queryId（X 改版了？）", X_SEARCH_OP)
        return None

    async def search_page(
        self,
        tag: str,
        idol: str,
        *,
        cursor: str | None = None,
        product: str = "Top",
    ) -> tuple[list[Art], str | None]:
        """一页。**`POST`，不是 GET** —— 见模块说明。

        ⚠️ `product` 默认 **`"Top"`**（热度栏）。写成 `"Latest"` 也能跑、
        也不报错，只是**捞回来的全是低赞的新推** —— 见模块说明那张对照表。
        """
        if self._requests >= MAX_REQUESTS:
            return [], None
        qid = await self._discover_query_id()
        if not qid:
            return [], None
        variables: dict[str, Any] = {
            "rawQuery": tag,
            "count": 20,
            "querySource": "typed_query",
            "product": product,
        }
        if cursor:
            variables["cursor"] = cursor
        params = {
            "variables": json.dumps(variables, separators=(",", ":"), ensure_ascii=False),
            # 实测传空对象照样 200，所以不去抠那 40 个 featureSwitches。
            "features": "{}",
        }
        try:
            self._requests += 1
            resp = await self._http.post(
                GRAPHQL_URL.format(qid=qid),
                params=params,
                headers=_api_headers(self._auth, self._ct0),
                timeout=SEARCH_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            _log.warning("贺图：搜 %s（%s）失败：%s: %s", tag, product, type(exc).__name__, exc)
            return [], None
        if resp.status_code in (401, 403):
            self.cookie_dead = True
            _log.error("贺图：X 返回 %d，cookie 多半失效了", resp.status_code)
            return [], None
        if resp.status_code != 200:
            _log.warning(
                "贺图：搜 %s（%s）得到 HTTP %d（%d 字节）",
                tag, product, resp.status_code, len(resp.content),
            )
            return [], None
        try:
            payload = resp.json()
        except ValueError:
            _log.warning("贺图：搜 %s 的响应不是 JSON（%d 字节）", tag, len(resp.content))
            return [], None
        return parse_search(payload, idol)

    async def search_tag(
        self,
        tag: str,
        idol: str,
        d: Date,
        *,
        plan: tuple[str, ...] = PAGE_PLAN_YEARFUL,
    ) -> list[Art]:
        """按 `plan` 一页页翻，直到没有游标、翻出时间窗、或撞上请求上限。

        ⚠️ **两种栏的游标不能混用**（Top 的游标喂给 Latest 是未定义行为），
        所以游标按栏各存一份。

        ⚠️ `plan` 里 `"Top"` 才是主力 —— 见模块说明那张对照表。`"Latest"`
        是按时间倒序的，「翻出窗口就停」那条只对它有；Top 的排序和热度有关，
        整页都老不代表后面没有新的，套那条会误停。
        """
        lo, _hi = window_bounds(d)
        found: dict[str, Art] = {}
        cursors: dict[str, str | None] = {}
        for product in plan:
            if self._requests >= MAX_REQUESTS:
                break
            batch, cursor = await self.search_page(
                tag, idol, cursor=cursors.get(product), product=product
            )
            cursors[product] = cursor
            for a in batch:
                found.setdefault(a.id, a)
            if not cursor:
                continue  # 这一栏到底了，换下一栏
            # 只有 Latest 是按时间倒序的，整页都翻出窗口就没必要再往下翻
            if product == "Latest" and max(a.posted_at for a in batch) < lo:
                break
            if self._requests >= MAX_REQUESTS:
                break
            await asyncio.sleep(SEARCH_GAP_SEC)
        return list(found.values())


# ---------- 轮询器 ----------


class BirthdayArtPoller:
    """每天定时收贺图。结构和 `BirthdayPoller` 同构：slot / 窗口 / 逐群隔离。"""

    def __init__(self, cfg, db, sender, media_http, notifier=None, *,
                 groups_override: tuple[str, ...] | None = None,
                 library=None) -> None:
        self.cfg = cfg
        self.db = db
        self.sender = sender
        self.http = media_http
        self.notifier = notifier
        # 图库（`imagelib.Library`）。**必须有** —— markdown 的图是 QQ 的服务器
        # 去抓的，它在大陆机房够不到 pbs.twimg.com。没有它就只能退回发不出去的
        # 原始 URL（探针/单测里就是这么跑的）。
        self.library = library
        # 只给探针/测试用：把「发哪些群」钉死，不去动库里的开关。
        # 生产路径永远是 None，走 `groups` 属性（每拍重查库）。
        self._groups_override = groups_override
        self._stopped = asyncio.Event()
        self._calendar: birthday_module.Calendar | None = None
        self._calendar_tried = False
        self.x = XSearch(media_http, cfg.x_auth_token, cfg.x_ct0)

    @property
    def groups(self) -> tuple[str, ...]:
        """这个功能该发到哪些群。**每次重查库** —— `/set` 改完下一拍就生效。

        群开关存在库里（见 `features.py`），`.env` 里**一个群参数都没有**。
        """
        if self._groups_override is not None:
            return self._groups_override
        return features.groups_for(self.db, "birthday_art")

    @property
    def enabled(self) -> bool:
        """三样都要：开关、群、**cookie**。缺 cookie 时整条链路跑不起来。"""
        return bool(
            self.cfg.birthday_art_enabled
            and self.groups
            and self.cfg.x_auth_token
            and self.cfg.x_ct0
        )

    @property
    def calendar_path(self) -> Path:
        return Path(self.cfg.birthday_dir) / birthday_module.CALENDAR_NAME

    def stop(self) -> None:
        self._stopped.set()

    def load(self):
        """冷数据和生日推送共用一份 `birthdays.toml`。失败只打 ERROR，不抛。"""
        if self._calendar_tried:
            return self._calendar
        self._calendar_tried = True
        try:
            self._calendar = birthday_module.load_calendar(self.calendar_path)
        except Exception as exc:  # noqa: BLE001
            _log.error("贺图：冷数据读不出来（%s）：%s", self.calendar_path, exc)
        return self._calendar

    async def run(self) -> None:
        # ⚠️ **这里不能 return。** 群开关在库里（`features.py`），`/set` 的语义是
        # 「改完下一拍就生效」—— 启动时库里一个群都没开的话，一 return 这个 task
        # 就**永久结束**了：之后再怎么 `/set` 开群都不会跑，只能重启容器。
        # 留着轮询不花什么钱 —— `poll_once` 每拍开头 `if not self.enabled: return 0`。
        if not self.enabled:
            if not (self.cfg.x_auth_token and self.cfg.x_ct0):
                _log.info("贺图：未配置 X cookie（auth_token / ct0），功能关闭")
            else:
                _log.info("贺图：库里没有开着的群，功能关闭（**仍然轮询**，/set 开了下一拍生效）")
        else:
            _log.info(
                "贺图：已启用，每天 %s（本地时间）收**当天**的贺图，目标 %d 个群，"
                "带年份的 tag 翻 %s，最多发 %d 张，点赞下限 %d",
                self.cfg.birthday_art_time,
                len(self.groups),
                "+".join(PAGE_PLAN_YEARFUL),
                self.cfg.birthday_art_max,
                self.cfg.birthday_art_min_likes,
            )
        while not self._stopped.is_set():
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001
                _log.exception("贺图：这一轮炸了，下一拍继续")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=CHECK_SEC)
            except TimeoutError:
                pass

    async def _collect(self, idols: list[str], year: int, d: Date) -> list[Art]:
        """搜完所有 tag、去重、过滤时间窗。**逐 tag 隔离**：一个 tag 挂了不连累别的。"""
        self.x.reset()
        found: dict[str, Art] = {}
        for name in idols:
            for tag in tags_for(name, year):
                plan = PAGE_PLAN_YEARFUL if is_yearful(tag, year) else PAGE_PLAN_BARE
                batch = await self.x.search_tag(tag, name, d, plan=plan)
                kept = [a for a in batch if in_window(a.posted_at, d)]
                _log.info("贺图：%s → %d 条，窗口内 %d 条", tag, len(batch), len(kept))
                for a in kept:
                    found.setdefault(a.id, a)
                await asyncio.sleep(SEARCH_GAP_SEC)
                if self.x.requests_made >= MAX_REQUESTS:
                    _log.warning("贺图：已达请求上限 %d，剩下的 tag 这轮不搜了", MAX_REQUESTS)
                    return list(found.values())
        return list(found.values())

    async def _fetch(self, url: str) -> bytes | None:
        """先要原图，**下不动就退到 `large`**（最长边 2048）。

        ⚠️ 推特的原图经常 3–8 MB，而 QQ 的 `file_data` 是 base64 后 5 MiB，
        也就是原图实际只有 **3.5 MB** 的余量（`preview.MAX_IMAGE_BYTES`）。
        实测一轮 5 张里就有 1 张原图超限。这里的产品优先级是
        **图真的能显示** —— 无损是 `preview.py` 那条路的要求，不是这条。
        """
        raw = await preview.fetch_image(self.http, url)
        if raw is not None:
            return raw
        alt = _with_name(url, "large")
        if alt == url:
            return None
        _log.info("贺图：原图下不来或太大，退到 large：%s", url)
        return await preview.fetch_image(self.http, alt)

    async def _download_by_idol(
        self, head: list[Art], rest: list[Art], want: int
    ) -> dict[str, list[tuple[Art, bytes]]]:
        """按偶像并发下载，**每个偶像最多保留前 `want` 张成功的**（下不动的往后补）。"""
        plans: dict[str, list[Art]] = {}
        extra: dict[str, list[Art]] = {}
        for a in head:
            plans.setdefault(a.idol, []).append(a)
        for a in rest:
            extra.setdefault(a.idol, []).append(a)
        for idol, arts in plans.items():
            plans[idol] = (arts + extra.get(idol, []))[: want * DOWNLOAD_LOOKAHEAD]

        flat = [(idol, a) for idol, arts in plans.items() for a in arts]
        blobs = await asyncio.gather(*(self._fetch(a.image) for _, a in flat))
        out: dict[str, list[tuple[Art, bytes]]] = {}
        for (idol, art), raw in zip(flat, blobs):
            if raw is None:
                _log.warning("贺图：%s 的图下不下来（%s），跳过", art.id, art.image)
                continue
            bucket = out.setdefault(idol, [])
            if len(bucket) >= want:
                continue
            bucket.append((art, raw))
        return {k: v for k, v in out.items() if v}

    async def _alert(self, text: str) -> None:
        """**群里看不见的失败必须私聊报。** cookie 失效的症状是「今天没图」，
        和「今天真没人画」长得一模一样。`C2CNotifier` 自带冷却，不会刷屏。"""
        if self.notifier is None:
            return
        for group in self.groups[:1]:
            try:
                await self.notifier.notify(group, text, key="xart")
            except Exception as exc:  # noqa: BLE001
                _log.warning("贺图：私聊告警也失败了：%s", exc)

    async def poll_once(
        self, *, now: datetime | None = None, arts: list[Art] | None = None
    ) -> int:
        """跑一轮，返回这轮发出去的**条数**。

        `arts` **只给测试用**：传进来就完全不碰网络（smoke 全靠这个跑离线）。
        """
        if not self.enabled:
            return 0

        now = now or datetime.now()
        slot = slot_for(now, parse_time(self.cfg.birthday_art_time))
        late = (now - slot).total_seconds()
        if late > self.cfg.birthday_art_window_sec:
            return 0

        d = art_date(slot)
        day_key = d.isoformat()
        done = self.db.birthday_art_posted(day_key)
        targets = [g for g in self.groups if g not in done]
        if not targets:
            return 0

        calendar = self.load()
        if calendar is None:
            return 0
        day = calendar.for_date(d)
        if not day.idols:
            # 366 天里有 121 天没有偶像过生日（只有声优，或什么都没有）。
            # **这是正常情况**，不打日志也不记账 —— 窗口内每拍都会走到这里。
            return 0
        idols = [i.name for i in day.idols]

        if arts is None:
            arts = await self._collect(idols, d.year, d)
            if self.x.cookie_dead:
                await self._alert(
                    f"生日贺图：X cookie 好像失效了，今天搜不到东西。\n"
                    f"（{day_key}）更新 .env 里的 auth_token / ct0 后重启容器。"
                )
        # 库只读文件，每拍读一次（几十 KB，可忽略）—— 改完库重启都不用，
        # 下一拍就生效。读不到时 load() 自己打 WARNING，这里退回「全是画师」。
        classify = make_classifier(
            seiyuu.load(), exclude=self.cfg.birthday_art_exclude)
        head, rest = select(
            arts,
            idols,
            min_likes=self.cfg.birthday_art_min_likes,
            max_n=self.cfg.birthday_art_max,
            exclude=self.cfg.birthday_art_exclude,
            skip_official=self.cfg.birthday_art_skip_official,
            classify=classify,
        )
        if not head:
            _log.info(
                "贺图：%s 共 %d 条候选，没有一条过下限（%d 赞），今天不发",
                day_key,
                len(arts),
                self.cfg.birthday_art_min_likes,
            )
            return 0
        counts: dict[str, int] = {}
        for a in head:
            label = classify(a)[1]
            counts[label] = counts.get(label, 0) + 1
        by_idol = len({a.idol for a in head})
        _log.info("贺图：%s 选中 %d 张 / %d 位偶像（%s）", day_key, len(head), by_idol,
                  " / ".join(f"{k} {v}" for k, v in counts.items()))

        picked = await self._download_by_idol(head, rest, self.cfg.birthday_art_max)
        if not picked:
            # 一张都没下来 —— 多半是网络。留窗口内下一拍重试，**绝不记账**。
            _log.warning("贺图：%s 候选 %d 条但一张图都没下来，这一拍不发",
                         day_key, len(head))
            return 0

        # 每个偶像一条 markdown。顺序跟 `idols` 走（日历里的顺序）。
        #
        # ⚠️ 图必须先落到**自建图床**上 —— markdown 的 `![](url)` 是 **QQ 的服务器
        # 去抓**的，而 `pbs.twimg.com` 在大陆机房根本够不到（预热两遍 45s / 180s
        # 群里都没有图）。落盘只做一次，所有群共用同一批 URL。
        leaked = 0
        if self.library is None or not self.library.enabled:
            _log.warning(
                "贺图：没有配图床（IMGSRV_BASE 为空）—— markdown 里会用推特原始 URL，"
                "**QQ 抓不到，群里不会有图**")
        brands = {i.name: i.brand for i in day.idols}
        messages: list[tuple[str, list[str]]] = []
        for idol in idols:
            items = picked.get(idol) or []
            if not items:
                continue
            rows: list[tuple[Art, bytes, str, str]] = []
            for a, raw in items:
                label = classify(a)[1]
                ref = self.library.store(
                    raw,
                    kind=imagelib.KIND_ART,
                    event_date=day_key,
                    role=idol,
                    brand=brands.get(idol, ""),
                    author_handle=a.author,
                    author_name=a.author_name,
                    author_category=label,
                    likes=a.likes,
                    posted_at=a.posted_at,
                    source_url=a.image,
                    page_url=a.url,
                    source_id=a.id,
                ) if self.library is not None else None
                if ref is None:
                    url = a.image
                    leaked += 1
                else:
                    url = ref.url
                rows.append((a, raw, label, url))
            md = render_markdown(idol, rows, d.year)
            messages.append((md, [_id_of(a.url) for a, _ in items]))
        if leaked:
            _log.warning("贺图：%s 有 %d 张没能放进图床，退回原始 URL（多半显示不出来）",
                         day_key, leaked)
        if not messages:
            _log.warning("贺图：%s 有候选但一条都渲染不出来，这一拍不发", day_key)
            return 0

        sent = 0
        for group in targets:
            await self._warmup(messages, day_key)
            delivered = await self._deliver(group, messages, day_key)
            if delivered:
                self.db.mark_birthday_art(
                    day_key, group, len(delivered), ",".join(delivered)
                )
                sent += len(delivered)
        return sent

    async def _warmup(self, messages: list[tuple[str, list[str]]],
                      day_key: str) -> None:
        """markdown 的图由 QQ 自己去抓，**冷链接第一次必炸** —— 先往预热群
        发一遍同样的内容，等它抓完，正式群那条才显示得出来。

        ⚠️ 预热必须是**真的发一条 markdown**：`post_group_file` 那条路
        预热不了渲染缓存（见 `config.preview_warmup_sec` 的说明）。
        """
        wg = self.cfg.birthday_art_warmup_group
        if not wg:
            return
        ok = 0
        for md, _ids in messages:
            try:
                await self.sender.send_markdown(wg, md)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                _log.warning("贺图：预热发送失败（%s）：%s", wg[:8], exc)
                break
        if ok:
            _log.info("贺图：%s 预热 %d 条已发，等 %d 秒让 QQ 抓图",
                      day_key, ok, self.cfg.preview_warmup_sec)
            await asyncio.sleep(self.cfg.preview_warmup_sec)

    async def _deliver(
        self, group: str, messages: list[tuple[str, list[str]]], day_key: str
    ) -> list[str]:
        """每个偶像一条 markdown，逐条发。

        **第一条挂了就整批放弃**（群里还没看到任何东西，下一拍重来）；
        后续某条挂了只记日志、跳过 —— 第一条已经发出去了，再退一次就是重复刷屏。
        返回这轮**确实发出去的推文 id**（记账用）。"""
        sent: list[str] = []
        ok = 0
        for i, (md, ids) in enumerate(messages):
            try:
                await self.sender.send_markdown(group, md)
            except Exception as exc:  # noqa: BLE001
                if not sent:
                    _log.warning("贺图：第一条就发不出去 → %s：%s（这一拍不记账）",
                                 group[:8], exc)
                    return []
                _log.warning("贺图：第 %d 条发失败 → %s：%s（跳过）", i + 1, group[:8], exc)
                continue
            ok += 1
            sent.extend(ids)
        if sent:
            _log.info("贺图：已推送 %s 共 %d 条消息 / %d 张图 → %s",
                      day_key, ok, len(sent), group[:8])
        return sent
