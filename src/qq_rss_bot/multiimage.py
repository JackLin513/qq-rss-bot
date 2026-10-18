"""markdown 多图：一条消息带 N 张图。

**背景。** `msg_type=7` 富媒体的 `MediaInfo` 只有一个 `file_info`，一条只能带一张，
所以多图只能连发 N 条 —— 一条 4 图的资讯就是 4 条消息，占 4 份主动消息配额、还刷屏。
`msg_type=2` markdown 可以在正文里放任意多个 `![](url)`，一条就够。

**代价：图由 QQ 的服务器自己去下载。** 富媒体那条路其实也是（`send_rich` 传的是
`url`），所以这不算新增的失败面。

**尺寸提示是必填的，而且比例必须对**（2026-10-18 实测，手机端和电脑端都看过）：

* 手机端**不带** `#宽px #高px` → **整条 markdown 不渲染**，裸文本打出来；
  电脑端带不带都行。所以尺寸提示不是「锦上添花」，缺了就是全废。
* 比例写错（比如统一按 16:9）→ 图被拉伸。所以必须**逐张读真实宽高**，
  不能像 bilibili 封面那样硬编码 —— 那个源给了 `@480w_270h_1c.jpg` 自己裁成
  16:9，这里没有那个保证。

**踩过的坑：绝不要给图片 URL 加 `_cb` 之类的「缓存击穿」参数。**
2026-10-18 排查时我给 CMS 的图 URL 加了 `&_cb=<时间戳>`，想造一个 QQ 没见过的
「冷 URL」。结果**带 `_cb` 的图一张都不渲染，不带的全好** —— 同一时间、同一格式的
对照：2 张原始 URL 都显示、2 张带 `_cb` 的都不显示；换成从没被抓过的图（CMS 第 61 篇
以后的旧新闻）用原始 URL，照样全显示。**QQ 的图片代理碰到多余的 query 参数就抓不到。**

这个假象让我一度得出「冷启动失败 / 需要预热 / 尺寸超限」等一串错误结论，绕了十几轮。
**排查图片问题时，先确认 URL 和生产环境完全一致，再怀疑别的。**
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:
    import httpx

_log = logging.getLogger("qq_rss_bot.multiimage")

# 渲染宽度固定 208（和 bilibili 封面一致），高度按真实比例算，夹在 [40, 400]。
COVER_W = 208
MIN_H, MAX_H = 40, 400

# 一条消息最多渲染几张。和 `rss.MAX_IMAGES`（摄入上限）对齐 —— 两个数不一致
# 的话，多出来的图会在某一层被静默砍掉，症状是「这条明明有 6 张只出来 4 张」。
# 真正的硬约束是正文长度，超了 `build()` 会从尾部丢图。
MAX_IMAGES = 6

# 只取前 32KB —— PNG 要 24 字节、JPEG 要扫到 SOFn，够用。
_PROBE_BYTES = 32768
_PROBE_TIMEOUT = 10.0

# markdown 正文的长度软上限。超了就**从尾部丢图**（丢图比截断语法好：
# 截断会把 `![图1 #208px #117px](http://…` 砍成半截，那条消息整条都不渲染）。
#
# **这个数是安全网，不是常规路径。** 一开始拍的 1400 是错的 —— 偶像大师的图 URL
# 约 152 字符，一行 `![图1 #208px #117px](…)` 就 ~175 字符，6 行 = 1050；再加
# 标题 + 标签 + 800 字摘要 ≈ 900，合计 ~1950 就**超了**，于是一张 6 图的资讯
# 又变成只出 3、4 张，症状和 `rss.MAX_IMAGES` 那个坑一模一样。
# QQ 那边实测纯文本 40000 字符照收（见 CLAUDE.md 的字数上限那节），
# 而正文长度本来就被 `SUMMARY_CHARS` 卡着，所以放宽到 4000 —— 现实组合够不到。
# 万一真超了，`send_markdown` 报错会落回连发，那条路不会丢图。
MAX_CHARS = 4000


def image_size(data: bytes) -> "tuple[int, int] | None":
    """从图片头部读宽高。PNG / GIF / JPEG / WebP，读不到返回 None。"""
    if len(data) < 30:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":
        return _jpeg_size(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_size(data)
    return None


def _jpeg_size(data: bytes) -> "tuple[int, int] | None":
    """扫到第一个 SOFn 段 —— 宽高就在它里面。"""
    i, n = 2, len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        ):
            h = int.from_bytes(data[i + 5 : i + 7], "big")
            w = int.from_bytes(data[i + 7 : i + 9], "big")
            return (w, h) if w and h else None
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2 : i + 4], "big")
    return None


def _webp_size(data: bytes) -> "tuple[int, int] | None":
    chunk = data[12:16]
    if chunk == b"VP8X":
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    if chunk == b"VP8 ":
        w = int.from_bytes(data[26:28], "little") & 0x3FFF
        h = int.from_bytes(data[28:30], "little") & 0x3FFF
        return (w, h) if w and h else None
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


async def measure(
    http: "httpx.AsyncClient", url: str, *, timeout: float = _PROBE_TIMEOUT
) -> "tuple[int, int] | None":
    """只取头部读宽高。读不到就返回 None —— 这张图会被丢掉。

    **这一步顺带是一次可达性检查**：我们这边都拉不到，QQ 那边多半也拉不到。
    """
    try:
        resp = await http.get(
            url, headers={"Range": f"bytes=0-{_PROBE_BYTES - 1}"}, timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001 —— 读不到尺寸只是少一张图
        _log.debug("多图：读尺寸失败 %s (%s)", url, exc)
        return None
    if resp.status_code >= 400:
        _log.debug("多图：读尺寸 HTTP %s %s", resp.status_code, url)
        return None
    return image_size(resp.content)


def box_for(wh: "tuple[int, int]") -> "tuple[int, int]":
    """真实宽高 → 渲染框。宽度固定，高度按比例，夹在 [MIN_H, MAX_H]。"""
    w, h = wh
    out_h = round(COVER_W * h / w) if w > 0 else COVER_W
    return COVER_W, max(MIN_H, min(MAX_H, out_h))


def image_line(index: int, url: str, wh: "tuple[int, int]") -> str:
    w, h = box_for(wh)
    return f"![图{index} #{w}px #{h}px]({url})"


async def measure_all(
    http: "httpx.AsyncClient",
    images: "Sequence[str] | Iterable[str]",
    *,
    limit: int = MAX_IMAGES,
) -> "list[tuple[str, tuple[int, int]]]":
    """逐张读尺寸，读不到的丢掉。返回 `[(url, (w, h)), …]`。

    丢掉而不是猜一个比例：猜错就是图被拉伸，用户一眼看得出来（这个坑踩过）。
    """
    out: list[tuple[str, tuple[int, int]]] = []
    for url in list(images)[:limit]:
        wh = await measure(http, url)
        if wh:
            out.append((url, wh))
        else:
            _log.info("多图：这张读不到尺寸，跳过 %s", url)
    return out


def build(text: str, items: "Sequence[tuple[str, tuple[int, int]]]", link: str = "") -> str:
    """正文 + N 张图 + 原文链接。

    `text` 是已经排好版的推送文案（`format_entry(..., with_link=False)`）——
    链接放最后，图夹在正文和链接之间。
    """
    lines = [text]
    for i, (url, wh) in enumerate(items, 1):
        lines.append(image_line(i, url, wh))
    if link:
        lines.append(f"[打开原文]({link})")

    # 超长就**从尾部丢图**，不截断 —— 截断会把 `![图1 #208px #117px](http://…`
    # 砍成半截，那条消息整条都不渲染（见模块 docstring）。
    while len(items) > 1 and len("\n".join(lines)) > MAX_CHARS:
        items = items[:-1]
        lines = [text]
        for i, (url, wh) in enumerate(items, 1):
            lines.append(image_line(i, url, wh))
        if link:
            lines.append(f"[打开原文]({link})")
        _log.info("多图：正文超长，丢掉最后一张（剩 %d 张）", len(items))
    return "\n".join(lines)
