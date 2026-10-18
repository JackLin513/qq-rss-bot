"""偶像大师官网 NEWS 的「企划 / 分类」标签。

**为什么需要这个模块。** RSSHub 的 `/idolmaster/news` 只把 CMS 的
`categories.subcategory` 映射成了 RSS 的 `<category>`，而且给的是**日文原名**
（`グッズ`、`ライブ・イベント`）；`brand`（企划）**一个字节都没输出**。
而群里最想知道的恰恰是「这条是哪个企划的」，所以只能自己调 CMS 补。

一次 `Article/list` 请求就**同时**拿到两个属性（都是 code 形式），所以
RSS 侧一个字都不用改 —— 不用去解析 `<category>`，也就没有
「日文原名 → code」那层反查。

实测（2026-10-18）：

* CMS API 从 NAS **不走代理**直连得通（200 / 0.57s），所以用 `rss_http`
  而不是 `media_http`（后者挂了代理，是给 YouTube / X 用的）。
* CMS 的 `url` 字段和 RSS 的 `<link>` **完全一致**（实测 11/11 对得上），
  所以可以按 URL 建映射。
* 一篇文章可以挂**多个企划**（180 篇里 21 篇多企划，其中 5 篇七个全挂 ——
  周年 / 通稿类），也可以挂**多个分类**（106 篇 1 个 / 65 篇 2 个 / 9 篇 3 个）。
* `brand` 数组的返回顺序是固定的，直接按序 join 就是想要的显示顺序，
  不用自己排。

任何一步失败都**静默降级成不带标签**，绝不抛 —— 这条路在推送主循环里，
标签拿不到只是少一行字，不该连累推送。
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

    from .rss import Entry

_log = logging.getLogger(__name__)

API_BASE = "https://cmsapi-frontend.idolmaster-official.jp"
_TOKEN_URL = f"{API_BASE}/sitern/api/cmsbase/Token/get"
_LIST_URL = f"{API_BASE}/sitern/api/idolmaster/Article/list"

# 一次拿 30 篇。RSSHub 那条路由的上限就是 30，而路由本身一次给 12 条、
# 订阅一次最多推 MAX_ITEMS_PER_PUSH（默认 5）条 —— 30 足够覆盖。
_LIST_LIMIT = 30

_HINT = re.compile(r"idolmaster-official\.jp|/idolmaster/", re.I)

# 企划。**这个 dict 的顺序就是显示顺序**（CMS 返回的也是这个序）。
# 「男人」= SideM（男团那条线）、「闪彩」= シャイニーカラーズ、
# 「学马」= 学園アイドルマスター。
BRAND_NAMES: dict[str, str] = {
    "IDOLMASTER": "本家",
    "CINDERELLAGIRLS": "灰姑娘",
    "MILLIONLIVE": "百万",
    "SIDEM": "男人",
    "SHINYCOLORS": "闪彩",
    "GAKUEN": "学马",
    "OTHER": "其他",
}

# 分类。**按 code 翻而不是按日文原名翻**，因为 code 更稳定。
# 两个 code 名不副实，别照着 code 直译：
#   · `CD` —— 日文原名是「ミュージック」（音乐），含数字发行和主题歌，不只 CD；
#   · `LIVE-EVENT` —— 日文是「ライブ・イベント」，涵盖演唱会和线下活动
#     （快闪店、见面会、上映会），所以译「演出」而不是「演唱会」。
TYPE_NAMES: dict[str, str] = {
    "GOODS": "谷子",
    "LIVE-EVENT": "演出",
    "COLLABO-CAMP": "联动",
    "LIVESTREAM": "直播",
    "CD": "音乐",
    "GAME": "游戏",
    "RADIO": "广播",
    "MEDIA": "媒体",
    "ANIME": "动画",
    "BOOK": "书籍",
    "OTHER": "其他",
}


def matches(url: str) -> bool:
    """这个订阅是不是偶像大师官网 NEWS。

    按 **URL** 判而不是按订阅名判 —— 订阅名是 feed 的 `<title>`，用户改不了，
    而 7 个企划路由的标题**一模一样**（都是 `NEWS | アイドルマスター`），
    靠名字根本判不出来。
    """
    return bool(_HINT.search(url))


def _render(codes: tuple[str, ...], table: dict[str, str]) -> str:
    """`('CINDERELLAGIRLS', 'MILLIONLIVE')` → `'灰姑娘/百万'`。

    表里没有的 code **原样保留** —— CMS 加新企划时宁可显示成 `【NEWBRAND】`
    让人看见，也不要静默丢掉（静默丢的症状是「这条怎么没标签」，很难查）。
    """
    return "/".join(table.get(code, code) for code in codes)


def tag_line(brands: tuple[str, ...], types: tuple[str, ...]) -> str:
    """`【灰姑娘】【演出/谷子】`。两边都空 → 空串（调用方据此不加这一行）。"""
    parts = []
    if brands:
        parts.append(f"【{_render(brands, BRAND_NAMES)}】")
    if types:
        parts.append(f"【{_render(types, TYPE_NAMES)}】")
    return "".join(parts)


def _codes(items: object) -> tuple[str, ...]:
    """CMS 的 `[{code, name}, ...]` → `(code, ...)`，顺手滤掉脏数据。"""
    if not isinstance(items, list):
        return ()
    return tuple(
        item["code"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("code"), str) and item["code"]
    )


async def fetch_meta(
    http: httpx.AsyncClient, *, timeout: float = 15.0
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """CMS 最新 30 篇的 `文章URL → (企划codes, 分类codes)`。任何失败 → `{}`。

    两步：先换 token，再查列表。token 是一次性的，**不做跨轮缓存** ——
    一次轮询多一个请求（~0.6s）换掉一整类缓存失效问题，划算。
    """
    try:
        token_rsp = await http.get(_TOKEN_URL, timeout=timeout)
        token_rsp.raise_for_status()
        token = (token_rsp.json().get("data") or {}).get("token")
        if not token:
            _log.debug("imas：token 响应里没有 token")
            return {}

        list_rsp = await http.get(
            _LIST_URL,
            params={
                "site": "jp",
                "ip": "idolmaster",
                "token": token,
                "sort": "desc",
                "data": json.dumps({"category": ["NEWS"]}),
                "limit": _LIST_LIMIT,
                "start": 0,
            },
            timeout=timeout,
        )
        list_rsp.raise_for_status()
        articles = (list_rsp.json().get("data") or {}).get("article_list") or []
    except Exception as exc:  # noqa: BLE001
        # 这条路在推送主循环里。标签拿不到只是少一行字，绝不能连累推送。
        _log.debug("imas：取分类失败，本条不带标签 (%s)", exc)
        return {}

    out: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for article in articles:
        if not isinstance(article, dict):
            continue
        url = article.get("url")
        if not isinstance(url, str) or not url:
            continue
        out[url] = (
            _codes(article.get("brand")),
            _codes((article.get("categories") or {}).get("subcategory")),
        )
    return out


async def tags_for(
    http: httpx.AsyncClient,
    url: str,
    entries: "list[Entry] | tuple[Entry, ...]",
    *,
    timeout: float = 15.0,
) -> dict[str, str]:
    """`{entry.key: 标签行}`。非偶像大师订阅、或任何一步失败 → `{}`。"""
    if not entries or not matches(url):
        return {}
    meta = await fetch_meta(http, timeout=timeout)
    if not meta:
        return {}

    out: dict[str, str] = {}
    for entry in entries:
        got = meta.get(entry.link) or meta.get(entry.key)
        if got is None:
            continue
        line = tag_line(*got)
        if line:
            out[entry.key] = line
    return out
