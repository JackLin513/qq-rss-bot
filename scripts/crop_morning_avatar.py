"""把官方那张 28 人立绘大图裁成一张一张的头像，落到 `data/morning/avatar/`。

早安那条现在只有语音，群里没人点开听。改成「一张头像 + 一句日语原文 + 语音」
之后，头像得**跟着当天抽到的那位偶像走** —— 所以文件名必须是 `<偶像 id>.png`，
运行时按 id 直接取文件，**不扫目录**（少一个「索引说有、文件没同步过去」的脏状态，
和 `morning.py` 对 `audio/` 的口径一致）。

    uv run python scripts/crop_morning_avatar.py              # 裁，落到 data/morning/avatar/
    uv run python scripts/crop_morning_avatar.py --check      # 只检查现有的齐不齐，一个字节都不写
    uv run python scripts/crop_morning_avatar.py --fetch      # 源图不在就现下
    uv run python scripts/crop_morning_avatar.py --src <png>  # 指定源图

## 源图

`https://storage.moegirl.org.cn/moegirl/commons/7/7e/IMAS_SC_Sprite_ver3.png`
—— 1800×540，**10 列 × 3 行**，每格 180×180（第 3 行只有 8 个人，右边两格是空白）。

存到 `data/morning/avatar/_sprite.png`。**别提交** —— `data/` 整个是 gitignored，
角色版权属于原作方，和 `source/` 同一个姿态。

## ⚠️ 格子顺序是**肉眼核对过**的，不是推出来的

`SHEET_ORDER` 这 28 个 id 的顺序，是拿一张**带编号的网格图**逐个对出来的。
**别照抄游戏里的偶像 ID 顺序当默认值** —— 这两件事碰巧一致，但一致是巧合，
不是保证。顺序错了的症状是「脸和声音不是同一个人」，而且**日志上完全看不出来**。

要重新核对：裁完之后 `open` 一下 `data/morning/avatar/`，文件名就是 id，
一眼能看出哪张对不上。

## 为什么用 ffmpeg 而不是 Pillow

项目**没有 Pillow**，也不想为一个离线一次性任务引进来（`make_morning.py`
也是这个口径：重活留在 Mac 上离线做，运行时零新依赖）。ffmpeg 切图一行就够。

## 空图自检

裁出来的每张都过一道**体积下限**：180×180 的纯白 PNG 压完只有一两 KB，
真人头像是三十几 KB。低于下限说明格子对歪了（源图布局变了、或者行尾空格
数错了），**直接报错不写文件** —— 静默裁出一张白图比不裁更糟。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qq_rss_bot.config import load_config  # noqa: E402

SPRITE_URL = (
    "https://storage.moegirl.org.cn/moegirl/commons/7/7e/IMAS_SC_Sprite_ver3.png"
)

# 源图的格子尺寸。改这里之前先确认源图没换。
CELL = 180
COLS = 10

# 低于这个字节数就当是空白格。纯白 180×180 PNG ≈ 1~2 KB，真头像 ≈ 30~40 KB，
# 中间留了很宽的余量 —— 这里只拦「整格是白的」，不追求精确。
MIN_BYTES = 6_000

# 格子顺序（第 1 格 → 第 28 格）。**肉眼核对过的**，见模块说明。
SHEET_ORDER: tuple[tuple[str, str], ...] = (
    ("mano", "櫻木真乃"),
    ("hiori", "風野灯織"),
    ("meguru", "八宮めぐる"),
    ("kogane", "月岡恋鐘"),
    ("mamimi", "田中摩美々"),
    ("sakuya", "白瀬咲耶"),
    ("yuika", "三峰結華"),
    ("kiriko", "幽谷霧子"),
    ("kaho", "小宮果穂"),
    ("chiyoko", "園田智代子"),
    ("juri", "西城樹里"),
    ("rinze", "杜野凛世"),
    ("natsuha", "有栖川夏葉"),
    ("amana", "大崎甘奈"),
    ("tenka", "大崎甜花"),
    ("chiyuki", "桑山千雪"),
    ("asahi", "芹沢あさひ"),
    ("fuyuko", "黛冬優子"),
    ("mei", "和泉愛依"),
    ("toru", "浅倉透"),
    ("madoka", "樋口円香"),
    ("koito", "福丸小糸"),
    ("hinana", "市川雛菜"),
    ("nichika", "七草にちか"),
    ("mikoto", "緋田美琴"),
    ("luca", "斑鳩ルカ"),
    ("hana", "鈴木羽那"),
    ("haruki", "郁田はるき"),
)


def _arg(name: str, default: str | None = None) -> str | None:
    """`--name value` 或 `--name=value`。"""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(f"{name}="):
            return a.split("=", 1)[1]
    return default


def _flag(name: str) -> bool:
    return name in sys.argv[1:]


def fetch_sprite(dest: Path) -> bool:
    """把源图下到 `dest`。返回是否成功。"""
    import httpx  # noqa: PLC0415 —— 只有 --fetch 这条路才用得上

    print(f"下载源图：{SPRITE_URL}")
    try:
        resp = httpx.get(SPRITE_URL, timeout=60.0, follow_redirects=True)
    except httpx.HTTPError as exc:
        print(f"❌ 下载失败：{exc}")
        return False
    if resp.status_code != 200 or not resp.content:
        print(f"❌ 下载失败：HTTP {resp.status_code}，{len(resp.content)} 字节")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    print(f"   → {dest}（{len(resp.content):,} 字节）")
    return True


def probe_size(path: Path) -> tuple[int, int]:
    """读源图尺寸。用 `sips` —— 只在 Mac 上离线跑，不值得为这个引 Pillow。"""
    try:
        out = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            capture_output=True, text=True, timeout=20, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"❌ 读不出源图尺寸（{path}）：{exc}") from exc
    nums = [int(x.split(":")[-1]) for x in out.strip().splitlines()[1:]]
    if len(nums) != 2:
        raise SystemExit(f"❌ 读不出源图尺寸（{path}）：{out!r}")
    return nums[0], nums[1]


def crop_one(src: Path, dest: Path, col: int, row: int) -> int:
    """裁一格，返回输出字节数。ffmpeg 出错就抛。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-vf", f"crop={CELL}:{CELL}:{col * CELL}:{row * CELL}",
            str(dest),
        ],
        check=True, timeout=60,
    )
    return dest.stat().st_size


def main() -> int:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0

    cfg = load_config()
    out_dir = Path(_arg("--out-dir") or (Path(cfg.morning_dir) / "avatar"))
    src = Path(_arg("--src") or (out_dir / "_sprite.png"))
    check_only = _flag("--check")

    # ---- 只检查：确认现有的一批齐不齐 ----
    if check_only:
        missing, thin = [], []
        for idol, name in SHEET_ORDER:
            path = out_dir / f"{idol}.png"
            if not path.is_file():
                missing.append(f"{idol}（{name}）")
            elif path.stat().st_size < MIN_BYTES:
                thin.append(f"{idol}（{path.stat().st_size:,} 字节）")
        print(f"头像目录：{out_dir}")
        if not missing and not thin:
            print(f"✅ {len(SHEET_ORDER)} 位偶像的头像都在，体积也都正常。")
            return 0
        if missing:
            print(f"❌ 缺 {len(missing)} 张：{'、'.join(missing)}")
        if thin:
            print(f"❌ {len(thin)} 张体积偏小（可能是空白格）：{'、'.join(thin)}")
        print("   跑一遍 scripts/crop_morning_avatar.py 重新裁。")
        return 1

    # ---- 源图 ----
    if not src.is_file():
        if _flag("--fetch"):
            if not fetch_sprite(src):
                return 1
        else:
            print(f"❌ 找不到源图 {src}")
            print(f"   加 --fetch 现下（{SPRITE_URL}），")
            print(f"   或者把已有的图放到那里，或用 --src <png> 指定。")
            return 1

    if shutil.which("ffmpeg") is None:
        print("❌ 没有 ffmpeg。`brew install ffmpeg` —— 这个脚本只在 Mac 上离线跑。")
        return 1

    width, height = probe_size(src)
    need_w, need_h = COLS * CELL, ((len(SHEET_ORDER) + COLS - 1) // COLS) * CELL
    print(f"源图  : {src}（{width}×{height}）")
    if width < need_w or height < need_h:
        print(f"❌ 源图太小，至少要 {need_w}×{need_h}（{COLS} 列 × "
              f"{need_h // CELL} 行，每格 {CELL}×{CELL}）")
        return 1

    print(f"输出  : {out_dir}（{len(SHEET_ORDER)} 张 {CELL}×{CELL}）\n")
    blank: list[str] = []
    total = 0
    for n, (idol, name) in enumerate(SHEET_ORDER):
        col, row = n % COLS, n // COLS
        dest = out_dir / f"{idol}.png"
        size = crop_one(src, dest, col, row)
        total += size
        flag = ""
        if size < MIN_BYTES:
            # 空白格：格子对歪了。**不删文件**，留着好让人看一眼到底裁出了什么。
            blank.append(f"{idol}（{name}）")
            flag = "  ⚠️ 体积偏小，可能是空白格"
        print(f"  [{n + 1:2d}/{len(SHEET_ORDER)}] {idol:8s} {name:7s} "
              f"{size:7,} 字节  (列{col + 1} 行{row + 1}){flag}")

    print(f"\n共 {len(SHEET_ORDER)} 张，{total:,} 字节，平均 {total // len(SHEET_ORDER):,} 字节")
    if blank:
        print(f"\n❌ {len(blank)} 张可能是空白格：{'、'.join(blank)}")
        print("   源图布局变了，或者 SHEET_ORDER 的行尾数错了。")
        print("   先看一眼这几张图，再决定要不要改 COLS / CELL。")
        return 1

    print("\n下一步：把这批图同步到 NAS 的 data/morning/avatar/，")
    print("然后 uv run python scripts/probe_morning_card.py --idol <id> --group <openid> --send")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
