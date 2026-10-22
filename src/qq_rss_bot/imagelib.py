"""图库：**内容寻址**地存图，并给出 QQ 抓得到的公网地址。

## 两个角色，别混（见 GLOSSARY）

- **图库**（这里）：长期资产，按 角色 / 日期 / 来源 查得回来，不自动过期。
- **图床**（`imgsrv.py`）：只负责把一张已入库的图通过 HTTP 发出去。

存储物理布局（ADR 0002）：

    <库根>/blobs/<sha 前两位>/<sha>.<扩展名>

文件名就是内容的 sha256。**同一张图被贺图和预览各引用一次，只存一份文件**，
索引里是两行 `image_refs`。分类是查询（`db.image_refs()`），不是目录结构 ——
一张图同时属于「某个角色」「某一天」「某个作者」，塞进目录只能留一个维度。

## 元数据都在入库那一刻抓

能抓到的时候不存，事后补不了：推特会删，作者会改名，点赞会一直涨。
所以 `author_name` / `author_category` / `likes_at_capture` 这些字段的名字里
都带"当时"的意思 —— 它们是**快照**，不是最新值。

## ⚠️ 地址里绝不加 query 参数

QQ 的图片代理碰到多余的 query 参数就抓不到，一张都不渲染（`multiimage.py`
的模块说明里有完整实测）。所以 URL 就是 `<base>/i/<sha>.<ext>`，干净为止。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import imgsrv, multiimage

_log = logging.getLogger("qq_rss_bot.imagelib")

# 图的类别。数据库里存这几个 ASCII 值，**不存中文** —— 中文留给界面文案，
# 落库的值要能当稳定的查询键。
KIND_ART = "art"          # 贺图（有角色）
KIND_PREVIEW = "preview"  # 群里分享链接回的那张（没有角色）
KIND_FEED = "feed"        # 订阅推送里的配图（没有角色）

KINDS = (KIND_ART, KIND_PREVIEW, KIND_FEED)


@dataclass(frozen=True)
class ImageRef:
    """入库的结果。`sha256` 是内容的身份，`url` 是给 QQ 抓的地址。"""

    sha256: str
    ext: str
    url: str
    width: int
    height: int
    fresh: bool  # 这次是不是第一次见到这份内容（诊断用）


class Library:
    """存图 + 索引。**只做这两件事**，不负责 HTTP（那是图床的活）。"""

    def __init__(self, db, root: str | Path, base: str) -> None:
        self.db = db
        self.root = Path(root) / "blobs"
        self.base = (base or "").rstrip("/")
        if not self.base:
            _log.warning(
                "图库：没有配对外地址（IMGSRV_BASE 为空）—— 图会照常入库，"
                "但消息里的图片地址 QQ 抓不到，群里不会有图")

    @property
    def enabled(self) -> bool:
        return bool(self.base)

    # ---------- 寻址 ----------

    def path_of(self, sha256: str, ext: str) -> Path:
        return self.root / sha256[:2] / f"{sha256}{ext}"

    def url_of(self, sha256: str, ext: str) -> str:
        return f"{self.base}/i/{sha256}{ext}"

    # ---------- 入库 ----------

    def store(
        self,
        raw: bytes,
        *,
        kind: str,
        event_date: str,
        role: str = "",
        brand: str = "",
        author_handle: str = "",
        author_name: str = "",
        author_category: str = "",
        likes: int | None = None,
        posted_at: datetime | str | None = None,
        source_url: str = "",
        page_url: str = "",
        source_id: str = "",
    ) -> ImageRef | None:
        """存一份图并记一次引用，返回它的公网地址。

        超过 `imgsrv.MAX_BYTES` 的**不收**（返回 None）—— 图床也不会服务它，
        收了等于给自己留一个永远打不开的地址。
        """
        if not raw:
            return None
        if len(raw) > imgsrv.MAX_BYTES:
            _log.warning("图库：%s 有 %d 字节，超过 %d，不入库",
                         source_id or "?", len(raw), imgsrv.MAX_BYTES)
            return None
        if kind not in KINDS:
            raise ValueError(f"未知的图类别：{kind!r}")

        sha = hashlib.sha256(raw).hexdigest()
        ext = imgsrv.suffix_of(raw)
        size = multiimage.image_size(raw) or (0, 0)
        width, height = size

        target = self.path_of(sha, ext)
        had = self.db.image_has(sha)
        if not had:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(raw)
        # 索引和文件分开判：文件在但索引缺（上次写到一半崩了）要能补上。
        fresh = self.db.image_put(
            sha256=sha, ext=ext, mime=imgsrv.mime_of(ext),
            size=len(raw), width=width, height=height)
        self.db.image_ref_add(
            sha256=sha, kind=kind, event_date=event_date, role=role,
            brand=brand, author_handle=author_handle, author_name=author_name,
            author_category=author_category, likes_at_capture=likes,
            posted_at=_iso(posted_at), source_url=source_url,
            page_url=page_url, source_id=source_id,
        )
        return ImageRef(sha256=sha, ext=ext, url=self.url_of(sha, ext),
                        width=width, height=height, fresh=fresh or not had)


def _iso(value: datetime | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


async def mirror(
    library: "Library | None",
    http,
    url: str,
    *,
    enabled: bool = True,
    kind: str,
    event_date: str,
    source_url: str = "",
    page_url: str = "",
    source_id: str = "",
) -> str:
    """第三方图床的图 → **我们自己的图库地址**。失败只降级这一张。

    bilibili / 抽票 / RSS 的图本来就由 QQ 自己去抓，能显示；镜像它们的意义是
    「图留在自己手里」（第三方删图、防盗链、CDN 换域名都不再影响历史消息）。

    ⚠️ **任何一步不成（下载失败 / 超体积 / 图库没配）都原样返回 `url`** ——
    一行 markdown 里只有这一张退回第三方地址，其余照常走图库，消息照发。
    整条发不出去是比「这张从别处加载」严重得多的错误。
    """
    if not enabled or library is None or not library.enabled or not url or http is None:
        return url
    # 局部 import：preview 依赖 multiimage，模块级互相 import 会绕成环。
    from . import preview

    raw = await preview.fetch_image(http, url, max_bytes=imgsrv.MAX_BYTES)
    if raw is None:
        return url
    ref = library.store(
        raw,
        kind=kind,
        event_date=event_date,
        source_url=source_url or url,
        page_url=page_url,
        source_id=source_id,
    )
    return ref.url if ref is not None else url
