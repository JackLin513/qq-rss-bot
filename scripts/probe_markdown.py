"""实测 markdown（`msg_type=2`）的渲染边界 —— **只发测试群**。

## 结论（2026-10-22）：**横排放弃**

`--case row` / `--case widths` 这两组是当时为了「两张竖图并排放」做的实验。
结论是**没有可用的统一参数** —— 一行能并排几张、每张多大，完全取决于设备：
同一组图在手机端两列能挤进去、三列不行，等宽 / 等高 / 总宽 240–300 四档也
各不相同。所以**图一张一行**，别再试横排（CLAUDE.md 的平台硬事实里也记了一条）。

`row` / `widths` 两个 case 保留着，万一以后想复测，参数都在。

## 三个数在离线永远测不出来，只能真发：

1. **一条消息最多渲染几张图**（库里没有这个数据，`multiimage.MAX_IMAGES` 是猜的）
2. **正文能有多长**（`multiimage.MAX_CHARS = 4000` 是软上限，不是实测值）
3. **自建图床的图首次发送要不要预热**（贺图那条的"冷链接必炸"是在
   `pbs.twimg.com` 上测的，换了地址没验过）

    uv run python scripts/probe_markdown.py --group <openid> --urls urls.txt
    uv run python scripts/probe_markdown.py --group <openid> --urls urls.txt --case count

`--urls` 是一个纯文本文件，一行一个**公网可达**的图片 URL（图床上真实的地址）。
探针不负责上传 —— 它测的是渲染，不是图床。

## ⚠️ 群 openid 没有默认值

群 openid 不可逆，看不出是不是测试群，所以必须显式给。
**正式群（尾号 `E0629`）写死拒发。**
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from qq_rss_bot import multiimage  # noqa: E402
from qq_rss_bot.config import load_config  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
MSG_TYPE_MARKDOWN = 2

# 正式群的尾号。**写死拒发** —— 一次实测要发十来条，误发到正式群没法撤回。
FORBIDDEN_TAIL = "E0629"

# 图片在 markdown 里的显示宽度。1200x1600 的竖图按 240 宽算高 320。
IMG_W = 240


def _arg(name: str, default: str | None = None) -> str | None:
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(f"{name}="):
            return a.split("=", 1)[1]
    return default


class _Sender:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.client = httpx.AsyncClient(timeout=30)
        self.headers: dict[str, str] | None = None
        self.sent: list[str] = []

    async def _h(self) -> dict[str, str]:
        if self.headers:
            return self.headers
        r = await self.client.post(
            TOKEN_URL, json={"appId": self.cfg.appid, "clientSecret": self.cfg.secret})
        tok = r.json().get("access_token") if r.status_code == 200 else None
        if not tok:
            raise RuntimeError(f"取 token 失败：HTTP {r.status_code}")
        self.headers = {"Authorization": f"QQBot {tok}",
                        "X-Union-Appid": self.cfg.appid,
                        "Content-Type": "application/json"}
        return self.headers

    async def send(self, group: str, content: str) -> dict:
        h = await self._h()
        r = await self.client.post(
            f"{API_BASE}/v2/groups/{group}/messages", headers=h,
            json={"msg_type": MSG_TYPE_MARKDOWN, "markdown": {"content": content}})
        data = r.json() if r.content else {}
        if r.status_code >= 300:
            raise RuntimeError(f"HTTP {r.status_code} {data}")
        if data.get("code"):
            # ⚠️ 业务错误码是 200 带 code 回来的（比如 304036 没模板权限）。
            # 不判这一下，探针会报"发成功"，实际群里一片空白。
            raise RuntimeError(f"code={data.get('code')} {data.get('message')}")
        mid = data.get("id") or ""
        self.sent.append(mid)
        return data

    async def close(self) -> None:
        await self.client.aclose()


async def _row_md_total(client, urls: list[str], total: int) -> str:
    """**等高 + 按比例分宽**，让这一行两张图的**总宽**尽量接近 `total`。

    先定高：`H = 总宽 / Σ(宽高比)`，再让每张宽 = `H × 各自的宽高比`。
    这样两张**高度完全一样**（整齐），宽度各自不同（比例不变）。
    """
    sizes = []
    for url in urls:
        resp = await client.get(url)
        wh = multiimage.image_size(resp.content) if resp.status_code == 200 else None
        sizes.append(wh)
    ratios = [(w / h) for wh in sizes if wh for w, h in [wh]]
    if len(ratios) != len(urls) or not ratios:
        return "**读不到宽高，跳过**"
    height = max(1, round(total / sum(ratios)))
    parts = []
    for index, (url, wh) in enumerate(zip(urls, sizes), start=1):
        w, h = wh
        dw = max(1, round(height * w / h))
        parts.append(f"![图{index} #{dw}px #{height}px]({url})")
    real = sum(round(height * w / h) for w, h in sizes)
    lines = [f"**等高横排：目标总宽 {total}px（实际 {real}px），高 {height}px**"]
    lines.append(" ".join(parts))
    return "\n".join(lines)


async def _row_md_height(client, urls: list[str], height: int) -> str:
    """**等高**的一行：定高度，宽度各自按比例算。

    等宽（`_row_md`）在宽高比接近的图上看着还行，差得多就参差不齐。
    等高才是整齐的排法 —— 代价是每张宽度不同。
    """
    lines = [f"**等高横排：{len(urls)} 张，高 {height}px**"]
    parts: list[str] = []
    for index, url in enumerate(urls, start=1):
        resp = await client.get(url)
        wh = multiimage.image_size(resp.content) if resp.status_code == 200 else None
        if wh is None:
            lines.append(f"（图 {index} 读不到宽高，跳过）")
            continue
        w, h = wh
        dw = max(1, round(height * w / h))
        parts.append(f"![图{index} #{dw}px #{height}px]({url})")
    if parts:
        lines.append(" ".join(parts))
    return "\n".join(lines)


async def _row_md(client, urls: list[str], width: int, *, separate: bool) -> str:
    """同一行摆 N 张图（`separate=True` 就是各占一行，做对照）。

    **尺寸提示必须按每张的真实宽高算** —— 比例写错图会被拉扁，那样就分不清
    「横排失败」和「比例错了」。
    """
    lines = [f"**{'各占一行' if separate else '横排'}：{len(urls)} 张，宽 {width}px**"]
    parts: list[str] = []
    for index, url in enumerate(urls, start=1):
        resp = await client.get(url)
        wh = multiimage.image_size(resp.content) if resp.status_code == 200 else None
        if wh is None:
            lines.append(f"（图 {index} 读不到宽高，跳过）")
            continue
        w, h = wh
        dh = max(1, round(width * h / w))
        parts.append(f"![图{index} #{width}px #{dh}px]({url})")
    if separate:
        lines.extend(parts)
    elif parts:
        # ⚠️ 关键就是这一行：**同一行、空格分隔**。markdown 里图片是行内元素，
        # 同一个段落里的多张图理论上会并排渲染 —— 但 QQ 的渲染器认不认，只能试。
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _md(urls: list[str], *, head: str, tail: str = "") -> str:
    lines = [head]
    for i, u in enumerate(urls, start=1):
        # 尺寸提示必填；比例和实际图一致（生成的都是 1200x1600）
        lines.append(f"![图{i} #{IMG_W}px #{round(IMG_W * 1600 / 1200)}px]({u})")
        lines.append(f"图 {i}")
    if tail:
        lines.append(tail)
    return "\n".join(lines)


async def main() -> int:
    group = _arg("--group")
    urls_path = _arg("--urls")
    case = _arg("--case", "all") or "all"
    if not group:
        print("❌ 必须给 --group <openid>（群 openid 不可逆，看不出是不是测试群）")
        return 2
    if group.upper().endswith(FORBIDDEN_TAIL):
        print("❌ 这是正式群，探针拒绝发送。")
        return 2
    if not urls_path:
        print("❌ 必须给 --urls <文件>（一行一个公网图片 URL）")
        return 2
    urls = [ln.strip() for ln in Path(urls_path).read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]
    if not urls:
        print("❌ --urls 里一条 URL 都没有")
        return 2

    cfg = load_config()
    s = _Sender(cfg)
    print(f"目标群 {group[:8]}…  可用图 {len(urls)} 张  用例 {case}")

    plan: list[tuple[str, str]] = []
    try:
        if case in ("all", "count"):
            for n in (1, 3, 6, 9, 12):
                if n > len(urls):
                    continue
                plan.append((f"张数 {n}", _md(urls[:n], head=f"**【张数】{n} 张**")))
        if case in ("all", "length"):
            for n in (2000, 4000, 8000):
                filler = "测" * n
                plan.append((f"正文 {n} 字", _md(urls[:1], head=f"**【长度】{n} 字**\n{filler}")))
        if case in ("all", "row"):
            import httpx as _hx  # noqa: PLC0415

            async with _hx.AsyncClient(timeout=30, follow_redirects=True) as hc:
                plan.append(("横排 140px",
                             await _row_md(hc, urls[:2], 140, separate=False)))
                plan.append(("横排 110px",
                             await _row_md(hc, urls[:2], 110, separate=False)))
                if len(urls) >= 3:
                    plan.append(("横排 3 张 110px",
                                 await _row_md(hc, urls[:3], 110, separate=False)))
                plan.append(("对照：各占一行",
                             await _row_md(hc, urls[:2], 140, separate=True)))
                plan.append(("等高横排 200px",
                             await _row_md_height(hc, urls[:2], 200)))
                plan.append(("等高横排 260px",
                             await _row_md_height(hc, urls[:2], 260)))
                if len(urls) >= 2:
                    plan.append(("3 张横排 140px",
                                 await _row_md(hc, urls + urls[:1], 140, separate=False)))
        if case in ("all", "widths"):
            import httpx as _hx  # noqa: PLC0415

            async with _hx.AsyncClient(timeout=30, follow_redirects=True) as hc:
                for total in (240, 260, 280, 300):
                    plan.append((f"总宽 {total}px",
                                 await _row_md_total(hc, urls[:2], total)))
        if case in ("all", "fresh"):
            # 用没发过的图（调用方保证这些 URL 是新生成的）**不预热**直接发
            fresh = urls[-1:]
            plan.append(("新图不预热", _md(fresh, head="**【预热】全新图，没预热过**")))

        for name, content in plan:
            chars = len(content)
            try:
                data = await s.send(group, content)
            except Exception as exc:  # noqa: BLE001
                print(f"  ❌ {name}: {exc}")
                continue
            print(f"  ✅ {name}（{chars} 字符）→  msg_id {data.get('id')}")
            await asyncio.sleep(2)
    finally:
        await s.close()

    print(f"\n共发出 {len(s.sent)} 条。请肉眼确认每条里：")
    print("  · 标题那行在不在")
    print("  · 图渲染了几张（和标题写的数字比）")
    print("  · 图的比例对不对（生成的都是 3:4 竖图，被拉扁就是比例错了）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
