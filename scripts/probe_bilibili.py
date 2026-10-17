"""看 bilibili 链接会被解析成什么、卡片长什么样。

**默认什么都不发。** 只是把「提取 → 解析 → 渲染」跑一遍打出来，方便在
真发到群里之前先肉眼过一遍 markdown。只有显式加 `--send <group_openid>`
才真的发一条出去。

跑法：
    uv run python scripts/probe_bilibili.py                      # 用内置的四条真实报文样本
    uv run python scripts/probe_bilibili.py https://b23.tv/xxx BV15eJt6pEam
    uv run python scripts/probe_bilibili.py --raw '<群消息的 content 原文>'
    uv run python scripts/probe_bilibili.py --send <group_openid> https://b23.tv/xxx

⚠️ **`--send` 必须显式给 `group_openid`，这里故意不给默认值。**
`test_keyboard.py` 那种「默认发到库里第一个已知群」的写法在这个项目里太危险 ——
群 openid 不是群号，拿到手也看不出是不是测试群（CLAUDE.md 里那条硬性约束）。

⚠️ `--send` 走的是**主动消息**（脚本里没有 msg_id，做不了被动回复），
所以要占主动消息配额，而且群里得开过「机器人主动在群聊内发言」。
机器人自己跑的时候走的是被动回复，不占配额。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from qq_rss_bot import bilibili  # noqa: E402
from qq_rss_bot.config import load_config  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

ERROR_HINTS = {
    "54001": "参数错误，看 response 里的 message",
    "54002": "消息体格式不对",
    "34104": "主动消息频次超限",
    "34005": "机器人没有「主动在群聊内发言」权限",
    "22009": "主动消息额度用完",
    "304036": "无 Markdown 模板权限",
    "40034127": "无 Markdown 模板权限",
}

# 2026-10-17 抓到的四条真实群消息 content，原样贴进来当样本。
SAMPLES: list[tuple[str, str, dict | None]] = [
    (
        "形态 2：纯文本 + b23.tv 短链",
        "【“男人声称机器人杀了他的狗，要求保险公司给他赔偿，谁知保险员却拿起了刀《机器纪元》”"
        "-哔哩哔哩】 https://b23.tv/uyKBSFP",
        None,
    ),
    (
        "形态 3：图文 H5 卡片（jump_url 在 content 里）",
        "[卡片消息] 图文H5\n摘要: [分享]【中字熟肉】气象厅の狠活！10月16日误报9.9级末日大震惊…\n"
        "title: 【中字熟肉】气象厅の狠活！10月16日误报9.9级末日大震惊…\ndesc: 来自哔哩哔哩\n"
        "jump_url: https://b23.tv/e1Xp08V\ntag: 哔哩哔哩HD",
        None,
    ),
    (
        "形态 4：纯文本 + BV 号",
        "https://www.bilibili.com/video/BV15eJt6pEam",
        None,
    ),
    (
        "形态 1：QQ 小程序卡片（报文里没有视频 ID，只能标题反查）",
        "[卡片消息] 小程序\n摘要: [QQ小程序]“男人声称机器人杀了他的狗，要求保险公司给他赔偿，"
        "谁知保险员却拿起了刀《机器纪元》”\nsource_logo: https://open.gtimg.cn/open/app_icon/x.png\n"
        "title: “男人声称机器人杀了他的狗，要求保险公司给他赔偿，谁知保险员却拿起了刀《机器纪元》”\n"
        "preview: https://pic.ugcimg.cn/5248e67e658cc9c3c54f616ce575790b/jpg1\nsource: 哔哩哔哩",
        {
            "ark_name": "小程序",
            "ark_type": "miniapp",
            "prompt": "[QQ小程序]…",
            "fields": {
                "preview": "https://pic.ugcimg.cn/5248e67e658cc9c3c54f616ce575790b/jpg1",
                "source": "哔哩哔哩",
                "title": "“男人声称机器人杀了他的狗，要求保险公司给他赔偿，"
                         "谁知保险员却拿起了刀《机器纪元》”",
            },
        },
    ),
]


def parse_args(argv: list[str]) -> tuple[str | None, str | None, list[str]]:
    """返回 (--send 的 group_openid, --raw 的原文, 位置参数)。"""
    group = raw = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--send":
            i += 1
            if i >= len(argv):
                raise SystemExit("--send 后面要跟 group_openid")
            group = argv[i]
        elif arg.startswith("--send="):
            group = arg.split("=", 1)[1]
        elif arg == "--raw":
            i += 1
            if i >= len(argv):
                raise SystemExit("--raw 后面要跟一段 content 原文")
            raw = argv[i]
        elif arg.startswith("--raw="):
            raw = arg.split("=", 1)[1]
        elif arg in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
        else:
            rest.append(arg)
        i += 1
    return group, raw, rest


async def main() -> int:
    group, raw, args = parse_args(sys.argv[1:])

    if raw is not None:
        cases: list[tuple[str, str, dict | None]] = [("--raw 给的原文", raw, None)]
    elif args:
        cases = [(f"命令行参数 {a}", a, None) for a in args]
    else:
        cases = SAMPLES

    print(f"共 {len(cases)} 个样本；" + ("**会真的发一条消息出去**" if group else "只打印，不发消息"))
    print("=" * 72)

    first_ok: str | None = None
    async with httpx.AsyncClient(timeout=bilibili.HTTP_TIMEOUT + 4) as http:
        for name, content, ark in cases:
            print(f"\n### {name}")
            print(f"content: {content[:140]}{'…' if len(content) > 140 else ''}")

            target = bilibili.find_target(content, ark)
            if target is None:
                print("  → find_target 返回 None（不是 bilibili 分享，机器人会静默忽略）")
                continue
            print(f"  → target: {target}")

            data = await bilibili.lookup(http, target)
            if data is None:
                print("  → lookup 返回 None（解析失败，机器人同样静默不回）")
                continue

            stat = data.get("stat") or {}
            print(f"  → bvid={data.get('bvid')} owner={(data.get('owner') or {}).get('name')!r} "
                  f"duration={data.get('duration')} view={stat.get('view')}")

            markdown = bilibili.render(data)
            print("  → 渲染出来的 markdown：")
            print("  ┌" + "─" * 68)
            for line in markdown.splitlines():
                print("  │ " + line)
            print("  └" + "─" * 68)
            if first_ok is None:
                first_ok = markdown

    if group is None:
        print("\n（没给 --send，到此为止。要真发一条：--send <group_openid> <url>）")
        return 0

    if first_ok is None:
        print("\n没有任何一个样本解析成功，不发。")
        return 1

    cfg = load_config()
    print(f"\n⚠️  正在往群 {group} 发一条**主动消息**（会占配额）…")
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(TOKEN_URL, json={"appId": cfg.appid, "clientSecret": cfg.secret})
        token = resp.json().get("access_token") if resp.status_code == 200 else None
        if not token:
            print(f"取 token 失败: HTTP {resp.status_code} {resp.text[:200]}")
            return 1

        body = {"msg_type": 2, "markdown": {"content": first_ok}}   # 不带 keyboard
        print("\n发送的 body：")
        print(json.dumps(body, ensure_ascii=False, indent=2))

        resp = await http.post(
            f"{API_BASE}/v2/groups/{group}/messages",
            headers={"Authorization": f"QQBot {token}", "X-Union-Appid": cfg.appid,
                     "Content-Type": "application/json"},
            json=body,
        )
        print(f"\n← HTTP {resp.status_code}\n← {resp.text[:600]}")

        if resp.status_code != 200:
            return 1
        try:
            code = str((resp.json() or {}).get("code") or "")
        except ValueError:
            code = ""
        if code and code != "0":
            print(f"\n❌ 业务错误码 {code}：{ERROR_HINTS.get(code, '见 response')}")
            return 1

        print("\n✅ 接口收下了。**去群里看封面貌不貌美** —— 200 不等于渲染出来了。")
        print("   要重点确认三件事（都只能实测）：")
        print("   1. `![封面 #208px #117px](url)` 的尺寸语法 QQ 认不认")
        print("   2. 标题里的 `|` / `[` 会不会破坏排版")
        print("   3. `i1.hdslb.com` 的封面平台能不能下载转存")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
