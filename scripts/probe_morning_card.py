"""早安卡片（**单图模式**）实测探针 —— 一张头像 + 一段文字，再补一条语音。

**要回答的问题**：`msg_type=7`（富媒体）带一张 180×180 的**本地** PNG 时，
客户端渲染出来**多大**？如果大小本来就合适，早安这条就不用 markdown 了 ——
markdown 的图必须公网 URL，**本地文件在 markdown 下根本没有办法**
（QQ 的服务器自己去拉 URL，我们一个字节都不经手）。

一次发**两条**（`msg_type=7` 的 `MediaInfo` 只有一个 `file_info`，一条只能一个媒体）：

    ① 头像（`file_type=1`，字节我们 base64 传）+ `content` = 角色名 + 日语原文
    ② 语音（`file_type=3`，silk）+ `content=""`

跑法：

    uv run python scripts/probe_morning_card.py                        # 只打印，一条都不发
    uv run python scripts/probe_morning_card.py --group <openid> --send
    uv run python scripts/probe_morning_card.py --idol hiori --line 2 --group <openid> --send
    uv run python scripts/probe_morning_card.py --image <png> --group <openid> --send
    uv run python scripts/probe_morning_card.py --group <openid> --send --no-voice

## ⚠️ 只发测试群（群号 575861535）或私聊用户本人。绝不发正式群。

`--group` **故意没有默认值** —— 群 openid 不是群号，拿到手也看不出是不是测试群。
正式群（尾号 …E0629）在这里**写死拒发**。

## 发完要用眼睛确认的

1. 头像渲染出来**多大**？占不占得住一条消息？（太小就没意义，不如继续用语音）
2. `content` 那段文字**渲不渲染**、在图的上面还是下面？
3. 两条消息的**先后顺序**对不对（先图后语音）？
4. 头像和语音**是同一位偶像**吗？
"""

from __future__ import annotations

import asyncio
import base64
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from qq_rss_bot import morning  # noqa: E402
from qq_rss_bot.config import load_config  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
MSG_TYPE_RICH = 7
FILE_TYPE_IMAGE = 1
FILE_TYPE_VOICE = 3

# 正式群 openid 尾号 —— 写死拒发。这个脚本是**手动**跑的，填错一次就是事故。
FORBIDDEN_SUFFIX = "E0629"

# 语气提示的形态：`[softly]` / `[cheerfully]`。**只给 TTS 用**，
# 发到群里得剥掉 —— 上游只保证英文、字母/空格/逗号/撇号/连字符。
_CUE = re.compile(r"\[[A-Za-z][A-Za-z ,'-]*\]\s*")


def _arg(name: str, default: str | None = None) -> str | None:
    """`--name value` 或 `--name=value`。"""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(f"{name}="):
            return a.split("=", 1)[1]
    return default


def strip_cue(ja: str) -> str:
    """`[softly] あ……おはよう` → `あ……おはよう`。

    `lines.toml` / `.manifest.json` 里的 `ja` 都是**带语气提示**的原文，
    那个方括号是喂给 TTS 的，发到群里只会显得莫名其妙。
    """
    return _CUE.sub("", ja).strip()


def load_lines(cfg) -> dict[str, dict]:
    """`lines.toml` → `{偶像 id: {"name": …, "lines": [ja, …]}}`（**只给探针用**）。"""
    path = Path(cfg.morning_dir) / "lines.toml"
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    out: dict[str, dict] = {}
    for idol in data.get("idols", []):
        out[idol.get("id", "")] = {
            "name": idol.get("name", ""),
            "lines": [strip_cue(ln.get("ja", "")) for ln in idol.get("lines", [])],
        }
    return out


async def _token(client: httpx.AsyncClient, cfg) -> str:
    resp = await client.post(
        TOKEN_URL, json={"appId": cfg.appid, "clientSecret": cfg.secret}
    )
    tok = resp.json().get("access_token") if resp.status_code == 200 else None
    if not tok:
        raise SystemExit(f"取 token 失败: HTTP {resp.status_code} {resp.text[:200]}")
    return tok


def _headers(tok: str, appid: str) -> dict[str, str]:
    return {
        "Authorization": f"QQBot {tok}",
        "X-Union-Appid": appid,
        "Content-Type": "application/json",
    }


async def _upload(
    client: httpx.AsyncClient, headers: dict, group: str, *,
    file_type: int, file_data: str, label: str,
) -> str | None:
    """上传拿 `file_info`（`srv_send_msg=False`，不单独发一条）。"""
    up = await client.post(
        f"{API_BASE}/v2/groups/{group}/files",
        headers=headers,
        json={"file_type": file_type, "file_data": file_data, "srv_send_msg": False},
    )
    try:
        data = up.json() or {}
    except ValueError:
        data = {}
    info = data.get("file_info")
    print(f"  {label} 上传 ← HTTP {up.status_code} file_info={'有' if info else '无'}")
    if not info:
        print(f"     {str(data)[:400]}")
    return info


async def _message(
    client: httpx.AsyncClient, headers: dict, group: str, *,
    content: str, file_info: str, label: str,
) -> int:
    sent = await client.post(
        f"{API_BASE}/v2/groups/{group}/messages",
        headers=headers,
        json={
            "msg_type": MSG_TYPE_RICH,
            "content": content,
            "media": {"file_info": file_info},
        },
    )
    try:
        data = sent.json() or {}
    except ValueError:
        data = {}
    code = str(data.get("code") or "")
    if sent.status_code < 300 and not code:
        print(f"  {label} ✅ 发出去了（HTTP {sent.status_code}）")
        return 0
    # botpy 只在 HTTP 非 2xx 时抛；业务错误码是 200 带 code 回来的，不判会被当成成功。
    print(f"  {label} ❌ 发送失败 HTTP {sent.status_code} code={code or '-'}")
    print(f"     {str(data)[:400]}")
    return 1


async def main() -> int:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0

    group = _arg("--group")
    do_send = "--send" in args
    no_voice = "--no-voice" in args
    idol = _arg("--idol", "mano") or "mano"
    line_no = int(_arg("--line", "0") or 0)
    cfg = load_config()

    if do_send and not group:
        print("❌ --send 必须配 --group <group_openid>。**这里故意没有默认值** ——\n"
              "   群 openid 不是群号，拿到手也看不出是不是测试群。\n"
              "   只发老测试群（…4C056）或私聊你自己。绝不发正式群。")
        return 2
    if group and group[-5:].upper() == FORBIDDEN_SUFFIX:
        print(f"🚫 拒绝：{group[-8:]} 是**正式群**。这个脚本只发测试群。")
        return 2

    # ---- 头像 ----
    avatar_dir = Path(_arg("--avatar-dir") or (Path(cfg.morning_dir) / "avatar"))
    image = Path(_arg("--image") or (avatar_dir / f"{idol}.png"))
    if not image.is_file():
        print(f"❌ 找不到头像 {image}。先跑 scripts/crop_morning_avatar.py 裁一批，"
              f"或用 --image 指定一张。")
        return 1

    # ---- 台词 ----
    table = load_lines(cfg)
    entry = table.get(idol, {})
    name = entry.get("name", "") or idol
    lines = entry.get("lines", [])
    ja = lines[line_no] if line_no < len(lines) else ""
    voice = Path(cfg.morning_dir) / "audio" / f"{idol}_{line_no}.silk"

    content = _arg("--text") or "\n".join(x for x in (name, ja) if x)

    raw = image.read_bytes()
    payload = base64.b64encode(raw).decode()
    print(f"头像    : {image}  （{len(raw):,} 字节 → base64 {len(payload):,} 字节）")
    print(f"尺寸    : ", end="")
    try:
        import subprocess  # noqa: PLC0415 —— 只为打印，不值得引 Pillow
        out = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(image)],
            capture_output=True, text=True, timeout=10,
        ).stdout
        print(" / ".join(x.split(":")[-1].strip() for x in out.strip().splitlines()[1:]))
    except Exception:  # noqa: BLE001
        print("（读不到）")
    print(f"偶像    : {idol}  {name}")
    print(f"台词    : {ja or '（没读到，检查 lines.toml）'}")
    print(f"语音    : {voice.name}  "
          f"{'✅ ' + format(voice.stat().st_size, ',') + ' 字节' if voice.is_file() else '❌ 不存在'}"
          f"{'（--no-voice，不发）' if no_voice else ''}")
    print(f"\n===== content（{len(content)} 字符）=====")
    print(content)
    print("=" * 40)

    if not do_send:
        print("\n（默认只打印。要真发：--group <openid> --send）")
        return 0

    print(f"\n⚠️  即将向群 …{group[-8:]} 发 2 条：① 头像+文字  ② 语音")
    print("   确认这是测试群（575861535）再继续。")
    async with httpx.AsyncClient(timeout=120) as client:
        headers = _headers(await _token(client, cfg), cfg.appid)

        rc = 0
        info = await _upload(
            client, headers, group,
            file_type=FILE_TYPE_IMAGE, file_data=payload, label="① 头像",
        )
        if not info:
            return 1
        rc |= await _message(
            client, headers, group, content=content, file_info=info, label="① 头像",
        )

        if not no_voice:
            if not voice.is_file():
                print(f"  ⏭  ② 语音：{voice} 不存在，跳过")
                return rc
            vraw = voice.read_bytes()
            if vraw[:10] != morning.SILK_MAGIC:
                # 上传接口对语音格式**完全不校验**，只有这里能挡。
                print(f"  ⏭  ② 语音：{voice.name} 不是 silk（头 {vraw[:10]!r}），跳过")
                return rc
            await asyncio.sleep(2)  # 两条挨太近客户端可能叠在一起
            vinfo = await _upload(
                client, headers, group, file_type=FILE_TYPE_VOICE,
                file_data=base64.b64encode(vraw).decode(), label="② 语音",
            )
            if vinfo:
                rc |= await _message(
                    client, headers, group, content="", file_info=vinfo, label="② 语音",
                )

    print("\n发完了。去客户端确认：")
    print("  1. 头像渲染出来**多大**？（太小就没意义，不如继续用语音）")
    print("  2. content 那段文字渲染了吗？在图上面还是下面？")
    print("  3. 两条的先后顺序对不对？头像和语音是同一位偶像吗？")
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
