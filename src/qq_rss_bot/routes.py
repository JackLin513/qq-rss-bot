"""RSSHub 路由检索：按关键词找路由，而不是靠记路径。

RSSHub 实例自带完整路由表（``GET /api/namespace``）。本地实测：4MB、
2064 个站点、4004 条路由、24 个分类，一次 0.12s。

每条路由带 ``features``，能判断它在**本实例**上跑不跑得起来 ——
这正是「照着文档推荐了路由、结果 503」那个坑的解药：

    requirePuppeteer: true            → 需要浏览器，默认镜像跑不了
    requireConfig: [{name: 'XXX'}]    → 需要环境变量，没配就 503
    antiCrawler: true                 → 有反爬，可能要 cookie / 代理

实测分布：开箱可用 3410、需要配置 130、有反爬 283、需要浏览器 181。

注意 ``requireConfig`` 里可能有 ``optional: true`` 的项（微博的
``WEIBO_COOKIES`` 就是），那种不配置也能用，不能当成阻塞。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

_log = logging.getLogger("qq_rss_bot.routes")

API_PATH = "/api/namespace"
DEFAULT_TTL_SEC = 24 * 3600
FETCH_TIMEOUT_SEC = 60

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_BAD = "bad"

_MARK = {LEVEL_OK: "✅", LEVEL_WARN: "⚠️", LEVEL_BAD: "❌"}

# 官方分类是英文 slug，这里给中文名 —— 群里没人会去搜 "new-media"
CATEGORY_ZH = {
    "anime": "动漫",
    "bbs": "论坛",
    "blog": "博客",
    "design": "设计",
    "finance": "金融",
    "forecast": "预报",
    "game": "游戏",
    "government": "政府",
    "journal": "期刊",
    "live": "直播",
    "multimedia": "多媒体",
    "new-media": "新媒体",
    "other": "其他",
    "picture": "图片",
    "program-update": "程序更新",
    "programming": "编程",
    "reading": "阅读",
    "shopping": "购物",
    "social-media": "社交媒体",
    "sport": "体育",
    "study": "学习",
    "traditional-media": "传统媒体",
    "travel": "出行",
    "university": "大学",
}


def _availability(features: object) -> tuple[str, str]:
    """从 features 判断这条路由在本实例上的可用性。"""
    f = features if isinstance(features, dict) else {}

    # 浏览器最先判：换镜像才能解决，是唯一「本实例铁定跑不了」的一类
    if f.get("requirePuppeteer"):
        return LEVEL_BAD, "需要浏览器，得换 chromium-bundled 镜像"

    cfg = f.get("requireConfig")
    if isinstance(cfg, list):
        need = [
            c.get("name")
            for c in cfg
            if isinstance(c, dict) and c.get("name") and not c.get("optional")
        ]
        if need:
            return LEVEL_WARN, f"需要配置 {'、'.join(need)}"

    if f.get("antiCrawler"):
        return LEVEL_WARN, "有反爬，可能要 cookie 或代理"

    return LEVEL_OK, ""


class RouteHit:
    """一条可订阅的路由。"""

    __slots__ = ("path", "name", "site", "site_url", "level", "note", "cats", "hay")

    def __init__(
        self,
        path: str,
        name: str,
        site: str,
        site_url: str,
        level: str,
        note: str,
        cats: object = (),
    ) -> None:
        self.path = path
        self.name = name
        self.site = site
        self.site_url = site_url
        self.level = level
        self.note = note
        self.cats = tuple(cats or ())
        zh = [CATEGORY_ZH.get(c, "") for c in self.cats]
        self.hay = " ".join(
            [path, name, site, site_url, *self.cats, *zh]
        ).lower()

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "site": self.site,
            "site_url": self.site_url,
            "level": self.level,
            "note": self.note,
            "cats": list(self.cats),
        }

    def score(self, q: str) -> int:
        """关键词命中打分。0 = 不命中。

        路径命中权重最高 —— 用户要的就是「能直接 /sub 的那个串」。
        """
        if q not in self.hay:
            return 0
        path_l = self.path.lower()
        if path_l == q or path_l.lstrip("/") == q:
            return 100
        if q in path_l:
            return 70
        if q in self.name.lower():
            return 60
        if q in self.site.lower():
            return 50
        if q in self.site_url.lower():
            return 40
        return 30  # 只命中分类

    def __repr__(self) -> str:
        return f"RouteHit({self.path!r}, {self.level})"


class RouteIndex:
    """全部路由，按需检索。构建一次常驻内存。"""

    def __init__(self, hits: list[RouteHit]) -> None:
        self._hits = hits

    def __len__(self) -> int:
        return len(self._hits)

    @classmethod
    def parse(cls, payload: object) -> "RouteIndex":
        """把 /api/namespace 的原始响应压成 RouteHit 列表。"""
        hits: list[RouteHit] = []
        if not isinstance(payload, dict):
            return cls(hits)
        for ns_key, ns in payload.items():
            if not isinstance(ns, dict):
                continue
            site = ns.get("name") or str(ns_key)
            site_url = ns.get("url") or ""
            ns_cats = ns.get("categories") or []
            routes = ns.get("routes")
            if not isinstance(routes, dict):
                continue
            for rpath, r in routes.items():
                if not isinstance(r, dict):
                    continue
                # example 是可直接订阅的成品路径；没有就退回带占位符的 path，
                # 至少让用户知道这条路由存在、参数长什么样。
                level, note = _availability(r.get("features"))
                hits.append(
                    RouteHit(
                        path=r.get("example") or rpath,
                        name=r.get("name") or rpath,
                        site=site,
                        site_url=site_url,
                        level=level,
                        note=note,
                        cats=r.get("categories") or ns_cats,
                    )
                )
        return cls(hits)

    def search(self, query: str, limit: int = 5) -> tuple[int, list[RouteHit]]:
        """返回 (命中总数, 前 limit 条)。

        排序：先按打分，同分时**可用的排前面** —— 搜出来的东西
        跑不起来是最没用的结果。
        """
        q = (query or "").strip().lower()
        if not q:
            return 0, []
        scored = [(h.score(q), h) for h in self._hits]
        matched = [(s, h) for s, h in scored if s]
        matched.sort(key=lambda t: (-t[0], 0 if t[1].level == LEVEL_OK else 1, len(t[1].path)))
        return len(matched), [h for _, h in matched[:limit]]

    def to_dict(self) -> dict:
        return {"hits": [h.as_dict() for h in self._hits]}

    @classmethod
    def from_dict(cls, data: object) -> "RouteIndex":
        hits = data.get("hits") if isinstance(data, dict) else None
        if not isinstance(hits, list):
            raise ValueError("路由缓存格式不对")
        return cls([RouteHit(**h) for h in hits])


def format_hits(query: str, total: int, hits: list[RouteHit], limit: int) -> str:
    """把搜索结果排成群消息。"""
    if not hits:
        cats = "、".join(list(CATEGORY_ZH.values())[:8])
        return (
            f"没找到「{query}」。\n"
            f"可以试站点名（github、知乎、36kr）、路由关键词，或分类名。\n"
            f"分类比如：{cats}…"
        )
    lines = [f"「{query}」找到 {total} 条，显示前 {len(hits)}："]
    for i, h in enumerate(hits, 1):
        mark = _MARK.get(h.level, "")
        tail = f"（{h.note}）" if h.note else ""
        site = h.site or h.site_url
        lines.append(f"{i}. {mark} {site}｜{h.name}{tail}")
        lines.append(f"   {h.path}")
    if total > limit:
        lines.append(f"还有 {total - limit} 条，换个更具体的关键词。")
    lines.append("用 /sub <路径> 订阅。")
    return "\n".join(lines)


async def load_index(
    cache_path: Path,
    http,
    base_url: str,
    ttl_sec: int = DEFAULT_TTL_SEC,
) -> RouteIndex:
    """取路由表：新鲜缓存 → 网络 → 过期缓存兜底。

    最后那条兜底是刻意的：RSSHub 挂了的时候，用旧表总比直接报错好。
    """
    stale: Path | None = None
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < ttl_sec:
            try:
                return RouteIndex.from_dict(json.loads(cache_path.read_text()))
            except (OSError, ValueError) as exc:
                _log.warning("路由缓存读不出来，重新拉：%r", exc)
        else:
            stale = cache_path

    try:
        resp = await http.get(base_url.rstrip("/") + API_PATH, timeout=FETCH_TIMEOUT_SEC)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 —— 网络层什么都可能抛
        if stale is not None:
            _log.warning("路由表拉取失败，用过期缓存兜底：%r", exc)
            try:
                return RouteIndex.from_dict(json.loads(stale.read_text()))
            except (OSError, ValueError):
                pass
        raise

    index = RouteIndex.parse(payload)
    _log.info("路由表已更新：%d 条", len(index))
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(index.to_dict(), ensure_ascii=False))
    except OSError as exc:
        _log.warning("路由缓存写不进去（不影响本次使用）：%r", exc)
    return index
