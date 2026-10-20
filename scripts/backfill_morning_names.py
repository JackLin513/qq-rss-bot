"""给已有的 `.manifest.json` 补上 `name` 字段 —— **不用重跑 TTS**。

早安那条要发「头像 + 角色名 + 日语原文」，角色名从 manifest 里取
（`morning.load_lines`）。`make_morning.py` 现在会写这个字段，但**已有记录的
跳过条件是 `key` 一致**，所以老记录永远补不上 —— 跑这个脚本一次性补。

    uv run python scripts/backfill_morning_names.py            # 只报告要补什么
    uv run python scripts/backfill_morning_names.py --write    # 真写

名字从 `lines.toml` 的 `[[idols]] name` 取（manifest 里只有 `idol` 这个 id，
没有显示名）。两边对不上、或者某条 silk 在台词表里找不到的，**逐条列出来**，
不静默跳过 —— 对不上说明 `lines.toml` 和 `audio/` 已经不是同一批了。

写盘走**原子替换**（临时文件 + `os.replace`），和 `make_morning.save_manifest`
同一个口径：半截的 manifest 会让下一次 `make_morning.py` 重做一批已经好的。
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qq_rss_bot.config import load_config  # noqa: E402


def load_names(morning_dir: Path) -> dict[str, str]:
    """`lines.toml` → `{偶像 id: 显示名}`。"""
    path = morning_dir / "lines.toml"
    if not path.is_file():
        raise SystemExit(f"❌ 找不到台词表 {path}")
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return {i.get("id", ""): i.get("name", "") for i in data.get("idols", [])}


def main() -> int:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0
    write = "--write" in args

    cfg = load_config()
    morning_dir = Path(cfg.morning_dir)
    manifest_path = morning_dir / ".manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"❌ 找不到 manifest {manifest_path}")

    names = load_names(morning_dir)
    manifest = json.loads(manifest_path.read_text())
    print(f"manifest：{manifest_path}（{len(manifest)} 条）")
    print(f"台词表：{len(names)} 位偶像\n")

    filled, already, unknown = 0, 0, []
    for fname, rec in sorted(manifest.items()):
        idol = rec.get("idol", "")
        name = names.get(idol, "")
        if not name:
            unknown.append((fname, idol))
            continue
        if rec.get("name") == name:
            already += 1
            continue
        filled += 1
        if write:
            rec["name"] = name

    if unknown:
        print(f"⚠️  {len(unknown)} 条在台词表里找不到对应偶像（名字留空）：")
        for fname, idol in unknown[:20]:
            print(f"     {fname}  idol={idol!r}")
        if len(unknown) > 20:
            print(f"     …… 还有 {len(unknown) - 20} 条")
        print()

    print(f"已有 name：{already} 条")
    print(f"要补：{filled} 条")

    if not filled:
        print("\n没有要补的。")
        return 0
    if not write:
        print("\n（默认只看。真写：--write）")
        return 0

    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    )
    os.replace(tmp, manifest_path)
    print(f"\n✅ 写好了：{manifest_path}")
    print("   记得把这个文件跟 audio/ 一起同步到 NAS —— 运行时要读它。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
