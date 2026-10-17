"""RSSHub / RSS 抓取与解析。

RSSHub 本身只是个 HTTP 服务：给它一条路由（如 /github/trending/daily/python），
它返回标准 RSS 2.0 XML。所以这里只需要 httpx 发请求 + feedparser 解析，
不依赖 RSSHub 的任何 SDK。
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import feedparser
import httpx

_log = logging.getLogger("qq_rss_bot.rss")

_TAG_RE = re.compile(r"<[^>]+>")

# 从正文 HTML 里抠配图。
# 注意两个坑：
#   1. 优先取 <img src>，比全篇扫 URL 准（正文里常混着站内链接）
#   2. 有些站把扩展名藏在 query 里（如 idolmaster 的 .../Image/get?path=.../x.jpeg），
#      所以 URL 分支必须连着 query 一起匹配，不能匹配到 "\\.jpg" 就收工
_IMG_SRC_RE = re.compile(r"""<img[^>]*?\ssrc\s*=\s*["']([^"']+)["']""", re.I)
_IMG_URL_RE = re.compile(
    r"""https?://[^\s"'<>()]+?\.(?:jpe?g|png|gif|webp|bmp|avif)(?:\?[^\s"'<>()]*)?""",
    re.I,
)


# 一条资讯最多带几张图。**不是随便定的**：这条走的是主动消息，每张图
# **各占一条配额**（20 条/分钟、每天还有上限），4 张图就是 4 条配额。
# 上限压在这个数，是为了让「一条资讯」不至于吃掉一天的额度。
MAX_IMAGES = 4


def all_images(*html_parts: str | None, limit: int = MAX_IMAGES) -> tuple[str, ...]:
    """从若干段 HTML 里按出现顺序取图，去重、限量。取不到就是空元组。

    顺序沿用 `first_image` 时代的老规矩：**段内先 `<img src>`、再扫裸 URL**，
    段落之间按传入顺序 —— 换成「先把所有 `<img>` 收完」会打乱正文里的图序。

    多图之后**去重变成必需品**（单图时代第一张就返回了，重复无所谓）：
    不少源会把同一张图既放进 `<img>` 又留一个裸链接，不去重就会发两遍。
    """
    seen: set[str] = set()
    out: list[str] = []

    def take(url: str) -> bool:
        """收下一张；返回 True 表示已经够数了，可以停。"""
        if not url or url in seen:
            return False
        seen.add(url)
        out.append(url)
        return len(out) >= limit

    for html in html_parts:
        if not html:
            continue
        for match in _IMG_SRC_RE.finditer(html):
            url = match.group(1).strip()
            if url.startswith("//"):
                url = "https:" + url
            if not url.startswith(("http://", "https://")):
                continue
            if take(url):
                return tuple(out)
        for found in _IMG_URL_RE.finditer(html):
            if take(found.group(0)):
                return tuple(out)
    return tuple(out)


def resolve_url(raw: str, base: str) -> str:
    """把用户输入统一成完整 URL。

    支持三种写法：
      /github/trending/daily/python        → {base}/github/trending/daily/python
      github/trending/daily/python         → 同上
      https://example.com/feed.xml         → 原样返回
    """
    raw = raw.strip()
    if raw.startswith(("http://", "https://")):
        return raw
    return f"{base.rstrip('/')}/{raw.lstrip('/')}"


def _clean_text(value: str | None, limit: int = 200) -> str:
    if not value:
        return ""
    text = html.unescape(_TAG_RE.sub("", value))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


@dataclass(frozen=True)
class Entry:
    key: str
    title: str
    link: str
    published: datetime | None
    summary: str
    images: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """防呆：`images` 传成**字符串**时要在这里拦住。

        它不会报错，只会静默变成「一串单字符的图片 URL」—— 症状是群里冒出
        `（图 2/19）`…`（图 19/19）` 十九条消息，每个字符一条。写测试时用
        位置参数传了个字符串就踩到了，所以加这道闸。
        """
        if isinstance(self.images, str):
            self.images = (self.images,) if self.images else ()

    @property
    def image(self) -> str | None:
        """第一张图。

        **保留是为了不炸旧调用点**（scheduler / commands / smoke 都在用）。
        新的多图代码请直接用 `images` —— 它才是真相。
        """
        return self.images[0] if self.images else None

    @classmethod
    def from_raw(cls, raw: dict) -> "Entry":
        link = (raw.get("link") or "").strip()
        title = _clean_text(raw.get("title"), 120) or "(无标题)"
        # guid > link > 标题+时间 的退化链，保证去重 key 尽量稳定
        key = (raw.get("id") or "").strip()
        if not key:
            key = link or hashlib.sha1(
                f"{title}|{raw.get('published', '')}".encode()
            ).hexdigest()

        published = None
        for field_name in ("published_parsed", "updated_parsed"):
            parsed = raw.get(field_name)
            if parsed:
                try:
                    published = datetime(*parsed[:6])
                except (TypeError, ValueError):
                    published = None
                break

        # 配图必须在 _clean_text 剥标签之前取，剥完 URL 就没了。
        # Atom 源的正文在 content[].value 里，RSS 的在 summary 里，两个都试。
        contents = [c.get("value") for c in (raw.get("content") or []) if isinstance(c, dict)]

        return cls(
            key=key,
            title=title,
            link=link,
            published=published,
            # **别在这里切太狠。** 很多源的 `<description>` 是完整导语（几百字），
            # 以前默认 200 一刀下去，后面怎么调 `SUMMARY_CHARS` 都没用 ——
            # 内容在进对象的这一步就没了。真正的长度上限在 `format_entry`。
            summary=_clean_text(raw.get("summary"), 4000),
            images=all_images(raw.get("summary"), *contents),
        )


@dataclass
class FeedResult:
    url: str
    title: str | None = None
    entries: list[Entry] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _parse(content: bytes) -> tuple[str | None, list[Entry]]:
    """feedparser 是同步阻塞的，放在线程里跑。"""
    parsed = feedparser.parse(content)
    feed_title = _clean_text(parsed.feed.get("title"), 80) or None
    entries = [Entry.from_raw(e) for e in parsed.entries]
    return feed_title, entries


async def fetch_feed(client: httpx.AsyncClient, url: str, timeout: int) -> FeedResult:
    result = FeedResult(url=url)
    try:
        resp = await client.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        result.error = f"HTTP {exc.response.status_code}"
        return result
    except httpx.HTTPError as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    try:
        title, entries = await asyncio.to_thread(_parse, resp.content)
    except Exception as exc:  # feedparser 对畸形 XML 偶尔会抛
        result.error = f"解析失败: {exc}"
        return result

    if not entries:
        result.error = "该地址没有解析出任何条目（路由可能写错，或上游已失效）"
        return result

    result.title, result.entries = title, entries
    return result
