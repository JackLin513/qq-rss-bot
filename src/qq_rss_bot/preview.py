"""群里分享 YouTube / X(Twitter) 链接 → 自动回一条带封面的卡片。

**和 bilibili 最大的不同：这两个站点的图，QQ 的服务器拉不到。**

实测（同一时刻、同一个群里、同一个接口）：

    i1.hdslb.com   交给 QQ 去下载 → ✅ 拿到 file_info
    i.ytimg.com    交给 QQ 去下载 → ❌ 850027「富媒体文件上传超时」（err_code 40034003）

`40034003` 是**超时**，不是 404 也不是 DNS 失败 —— QQ 的机房够得到域名、
但拉不下内容。也就是说「能不能用 URL 形态」取决于 QQ 机房到那个图床的
连通性，**我们控制不了**。所以这两个站点的图走另一条路：

    我们自己下载（走 media_proxy）→ **存进图库** → markdown 里用图库的地址

（2026-10-22 之前是「下载 → base64 → 交给 QQ 的 /files 接口」，一条消息只能带
一张图，所以多图要连发 1+N 条。换成自建图床之后这两件事一起解决了：
**一个分享只发一条 markdown**，图全在正文里，也不再受 base64 那 3.5 MB 上限。）

代价是**我们这边必须能出网**：容器直连 youtube.com / x.com 全部超时，所以
`MEDIA_PROXY` 是必需的（见 config.py）。代理没配或不通时，`lookup()` 会返回
None，调用方静默不发 —— 不会连累 bilibili 和 RSS 推送。

**接口选型**（2026-10-17 本机实测确认的字段，别凭直觉换）：

===================  ==========================================================
YouTube oEmbed       title / author_name / thumbnail_url —— **没有时长和播放量**
YouTube youtubei      videoDetails 里 lengthSeconds / viewCount 都有
X fxtwitter           一次拿全：text / likes / retweets / replies / author / media
X 官方 oEmbed         author_name + 正文（在 html 的 blockquote 里），**没有图**
X 官方 syndication    ❌ 本机 SSL 直接不通，别指望
===================  ==========================================================

所以 **YouTube 以 youtubei 为主**（它是 YouTube 网页自己在用的接口，
`videoDetails` 对受限视频照样返回，只有 `playabilityStatus` 会变），
oEmbed 兜底；**X 以 fxtwitter 为主**、官方 oEmbed 兜底（至少还有作者和正文）。

设计上沿用 bilibili 的约定：**全程静默** —— 任何一步拿不到数据就返回 None，
调用方什么都不发。群里没人希望机器人因为认不出链接而回一句报错。
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass

import httpx

# 计数和时长的格式化**复用 bilibili 那份** —— 两边的口径必须一致
# （「3.1万」的边界在哪、`03:07` 还是 `3:07`），各写一份迟早会漂。
from . import multiimage
from .bilibili import fmt_count, fmt_duration

_log = logging.getLogger("qq_rss_bot.preview")

# ---------- 链接识别 ----------

# YouTube 的几种形态。ID 固定 11 位，字符集 `[A-Za-z0-9_-]`。
# 顺序有讲究：`watch\?(?:[^#]*&)?v=` 要能吃掉 `?v=` 和 `?list=xx&v=` 两种。
YT_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|embed/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)
# X / Twitter 的推文链接。用户名任意，ID 是纯数字。
X_RE = re.compile(r"(?:twitter\.com|x\.com)/[A-Za-z0-9_]+/status/(\d+)")

_API_TIMEOUT = 12.0

# 公开的 YouTube WEB 客户端 key —— YouTube 自己网页里写死的，不是我们的凭据。
_YTI_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

# 浏览器 UA。这两个站点都拦默认 UA（和 bilibili 的 `wbi/search` 是同一类坑）。
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 封面在我们这边的下载上限。超了就**只发文字不发图**，不是整条丢弃 ——
# 一张图抠不下来不该让整条分享消失（和 RSS 推送的降级策略一致）。
#
# 这个数是**倒推**出来的，不是拍的。官方文档对 `file_data` 的原话是
# 「直接上传二进制有大小限制，需要在 5M 以内」，而**这个 5M 限的是 base64
# 之后那个字段本身**，不是原始字节。base64 膨胀 4/3，所以原始图的上限是
# 5MiB × 3/4 ≈ 3.75MiB；这里取 3.5MB，给正文和 JSON 外壳留余量。
#
# 两端都有实测：2500x3500 的原图 943KB → base64 1.29MB ✅ 通过；而按
# 5MB **原始字节**设的话 base64 会到 6.7MB，**平台会拒**。
MAX_IMAGE_BYTES = 3_500_000

# 一条分享最多带几张图。**卡在 4 是有原因的**：这里走的是被动回复，同一个
# `msg_id` 下平台只给有限的几次回复额度（代码里一直按 5 次设计），正文+首图
# 已经用掉 1 次，再留 1 次余量给重试 —— 4 张正好安全。这个上限**没有官方
# 文档背书**，是保守取值，实测边界见 CLAUDE.md。
MAX_IMAGES = 4

# `file_data` 字段自身的天花板，官方写死 5M。`_upload_image` 拿它当最后一道
# 闸：就算上面那个上限哪天被调大，也不会把超限的 body 递出去。
MAX_FILE_DATA_BYTES = 5 * 1024 * 1024

# 下载封面单独给个更长的超时。大原图走代理可能要十几秒，
# 用元数据接口那个 12s 会误伤（表现是「图没了但卡片还在」）。
IMAGE_TIMEOUT = 30.0

# 推文正文在卡片里的截断长度。正文可能几千字，QQ 的文本消息有长度上限
# （README 里记的是 1500），留足余量。
_MAX_TWEET_CHARS = 180

# 启动自检用的样例。YouTube 那条是官方 MV（不会删），X 那条是史上点赞最多
# 的推文之一（也不会删）—— 探测报错时能确定是网络问题而不是样本失效。
_PROBE_YT = "dQw4w9WgXcQ"
_PROBE_X = "896523232098078720"


def make_client(
    proxy: str,
    *,
    ca_bundle: str = "",
    insecure: bool = False,
    timeout: float = _API_TIMEOUT,
) -> httpx.AsyncClient:
    """建一个访问这两个站点用的客户端：浏览器 UA + 代理（+ 可选的证书处理）。

    **和 `RssBot.rss_http` 是两个独立的客户端，别合并。** rss_http 必须直连
    （RSSHub 是容器内部服务名，代理解析不了；QQ 的接口直连更快），
    而这里**必须走代理**（容器直连这两个站点全部超时）。
    代理留空就退化成直连 —— 那两个功能会失效，但其余一切照常。

    `ca_bundle` / `insecure` 是给**会做 MITM 的代理**（实测 Surge 就是）准备的：
    它用自己的根证书重签 `www.youtube.com`，而 httpx 默认只信 certifi 那份
    CA 列表，于是报 `self-signed certificate in certificate chain`。
    优先用 `ca_bundle` 指到代理的根证书；`insecure` 是拿不到证书时的逃生舱。
    """
    verify: bool | str = True
    if insecure:
        verify = False
    elif ca_bundle:
        verify = ca_bundle
    return httpx.AsyncClient(
        headers={"User-Agent": BROWSER_UA},
        follow_redirects=True,
        timeout=timeout,
        proxy=proxy or None,
        verify=verify,
    )


@dataclass(frozen=True)
class Target:
    """从群消息里抽出来的「要查哪个站点的哪一条」。"""

    site: str  # "youtube" | "twitter"
    vid: str  # 视频 ID / 推文 ID


@dataclass(frozen=True)
class Card:
    """归一化后的卡片内容 —— 两个站点共用一个渲染器。

    `lookup` 负责把各站点千奇百怪的响应压成这个形状，`render_*` 只认它，
    所以渲染是纯函数，能离线测。
    """

    site: str
    url: str  # 「打开」链接
    title: str  # 视频标题 / 推文正文（已截断）
    author: str  # 频道名 / 作者显示名
    covers: tuple[str, ...] = ()  # 配图原始 URL（还没下载），按站点给的顺序
    extra: tuple[str, ...] = ()  # 作者行下面的信息行（时长/播放量/互动数）

    @property
    def cover(self) -> str | None:
        """第一张配图。

        **保留是为了不炸旧调用点**（`render_markdown` / `render_cover` /
        bilibili 那套）。新的多图代码请直接用 `covers`。
        """
        return self.covers[0] if self.covers else None


def find_target(content: str, ark_data: object = None) -> Target | None:
    """从群消息的 `content` 里找出 YouTube / X 链接。

    只扫 `content` —— 实测平台把卡片消息的全文预渲染在里面了（bilibili 那次
    踩过：以为得从 `ark_data` 里挖，其实 `content` 里就有）。`ark_data` 留着
    只是为了和 `bilibili.find_target` 的签名一致，这里用不上。

    返回 `None` = 这条消息和这两个站点无关，调用方应当什么都不做。
    """
    text = content or ""

    m = YT_RE.search(text)
    if m:
        return Target(site="youtube", vid=m.group(1))

    m = X_RE.search(text)
    if m:
        return Target(site="twitter", vid=m.group(1))

    return None


# ---------- 取数 ----------


async def lookup(
    http: httpx.AsyncClient, target: Target, *, timeout: float = _API_TIMEOUT
) -> Card | None:
    """把 `Target` 解析成 `Card`。**任何一步失败都返回 None**（调用方静默）。

    `http` 必须是**走代理**的那个客户端（`RssBot.media_http`）——
    这两个站点容器直连不通。
    """
    if target.site == "youtube":
        return await _lookup_youtube(http, target.vid, timeout)
    if target.site == "twitter":
        return await _lookup_twitter(http, target.vid, timeout)
    return None


async def _lookup_youtube(
    http: httpx.AsyncClient, vid: str, timeout: float
) -> Card | None:
    """youtubei 为主、oEmbed 兜底。

    youtubei 是 YouTube 网页自己在用的接口，`videoDetails` 里 title / author /
    lengthSeconds / viewCount 一次给全 —— oEmbed **给不了时长和播放量**。
    它对受限视频会返回 `playabilityStatus=UNPLAYABLE`，但 `videoDetails`
    通常照样有；真的没有时才退回 oEmbed（那条路至少保证有标题和频道名）。
    """
    watch = f"https://www.youtube.com/watch?v={vid}"
    # 固定用 hqdefault：480x360、约 20KB，够看且下载快。
    # （maxresdefault 在冷门视频上会 404，hqdefault 永远有。）
    cover = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    title = author = ""
    extra: list[str] = []

    try:
        resp = await http.post(
            "https://www.youtube.com/youtubei/v1/player",
            params={"key": _YTI_KEY},
            headers={"User-Agent": BROWSER_UA, "Content-Type": "application/json"},
            json={
                "videoId": vid,
                "context": {
                    "client": {
                        "clientName": "WEB",
                        "clientVersion": "2.20240101.00.00",
                        "hl": "zh-CN",
                    }
                },
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        details = (resp.json() or {}).get("videoDetails") or {}
    except (httpx.HTTPError, ValueError) as exc:
        _log.info("preview：youtubei 取数失败 %s：%s: %s", vid, type(exc).__name__, exc)
        details = {}

    if details:
        title = str(details.get("title") or "")
        author = str(details.get("author") or "")
        bits: list[str] = []
        # 直播没有 lengthSeconds —— 硬算会得到 `00:00`，那是误导
        if details.get("lengthSeconds"):
            bits.append(fmt_duration(details["lengthSeconds"]))
        views = _count_line("播放", details.get("viewCount"))
        if views:
            bits.append(views)
        # 和 bilibili 卡片同一条约定：信息行每行最多两项
        extra = [" · ".join(bits)] if bits else []

    if not title:
        # youtubei 没给出标题 —— 退回 oEmbed。它不看视频状态，永远有标题。
        try:
            resp = await http.get(
                "https://www.youtube.com/oembed",
                params={"url": watch, "format": "json"},
                headers={"User-Agent": BROWSER_UA},
                timeout=timeout,
            )
            resp.raise_for_status()
            body = resp.json() or {}
        except (httpx.HTTPError, ValueError) as exc:
            _log.info(
                "preview：YouTube oEmbed 兜底也失败 %s：%s: %s", vid, type(exc).__name__, exc
            )
            return None
        title = str(body.get("title") or "")
        author = author or str(body.get("author_name") or "")
        cover = str(body.get("thumbnail_url") or "") or cover

    if not title:
        _log.info("preview：YouTube %s 没拿到标题，放弃", vid)
        return None

    return Card(
        site="youtube",
        url=watch,
        title=title,
        author=author,
        covers=(cover,),
        extra=tuple(extra),
    )


async def _lookup_twitter(
    http: httpx.AsyncClient, tid: str, timeout: float
) -> Card | None:
    """fxtwitter 为主、官方 oEmbed 兜底。

    **为什么用 fxtwitter**：官方 oEmbed 只给作者名和正文（正文还藏在 html 的
    blockquote 里），**拿不到配图，也拿不到点赞/转发数**；官方 syndication
    实测本机 SSL 直接不通。fxtwitter 一个请求全给。
    代价是它是个第三方服务 —— 挂了就退回官方 oEmbed，那条路至少还有作者和正文。
    """
    url = f"https://x.com/i/status/{tid}"

    try:
        resp = await http.get(
            f"https://api.fxtwitter.com/status/{tid}",
            headers={"User-Agent": BROWSER_UA},
            timeout=timeout,
        )
        resp.raise_for_status()
        tweet = (resp.json() or {}).get("tweet") or {}
    except (httpx.HTTPError, ValueError) as exc:
        _log.info("preview：fxtwitter 取数失败 %s：%s: %s", tid, type(exc).__name__, exc)
        tweet = {}

    if tweet:
        author = tweet.get("author") or {}
        name = str(author.get("name") or "")
        handle = str(author.get("screen_name") or "")
        if name and handle:
            author_line = f"{name} (@{handle})"
        else:
            author_line = name or (f"@{handle}" if handle else "")

        # 正文：`text` 是给展示用的（已经有 t.co 展开），`raw_text` 是原始的。
        # ⚠️ 别写成 `str(text or raw_text or "")` —— 见 `_tweet_text`。
        text = _tweet_text(tweet)
        stats = [
            _count_line("点赞", tweet.get("likes")),
            _count_line("转发", tweet.get("retweets")),
            _count_line("评论", tweet.get("replies")),
        ]
        # **每行最多两项**，和 bilibili 卡片同一条约定：四项挤一行在手机窄屏上
        # 会被 QQ 按自己的规则折行，折点不受控。
        stats = [s for s in stats if s]

        return Card(
            site="twitter",
            url=str(tweet.get("url") or url),
            title=_trim(text),
            author=author_line,
            covers=_twitter_covers(tweet.get("media")),
            extra=tuple(" · ".join(stats[i : i + 2]) for i in range(0, len(stats), 2)),
        )

    # --- 兜底：官方 oEmbed ---
    try:
        resp = await http.get(
            "https://publish.twitter.com/oembed",
            params={"url": url},
            headers={"User-Agent": BROWSER_UA},
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json() or {}
    except (httpx.HTTPError, ValueError) as exc:
        _log.info("preview：X oEmbed 兜底也失败 %s：%s: %s", tid, type(exc).__name__, exc)
        return None

    author = str(body.get("author_name") or "")
    text = _text_from_oembed(str(body.get("html") or ""))
    if not (author or text):
        _log.info("preview：X %s 没拿到内容，放弃", tid)
        return None

    return Card(
        site="twitter",
        url=str(body.get("url") or url),
        title=_trim(text),
        author=author,
        cover=None,  # oEmbed 这条路**没有图**
    )


# ---------- 渲染 ----------
#
# 两个出口：`render_text` 给富媒体消息的正文（我们上传图片字节那条路），
# `render_markdown` 给 markdown 卡片。两者都是纯函数。


def render_text(card: Card) -> str:
    """富媒体消息（`msg_type=7`）的正文。图走 `media` 字段，这里只管文字。"""
    lines = [card.title]
    if card.author:
        lines.append(card.author)
    lines.extend(card.extra)
    lines.append(card.url)
    return "\n".join(line for line in lines if line)


# 合并成一条之后，图可以比原来的 208 封面宽一档 —— 手机上一屏放得下，
# 又明显比现在的卡片好看。
PREVIEW_IMAGE_W = 320


def render_markdown(
    card: Card, images: tuple[tuple[str, int, int], ...] = ()
) -> str:
    """markdown 卡片（`msg_type=2`）。`images` 是 `(URL, 宽, 高)`，按显示顺序。

    **一个分享只发一条消息**，图全放在正文里（以前是"首条带正文 + 每图一条"，
    1+N 条消息，刷屏还吃额度）。

    ⚠️ `images` 里的 URL **必须来自我们自己的图库**。直接把 `i.ytimg.com` /
    `pbs.twimg.com` 塞进来会渲染成裂图 —— QQ 的服务器在大陆机房拉不到那些域名
    （2026-10-22 实测）。图先入库、再由图床用自己的地址发，这条才成立。

    ⚠️ 尺寸提示**必填**且**比例必须和原图一致**（`multiimage.box_for` 负责，
    它只缩宽度、绝不夹高度）。
    """
    lines: list[str] = []
    title = _MD_STRIP_RE.sub("", card.title).strip()
    lines.append(f"**{title or '（无标题）'}**")
    if card.author:
        lines.append(card.author)
    lines.extend(card.extra)
    for index, (url, width, height) in enumerate(images, start=1):
        if width <= 0 or height <= 0:
            # 读不出宽高就不放 —— 缺尺寸提示手机端整条都不渲染（见 multiimage）。
            continue
        dw, dh = multiimage.box_for((width, height), PREVIEW_IMAGE_W)
        lines.append(f"![图{index} #{dw}px #{dh}px]({url})")
    lines.append(f"[打开原文]({card.url})")
    return "\n".join(lines)


def render_cover(card: Card) -> str | None:
    """`![封面](url)` 那一行 —— **仅当封面确实放在 QQ 够得到的地方时**才该用。"""
    if not card.cover:
        return None
    return f"![封面 #{_COVER_W}px #{_COVER_H}px]({card.cover})"


_COVER_W, _COVER_H = 208, 117
_MD_STRIP_RE = re.compile(r"[*_`~\\]")


# ---------- 图片 ----------


async def fetch_image(
    http: httpx.AsyncClient,
    url: str,
    *,
    timeout: float = IMAGE_TIMEOUT,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> bytes | None:
    """把封面接下来，交给调用方（入库或 base64 上传）。

    超限或失败都返回 None —— **只影响有没有图，不影响这张卡片发不发**。

    `max_bytes` 默认是 base64 那条路的 3.5 MB 上限；**存进图库的调用方要传
    更大的值**（图床的上限），否则会白白丢掉本来存得下的原图。
    """
    try:
        resp = await http.get(url, headers={"User-Agent": BROWSER_UA}, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        _log.info("preview：封面下载失败 %s：%s: %s", url, type(exc).__name__, exc)
        return None
    raw = resp.content
    if not raw:
        return None
    if len(raw) > max_bytes:
        _log.info("preview：封面 %d 字节超过上限 %d，这张不发图", len(raw), max_bytes)
        return None
    return raw


# ---------- 启动自检 ----------


async def probe(http: httpx.AsyncClient, *, timeout: float = _API_TIMEOUT) -> str:
    """探一次「走这个代理能不能到 YouTube / X」，返回一句能直接打进日志的话。

    和 `bilibili.probe` 同一个用途：**出问题时日志里必须有痕迹**。
    这两个站点的失败比 bilibili 隐蔽得多 —— 代理没配、代理不通、被墙，
    症状都是「群里发了链接没反应」。
    """
    parts: list[str] = []
    for site, target in (
        ("YouTube", Target(site="youtube", vid=_PROBE_YT)),
        ("X", Target(site="twitter", vid=_PROBE_X)),
    ):
        try:
            card = await lookup(http, target, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 —— 探测就是要连异常类型一起打出来
            parts.append(f"{site} ❌ {type(exc).__name__}: {exc}")
            continue
        if card is None:
            parts.append(f"{site} ❌ 解析不出来（代理不通？看上面的 INFO 日志）")
        else:
            parts.append(f"{site} ✅ {card.title[:20]}")
    return "；".join(parts)


# ---------- 内部 ----------


def _count_line(label: str, value: object) -> str:
    """`播放 18.3亿` —— 值拿不准就返回空串（宁可少一格，不要出一格 `播放 ?`）。"""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    return f"{label} {fmt_count(n)}" if n >= 0 else ""


# 「整段只有 t.co 链接」—— 纯图推的 raw_text 长这样，显示出来毫无意义。
_TCO_ONLY_RE = re.compile(r"^(?:https?://t\.co/\w+\s*)+$")


def _raw_text(tweet: dict) -> str:
    """`raw_text` **两种形态都要吃**：老版 API 是字符串，新版是对象
    `{"text": ..., "display_text_range": [...], "facets": [...]}`。"""
    raw = tweet.get("raw_text")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        inner = raw.get("text")
        return inner if isinstance(inner, str) else ""
    return ""


def _tweet_text(tweet: dict) -> str:
    """推文正文（展示用）。拿不到就返回空串。

    ⚠️⚠️ **`raw_text` 是新版 fxtwitter 的**对象**，不是字符串。**
    老代码是 `str(tweet.get("text") or tweet.get("raw_text") or "")` ——
    正文非空时走不到那个回退，所以**平时完全看不出来**；但**纯图片推的
    `text` 是空串**，一落到回退就把整个 dict `str()` 出来，群里收到的是
    Python 的 repr：

        {'text': 'https://t.co/61cVhplQpn', 'display_text_range': [0, 0],
         'facets': [{'type': 'media', ...}]}

    实测 2026-10-20 `@latiechi` 那条纯图推（9913 赞、一张图）就是这个症状。

    纯图推的 `raw_text.text` 就是那个 t.co 图片链接，显示出来一样难看，
    所以「整段只是一串 t.co」也当没有正文 —— 卡片退化成
    「作者 + 数据 + 链接」，**图照发**（图走 `media`，和正文无关）。
    """
    for cand in (tweet.get("text"), _raw_text(tweet)):
        if isinstance(cand, str):
            text = cand.strip()
            if text and not _TCO_ONLY_RE.match(text):
                return text
    return ""


def _trim(text: str, limit: int = _MAX_TWEET_CHARS) -> str:
    """推文正文截断。

    按**显示宽度**（中日韩字符算 2）而不是字符数 —— 180 个汉字在手机上
    比 180 个字母长一倍，按字符数截会在中文推文上超出一大截。
    """
    text = " ".join(text.split())  # 正文里的换行会把卡片撑高，压成单段
    width = 0
    for i, ch in enumerate(text):
        width += 2 if _WIDE_RE.match(ch) else 1
        if width > limit:
            return text[:i] + "…"
    return text


# 粗略的「全角」判据：中日韩、全角标点、以及 emoji 所在的区段。
_WIDE_RE = re.compile(
    r"[ᄀ-ᅟ⺀-꓏ꥠ-꥿가-퟿豈-﫿"
    r"︐-︙︰-﹯＀-｠￠-￦]"
)


def _twitter_covers(media: object) -> tuple[str, ...]:
    """推文配图 → 封面 URL。**第一条**，没有就返回 None。

    **强制 `name=orig`（原图）** —— 这是明确的产品要求：B站 / YouTube 的封面
    压缩无所谓，推文的图必须无损。fxtwitter 给的本来就是 `?name=orig`，
    这里统一改写一遍，免得哪天它的默认档变了。

    实测各档差多少（一条 2500x3500 的竖图）：orig 943KB / large 434KB /
    medium 180KB / small 70KB。**原图小的时候 Twitter 不放大**，
    orig 和 large 会一样大（1200x800 那条三档都是 92.6KB），所以用 orig 没有额外代价。

    代价是 base64 之后会更大（943KB → 1.29MB），见 `MAX_IMAGE_BYTES` 的说明。
    """
    if not isinstance(media, dict):
        return ()
    photos = media.get("photos")
    if not isinstance(photos, list):
        return ()
    out: list[str] = []
    for photo in photos[:MAX_IMAGES]:
        if not isinstance(photo, dict):
            continue
        url = str(photo.get("url") or "")
        if not url:
            continue
        out.append(
            re.sub(r"name=\w+", "name=orig", url) if "name=" in url else f"{url}?name=orig"
        )
    return tuple(out)


def _text_from_oembed(html_fragment: str) -> str:
    """从 oEmbed 的 blockquote 里抠出正文。

    形态固定（实测）：`<blockquote class="twitter-tweet"><p ...>正文</p>&mdash;
    作者 (@handle) <a ...>日期</a></blockquote><script ...>`。
    取第一个 `<p>` 的内容，去掉标签和 `&mdash;` 之后的所有尾巴。
    """
    m = re.search(r"<p[^>]*>(.*?)</p>", html_fragment, re.S)
    if not m:
        return ""
    text = m.group(1)
    # 链接在正文里是 `<a ...>显示文字</a>`，留显示文字
    text = re.sub(r"<a[^>]*>(.*?)</a>", r"\1", text, flags=re.S)
    text = re.sub(r"<br\s*/?>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
