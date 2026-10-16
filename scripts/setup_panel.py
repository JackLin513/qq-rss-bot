"""配置 QQ 官方机器人的「指令面板」（群聊场景）。

指令面板的用途：用户在 QQ 客户端输入框里打 `/` 时会弹出半屏面板，
展示本机器人可用的指令，点击即把指令填进输入框。

接口（官方文档 api-v2，host 与消息接口相同）：
    GET    /v2/panels                  查询指令面板列表
    POST   /v2/panels                  创建，限 10 QPM，一个机器人最多 20 个面板
    GET    /v2/panels/{panel_id}       查询详情
    PUT    /v2/panels/{panel_id}       修改
    DELETE /v2/panels/{panel_id}       删除
    PUT    /v2/panels/{panel_id}/target 修改关联对象

文档没写 host，这里按消息接口的 host 走（`https://api.sgroup.qq.com`）。
脚本会先打一条只读的 GET 验证这一点，再动任何东西。

注意：指令面板是较新的能力（约 2026-08 起），**需要较新版 QQ 客户端**才会显示，
且手机版优先、桌面版可能延后。所以「接口返回成功但客户端看不到」是可能发生的，
属于灰度发布的正常现象，不是配错了。

跑法：
    uv run python scripts/setup_panel.py --list       # 只读，先看现有的
    uv run python scripts/setup_panel.py --create     # 创建群聊面板
    uv run python scripts/setup_panel.py --update <panel_id>
    uv run python scripts/setup_panel.py --delete <panel_id>
"""

from __future__ import annotations

import asyncio
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

SCOPE = "group"
ALL_SCOPES = ("group", "c2c", "channel", "dm")
TARGET_TYPE = "all"  # 该场景下所有群都生效（机器人在群里的 openid 要进群后才知道，
# 所以不用 specific —— 那需要先把 group_openid 列进去）

# 官方限制：name ≤14、desc ≤30、单面板 ≤20 个元素。
# 这几个数是**显示宽度**不是字符数：非 ASCII（中文）按 2 算。
# 按字符数校验会在中文描述上放行、到接口才报 30013，而 30013 文档写的是
# 「超出数量限制」，极易误判成元素太多 —— 实际是字数太宽。见 _width()。
NAME_LIMIT = 14
DESC_LIMIT = 30

# only_admin 与代码里的 RESTRICTED_TOKENS 对齐：
# /sub 和 /unsub 在代码里就是仅群主/管理员可用，面板上也标上，UI 语义才一致。
# 名称用**裸名**（不带斜杠）：实测服务端会把开头的 `/` 剥掉存成 `sub`，
# 客户端点击时自己补 `/`。写成 `/sub` 不会被拒，但回读时对不上，制造假差异。
PANEL_ITEMS = [
    {"name": "sub", "desc": "订阅 RSS 源，可加间隔分钟", "type": "command", "only_admin": True},
    {"name": "unsub", "desc": "取消本群某个订阅", "type": "command", "only_admin": True},
    {"name": "list", "desc": "查看本群所有订阅", "type": "command", "only_admin": False},
    {"name": "check", "desc": "立刻抓一次验证源是否可用", "type": "command", "only_admin": False},
    {"name": "ping", "desc": "检查机器人在线状态", "type": "command", "only_admin": False},
    {"name": "help", "desc": "查看完整用法说明", "type": "command", "only_admin": False},
]

PANEL_REMARK = "qq-rss-bot 指令面板（由 scripts/setup_panel.py 生成）"

# 官方错误码 → 人话。
# 注意：文档写的是 4003xxxx，实测运行时返回的是后 5 位（如 30011），
# 所以这里按规范化后的短码做键，两种写法都能命中。
ERROR_HINTS = {
    "30001": "参数错误，检查请求参数",
    "30008": "链接格式错误，必须以 https:// 开头",
    "30009": "有并发的面板操作在进行，稍后重试",
    "30011": "生效场景不合法，scope 只能是 c2c / group / channel / dm",
    "30012": "target_type 非法，或 channel/dm 只支持 all",
    "30013": "超出数量限制：单面板 ≤20 元素，或 name/desc 显示宽度超限"
             "（中文按 2 计，name ≤14、desc ≤30）",
    "30006": "指令面板不存在：panel_id 不对或已被删除，先用 --list 拿正确的",
    "30015": "元素 type 非法，只能是 command / link",
    "30016": "缺少必填字段",
    "30018": "该场景不支持此操作",
    "30020": "内容未通过安全检查",
    "30021": "全局面板不能带 specific 目标",
}


def _norm_code(code: object) -> str:
    """把 40030011 / 30011 都归一到 '30011'。"""
    s = str(code)
    return s[3:] if s.startswith("400") and len(s) == 8 else s


def _name_key(name: object) -> str:
    """指令名归一：去掉开头斜杠后比对。

    服务端存的是裸名，但不同来源（代码、手配、历史数据）可能带斜杠。
    不归一的话会报出一堆「缺少 /sub / 多出 sub」的假差异，把真正的问题淹掉。
    """
    return str(name or "").lstrip("/")


def compare_items(remote: list[dict]) -> list[str]:
    """把线上面板和代码里的 PANEL_ITEMS 对比，返回差异描述。

    面板是用户看到的唯一入口，和代码不一致会让人按着面板点了却执行不了，
    所以这个对比是「适配」的核心检查。
    """
    diffs: list[str] = []
    local = {_name_key(i["name"]): i for i in PANEL_ITEMS}
    online = {_name_key(i.get("name")): i for i in remote}

    for name in sorted(local.keys() - online.keys()):
        diffs.append(f"面板缺少元素 {name}")
    for name in sorted(online.keys() - local.keys()):
        diffs.append(f"面板多出元素 {name}（代码里没有）")
    for name in sorted(local.keys() & online.keys()):
        for field in ("desc", "type", "only_admin"):
            want, got = local[name].get(field), online[name].get(field)
            if field == "only_admin":
                # 服务端在 false 时**省略该字段**，回读成 None。
                # 不归一的话每个非管理员指令都会报一条假差异。
                want, got = bool(want), bool(got)
            if want != got:
                diffs.append(f"{name} 的 {field}：代码={want!r} 面板={got!r}")
    if [_name_key(i["name"]) for i in PANEL_ITEMS] != [_name_key(i.get("name")) for i in remote]:
        diffs.append("元素顺序不一致（代码里的顺序视为期望顺序）")
    return diffs


def _width(text: str) -> int:
    """按官方口径算显示宽度：全角/宽字符算 2，其余算 1。

    官方校验的是宽度不是字符数。用 len() 数的话，
    「订阅本群所有 RSS 更新」这种 11 个字的中文描述会被当成 11 放行，
    实际宽度 20+，接口才打回来。
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def validate(items: list[dict]) -> list[str]:
    """本地先校验一遍，免得被接口打回来还要猜原因。"""
    problems: list[str] = []
    if len(items) > 20:
        problems.append(f"元素数 {len(items)} 超过上限 20")
    for i, item in enumerate(items):
        name, desc = item.get("name", ""), item.get("desc", "")
        if _width(name) > NAME_LIMIT:
            problems.append(
                f"第 {i + 1} 个元素 name 宽 {_width(name)}，上限 {NAME_LIMIT}：{name!r}"
            )
        if _width(desc) > DESC_LIMIT:
            problems.append(
                f"第 {i + 1} 个元素 desc 宽 {_width(desc)}，上限 {DESC_LIMIT}：{desc!r}"
            )
        if item.get("type") not in ("command", "link"):
            problems.append(f"第 {i + 1} 个元素 type 非法：{item.get('type')!r}")
        if item.get("type") == "link" and not str(item.get("link", "")).startswith("https://"):
            problems.append(f"第 {i + 1} 个元素是 link 但 link 不以 https:// 开头")
    keys = [_name_key(i.get("name")) for i in items]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        problems.append(f"指令名重复（归一后）：{sorted(dupes)}")
    return problems


def explain(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:
        return f"HTTP {resp.status_code} {resp.text[:200]}"
    code = body.get("code")
    text = f"HTTP {resp.status_code} code={code} {body.get('message', '')}"
    if resp.status_code == 200:
        return text
    hint = ERROR_HINTS.get(_norm_code(code))
    # 失败时把原始响应也带上：错误码映射可能不全，原文最可靠
    return f"{text}\n  原始响应: {resp.text[:300]}" + (f"\n  → {hint}" if hint else "")


async def main() -> int:
    argv = sys.argv[1:]
    mode = "list"
    panel_id = None
    if "--create" in argv:
        mode = "create"
    elif "--update" in argv:
        mode, panel_id = "update", argv[argv.index("--update") + 1]
    elif "--delete" in argv:
        mode, panel_id = "delete", argv[argv.index("--delete") + 1]
    elif "--list" in argv:
        mode = "list"

    scope_arg = None
    if "--scope" in argv:
        scope_arg = argv[argv.index("--scope") + 1]
        if scope_arg not in ALL_SCOPES:
            print(f"--scope 只能是 {' / '.join(ALL_SCOPES)}")
            return 1

    from qq_rss_bot.config import load_config

    cfg = load_config()

    problems = validate(PANEL_ITEMS)
    if problems:
        print("本地校验不通过，未发起任何请求：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print(
        f"本地校验通过：{len(PANEL_ITEMS)} 个元素，名称/描述显示宽度均在官方限制内"
        f"（name ≤{NAME_LIMIT}、desc ≤{DESC_LIMIT}，中文按 2 计）"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            TOKEN_URL, json={"appId": cfg.appid, "clientSecret": cfg.secret}
        )
        token = resp.json().get("access_token") if resp.status_code == 200 else None
        if not token:
            print(f"取 token 失败: HTTP {resp.status_code} {resp.text[:200]}")
            return 1
        headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": cfg.appid,
            "Content-Type": "application/json",
        }

        # 先只读探测：scope 是必填查询参数（漏了返回 30011「生效场景不合法」）。
        # 列表面板时把四个场景都查一遍 —— 面板可能建在别的 scope 下
        # （平台 UI 里不选群聊就会落到单聊），只查 group 会显示成一个都没有。
        scopes = [scope_arg] if scope_arg else (list(ALL_SCOPES) if mode == "list" else [SCOPE])

        total = 0
        for sc in scopes:
            resp = await client.get(
                f"{API_BASE}/v2/panels", params={"scope": sc}, headers=headers
            )
            if resp.status_code != 200:
                print(f"GET /v2/panels?scope={sc} → {explain(resp)}")
                print("\n只读查询失败，先别继续：可能是 host 与文档不符，或账号无此能力。")
                return 1
            records = resp.json().get("records") or []
            total += len(records)
            print(f"\n=== scope={sc}：{len(records)} 个面板 ===")
            for p in records:
                items = (p.get("panel") or {}).get("items") or []
                print(
                    f"\n  panel_id={p.get('panel_id')}  target={p.get('target_type')}"
                    f"  version={p.get('version')}"
                )
                print(f"  updated_at={p.get('updated_at')}")
                for it in items:
                    marks = []
                    if it.get("only_admin"):
                        marks.append("仅管理员")
                    if it.get("type") == "link":
                        marks.append(f"→ {it.get('link')}")
                    suffix = ("  " + " ".join(marks)) if marks else ""
                    print(
                        f"    {it.get('type', '?'):7} {str(it.get('name', '?')):<9}"
                        f" {it.get('desc', '')}{suffix}"
                    )

                if sc != SCOPE:
                    print(f"\n    ⚠️ 这个面板建在 scope={sc} 下，群聊里不会显示")
                    print(f"    修法：--delete {p.get('panel_id')}，再用 --create 建到 group")
                    continue

                diffs = compare_items(items)
                if diffs:
                    print("\n    与代码不一致（用户照面板点会执行不了）：")
                    for d in diffs:
                        print(f"      ✗ {d}")
                    print(f"    修法：uv run python scripts/setup_panel.py --update {p.get('panel_id')}")
                else:
                    print("\n    ✅ 与代码完全一致")

        if mode == "list":
            print(f"\n合计 {total} 个面板")
            return 0

        if mode == "delete":
            resp = await client.delete(f"{API_BASE}/v2/panels/{panel_id}", headers=headers)
            print(f"DELETE {panel_id} → {explain(resp)}")
            return 0 if resp.status_code == 200 else 1

        body = {
            "scope": SCOPE,
            "target_type": TARGET_TYPE,
            "panel": {"items": PANEL_ITEMS, "remark": PANEL_REMARK, "version": 1},
        }

        if mode == "create":
            resp = await client.post(f"{API_BASE}/v2/panels", headers=headers, json=body)
            print(f"POST /v2/panels → {explain(resp)}")
            if resp.status_code == 200:
                pid = resp.json().get("panel_id")
                print(f"\n✅ 创建成功，panel_id = {pid}")
                print("   以后改内容用：uv run python scripts/setup_panel.py --update " + str(pid))
                print("\n   如果 QQ 客户端里打 / 看不到面板：需要较新版客户端，")
                print("   手机版优先、桌面版可能延后，属灰度期的正常现象。")
                return 0
            return 1

        # mode == update
        resp = await client.put(f"{API_BASE}/v2/panels/{panel_id}", headers=headers, json=body)
        print(f"PUT {panel_id} → {explain(resp)}")
        return 0 if resp.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
