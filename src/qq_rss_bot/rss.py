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


def first_image(*html_parts: str | None) -> str | None:
    """从若干段 HTML 里取第一张图，取不到返回 None。"""
    for html in html_parts:
        if not html:
            continue
        for match in _IMG_SRC_RE.finditer(html):
            url = match.group(1).strip()
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith(("http://", "https://")):
                return url
        found = _IMG_URL_RE.search(html)
        if found:
            return found.group(0)
    return None


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
    image: str | None = None

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
            summary=_clean_text(raw.get("summary")),
            image=first_image(raw.get("summary"), *contents),
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
