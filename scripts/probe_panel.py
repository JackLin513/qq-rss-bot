"""探测指令面板 API 的 schema 边界 —— 重点是「能不能嵌套」。

为什么只能靠探：官方文档站是 SPA，抓任何 URL 都返回同一页正文，拿不到 schema；
而 item 里有 `type` 字段，说明它可能有 `command` 以外的取值（比如 menu），
但没有任何文档佐证。所以直接问接口。

做法：在 **c2c** 场景建一次性面板（那个 scope 实测是空的，**不碰群里的面板**），
读回来看看服务端怎么处理，**然后立刻删掉**。全程几秒。

判读：
* POST 被拒            → 该 type / 字段不被接受，报错通常能看出允许哪些值
* POST 成功但回读没了   → 服务端静默丢弃未知字段 ⇒ **不支持嵌套**
* POST 成功且回读还在   → 服务端真存下来了，值得进一步试

跑法：uv run python scripts/probe_panel.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from qq_rss_bot.config import load_config  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

# 建在 c2c：实测 group 下有正式面板，c2c/channel/dm 都是 0 个。
# 探针只动这里，不会影响群里那个。
SCOPE = "c2c"
TARGET_TYPE = "all"

CHILDREN = [
    {"name": "imas-news", "desc": "偶像大师最新新闻", "type": "command", "only_admin": False},
]

PROBES: list[tuple[str, dict]] = [
    # ① 嵌套：三种可能的字段名都会被**静默丢弃**
    #    （POST 返回 200，回读只剩 name/desc/type）⇒ 面板是平的
    ("command + children（嵌套字段名 A）",
     {"name": "pb-kids", "desc": "探测 children", "type": "command", "children": CHILDREN}),
    ("command + items / sub_items（嵌套字段名 B、C）",
     {"name": "pb-nest", "desc": "探测 items/sub_items", "type": "command",
      "items": CHILDREN, "sub_items": CHILDREN}),
    # ② type 是**校验过的枚举**：非法值报 30015
    ("type=menu（非法）", {"name": "pb-menu", "desc": "探测 menu 类型", "type": "menu"}),
    ("type=text（非法）", {"name": "pb-text", "desc": "探测 text 类型", "type": "text"}),
    # ③ link 是合法类型，但报 30016 缺字段 —— 和 30015 是**不同的错**，
    #    这个区别就是「不存在」和「存在但还差东西」的分界
    ("type=link 但不给 link 字段", {"name": "pb-nolk", "desc": "探测 link 必填",
                                    "type": "link"}),
    # ④ 必填字段就叫 link
    ("type=link + link 字段（合法）",
     {"name": "pb-link", "desc": "探测 link 全字段", "type": "link",
      "link": "https://example.com"}),
]


async def _read_back(client, headers, panel_id: str) -> list | None:
    """读回面板内容。

    先试详情接口，再退回列表接口 —— 两条路都试是因为不知道哪条可靠。
    **把原始响应打出来**：回读失败时最需要看到的就是服务端到底回了什么。
    """
    r = await client.get(f"{API_BASE}/v2/panels/{panel_id}", headers=headers,
                         params={"scope": SCOPE})
    print(f"  GET /v2/panels/{panel_id} → HTTP {r.status_code} {r.text[:400]}")
    if r.status_code == 200:
        items = (r.json().get("panel") or {}).get("items")
        if items:
            return items

    r = await client.get(f"{API_BASE}/v2/panels", headers=headers, params={"scope": SCOPE})
    print(f"  GET /v2/panels?scope={SCOPE} → HTTP {r.status_code} {r.text[:600]}")
    if r.status_code != 200:
        return None
    for rec in r.json().get("data") or []:
        if rec.get("panel_id") == panel_id:
            return (rec.get("panel") or {}).get("items") or []
    print("  （列表里没找到刚建的面板 —— 可能列表有延迟）")
    return None


async def _delete(client, headers, panel_id: str) -> None:
    r = await client.delete(f"{API_BASE}/v2/panels/{panel_id}", headers=headers)
    if r.status_code != 200:
        # 有些接口要求 scope 也带上，退一步再试
        r = await client.delete(f"{API_BASE}/v2/panels/{panel_id}", headers=headers,
                                params={"scope": SCOPE})
    print(f"  DELETE → HTTP {r.status_code} {'（已清理）' if r.status_code == 200 else r.text[:200]}")


async def probe(client, headers, label: str, item: dict) -> None:
    print(f"\n{'─' * 62}")
    print(f"探测：{label}")
    print(f"  发出 {json.dumps(item, ensure_ascii=False)}")
    body = {
        "scope": SCOPE,
        "target_type": TARGET_TYPE,
        "panel": {"items": [item], "remark": "一次性探测面板，用完即删", "version": 1},
    }
    panel_id = None
    try:
        r = await client.post(f"{API_BASE}/v2/panels", headers=headers, json=body)
        print(f"  POST → HTTP {r.status_code} {r.text[:260]}")
        if r.status_code != 200:
            print("  ⇒ 被拒：这个 type / 字段服务端不接受")
            return
        panel_id = r.json().get("panel_id")
        print(f"  panel_id = {panel_id}")

        items = await _read_back(client, headers, panel_id)
        if items is None:
            print("  ⇒ 回读失败，无法判读")
            return
        print(f"  回读 → {json.dumps(items, ensure_ascii=False)}")
        got = items[0] if items else {}
        print(f"  回读的 type = {got.get('type')!r}")
        if "children" in got:
            print("  ⇒ children **存活**：服务端存下了嵌套结构")
        else:
            print("  ⇒ children **没了**：服务端静默丢弃未知字段 ⇒ 不支持嵌套")
        if got.get("type") != item.get("type"):
            print(f"  ⇒ type 被服务端改写：{item.get('type')!r} → {got.get('type')!r}")
    finally:
        if panel_id:
            await _delete(client, headers, panel_id)


async def main() -> int:
    cfg = load_config()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, json={"appId": cfg.appid, "clientSecret": cfg.secret})
        token = resp.json().get("access_token") if resp.status_code == 200 else None
        if not token:
            print(f"取 token 失败: HTTP {resp.status_code} {resp.text[:200]}")
            return 1
        headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": cfg.appid,
            "Content-Type": "application/json",
        }

        # 先确认目标 scope 是空的，避免误删别人的东西
        r = await client.get(f"{API_BASE}/v2/panels", headers=headers, params={"scope": SCOPE})
        before = len(r.json().get("data") or []) if r.status_code == 200 else -1
        print(f"探测前 scope={SCOPE} 有 {before} 个面板"
              + ("（预期 0，否则中止）" if before != 0 else "（空的，可以探）"))
        if before != 0:
            print("✗ 目标 scope 不是空的，中止以免动到已有配置")
            return 1

        for label, item in PROBES:
            await probe(client, headers, label, item)

        r = await client.get(f"{API_BASE}/v2/panels", headers=headers, params={"scope": SCOPE})
        after = len(r.json().get("data") or []) if r.status_code == 200 else -1
        print(f"\n{'=' * 62}")
        print(f"探测后 scope={SCOPE} 有 {after} 个面板"
              + ("  ✅ 已清干净" if after == 0 else "  ⚠️ 有残留，手工删掉"))
        return 0 if after == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
