"""把 `audio/` 里的语音**全部**发一遍到指定群 —— 用来一次性听完全部素材。

    uv run python scripts/probe_morning_all.py                        # 只列清单，一条都不发
    uv run python scripts/probe_morning_all.py --group <openid> --send
    uv run python scripts/probe_morning_all.py --group <openid> --send --idol mano
    uv run python scripts/probe_morning_all.py --group <openid> --send --start 28 --limit 28
    uv run python scripts/probe_morning_all.py --group <openid> --send --per-minute 10 --yes

**和 `probe_morning.py` 的分工**：那个回答「线上这一拍会挑谁」，
是**每次只发一条**的诊断。这个是**批量试听**，按 `<偶像 id>_<序号>` 排序逐条发，
带一句日语原文方便对照 —— 上线前把 196 条全听一遍用的。

## 为什么必须自己限速

QQ 官方频控是**群维度 20 条/分钟**。这条路的请求直接 POST HTTP API，
**不经过 `quota.py` 的令牌桶**（探针本来就不该动线上的配额状态），
所以限速得自己来。默认 `--per-minute 15`，留了余量。

## ⚠️ 三条硬性约束

1. **`--group` 故意没有默认值。** 群 openid 不是群号，拿到手看不出是不是测试群
   （CLAUDE.md 里那条：「先查一下是不是测试群」在库里做不到，只能靠事先确认）。
2. **正式群（尾号 …E0629）写死拒发** —— 这个脚本一次要发一百多条，
   填错了是灾难，所以在这里硬挡一道。
3. **不写库。** 所以这轮发送**不会**让线上那一拍认为「今天发过了」，
   真实的 07:30 定时完全不受影响。

## 发完要**用耳朵**确认的

1. 每一条都出现**语音气泡**、点开**能播**、是**女声日语**、音质正常
2. **开头没有「啪」**（那个是 pysilk 编码器第 0 帧的锅，见 `make_morning.py`）
3. **听起来像那位角色** —— 音色只能靠耳朵，看日志看不出来
4. **语气提示有没有生效**（起伏、软硬）——「语音习惯」全靠它
"""

from __future__ import annotations

import asyncio
import base64
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

# 正式群尾号。**写死拒发** —— 这个脚本一次发一百多条，填错就是事故。
# 只存尾号不存完整 openid：仓库是公开的（CLAUDE.md 的硬性约束）。
OFFICIAL_GROUP_SUFFIX = "E0629"


def _arg(name: str, default: str | None = None) -> str | None:
    """`--name value` 或 `--name=value`。"""
    args = sys.argv[1:]
    if name in args:
        i = args.index(name)
        if i + 1 >= len(args):
            raise SystemExit(f"{name} 后面要跟一个值")
        return args[i + 1]
    for a in args:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def _flag(name: str) -> bool:
    return name in sys.argv[1:]


def load_ja(cfg) -> dict[str, str]:
    """从 `lines.toml` 读 `<文件名> → 日语原文`，纯粹为了打印好看。

    读不到就返回空 dict —— **这个脚本不依赖台词表**，素材目录才是唯一输入
    （运行时也是这个口径）。所以文件不在、或者条目对不上都不算错。
    """
    path = Path(cfg.morning_dir) / "lines.toml"
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    out: dict[str, str] = {}
    for idol in data.get("idols", []):
        for i, line in enumerate(idol.get("lines", [])):
            ja = line.get("ja")
            if ja:
                out[f"{idol.get('id')}_{i}.silk"] = ja
    return out


async def _token(client: httpx.AsyncClient, cfg) -> str:
    resp = await client.post(
        TOKEN_URL, json={"appId": cfg.appid, "clientSecret": cfg.secret}
    )
    token = resp.json().get("access_token") if resp.status_code == 200 else None
    if not token:
        raise SystemExit(f"取 token 失败: HTTP {resp.status_code} {resp.text[:200]}")
    return token


async def send_all(group: str, files: list[Path], cfg, per_minute: float,
                   ja: dict[str, str]) -> int:
    """逐条发。返回失败条数。

    请求形状和 `RssBot.send_voice` / `probe_morning._send_voice` 一致
    （`file_type=3` + `srv_send_msg=False` → `msg_type=7` + `content=""`），
    区别只是**token 取一次重复用**，不是每条都去换一个新的。
    """
    gap = 60.0 / per_minute
    failed, sent = 0, 0
    async with httpx.AsyncClient(timeout=120) as client:
        token = await _token(client, cfg)

        def headers() -> dict[str, str]:
            return {
                "Authorization": f"QQBot {token}",
                "X-Union-Appid": cfg.appid,
                "Content-Type": "application/json",
            }

        for n, path in enumerate(files, 1):
            raw = path.read_bytes()
            tag = f"[{n:3d}/{len(files)}] {path.stem:14s}"
            # 魔数闸：上传接口不校验格式，不自己挡就会发出一个点不开的气泡
            if raw[:10] != morning.SILK_MAGIC:
                failed += 1
                print(f"{tag} ⏭  不是 silk（头 {raw[:10]!r}），跳过")
                continue

            up = await client.post(
                f"{API_BASE}/v2/groups/{group}/files",
                headers=headers(),
                json={
                    "file_type": 3,  # 3=语音 silk
                    "file_data": base64.b64encode(raw).decode(),
                    "srv_send_msg": False,
                },
            )
            try:
                up_data = up.json() or {}
            except ValueError:
                up_data = {}
            file_info = up_data.get("file_info")
            if not file_info:
                failed += 1
                print(f"{tag} ❌ 上传失败 HTTP {up.status_code} {str(up_data)[:200]}")
                continue

            resp = await client.post(
                f"{API_BASE}/v2/groups/{group}/messages",
                headers=headers(),
                json={
                    "msg_type": MSG_TYPE_RICH,
                    "content": "",          # 只发语音气泡，不要字幕
                    "media": {"file_info": file_info},
                },
            )
            try:
                data = resp.json() or {}
            except ValueError:
                data = {}
            code = str(data.get("code") or "")
            if resp.status_code < 300 and not code:
                sent += 1
                print(f"{tag} ✅  {ja.get(path.name, '')}")
            else:
                failed += 1
                print(f"{tag} ❌ HTTP {resp.status_code} code={code or '-'} "
                      f"{str(data)[:200]}")

            if n < len(files):
                await asyncio.sleep(gap)

    print(f"\n发出 {sent} 条，失败 {failed} 条（共 {len(files)} 条）")
    return failed


def main() -> int:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0

    cfg = load_config()
    group = _arg("--group")
    do_send = _flag("--send")
    per_minute = float(_arg("--per-minute", "15") or 15)
    only = _arg("--idol")
    limit = int(_arg("--limit", "0") or 0)
    start = int(_arg("--start", "0") or 0)

    directory = Path(_arg("--dir") or cfg.morning_dir) / "audio"
    if not directory.is_dir():
        print(f"❌ 素材目录不存在：{directory}")
        print("   先在 Mac 上跑 scripts/make_morning.py，再把 audio/ 同步上去。")
        return 1

    voices, skipped = morning.scan_voices(directory)
    if skipped:
        print(f"⚠️  跳过 {len(skipped)} 个文件：")
        for name, why in skipped:
            print(f"  · {name} —— {why}")

    # 按 <偶像 id>_<序号> 排序，序号按数值排（不是字符串，否则 _10 会排在 _2 前面）
    files: list[Path] = []
    for idol in sorted(voices):
        if only and idol != only:
            continue
        files.extend(sorted(
            voices[idol],
            key=lambda p: int(p.stem.rpartition("_")[2] or 0),
        ))

    if only and not files:
        print(f"❌ 没有 id 为 {only!r} 的素材。有的：{'、'.join(sorted(voices))}")
        return 1

    files = files[start:]
    if limit:
        files = files[:limit]

    total = sum(len(v) for v in voices.values())
    print(f"素材：{len(voices)} 位偶像 / {total} 条，本次要发 {len(files)} 条")
    if not files:
        print("❌ 过滤之后一条都不剩。")
        return 1

    # 一轮大约多久 —— 196 条按 15/分钟算要十几分钟，先说清楚
    print(f"节奏：{per_minute:g} 条/分钟（官方群维度上限 20）"
          f" → 预计 {len(files) / per_minute:.1f} 分钟")

    ja = load_ja(cfg)
    print(f"台词表：{'读到了，会带日语原文' if ja else '没读到（不影响发送，只是不打原文）'}")

    if not do_send:
        print("\n" + "-" * 72)
        for n, path in enumerate(files, 1):
            print(f"  [{n:3d}] {path.stem:14s} {path.stat().st_size:6,} 字节  "
                  f"{ja.get(path.name, '')}")
        print("-" * 72)
        print("\n（默认只列清单。要真发：--group <openid> --send）")
        return 0

    if not group:
        print("\n❌ --send 必须配 --group <openid>。**这里故意没有默认值** ——\n"
              "   群 openid 不是群号，拿到手也看不出是不是测试群。\n"
              "   只发老测试群（…4C056）或新测试群（…36A1）。绝不发正式群。")
        return 2

    # 正式群硬挡。尾号只有 5 位十六进制，但这是防手滑不是防攻击。
    if group.endswith(OFFICIAL_GROUP_SUFFIX):
        print(f"\n🚫 拒绝：这个 openid 尾号是 …{OFFICIAL_GROUP_SUFFIX}，"
              "是**正式群**。\n"
              "   这个脚本一次要发一百多条，正式群绝不用来试听。\n"
              "   换成老测试群（…4C056）或新测试群（…36A1）。")
        return 2

    print("\n" + "=" * 72)
    print(f"⚠️  即将向群 …{group[-8:]} 连发 {len(files)} 条语音，"
          f"**每条都没有文字**（content=\"\"）")
    print(f"    节奏 {per_minute:g} 条/分钟，预计 {len(files) / per_minute:.1f} 分钟")
    print("    这**不写库**，所以不影响真实的 07:30 定时。")
    print("=" * 72)
    if not _flag("--yes"):
        try:
            got = input(f"确认请输入群 openid 的**后 8 位**（{group[-8:]}）：").strip()
        except EOFError:
            print("（非交互环境，加 --yes 才能发）")
            return 2
        if got.upper() != group[-8:].upper():
            print("对不上，不发。")
            return 2

    return 1 if asyncio.run(send_all(group, files, cfg, per_minute, ja)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
