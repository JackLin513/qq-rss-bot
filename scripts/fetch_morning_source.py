"""把「小偶像早安语音」要用的资料抓到本地。

**抓的不是代码，是资料** —— 台词要靠它们才能写得像那位偶像：

| 抓什么 | 从哪来 | 干什么用 |
|---|---|---|
| `characters.toml` | eidolon-sc | 28 位名册 + `voice_id`（Fish Audio 的 `reference_id`） |
| `sheets/<id>.md` | eidolon-sc | 人设：**自称、对制作人的称呼、句尾口癖** |
| `facts/<id>.md` | eidolon-sc | 档案：性格、说话方式的补充 |
| `readings.toml` | eidolon-sc | 读音表（台词提到人名/组合名时念对） |

⚠️ **抓到的东西一律落在 `data/morning/source/`，绝不提交。**
eidolon-sc 是 AGPL-3.0，而且它自己 README 第 243 行就写明「剧情数据、角色音色、立绘素材
不在仓库中，使用时须遵守各自来源的条款」—— 这些资料是**别人的**，我们只当离线上下文用。
`data/` 整个已在 `.gitignore` 里，所以天然不会被提交；**别把它挪到 `data/` 外面**。

## 为什么逐文件抓，不下载 tarball

tarball 是 chunked 响应、**拿不到 `Content-Length`**，没法先估体积；而仓库里
`characters/avatars/`、`backgrounds/`、`frontend/` 那些图片和前端资源我们一个都不要。
要的只有 58 个小文件（1 名册 + 28 人设 + 28 档案 + 1 读音表），并发抓 8 路几秒钟就完了。

⚠️ **GitHub 直连是通的，不需要代理**（`raw.githubusercontent.com` 实测 200）。
需要代理的是 Fish Audio —— 见 `make_morning.py`。别把这两个搞混。

跑法：

    uv run python scripts/fetch_morning_source.py
    uv run python scripts/fetch_morning_source.py --refresh     # 重下（默认已存在就跳过）
    uv run python scripts/fetch_morning_source.py --only mano   # 只抓一位
"""

from __future__ import annotations

import asyncio
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

REPO = "Sallyn0225/eidolon-sc"
BRANCH = "main"
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"

DEFAULT_OUT = ROOT / "data" / "morning" / "source"

# 并发数。GitHub 对 raw 没给很严的限制，8 路足够快也不至于被掐。
CONCURRENCY = 8


def _arg(name: str, default: str | None = None) -> str | None:
    args = sys.argv[1:]
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


async def _get(client: httpx.AsyncClient, path: str) -> bytes:
    """抓一个文件，**失败就抛**（调用方决定怎么处理）。"""
    resp = await client.get(f"{RAW}/{path}")
    resp.raise_for_status()
    return resp.content


def _ids_from_toml(raw: bytes) -> list[dict]:
    """从 characters.toml 里读出 28 位（id / name / voice_id）。"""
    data = tomllib.loads(raw.decode("utf-8"))
    return list(data.get("characters") or [])


async def main() -> int:
    out = Path(_arg("--out", str(DEFAULT_OUT)) or DEFAULT_OUT)
    refresh = "--refresh" in sys.argv
    only = _arg("--only")

    if out.exists() and not refresh:
        print(f"目标已存在：{out}")
        print("（要重下加 --refresh）")

    # ---- 第一步：名册。必须先拿到它才知道有哪 28 位 ----
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        print(f"抓名册 ← {REPO}@{BRANCH}/characters/characters.toml")
        try:
            roster_raw = await _get(client, "characters/characters.toml")
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 名册抓不到：{type(exc).__name__}: {exc}")
            print("   直连 raw.githubusercontent.com 应该通；不通就先查网络，别急着上代理。")
            return 1

        idols = _ids_from_toml(roster_raw)
        if not idols:
            print("❌ 名册里没有 [[characters]] 条目 —— 上游结构变了？")
            return 1
        print(f"   名册里 {len(idols)} 位")

        missing_voice = [c.get("id") for c in idols if len(str(c.get("voice_id") or "")) != 32]
        if missing_voice:
            print(f"   ⚠️ 这些没有可用的 voice_id：{missing_voice}")

        # ---- 第二步：人设 / 档案 / 读音表，并发抓 ----
        jobs: list[tuple[str, Path]] = []
        for c in idols:
            cid = c.get("id")
            if not cid or (only and cid != only):
                continue
            jobs.append((f"characters/sheets/{cid}.md", out / "sheets" / f"{cid}.md"))
            jobs.append((f"characters/facts/{cid}.md", out / "facts" / f"{cid}.md"))
        jobs.append(("characters/readings.toml", out / "readings.toml"))

        print(f"\n并发抓 {len(jobs)} 个文件（{CONCURRENCY} 路）…")
        sem = asyncio.Semaphore(CONCURRENCY)
        failed: list[tuple[str, str]] = []
        done = 0

        async def one(path: str, dest: Path) -> None:
            nonlocal done
            if dest.exists() and not refresh:
                done += 1
                return
            async with sem:
                try:
                    blob = await _get(client, path)
                except Exception as exc:  # noqa: BLE001
                    failed.append((path, f"{type(exc).__name__}: {exc}"))
                    return
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)
            done += 1

        await asyncio.gather(*(one(p, d) for p, d in jobs))

    # ---- 第三步：把名册也落盘（离线起草时要读它拿 voice_id）----
    out.mkdir(parents=True, exist_ok=True)
    (out / "characters.toml").write_bytes(roster_raw)

    print(f"   写好 {done}/{len(jobs)} 个")
    if failed:
        print(f"\n❌ 有 {len(failed)} 个没抓到：")
        for path, err in failed:
            print(f"   {path}  ← {err}")
        print("   重跑一次即可（已下好的会跳过）。")
        return 1

    # ---- 汇总，让「抓全了没」一眼可见 ----
    sheets = sorted((out / "sheets").glob("*.md")) if (out / "sheets").is_dir() else []
    facts = sorted((out / "facts").glob("*.md")) if (out / "facts").is_dir() else []
    print(f"\n目录：{out}")
    print(f"  名册      {len(idols)} 位")
    print(f"  人设      {len(sheets)} 份（{sum(p.stat().st_size for p in sheets):,} 字节）")
    print(f"  档案      {len(facts)} 份（{sum(p.stat().st_size for p in facts):,} 字节）")
    rd = out / "readings.toml"
    print(f"  读音表    {'有' if rd.is_file() else '没有'}（{rd.stat().st_size if rd.is_file() else 0:,} 字节）")

    lack = [
        c["id"] for c in idols
        if not only or c["id"] == only
        if not (out / "sheets" / f"{c['id']}.md").is_file()
        or not (out / "facts" / f"{c['id']}.md").is_file()
    ]
    if lack:
        print(f"  ⚠️ 资料不全的：{lack}")
    else:
        print("  ✅ 每位的人设和档案都在")

    print("\n下一步：uv run python scripts/make_morning_lines.py --only mano")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
