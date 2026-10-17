"""抓文章正文 → 交给 DeepSeek 翻译 + 精简。

**为什么需要这个**：RSS 的 `<description>` 只是一段导语，长度由**源**决定，
我们控制不了。实测（2026-10-18，10 个路由）：

    少数派 3914 字 / Solidot 368 / 36氪快讯 162 / V2EX 93 / Hacker News 32

`/v2ex/topics/hot` 和 `/hackernews/best` 这类**只给了标题**，光靠 feed 永远
拼不出正文；而 36氪快讯的 162 字就是它的全文。所以「抓正文」只在有需要时才做，
**按源开关**（`subscriptions.ai_enabled`），默认关。

**和 QQ 无关**：QQ 单条消息实测能收 40000 字符（`scripts/probe_textlen.py`），
限制文案长度的从来不是平台，是可读性。

三条硬约束：

1. **任何一步失败都返回 `None`**，调用方静默退回 RSS 原本的文案。
   抓不到正文（付费墙 / JS 渲染 / 反爬）、模型超时、返回的 JSON 烂掉 ——
   都不该让这条推送消失。
2. **绝不抛异常**。这条路在推送主循环里，抛出去等于整批推送挂掉。
3. **`trafilatura` 是可选依赖**。容器里没装（新依赖要重建镜像）时退回一个
   很朴素的提取器 —— 效果差得多，但功能不至于整个不可用。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

_log = logging.getLogger("qq_rss_bot.ai")

# 送进模型的正文上限。按字符算而不是 token：中英混排下 token 数不好估，
# 而这个数的作用只是「别把一篇三万字的长文整个塞进去把预算烧光」。
# 6000 字中文 ≈ 8k tokens，摘要任务足够了（真正重要的信息集中在开头）。
MAX_INPUT_CHARS = 6000

# 摘要要多长。给模型的目标值 —— 最终还会有 `SUMMARY_CHARS` 那道截断。
TARGET_SUMMARY_CHARS = 260

# 浏览器 UA。很多站点对默认 UA 直接 403（和 bilibili 的 wbi 是同一类坑）。
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

DEFAULT_BASE_URL = "https://api.deepseek.com"

# **别凭印象写模型名。** `deepseek-chat` 已经退役了（2026-10-18 查官方定价页，
# 现在只有 `deepseek-flash` 和 `deepseek-v4-pro`）。写错的话调用直接 400，
# 而我们是**静默降级** —— 症状是「开了 AI 却什么都没变」，日志里只有一条
# WARNING。所以这个常量必须对着官方文档核。
DEFAULT_MODEL = "deepseek-flash"

_SYSTEM_PROMPT = """你是一个 QQ 群 RSS 推送助手。读者在手机上扫一眼，你要让他们三秒内知道这条值不值得点开。

输出一个 JSON 对象，三个字段：
- "title"：中文标题。原标题是中文就原样返回；是外文就翻译成中文（自然、不逐字直译）。
- "summary"：3~5 句中文摘要，说清楚「发生了什么」和「为什么值得看」。不要复述标题。
- "translated"：标题是否被你翻译过（true/false）。

硬性要求：不要 markdown 语法，不要「本文」「据悉」这类客套开头，不要罗列关键词。
如果正文残缺到看不出讲什么，summary 就填一个空字符串。"""


@dataclass(frozen=True)
class Digest:
    """一次 AI 处理的产物。`translated=True` 时调用方会附上原标题。"""

    title: str
    summary: str
    translated: bool


def extractor_name() -> str:
    """当前用的是哪个正文提取器 —— 启动日志里要能看出来（可观测性）。"""
    return "trafilatura" if _TRAFILATURA is not None else "内置（降级）"


def _load_trafilatura():
    try:
        import trafilatura  # noqa: PLC0415
    except ImportError:
        return None
    return trafilatura


_TRAFILATURA = _load_trafilatura()


def extract_text(html: str) -> str:
    """从 HTML 里抠正文。抠不出来就返回空串。"""
    if not html:
        return ""
    if _TRAFILATURA is not None:
        try:
            got = _TRAFILATURA.extract(html, include_comments=False, include_tables=True)
        except Exception as exc:  # noqa: BLE001 —— 解析器对畸形 HTML 什么都可能抛
            _log.info("ai：trafilatura 解析失败 %s: %s", type(exc).__name__, exc)
            got = None
        if got:
            return got.strip()
    return _fallback_extract(html)


# 降级路径的提取器。**故意写得很笨** —— 它只在没装 trafilatura 时用，
# 目标是「能用」而不是「好用」：剥掉 script/style/nav/footer，把剩下的
# 文本按长度倒序取最大的那块。
_DROP_RE = re.compile(
    r"<(script|style|nav|footer|header|aside|form|noscript)\b[^>]*>.*?</\1>",
    re.S | re.I,
)
_BLOCK_RE = re.compile(r"<(p|div|br|li|h[1-6])\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_DROP_TAGS = ("script", "style", "nav", "footer", "header", "aside", "form", "noscript")


def _fallback_extract(html: str) -> str:
    body = _DROP_RE.sub(" ", html)
    body = _BLOCK_RE.sub("\n", body)
    body = _TAG_RE.sub("", body)
    body = _unescape(body)
    # 按行分组，取连续行里最长的一段 —— 正文一定是中间那一大坨，
    # 导航和版权声明都是零散短行。
    best: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            current.append(line)
        else:
            if sum(len(x) for x in current) > sum(len(x) for x in best):
                best = current
            current = []
    if sum(len(x) for x in current) > sum(len(x) for x in best):
        best = current
    return "\n".join(best).strip()


def _unescape(text: str) -> str:
    import html as _html  # noqa: PLC0415

    return _html.unescape(text)


async def fetch_article(
    http: httpx.AsyncClient, url: str, *, timeout: float, proxy_http=None
) -> str:
    """把文章正文抓下来。失败返回空串（调用方会退回 RSS 的导语）。

    `http` 是**直连**的客户端；直连失败时如果给了 `proxy_http`，再走代理试一次。
    顺序不能反 —— 国内的源走代理既慢又可能撞上 MITM 证书问题，
    而国外的源（Hacker News 链出去的博客）直连大概率超时。
    """
    for label, client in (("直连", http), ("代理", proxy_http)):
        if client is None:
            continue
        try:
            resp = await client.get(
                url, headers={"User-Agent": BROWSER_UA}, timeout=timeout
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            _log.info("ai：正文抓取失败（%s）%s: %s: %s", label, url, type(exc).__name__, exc)
            continue
        text = extract_text(resp.text)
        if text:
            return text
        _log.info("ai：正文抓到了但提取不出内容（%s）%s", label, url)
    return ""


def _build_prompt(title: str, text: str) -> str:
    body = text[:MAX_INPUT_CHARS]
    return f"标题：{title}\n\n正文：\n{body}"


def parse_digest(raw: str) -> Digest | None:
    """把模型的回复解析成 `Digest`。**烂 JSON 一律返回 None**，不猜。

    单独抽出来是为了能离线测 —— 这条路最容易出错的地方就是模型偶尔
    裹一层 ```json 或者多说一句话，而那些都不该让整条推送挂掉。
    """
    if not raw:
        return None
    text = raw.strip()
    # 模型时不时会裹一层代码块，就算说了「不要 markdown」也一样
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    # 还不行就从第一个 { 扫到最后一个 } —— 前后多说的话直接扔掉
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "").strip()
    summary = str(data.get("summary") or "").strip()
    if not title:
        return None
    return Digest(
        title=title,
        summary=summary,
        translated=bool(data.get("translated")),
    )


async def digest(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    title: str,
    text: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    timeout: float = 30.0,
    disable_thinking: bool = True,
) -> Digest | None:
    """调一次模型，拿译文标题 + 摘要。**任何失败都返回 None，绝不抛。**

    `client` 必须是**直连**的：DeepSeek 在国内直连 0.2 秒，走代理反而更慢，
    而且代理做 MITM 时还可能撞证书问题。
    """
    if not api_key:
        return None
    if not text and not title:
        return None

    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(title, text)},
        ],
        # 低温度：这是抽取式任务，不需要它发挥。
        # ⚠️ **thinking 模式下 temperature 是无效的**（官方原话），所以下面
        # 那句关 thinking 是这行生效的前提 —— 两个是一套的，别拆。
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "max_tokens": 800,
    }
    if disable_thinking:
        # DeepSeek 的 thinking **默认是开的**，而且默认档位是 high。
        # 翻译+摘要这种抽取式任务根本不需要推理链，开着的话：
        #   · output token 翻好几倍（thinking token 按 output 计费）
        #   · 每次多等 2~5 秒 —— 这个延迟在推送主循环里是实打实的
        # 非 DeepSeek 的 OpenAI 兼容端点可能不认这个字段（OpenAI 会 400），
        # 那种情况把 `AI_DISABLE_THINKING` 设成 false。
        payload["thinking"] = {"type": "disabled"}
    try:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        _log.warning("ai：调用失败 %s: %s", type(exc).__name__, exc)
        return None

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        _log.warning("ai：返回结构不认识 %s: %s", type(exc).__name__, str(body)[:200])
        return None
    got = parse_digest(content)
    if got is None:
        _log.warning("ai：解析不出 JSON，原始回复 %.200s", content)
    return got


def probe(
    api_key: str, *, configured: bool, fetch_article: bool = True, model: str = ""
) -> str:
    """启动自检用的一句话。**不发请求** —— 每次启动都花一次调用太浪费。

    **不要在返回值里带 `AI 摘要：` 前缀** —— 调用方（`bot.py`）自己会加，
    以前两边都写，日志里就成了 `AI 摘要：AI 摘要：已配置…`（实测踩到过）。

    「模型名对不对」这里查不出来（要真发一次请求）。写错的话症状是
    **静默降级**，所以下面把模型名和正文来源都印出来，对照官方文档一眼可辨。
    """
    if not configured:
        return "未配置（缺 DEEPSEEK_API_KEY），已订阅的源会退回 RSS 导语"
    source = "抓文章页正文" if fetch_article else "只用 RSS 导语（不抓文章页）"
    if fetch_article and _TRAFILATURA is None:
        return (
            f"已配置，{source}，但 trafilatura 没装（需要重建镜像），"
            "正文提取走降级实现，效果差"
        )
    return f"已配置，{source}，模型 {model or DEFAULT_MODEL}"
