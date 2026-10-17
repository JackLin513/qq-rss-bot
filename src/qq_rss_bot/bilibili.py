"""群里分享 bilibili 链接时，自动回一张带封面和数据的卡片。

**为什么能做**：QQ 2026 新协议下平台推的是**全量群消息**（`GROUP_MESSAGE_CREATE`），
非 @ 的分享消息本来就会到达机器人 —— 以前被 `bot.is_bot_mentioned` 直接丢掉了。

**四种分享形态**（2026-10-17 抓的 5 条真实报文，`scripts/smoke.py [4.94]` 里
把原始 `content` 原样喂进 `find_target` 做回归）：

===========  ===============  ==========================  ==============
形态          message_type     视频 ID 在哪                处理
===========  ===============  ==========================  ==============
小程序卡片    3 (ark)          报文里**没有**               标题反查
纯文本+短链   0                `content` 里的 b23.tv       302 拿 BV
图文 H5 卡片  3 (ark)          `content` 里的 jump_url     302 拿 BV
纯文本+BV     0                `content`                   直接用
===========  ===============  ==========================  ==============

三条踩过的坑，改这个模块前先看：

* **卡片消息的 `content` 不是空的** —— 平台预渲染了全文，图文 H5 卡片的
  `jump_url` 就躺在里面。所以提取**只扫 `content`** 就够，`ark_data` 只在
  小程序卡片兜底时才碰。
* **`ark_data` 的数据嵌在 `fields` 子字典里**，结构是
  `{ark_name, ark_type, fields: {...}, prompt}`，不是平铺。
* **`wbi/search` 必须同时带 Chrome UA 和 `Referer`** —— 见 `_search`。
  缺任何一个都是「HTTP 200 + code=0 + 空结果」的静默失败。

设计上**全程静默**：任何一步拿不到数据就返回 `None`，调用方什么都不发。
群里没人希望机器人因为解析不了链接而回一句报错。
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass

import httpx

_log = logging.getLogger("qq_rss_bot.bilibili")

# ---------- 链接识别 ----------

SHORT_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+")
# 不要求 bilibili.com/video/ 前缀 —— 群里经常只贴一个裸 BV 号。
# 但要用后行断言排掉 b23.tv 的短链 token（那也是 [0-9A-Za-z]+，万一恰好
# 以 BV + 10 位开头就会被误当成真 BV 号），末尾 \b 防止匹配到更长 token 的前缀。
BV_RE = re.compile(r"(?<!b23\.tv/)(BV[0-9A-Za-z]{10})\b")
AV_RE = re.compile(r"bilibili\.com/video/av(\d+)", re.I)
# 「这条消息和 bilibili 有关」的弱信号。小程序卡片的 content 里只有
# `source: 哔哩哔哩`，没有任何链接，靠这个才认得出是 bilibili 分享。
BILI_HINT = re.compile(r"b23\.tv|bilibili\.com|哔哩哔哩", re.I)

_API = "https://api.bilibili.com/x/web-interface"

# ⚠️ 这两个头是**成对**的硬要求，不是「保险起见」：
# 实测 项目UA / 项目UA+Referer / ChromeUA / ChromeUA+Referer 四组，
# 只有最后一组能搜出 20 条结果，其余三组都是 code=0 但 0 条。
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_BILI_HEADERS = {"User-Agent": _BROWSER_UA, "Referer": "https://www.bilibili.com/"}

# 短链展开 + view 取数据两次请求的总预算。放在 botpy 的事件回调里 await，
# 不能久等（虽然回调是 create_task 并发跑的，不会卡住别的消息）。
HTTP_TIMEOUT = 6.0

# 标题反查的标题最短长度（归一化后）。太短的标题必然误判，直接放弃反查。
_MIN_TITLE_CHARS = 8

# 启动自检用的样例视频。挑一个稳定的公开视频；哪天它被删了，探测会返回
# 「网络通但接口非 0」，和「连不上」是两种不同的输出，不会看混。
_PROBE_BVID = "BV15eJt6pEam"

# 封面渲染尺寸。QQ markdown 的图片语法 `![文字 #宽px #高px](url)`
# **尺寸是必填的**（官方文档的示例带尺寸）。bilibili 封面是 16:9。
_COVER_W, _COVER_H = 208, 117
# bilibili 图床的缩放后缀：原图 214KB → 33KB。QQ 要自己去下载转存这张图，
# 小一点对双方都好。（封面**没有**防盗链，加不加后缀都是 HTTP 200。）
_COVER_SUFFIX = "@480w_270h_1c.jpg"

_TAG_RE = re.compile(r"<[^>]+>")
# 标题里只保留「词字符」：`\w` 在 str 模式下本身就覆盖中日韩字，
# 标点（含全角）全部丢掉 —— 分享标题和线上标题经常就差几个标点。
_KEEP_RE = re.compile(r"\w")
# 会破坏 markdown 结构的字符。**只用在标题上**，且不放进 [文字](url)。
_MD_STRIP_RE = re.compile(r"[*_`~\\]")


@dataclass(frozen=True)
class Target:
    """从一条群消息里抽出来的「要查什么」。三个字段至多填一个。"""

    bvid: str | None = None  # 已经知道 BV（或 av）号，直接查
    short: str | None = None  # b23.tv 短链，要跟一次 302
    title: str | None = None  # 只有小程序卡片有，拿去反查


def find_target(content: str, ark_data: object = None) -> Target | None:
    """从群消息的 `content`（必要时加上 `ark_data`）里找出要解析的目标。

    返回 `None` = 这条消息和 bilibili 无关，调用方应当什么都不做。
    """
    text = content or ""

    short = None
    m = SHORT_RE.search(text)
    if m:
        short = m.group(0)

    bvid = None
    m = BV_RE.search(text)
    if m:
        bvid = m.group(1)
    if bvid is None:
        m = AV_RE.search(text)
        if m:
            bvid = "av" + m.group(1)

    if bvid or short:
        return Target(bvid=bvid, short=short)

    # 小程序卡片兜底：报文里没有任何视频 ID，只有标题。
    # **必须先用「哔哩哔哩」这个来源标记筛一次** —— 否则任何 QQ 小程序卡片
    # （音乐、新闻、游戏…）都会被拿去搜 bilibili，搜不到是浪费，搜错了更糟。
    fields = _ark_fields(ark_data)
    source = str(fields.get("source") or "")
    if BILI_HINT.search(text) or BILI_HINT.search(source):
        title = str(fields.get("title") or "").strip()
        if title:
            return Target(title=title)

    return None


def norm_title(raw: str) -> str:
    """归一化标题，用于反查比对。

    搜索结果里的标题带 `<em class="keyword">` 高亮标签，要去掉；群里分享的
    标题和线上真实标题经常差在标点上（实测差三个 `！`），所以标点一律丢掉。
    """
    return "".join(_KEEP_RE.findall(html.unescape(_TAG_RE.sub("", raw or ""))))


def fmt_count(n: int) -> str:
    """30510 → ``3.0万``；120000000 → ``1.2亿``；9999 → ``9999``。

    亿的门槛取 99_995_000 而不是 1e8：`.1f` 会四舍五入，直接按 1e8 分档的话
    99999999 会输出「10000.0万」。往上够一档更自然。
    """
    if n >= 99_995_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def fmt_duration(sec: object) -> str:
    """187 → ``03:07``；3723 → ``1:02:03``。

    拿不准类型就按 0 算 —— 一个字段脏了只该让那一格难看，不该把整张卡片丢掉。
    """
    try:
        total = max(0, int(sec))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def render(data: dict) -> str:
    """把 view API 的 ``data`` 渲染成 QQ 的 markdown。

    **标题单独一行加粗，不放进 ``[文字](url)`。** 实测 100 条热门标题里 7 条含
    `|` / `[` / `]` / `!` —— 塞进链接文字里会把后面所有的括号配对搞乱。
    标题里的 `*` `_` 等会被剥掉（否则会破坏外面那对 `**`）。
    """
    lines: list[str] = []

    cover = _cover_url(data.get("pic"))
    if cover:
        lines.append(f"![封面 #{_COVER_W}px #{_COVER_H}px]({cover})")

    title = _MD_STRIP_RE.sub("", str(data.get("title") or "")).strip()
    lines.append(f"**{title or '（无标题）'}**")

    owner = str((data.get("owner") or {}).get("name") or "").strip()
    meta = " · ".join(p for p in (owner, fmt_duration(data.get("duration") or 0)) if p)
    if meta:
        lines.append(meta)

    stat = data.get("stat") or {}
    counts = [
        f"{label} {fmt_count(int(stat[key]))}"
        for label, key in (("播放", "view"), ("弹幕", "danmaku"), ("评论", "reply"), ("点赞", "like"))
        if isinstance(stat.get(key), int)
    ]
    # **每行最多两项。** 四项挤一行在手机窄屏上会自己折行，而折点由 QQ 决定、
    # 不受控 —— 实测就是「播放…弹幕…评论…点赞」在中间某个位置断开，很难看。
    # 固定两项一行是**确定**的排版：播放·弹幕 / 评论·点赞，顺序和原来一致，
    # 只是插了个换行，所以在宽屏上也不会变得难读。
    for i in range(0, len(counts), 2):
        lines.append(" · ".join(counts[i:i + 2]))

    bvid = str(data.get("bvid") or "")
    if bvid:
        lines.append(f"[在 bilibili 打开](https://www.bilibili.com/video/{bvid})")

    return "\n".join(lines)


async def lookup(http: httpx.AsyncClient, target: Target, *, timeout: float = HTTP_TIMEOUT) -> dict | None:
    """把 `Target` 解析成 view API 的 ``data``。任何一步失败都返回 `None`。

    顺序：已知 BV → 展开短链 → 标题反查 → 取视频信息。
    复用调用方传进来的共享 `httpx.AsyncClient`（`RssBot.rss_http`），
    但 bilibili 的请求都带 `_BILI_HEADERS` 覆盖客户端默认的 UA。
    """
    bvid = target.bvid
    if bvid is None and target.short:
        bvid = await _expand(http, target.short, timeout)
    if bvid is None and target.title:
        bvid = await _search(http, target.title, timeout)
    if not bvid:
        _log.info("bilibili：没能解析出 BV（target=%r）", target)
        return None
    return await _view(http, bvid, timeout)


async def probe(http: httpx.AsyncClient, *, timeout: float = HTTP_TIMEOUT) -> str:
    """探一次「这个进程能不能连上 bilibili」，返回一句能直接打进日志的话。

    **容器里的网络环境和宿主机不是一回事** —— 代理、DNS、CA 都可能是另一套。
    实测踩到过：宿主机上 Synology 那个 Python 的 urllib 会莫名其妙走
    `192.168.0.63:6152` 那台 Surge，而容器里的 Debian Python 不会。
    所以别拿宿主机的结果推断容器。

    加这个是因为：出问题时「群里发了链接没反应」在日志里**一点痕迹都没有**
    （失败的路径以前全是 DEBUG），排查只能靠猜。启动时先探一次，把成功或
    失败原因钉在日志里。
    """
    try:
        resp = await http.get(
            f"{_API}/view", params={"bvid": _PROBE_BVID}, headers=_BILI_HEADERS, timeout=timeout
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 —— 探测就是要连异常类型一起打出来
        return f"❌ 连不上 {type(exc).__name__}: {exc}"

    if not isinstance(body, dict) or body.get("code") != 0:
        # 能拿到业务码就说明**网络和证书都没问题**，只是样例视频没了
        code = body.get("code") if isinstance(body, dict) else None
        msg = body.get("message") if isinstance(body, dict) else None
        return f"⚠️ 网络通（HTTP {resp.status_code}），但样例视频返回 code={code} message={msg!r}"

    title = str(((body.get("data") or {}).get("title") or ""))[:24]
    return f"✅ 正常（{_PROBE_BVID} {title}）"


# ---------- 内部 ----------


def _ark_fields(ark_data: object) -> dict:
    """取 ark 卡片的数据字典。

    ⚠️ 数据**嵌在 `fields` 里**：`{ark_name, ark_type, fields: {...}, prompt}`。
    实测报文确认，直接 `ark_data["title"]` 是取不到的。
    """
    if not isinstance(ark_data, dict):
        return {}
    fields = ark_data.get("fields")
    return fields if isinstance(fields, dict) else {}


def _cover_url(pic: object) -> str | None:
    """封面 URL：``http`` 改 ``https``，并加上图床的缩放后缀。"""
    if not isinstance(pic, str) or not pic.strip():
        return None
    url = pic.strip()
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    if url.startswith("https://") and "@" not in url:
        url += _COVER_SUFFIX
    return url


async def _expand(http: httpx.AsyncClient, short: str, timeout: float) -> str | None:
    """b23.tv 短链 → BV 号（跟一次 302，读 `Location` 头）。

    **必须按请求关掉 `follow_redirects`。** 共享客户端构造时是
    `follow_redirects=True`，跟下去会把整个视频页 HTML（几百 KB）拉下来，
    而我们只要 Location 里那一个 BV 号。
    """
    try:
        resp = await http.get(short, headers=_BILI_HEADERS, timeout=timeout, follow_redirects=False)
    except httpx.HTTPError as exc:
        _log.info("bilibili：展开短链失败 %s：%s: %s", short, type(exc).__name__, exc)
        return None
    # ⚠️ **不能调 raise_for_status()**：httpx 里 3xx 不算 success，短链的正常形态
    # 就是 302，调了必然抛 —— 症状是所有短链都静默解析失败。只有 >=400 才是真错。
    if resp.status_code >= 400:
        _log.info("bilibili：展开短链 HTTP %s：%s", resp.status_code, short)
        return None

    m = BV_RE.search(resp.headers.get("location") or "")
    if not m:
        _log.info(
            "bilibili：短链没跳到视频页 %s → HTTP %s location=%r",
            short,
            resp.status_code,
            (resp.headers.get("location") or "")[:120],
        )
        return None
    return m.group(1)


async def _search(http: httpx.AsyncClient, title: str, timeout: float) -> str | None:
    """标题反查 BV。**归一化后恰好一条相等**才认；0 条或 ≥2 条都放弃。

    两条硬约束，都实测过：

    * `wbi/search` **必须同时带 Chrome UA 和 `Referer`**，缺任何一个都是
      「HTTP 200 + code=0 + 空结果」的静默失败（项目自己的 UA 无论如何都是 0 条）。
    * 旧接口 `search/type` 被 412 风控拦死，别换回去。

    这里**不看** `code` 字段：唯一判据是「有没有恰好一条归一化后相等的标题」。
    风控、空结果、确实没有，最后都归结为「放弃」—— 反正调用方是静默的。
    """
    key = norm_title(title)
    if len(key) < _MIN_TITLE_CHARS:
        _log.info("bilibili：标题太短（归一化后 %d 字），不反查：%r", len(key), title)
        return None

    try:
        resp = await http.get(
            f"{_API}/wbi/search/type",
            params={"search_type": "video", "keyword": title},
            headers=_BILI_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        _log.info("bilibili：标题反查请求失败 %r：%s: %s", title, type(exc).__name__, exc)
        return None

    data = body.get("data") if isinstance(body, dict) else None
    results = (data or {}).get("result") if isinstance(data, dict) else None
    hits = [
        r.get("bvid")
        for r in (results or [])
        if isinstance(r, dict) and r.get("bvid") and norm_title(r.get("title") or "") == key
    ]
    if len(hits) != 1:
        # 0 条很常见（视频被删、标题改太多、风控）；≥2 条说明标题不唯一，宁可不回。
        _log.info("bilibili：标题反查命中 %d 条（需恰好 1 条），放弃：%r", len(hits), title)
        return None
    return str(hits[0])


async def _view(http: httpx.AsyncClient, bvid: str, timeout: float) -> dict | None:
    """取视频信息。`bvid` 也可能是 ``av`` 开头的（旧号），走 `aid` 参数。"""
    params = {"aid": bvid[2:]} if bvid.lower().startswith("av") else {"bvid": bvid}
    try:
        resp = await http.get(f"{_API}/view", params=params, headers=_BILI_HEADERS, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        _log.info("bilibili：取视频信息失败 %s：%s: %s", bvid, type(exc).__name__, exc)
        return None

    if not isinstance(body, dict) or body.get("code") != 0:
        code = body.get("code") if isinstance(body, dict) else None
        msg = body.get("message") if isinstance(body, dict) else None
        _log.info("bilibili：view 返回非 0（%s）：code=%s message=%r", bvid, code, msg)
        return None

    data = body.get("data")
    return data if isinstance(data, dict) else None
