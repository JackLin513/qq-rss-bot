"""探测 YouTube / X(Twitter) 的元数据接口在**容器里**通不通、返回什么结构。

**为什么要单独探这一层**：这两个站点的接口（元数据和图床）容器**直连全部不通**，
必须走代理。而在宿主机上探是没意义的 —— 实测过 `api.bilibili.com` 从 NAS 宿主
是 000、从容器里是 ✅（宿主那套 Surge MITM 的干扰）。所以只能在容器里跑。

    sudo docker compose exec bot python scripts/probe_meta.py --proxy http://192.168.0.63:6152

**不碰 QQ，不往任何群发消息。** 纯出网探测，成功失败都只往 stdout 打。

各站点靠什么拿数据（2026-10-17 本机实测确认的结构）：

============================  ==================================================
YouTube oEmbed                title / author_name / thumbnail_url
YouTube youtubei/player       lengthSeconds / viewCount（**oEmbed 给不了这两项**）
X fxtwitter                   text / likes / retweets / replies / author / media
X 官方 oEmbed                 author_name + 正文（在 html 的 blockquote 里），**没有图**
X 官方 syndication            ❌ 实测本机 SSL 不通，别指望
============================  ==================================================

所以取数策略：YouTube = youtubei 为主、oEmbed 兜底（拿标题/频道名）；
X = fxtwitter 为主、官方 oEmbed 兜底（至少还有作者和正文）。

⚠️ 代理地址**不进代码**，只从 `--proxy` 或环境变量 `MEDIA_PROXY` 读。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

# YouTube 的三种链接形态。ID 固定 11 位（`[A-Za-z0-9_-]`）。
YT_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|embed/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)
# X / Twitter 的推文链接。用户名可以是任意 `[A-Za-z0-9_]+`，ID 是纯数字。
X_RE = re.compile(r"(?:twitter\.com|x\.com)/[A-Za-z0-9_]+/status/(\d+)")

YT_SAMPLE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
# 这条是史上点赞最多的推文之一，**带一张图** —— 专门用来确认 media 结构
X_SAMPLE = "https://x.com/BarackObama/status/896523232098078720"

# 公开的 YouTube WEB 客户端 key（YouTube 自己网页里写死的，不是我们的凭据）。
# 换成空串也能用，但带上更贴近真实客户端。
_YTI_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

# 元数据接口的超时。这几个都是境外站，走代理比直连慢，给宽一点。
TIMEOUT = 12.0

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def parse_args(argv: list[str]) -> tuple[str, list[str]]:
    """返回 (代理地址, 额外的链接)。代理优先 `--proxy`，其次环境变量。"""
    import os

    proxy = (os.environ.get("MEDIA_PROXY") or "").strip()
    extra: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--proxy":
            if i + 1 >= len(argv):
                raise SystemExit("--proxy 后面要跟代理地址")
            i += 1
            proxy = argv[i]
        elif a.startswith("--proxy="):
            proxy = a.split("=", 1)[1]
        elif a in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
        elif a.startswith("-"):
            raise SystemExit(f"未知参数：{a}（-h 看用法）")
        else:
            extra.append(a)
        i += 1
    return proxy, extra


def classify(url: str) -> tuple[str, str] | None:
    """链接 → (站点, ID)。认不出返回 None。"""
    m = YT_RE.search(url)
    if m:
        return "youtube", m.group(1)
    m = X_RE.search(url)
    if m:
        return "twitter", m.group(1)
    return None


def _show(label: str, data: object) -> None:
    print(f"  {label}: {json.dumps(data, ensure_ascii=False)[:220]}")


async def probe_youtube(http: httpx.AsyncClient, vid: str) -> dict | None:
    """YouTube：youtubei/player 拿时长+播放量，oEmbed 拿标题+频道名。

    两个都试，**互补**：youtubei 对受限视频会是 `playabilityStatus=UNPLAYABLE`，
    但 `videoDetails` 通常照样返回；oEmbed 则完全不看视频状态，永远给标题。
    这里只是探结构，真正的取舍在 preview.py 里。
    """
    print("\n### YouTube")
    watch = f"https://www.youtube.com/watch?v={vid}"

    # --- oEmbed ---
    try:
        r = await http.get(
            "https://www.youtube.com/oembed",
            params={"url": watch, "format": "json"},
            headers={"User-Agent": _UA},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            print(f"  ✅ oEmbed HTTP 200")
            for k in ("title", "author_name", "thumbnail_url", "thumbnail_width", "thumbnail_height"):
                _show(k, d.get(k))
        else:
            print(f"  ❌ oEmbed HTTP {r.status_code}: {r.text[:160]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ oEmbed {type(exc).__name__}: {exc}")

    # --- youtubei/player ---
    try:
        r = await http.post(
            "https://www.youtube.com/youtubei/v1/player",
            params={"key": _YTI_KEY},
            headers={"User-Agent": _UA, "Content-Type": "application/json"},
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
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            print(f"  ❌ youtubei HTTP {r.status_code}: {r.text[:160]}")
            return None
        body = r.json()
        status = (body.get("playabilityStatus") or {}).get("status")
        v = body.get("videoDetails") or {}
        print(f"  ✅ youtubei HTTP 200（playabilityStatus={status}）")
        for k in ("title", "author", "lengthSeconds", "viewCount", "isLiveContent"):
            _show(k, v.get(k))
        mf = (body.get("microformat") or {}).get("playerMicroformatRenderer") or {}
        _show("publishDate", mf.get("publishDate"))
        return v or None
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ youtubei {type(exc).__name__}: {exc}")
        return None


async def probe_twitter(http: httpx.AsyncClient, tid: str) -> dict | None:
    """X：fxtwitter 拿全部（正文/互动数/作者/配图），官方 oEmbed 兜底。"""
    print("\n### X (Twitter)")
    url = f"https://x.com/i/status/{tid}"

    # --- fxtwitter ---
    ok = False
    try:
        r = await http.get(
            f"https://api.fxtwitter.com/status/{tid}",
            headers={"User-Agent": _UA},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            ok = True
            d = r.json()
            t = d.get("tweet") or {}
            print(f"  ✅ fxtwitter HTTP 200 (code={d.get('code')})")
            for k in ("text", "likes", "retweets", "replies", "created_at"):
                _show(k, t.get(k))
            au = t.get("author") or {}
            _show("author", {"name": au.get("name"), "screen_name": au.get("screen_name")})
            media = t.get("media") or {}
            photos = media.get("photos") or []
            _show("media.photos", photos[:2])
            print(f"     （photos {len(photos)} 张）")
        else:
            print(f"  ❌ fxtwitter HTTP {r.status_code}: {r.text[:160]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ fxtwitter {type(exc).__name__}: {exc}")

    # --- 官方 oEmbed（兜底） ---
    try:
        r = await http.get(
            "https://publish.twitter.com/oembed",
            params={"url": url},
            headers={"User-Agent": _UA},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            print("  ✅ 官方 oEmbed HTTP 200")
            _show("author_name", d.get("author_name"))
            _show("html", (d.get("html") or "")[:180])
        else:
            print(f"  ❌ 官方 oEmbed HTTP {r.status_code}: {r.text[:160]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 官方 oEmbed {type(exc).__name__}: {exc}")

    return True if ok else None


async def probe_image(http: httpx.AsyncClient, url: str) -> None:
    """我们自己下不下得到这张图（决定了 base64 那条路走不走得通）。"""
    try:
        r = await http.get(url, headers={"User-Agent": _UA}, timeout=TIMEOUT)
        r.raise_for_status()
        print(f"  ✅ {len(r.content)} 字节  {url}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ {type(exc).__name__}: {exc}  {url}")


async def main() -> int:
    proxy, extra = parse_args(sys.argv[1:])
    targets = extra or [YT_SAMPLE, X_SAMPLE]
    print(f"代理：{proxy or '（无，直连 —— 预期失败）'}")
    print("=" * 72)
    fails = 0

    async with httpx.AsyncClient(proxy=proxy or None, follow_redirects=True) as http:
        for url in targets:
            hit = classify(url)
            if hit is None:
                print(f"\n### 认不出站点，跳过：{url}")
                continue
            site, vid = hit
            print(f"\n{'=' * 72}\n{url}  →  {site} {vid}")
            if site == "youtube":
                fails += 0 if await probe_youtube(http, vid) is not None else 1
                print("\n  图片（走代理自己下载）：")
                await probe_image(http, f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
                await probe_image(http, f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg")
            else:
                fails += 0 if await probe_twitter(http, vid) is not None else 1

    print("\n" + "=" * 72)
    if fails == 0:
        print("✅ 两个站点的元数据接口都通。")
    else:
        print(f"❌ 有 {fails} 处不通。排查顺序：")
        print("   ① 代理地址对不对、代理本身通不通")
        print("   ② **代理若做 MITM**（Surge 就是）：报 "
              "`CERTIFICATE_VERIFY_FAILED: self-signed certificate` 就是它 ——")
        print("      代理用自己的根证书重签了流量，而 httpx 只信 certifi 那份列表。")
        print("      迷惑点：**图片那条路可能是通的**（代理通常只重签 API 域名，")
        print("      没碰图床域名），所以别看到「图下得到」就以为接口没问题。")
        print("      修法见 .env.example 的 MEDIA_CA_BUNDLE / MEDIA_INSECURE_TLS。")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
