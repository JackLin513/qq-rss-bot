"""扒「アイマス誕生日カレンダー」的**全部**生日数据 → `data/birthday/birthdays.toml`。

    uv run python scripts/fetch_birthday_source.py            # 扒下来写盘
    uv run python scripts/fetch_birthday_source.py --dry-run  # 只打印统计，不写
    uv run python scripts/fetch_birthday_source.py --proxy http://192.168.0.63:6152

⚠️ **imas-db.jp 在国内直连不通**（容器里实测超时），多半要挂代理。
走 `--proxy`，或把 `MEDIA_PROXY` 写进 `.env`（会自动读到）。

## 为什么是冷数据

生日是**固定不变的**（角色设定），一年 366 天、382 位偶像 + 313 位声优，
整个文件几百 KB。**扒一次存着，运行时只读本地文件** —— 少一个「imas-db 挂了
今天就没推送」的依赖。`--refresh` 想重扒就重扒。

## 数据源

`https://imas-db.jp/calendar/birthdays.html` —— 个人运营的非官方粉丝站
（@imas_DB / @maruamyu，版权归 BNEI）。**别提交进 git**：落在 `data/` 下，
`data/` 整个已在 `.gitignore` 里。

⚠️ 首页 `https://birthdays.imas-db.jp/` 是 **JS 应用**，静态请求只会拿到
「ページを準備しています…」。真正的数据在这个 **列表页的静态 HTML** 里，
一次请求全给你，不用管那个日历页。

## 结构

每天一行 `<tr>`，里面 `<li>` 分两类，**靠 `data-character-id` /
`data-person-id` 区分**：

    <span class="idol-name" data-brand-id="4" data-character-id="63">道明寺歌鈴</span>
    <span class="badge ... bg-imas-brand-cinderella">シンデレラ</span>

    <span class="idol-name" data-brand-id="8" data-person-id="228">永井真里子 (西城樹里役)</span>
    <span class="badge ... bg-imas-brand-shinycolors">シャニ</span>

声优的名字单元格里带 `(某某役)`，扒的时候把 `役` 剥掉存成 `role`。
（`土屋翔 (舞台SideM 伊瀬谷四季役)` 这种，role 会是 `舞台SideM 伊瀬谷四季` ——
**原样保留**，不在这一层做归一化。）

⚠️ **不做任何过滤** —— 961プロ（反派）、Xenoglossia（机甲 AU）、その他
全都要。用户明确要求「能从 imasdb 抓下来的都要显示」。

⚠️ **有 02-29**（366 天不是 365）。但**运行时不需要判闰年** ——
`datetime.date` 本身就构造不出平年的 02-29，所以 `"02-29"` 这个键只在闰年
会被查到。别在 `Calendar.for_date` 里加 `isleap` 分支，那是走不到的死代码。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "https://imas-db.jp/calendar/birthdays.html"
DEFAULT_OUT = ROOT / "data" / "birthday" / "birthdays.toml"
CST = timezone(timedelta(hours=8))

ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.S)
DATE_RE = re.compile(r">(\d{1,2})/(\d{1,2})<")
LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S)
BRAND_RE = re.compile(r'bg-imas-brand-([a-z0-9]+)"\s*>([^<]*)<')
PERSON_RE = re.compile(r'data-person-id="\d+"')
NAME_RE = re.compile(r'class="idol-name"[^>]*>(.*?)</span>', re.S)
ROLE_RE = re.compile(r"^(.*?)\s*\((.*?)役\)\s*$")


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse(page: str) -> tuple[dict[str, str], list[dict]]:
    """→ (品牌表, 每天一条的列表)。"""
    brands: dict[str, str] = {}
    for key, label in BRAND_RE.findall(page):
        brands.setdefault(key, html.unescape(label).strip())

    days: list[dict] = []
    for row in ROW_RE.findall(page):
        m = DATE_RE.search(row)
        if not m:
            continue
        month, day = int(m.group(1)), int(m.group(2))
        idols, people = [], []
        for li in LI_RE.findall(row):
            name_html = NAME_RE.search(li)
            if not name_html:
                continue
            name = _text(name_html.group(1))
            if not name:
                continue
            bm = BRAND_RE.search(li)
            brand = bm.group(1) if bm else ""
            if PERSON_RE.search(li):
                rm = ROLE_RE.match(name)
                people.append(
                    {"name": rm.group(1) if rm else name,
                     "role": rm.group(2) if rm else "",
                     "brand": brand}
                )
            else:
                idols.append({"name": name, "brand": brand})
        days.append({"date": f"{month:02d}-{day:02d}",
                     "idols": idols, "people": people})
    days.sort(key=lambda d: d["date"])
    return brands, days


def _q(s: str) -> str:
    """TOML 基本字符串。JSON 的转义规则在这里够用（都用 \\" 和 \\\\）。"""
    return json.dumps(s, ensure_ascii=False)


def render(brands: dict[str, str], days: list[dict], digest: str, now: datetime) -> str:
    n_idol = sum(len(d["idols"]) for d in days)
    n_people = sum(len(d["people"]) for d in days)
    out = [
        "# アイマス誕生日カレンダー —— **冷数据**，运行时只读。",
        "#",
        f"# 来源 {SOURCE}",
        "# 由 scripts/fetch_birthday_source.py 生成，**别手改**（重扒会覆盖）。",
        "#",
        "# 个人运营的非官方粉丝站，版权归 BNEI。**别提交进 git** ——",
        "# 落在 data/ 下，data/ 整个已在 .gitignore 里。",
        "#",
        "# ⚠️ **不做任何过滤**：961プロ（反派）、Xenoglossia（机甲 AU）、",
        "#    その他 全都在里面。要过滤的话在**运行时**做，别在这一层删数据。",
        "# ⚠️ **有 02-29**。但运行时**不用判闰年** —— datetime.date 构造不出",
        "#    平年的 02-29，所以这个键只在闰年会被查到。",
        "",
        "[meta]",
        f"source = {_q(SOURCE)}",
        f"fetched_at = {_q(now.isoformat(timespec='seconds'))}",
        f"html_sha256 = {_q(digest)}",
        f"days = {len(days)}",
        f"idols = {n_idol}",
        f"people = {n_people}",
        "",
        "[brands]",
    ]
    out += [f"{k} = {_q(v)}" for k, v in sorted(brands.items())]
    out.append("")
    for d in days:
        out.append("[[days]]")
        out.append(f"date = {_q(d['date'])}")
        if d["idols"]:
            out.append("idols = [")
            out += [
                f"  {{ name = {_q(i['name'])}, brand = {_q(i['brand'])} }},"
                for i in d["idols"]
            ]
            out.append("]")
        if d["people"]:
            out.append("people = [")
            out += [
                f"  {{ name = {_q(p['name'])}, role = {_q(p['role'])}, "
                f"brand = {_q(p['brand'])} }},"
                for p in d["people"]
            ]
            out.append("]")
        out.append("")
    return "\n".join(out)


def _env_proxy() -> str:
    """`--proxy` 没给时的兜底：`.env` 里的 `MEDIA_PROXY`（不回显它的值）。"""
    env = ROOT / ".env"
    if not env.is_file():
        return ""
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("MEDIA_PROXY="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--refresh", action="store_true",
                    help="强制重新下载（默认也是每次重下，这个参数只为显式）")
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写盘")
    ap.add_argument("--proxy", default=None,
                    help="出站代理，如 http://192.168.0.63:6152。"
                         "不给他就依次看 .env 里的 MEDIA_PROXY，再退回直连")
    args = ap.parse_args()

    proxy = args.proxy or _env_proxy()
    print(f"抓 {SOURCE}" + (f"（经 {proxy}）" if proxy else "（直连）"))
    # ⚠️ 没有代理时**直连必然超时**，而且 `urllib` 的报错是一句很难读的
    # `<urlopen error timed out>`，所以下面兜一下并给出下一步。
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    )
    req = urllib.request.Request(SOURCE, headers={"User-Agent": "qq-rss-bot/1.0"})
    try:
        with opener.open(req, timeout=30) as resp:
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 拉不到：{type(exc).__name__}: {exc}")
        print("   imas-db.jp 直连多半不通 —— 加 --proxy http://192.168.0.63:6152 再试。")
        return 1
    page = raw.decode("utf-8", errors="replace")
    digest = hashlib.sha256(raw).hexdigest()
    print(f"  {len(raw):,} 字节  sha256 {digest[:16]}…")

    brands, days = parse(page)
    if not days:
        print("❌ 一条都没解析出来 —— 页面结构变了？别写盘。")
        return 1

    n_idol = sum(len(d["idols"]) for d in days)
    n_people = sum(len(d["people"]) for d in days)
    empty = sum(1 for d in days if not d["idols"] and not d["people"])
    print(f"  解析出 {len(days)} 天 / {n_idol} 位偶像 / {n_people} 位声优"
          f"（{empty} 天没人过生日）")
    print(f"  品牌 {len(brands)} 个：" + "、".join(sorted(brands)))

    # 抽两天验一眼，别等运行到那天才发现解析错了
    for want in ("10-19", "12-25"):
        d = next((x for x in days if x["date"] == want), None)
        if d:
            names = [i["name"] for i in d["idols"]] + [
                f"{p['name']}（{p['role']}役）" for p in d["people"]
            ]
            print(f"  {want}: " + "、".join(names))
    if not any(x["date"] == "02-29" for x in days):
        print("  ⚠️ 没找到 02-29 —— 源站结构可能变了")

    if args.dry_run:
        print("\n--dry-run：没写盘。")
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".toml.tmp")
    tmp.write_text(render(brands, days, digest, datetime.now(CST)), encoding="utf-8")
    tmp.replace(out)
    print(f"\n写出 {out}（{out.stat().st_size:,} 字节）")
    print("⚠️ 这个文件在 data/ 下，**不会进 git**，也别挪出去。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
