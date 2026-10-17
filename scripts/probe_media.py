"""探测 QQ 的服务器能不能拉到某个图片 URL。

**为什么需要单独探这个**：发图是两步，第一步 `post_*_file` 只是把 URL 交给 QQ，
**真正去下载的是 QQ 自己的服务器**。所以「图能不能显示出来」取决于 QQ 的服务器
够不够得到那个域名 —— 不是我们的网络。这一条我们完全控制不了，只能实测。

bilibili 的 `i1.hdslb.com` 实测可以；YouTube / Twitter 的图床在墙内就不好说了。
**别拿宿主机的 curl 结果推断** —— 实测过：`api.bilibili.com` 从 NAS 宿主机是
000，但从容器里是 ✅（宿主机那套 Surge MITM 的干扰）。

这个脚本就测这一步，而且**只上传、不发消息**（`srv_send_msg=False`）：
拿到 `file_info` 就是 QQ 能拉到，报错就是拉不到。所以它不会往群里发任何东西。

跑法：
    uv run python scripts/probe_media.py                    # 只列出候选 URL，不碰 QQ
    uv run python scripts/probe_media.py --c2c-me           # 私聊你自己，零群参与
    uv run python scripts/probe_media.py --c2c <openid>
    uv run python scripts/probe_media.py --group <openid>   # 群 openid，**不是群号**
    uv run python scripts/probe_media.py --c2c-me <url> [<url> ...]   # 自己加 URL

    # base64 模式：我们自己下载，再上传字节 —— QQ 就不需要够得到那个域名
    uv run python scripts/probe_media.py --c2c-me --data
    uv run python scripts/probe_media.py --c2c-me --data --proxy http://192.168.0.63:6152

**2026-10-17 实测结论**（决定了为什么需要 `--data`）：
`i.ytimg.com` 交给 QQ 的服务器去拉，返回 `850027 富媒体文件上传超时`
（err_code 40034003）—— **不是 404 也不是 DNS 失败，是超时**，说明 QQ 的服务器
够得到域名但拉不下内容。bilibili 的 `i1.hdslb.com` 同一时刻是 ✅。
所以 YouTube / Twitter 这类必须走 `--data`。

⚠️ **目标必须显式给，这里故意不给默认值。** `test_keyboard.py` 那种「默认发到
库里第一个已知群」的写法在这个项目里太危险 —— 群 openid 不是群号，拿到手也
看不出是不是测试群（CLAUDE.md 里那条硬性约束）。所以只提供 `--c2c-me` 这一个
便利写法：它要求库里登记的通知对象**恰好一个**，否则报错让你显式指定。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from qq_rss_bot.config import load_config  # noqa: E402
from qq_rss_bot.db import Database  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

ERROR_HINTS = {
    "850027": "QQ 的服务器**拉这个 URL 超时**（err_code 40034003）—— 不是 404 也不是"
              "DNS 失败。加 --data 改成我们自己下载再上传 base64。",
    "40034074": "URL 拉取失败 —— **QQ 的服务器下载不到这个图**（就是我们要测的结论）",
    "54001": "参数错误，看 response 里的 message",
    "54002": "消息体格式不对",
    "304036": "无 Markdown 模板权限",
}

# 候选 URL。第一条是**对照组**：bilibili 封面，现在线上就在用，必须成功 ——
# 它要是也失败，说明是脚本或权限的问题，不是「QQ 拉不到 YouTube」。
CANDIDATES: list[tuple[str, str]] = [
    ("对照组 bilibili 封面（线上在用的，必须成功）",
     "https://i1.hdslb.com/bfs/archive/ab534aee2845949a97f88ca7e6673b18b9fd35b1.jpg"
     "@480w_270h_1c.jpg"),
    ("YouTube 缩略图 hqdefault", "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"),
    ("YouTube 缩略图 maxresdefault", "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"),
    # Twitter 的图床（pbs.twimg.com）需要一条真实的推文配图 URL，没法内置稳定的样本，
    # 用法：把推文里那张图的地址当位置参数传进来
    #   uv run python scripts/probe_media.py --c2c-me 'https://pbs.twimg.com/media/XXXX.jpg'
]


def parse_args(argv: list[str]) -> tuple[str, str, list[str], bool, str]:
    """返回 (模式, 目标 openid, 额外的 URL, 是否走 base64, 下载用的代理)。"""
    mode, target, extra = "c2c", "", []
    use_data, proxy = False, ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--data":
            use_data = True
        elif a == "--proxy":
            if i + 1 >= len(argv):
                raise SystemExit("--proxy 后面要跟代理地址，如 http://192.168.0.63:6152")
            i += 1
            proxy = argv[i]
        elif a.startswith("--proxy="):
            proxy = a.split("=", 1)[1]
        elif a == "--c2c-me":
            mode = "c2c"
            target = "<me>"
        elif a in ("--c2c", "--group"):
            if i + 1 >= len(argv):
                raise SystemExit(f"{a} 后面要跟 openid")
            i += 1
            mode = "c2c" if a == "--c2c" else "group"
            target = argv[i]
        elif a.startswith("--c2c=") or a.startswith("--group="):
            mode = "c2c" if a.startswith("--c2c=") else "group"
            target = a.split("=", 1)[1]
        elif a in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
        elif a.startswith("-"):
            raise SystemExit(f"未知参数：{a}（-h 看用法）")
        else:
            extra.append(a)
        i += 1
    return mode, target, extra, use_data, proxy


def resolve_me(cfg) -> str:
    """`--c2c-me`：从库里登记的**通知对象**里取，要求恰好一个。

    为什么不用「库里第一个已知群」那种写法：群 openid 看不出是不是测试群。
    私聊对象则天然只有你自己 —— 但前提是没配 NOTIFY_OPENIDS 白名单、或者
    白名单就一个人；登记表里出现多个（比如还有别的管理员）就报错，
    逼你显式指定，别猜。
    """
    if cfg.notify_openids:
        if len(cfg.notify_openids) == 1:
            return cfg.notify_openids[0]
        raise SystemExit(
            f"NOTIFY_OPENIDS 里有 {len(cfg.notify_openids)} 个 openid，"
            "没法判断哪个是你 —— 请显式 --c2c <openid>"
        )
    db = Database(cfg.db_path)
    try:
        found = {
            m
            for g in db.known_groups()
            for m in db.notify_targets(g)
        }
    finally:
        db.close()
    if len(found) == 1:
        return found.pop()
    raise SystemExit(
        f"库里登记了 {len(found)} 个通知对象，没法判断哪个是你 —— "
        "请显式 --c2c <openid>（或配 NOTIFY_OPENIDS 白名单）"
    )


async def main() -> int:
    mode, target, extra, use_data, proxy = parse_args(sys.argv[1:])
    urls = extra or [u for _, u in CANDIDATES]
    labels = {u: n for n, u in CANDIDATES}
    if extra:
        labels.update({u: "命令行给的" for u in extra})

    if not target:
        print("候选 URL（还没碰 QQ）：\n")
        for name, url in CANDIDATES:
            print(f"  · {name}\n    {url}")
        print("\n加 --c2c-me（私聊你自己）或 --group <openid> 才会真的去探。")
        print("**只上传、不发消息**，所以不会往群里发任何东西。")
        return 0

    cfg = load_config()
    if target == "<me>":
        target = resolve_me(cfg)
        print(f"目标：私聊（openid …{target[-8:]}）")
    else:
        print(f"目标：{'私聊' if mode == 'c2c' else '群'}（openid …{target[-8:]}）")
    print(f"模式：{'base64（我们自己下载）' if use_data else 'URL（QQ 自己去下载）'}"
          + (f"，下载代理 {proxy}" if proxy else ""))

    route = (f"{API_BASE}/v2/users/{target}/files" if mode == "c2c"
             else f"{API_BASE}/v2/groups/{target}/files")

    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(TOKEN_URL, json={"appId": cfg.appid, "clientSecret": cfg.secret})
        token = r.json().get("access_token") if r.status_code == 200 else None
        if not token:
            print(f"取 token 失败: HTTP {r.status_code} {r.text[:200]}")
            return 1
        headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": cfg.appid,
            "Content-Type": "application/json",
        }

        print("=" * 72)
        ok_count = 0
        for url in urls:
            print(f"\n### {labels.get(url, '')}\n{url}")
            if use_data:
                # 我们自己下载 —— 这一步和 QQ 无关，是容器自己的出网
                try:
                    if proxy:
                        async with httpx.AsyncClient(
                            timeout=20, proxy=proxy, follow_redirects=True
                        ) as dh:
                            img = await dh.get(url)
                    else:
                        img = await http.get(url, timeout=20)
                    img.raise_for_status()
                except Exception as exc:  # noqa: BLE001
                    print(f"  ⚠️  我们自己下载失败（和 QQ 无关）：{type(exc).__name__}: {exc}")
                    print(f"      {'代理：' + proxy if proxy else '直连'}"
                          " —— 直连失败就加 --proxy 试试")
                    continue
                raw = img.content
                b64 = base64.b64encode(raw).decode()
                print(f"  → 自己下载到 {len(raw)} 字节 → base64 {len(b64)} 字节")
                body = {"file_type": 1, "file_data": b64, "srv_send_msg": False}
            else:
                # srv_send_msg=False = 只上传拿 file_info，**不往群里发消息**
                body = {"file_type": 1, "url": url, "srv_send_msg": False}
            resp = await http.post(route, headers=headers, json=body)
            try:
                data = resp.json() or {}
            except ValueError:
                data = {}
            code = str(data.get("code") or "")
            if resp.status_code == 200 and not code and data.get("file_info"):
                ok_count += 1
                print(f"  ✅ QQ 拉得到（HTTP {resp.status_code}，拿到 file_info）")
            else:
                print(f"  ❌ HTTP {resp.status_code} code={code or '-'}")
                print(f"     {ERROR_HINTS.get(code, '')}")
                print(f"     {json.dumps(data, ensure_ascii=False)[:300]}")
                if not data:
                    print(f"     {resp.text[:300]}")

    print("\n" + "=" * 72)
    if use_data:
        print(f"结论：{ok_count}/{len(urls)} 个图片走 base64 上传成功。")
        print("失败要分清两种：⚠️ 是**我们自己下载不到**（容器出网/代理），"
              "❌ 是 QQ 不收字节 —— 排查方向完全不同。")
    else:
        print(f"结论：{ok_count}/{len(urls)} 个 URL 的图片，QQ 的服务器拉得到。")
    print("对照组若失败，先查权限/参数，别急着下「YouTube 不行」的结论。")
    return 0 if ok_count else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
