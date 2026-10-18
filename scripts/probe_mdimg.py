"""markdown 多图（`msg_type=2` 里放多个 `![](url)`）的实测探针。

**要回答的问题**：一条 markdown 消息能不能渲染多张图、渲染成什么样。
如果能，「多图」就不必走「连发 N 条富媒体」（每条各占一条主动配额、还刷屏）。

背景（见 CLAUDE.md「一条消息只能带一个媒体」）：

* `msg_type=7` 富媒体 —— `MediaInfo` 只有一个 `file_info`，**一条只能一张**；
* `msg_type=2` markdown —— 可以放多个 `![](url)`，但图**由 QQ 的服务器
  自己去下载**，所以 URL 必须是 QQ 够得到的公网地址。

所以本探针默认用**偶像大师 CMS 的图**（实测 QQ 拉得到，HTTP 200），
把「markdown 能不能多图」和「QQ 够不够得到这个图床」两件事**分开**测 ——
混在一起测的话，裂图了也不知道是哪个原因。

跑法：

    uv run python scripts/probe_mdimg.py                    # 只打印，不发
    uv run python scripts/probe_mdimg.py --n 4              # 换成 4 张
    uv run python scripts/probe_mdimg.py --urls <u1>,<u2>   # 自己指定图
    uv run python scripts/probe_mdimg.py --no-size          # 不带 #宽px #高px 试试
    uv run python scripts/probe_mdimg.py --group <openid> --send --both   # 群里 A/B 各发一条
    uv run python scripts/probe_mdimg.py --send-c2c         # 私聊自己（允许）
    uv run python scripts/probe_mdimg.py --group <openid> --send

⚠️ **只允许发到机器人测试群（群号 575861535）或私聊用户本人。绝不发正式群。**
所以 `--group` 必须显式给 openid，**没有默认值** —— 群号查不出 openid
（库里 `groups` 表存的是 openid），只能你自己事先确认好是哪个群。
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from qq_rss_bot import idolmaster  # noqa: E402
from qq_rss_bot.rss import fetch_feed, resolve_url  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
MSG_TYPE_MARKDOWN = 2
RSSHUB = "http://100.91.151.20:1200"

# 渲染宽度。高度**按每张图的真实比例算** —— 硬编码 16:9 会把正方形图压扁
# （实测偶像大师那个源的图 16:9 / 1:1 / 0.77 / 1.37 混着来）。
#
# 实测（2026-10-18，用户两边都看了）：
#   · **手机端：不带尺寸提示 markdown 整个不渲染**，裸文本打出来；
#   · 电脑端：带不带都行；
#   · 所以尺寸提示是**必填**的，而且比例必须对。
COVER_W = 208
MIN_H, MAX_H = 40, 400


def pick_c2c_openid() -> str | None:
    """从库里挑一个「给自己发私聊」的 openid（和通知器同源）。"""
    from qq_rss_bot.config import load_config

    cfg = load_config()
    if cfg.notify_openids:
        return cfg.notify_openids[0]
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT member_openid FROM notify_targets ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return row["member_openid"] if row else None


def _arg(name: str, default: str) -> str:
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def pick_diverse(
    measured: list[tuple[str, tuple[int, int] | None]], n: int
) -> list[tuple[str, tuple[int, int] | None]]:
    """按比例跨度挑 n 张 —— **全是 16:9 的话根本测不出压扁**。"""
    ok = [(u, wh) for u, wh in measured if wh]
    if len(ok) <= n:
        return ok
    ok.sort(key=lambda pair: pair[1][0] / pair[1][1])
    step = (len(ok) - 1) / (n - 1) if n > 1 else 0
    return [ok[round(i * step)] for i in range(n)]


async def collect_images(n: int, *, skip_entries: int = 0) -> list[str]:
    """从偶像大师 feed 里取候选图（QQ 实测拉得到的那个图床）。

    `skip_entries` 跳过前几个条目 —— 用来拿**从没发过的图**，验证
    「冷图加载失败」到底是真冷，还是 `_cb` 那个多余参数惹的。
    """
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        url = resolve_url("/idolmaster/news", RSSHUB)
        result = await fetch_feed(http, url, 30)
    out: list[str] = []
    for entry in result.entries[skip_entries:]:
        for img in entry.images:
            if img not in out:
                out.append(img)
            if len(out) >= n:
                return out
    return out


def image_size(data: bytes) -> tuple[int, int] | None:
    """只读文件头拿宽高 —— 不依赖 Pillow，也不用整张下下来。

    QQ 的 markdown 尺寸提示必须和真实比例一致，否则图会被**拉伸**
    （实测：正方形图配 16:9 的框，出来是长方形）。
    """
    if len(data) < 24:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":  # PNG
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a"):  # GIF
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":  # JPEG：扫到 SOFn 段
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seglen = int.from_bytes(data[i + 2 : i + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                return (int.from_bytes(data[i + 7 : i + 9], "big"),
                        int.from_bytes(data[i + 5 : i + 7], "big"))
            i += 2 + seglen
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":  # WebP 三种子格式
        fourcc = data[12:16]
        if fourcc == b"VP8X":
            return (int.from_bytes(data[24:27], "little") + 1,
                    int.from_bytes(data[27:30], "little") + 1)
        if fourcc == b"VP8 ":
            return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                    int.from_bytes(data[28:30], "little") & 0x3FFF)
        if fourcc == b"VP8L":
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


async def measure(http: "httpx.AsyncClient", url: str) -> tuple[int, int] | None:
    """Range 只取前 32KB —— PNG 要 24 字节、JPEG 要扫到 SOFn，够了。"""
    try:
        resp = await http.get(url, headers={"Range": "bytes=0-32767"})
        return image_size(resp.content)
    except Exception:  # noqa: BLE001
        return None


def box_for(wh: tuple[int, int] | None) -> tuple[int, int] | None:
    """真实宽高 → 渲染框（宽固定 COVER_W，高按比例）。读不到尺寸就返回 None。"""
    if not wh or wh[0] <= 0 or wh[1] <= 0:
        return None
    w, h = wh
    out_h = round(COVER_W * h / w)
    return COVER_W, max(MIN_H, min(MAX_H, out_h))


def build_markdown(
    items: list[tuple[str, tuple[int, int] | None]], *, with_size: bool, label: str = ""
) -> str:
    """一条 markdown：标题 + 正文 + n 张图 + 链接。

    `with_size=False` 时不带尺寸提示 —— 用来复现「手机端整个不渲染」那条。
    """
    tag = "带尺寸" if with_size else "不带尺寸"
    head = label or tag
    lines = [f"**markdown 多图实测 · {head}**",
             f"这条里有 {len(items)} 张图，看比例有没有被拉变形。"]
    for i, (img, wh) in enumerate(items, 1):
        box = box_for(wh)
        hint = f" #{box[0]}px #{box[1]}px" if (with_size and box) else ""
        shape = f"{wh[0]}x{wh[1]}" if wh else "?"
        lines.append(f"![图{i}{hint}]({img})")
        lines.append(f"（图 {i}/{len(items)}　原图 {shape}）")
    lines.append("[打开原文](https://idolmaster-official.jp/news)")
    return "\n".join(lines)


def bust(url: str, salt: str) -> str:
    """加一个无意义 query 参数 —— CMS 照常返回原图（实测），
    但 QQ 那边是个**没见过的新 URL**，等于强制冷抓。"""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_cb={salt}"


async def run_sequence(
    *, target: str, is_group: bool, appid: str, secret: str,
    items: list[tuple[str, tuple[int, int] | None]],
) -> int:
    """四轮：冷图/热图 × 不带尺寸/带尺寸。

    目的：把「QQ 抓冷图会失败」和「尺寸提示有问题」两个假设**分开**。
    三轮的预期互不相同，所以一轮就能分辨：
        缓存假设   ①✗ ②✓ ③✗ ④✓
        尺寸有害   ①✓ ②✓ ③✗ ④✗
        尺寸必需   ①✗ ②✗ ③✓ ④✓
    """
    salt_a, salt_b = str(int(time.time())), str(int(time.time()) + 7)
    cold_a = [(bust(u, salt_a), wh) for u, wh in items]
    cold_b = [(bust(u, salt_b), wh) for u, wh in items]
    rounds = [
        ("① 冷图·无尺寸", cold_a, False),
        ("② 同图·无尺寸", items, False),
        ("③ 冷图·带尺寸", cold_b, True),
        ("④ 同图·带尺寸", items, True),
    ]
    rc = 0
    for i, (label, urls, with_size) in enumerate(rounds):
        if i:
            await asyncio.sleep(8)  # 给 QQ 抓图留时间，别让两轮叠一起
        print(f"\n===== {label} =====")
        rc |= await send(
            target=target, is_group=is_group,
            markdown=build_markdown(urls, with_size=with_size, label=label),
            appid=appid, secret=secret,
        )
    print("\n四轮发完。预期对照：")
    print("  缓存假设 → ①✗ ②✓ ③✗ ④✓")
    print("  尺寸有害 → ①✓ ②✓ ③✗ ④✗")
    print("  尺寸必需 → ①✗ ②✗ ③✓ ④✓")
    return rc


async def send(*, target: str, is_group: bool, markdown: str, appid: str, secret: str) -> int:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, json={"appId": appid, "clientSecret": secret})
        token = resp.json().get("access_token") if resp.status_code == 200 else None
        if not token:
            print(f"  取 token 失败: HTTP {resp.status_code} {resp.text[:200]}")
            return 1

        headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": appid,
            "Content-Type": "application/json",
        }
        payload = {"msg_type": MSG_TYPE_MARKDOWN, "markdown": {"content": markdown}}
        url = (
            f"{API_BASE}/v2/groups/{target}/messages"
            if is_group
            else f"{API_BASE}/v2/users/{target}/messages"
        )
        print(f"  POST {url}")
        r = await client.post(url, headers=headers, json=payload)
        print(f"\n  ← HTTP {r.status_code}\n  ← {r.text[:400]}")

        if r.status_code < 300 and not (isinstance(r.json(), dict) and r.json().get("code")):
            where = "群里" if is_group else "私聊里"
            print(f"\n  ✅ 发出去了 —— 去{where}看：")
            print("     · 几张图渲染出来了？")
            print("     · 有没有裂图（一个小方块 / 空白）？")
            print("     · 带尺寸提示的和不带的，表现一样吗？")
            print("     · 图和它下面那行「（图 i/n）」的相对位置对不对？")
            return 0
        print("\n  ❌ 失败，看上面的原始响应。")
        return 1


async def main() -> int:
    args = sys.argv[1:]
    do_send = "--send" in args
    send_c2c = "--send-c2c" in args
    group = None
    if "--group" in args:
        i = args.index("--group")
        group = args[i + 1] if i + 1 < len(args) else None
        if not group:
            print("--group 后面要给 group_openid")
            return 2
    n = 4
    if "--n" in args:
        i = args.index("--n")
        n = int(args[i + 1]) if i + 1 < len(args) else 4
    images: list[str] | None = None
    if "--urls" in args:
        i = args.index("--urls")
        images = [u.strip() for u in args[i + 1].split(",") if u.strip()]

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        if images is None:
            print(f"从偶像大师 feed 取候选图（要挑 {n} 张比例不同的）…")
            urls = await collect_images(
                max(n * 3, 9), skip_entries=int(_arg("--skip", "0"))
            )
        else:
            urls = images
        if not urls:
            print("一张图都没取到，用 --urls 自己指定")
            return 1
        measured: list[tuple[str, tuple[int, int] | None]] = []
        for img in urls:
            measured.append((img, await measure(http, img)))
        items = pick_diverse(measured, n)

    if not items:
        print("尺寸一张都没读出来")
        return 1
    print(f"用图 {len(items)} 张（按比例跨度挑的）：")
    for img, wh in items:
        ratio = f"{wh[0] / wh[1]:.2f}" if wh else "?"
        box = box_for(wh)
        print(f"  原图 {str(wh):<14} 比例 {ratio:<5} → 渲染框 {box}")

    send_both = "--both" in args
    with_size = "--no-size" not in args
    md = build_markdown(items, with_size=with_size, label=_arg("--label", ""))
    print(f"\n（尺寸提示：{'带' if with_size else '不带'}）")
    print(f"\n===== markdown（{len(md)} 字符）=====")
    print(md)
    print("=" * 40)

    if not (do_send or send_c2c):
        print("\n（默认只打印。要真发：--send-c2c 私聊自己，或 --group <openid> --send）")
        return 0

    from qq_rss_bot.config import load_config

    cfg = load_config()
    if group:
        print("\n⚠️  即将发到**群** —— 确认这是测试群（575861535）再继续。")
        target, is_group = group, True
    else:
        target = pick_c2c_openid()
        if not target:
            print("库里没有可用的 C2C openid，先给机器人发条私聊，或用 --group")
            return 1
        target, is_group = target, False
        print(f"\n私聊目标：{target[:12]}…（你自己）")
    if "--seq" in args:
        return await run_sequence(
            target=target, is_group=is_group, items=items,
            appid=cfg.appid, secret=cfg.secret,
        )

    rc = await send(
        target=target, is_group=is_group, markdown=md,
        appid=cfg.appid, secret=cfg.secret,
    )
    if send_both:
        # 隔 5 秒 —— 连发太快客户端可能把两条叠在一起，比不出来
        await asyncio.sleep(5)
        other = build_markdown(items, with_size=not with_size)
        print(f"\n===== 第二条（尺寸提示：{'带' if not with_size else '不带'}）=====")
        rc |= await send(
            target=target, is_group=is_group, markdown=other,
            appid=cfg.appid, secret=cfg.secret,
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
