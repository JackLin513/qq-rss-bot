"""实测 markdown（`msg_type=2`）的渲染边界 —— **只发测试群**。

三个数在离线永远测不出来，只能真发：

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
