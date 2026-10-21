"""建「角色 → 现任声优」的库 → `src/qq_rss_bot/seiyuu.toml`（**进 git**）。

    uv run python scripts/fetch_seiyuu.py              # 抓 + 写盘
    uv run python scripts/fetch_seiyuu.py --dry-run    # 只看统计和缺哪些，不写
    uv run python scripts/fetch_seiyuu.py --proxy http://192.168.0.63:6152

只在 Mac 上手跑，**没有定时任务**。产出是公开事实（谁给谁配音），所以进仓库；
运行时不联网、重跑也不会去抓。

## 两个页面，用 character_id 关联

| 页面 | 给什么 |
|---|---|
| `calendar/birthdays.html` | 角色名 + `data-character-id`（**生日表里有的才算**） |
| `misc/cv.html` | 同一个 `data-character-id` + 声优 + 任期 |

⚠️ **绝不能按名字关联。** 两边写法不一致（生日表 `エミリー・スチュアート` /
声优页 `エミリー`），按名字匹配只能覆盖一部分，而且失败是**静默的** ——
漏掉的会被当成「没有声优」。见 `docs/adr/0001-join-by-character-id.md`。

## 现任怎么定：读 span 的 `title`

声优页上同一个角色会在**每个配过它的人的行**里各出现一个 span，
但那些 span 的 `data-person-id` 和 `title` 永远写的是**现任**：

    <span class="idol-name" data-character-id="3" data-person-id="41"
          title="萩原雪歩(CV:浅倉杏美)">萩原雪歩</span>

所以 `data-person-id` **不是「这一行的人」**，是「这个角色的现任」。
拿它当行身份会张冠李戴（第一版就是这么错的）。人的身份一律取**行首 `<rb>`**，
现任一律取 `title` 里的 `(CV:…)`。

任期只有 14 个 span 带 `<small class="date">(起始〜结束)</small>`：

- `(2010-07-04〜)` → 从这天起，**没结束** → 现任
- `(〜2010-07-03)` → **已经结束** → 前任
- 没有 `<small>` 标签 → 没有换届记录，按现任（`title` 说的算）

## 输出

`[roles]` 的 key 是**生日表里的角色名**，不是 character_id —— 运行时手里只有名字，
而且这个名字和生日表**同源**，所以按名字查不会引入跨源的歧义。join 本身仍然是
按 id 做的。代价：生日表哪天改了名，key 会失效，所以 smoke 有一条钉子逐个核对。

`source = "manual"` 的条目**重跑不覆盖** —— 人工改过的以人为准。

## 不写进仓库的

声优页的原始数据不落盘、不进 git（只在内存里解析）。产出只有「角色 → 姓名」
这个结论，是公开事实。

## 两个**不由这个脚本生成**的 section

`[character_accounts]`（角色本人开的号，目前只有 vα-liv 三位）和
`[officials]`（企划/品牌官号）是**手写**的：源站的声优页里没有这些角色的条目，
也从不给 X 账号。脚本只在重跑时**原样带过去**，不会删也不会改。

⚠️ 别把这三类账号混在一起：`[persons]` 是配音的真人，`[character_accounts]`
是角色自己，`[officials]` 的账号属于企划。
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import tomllib
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_birthday_source as fb  # noqa: E402

CV_SOURCE = "https://imas-db.jp/misc/cv.html"
DEFAULT_OUT = ROOT / "src" / "qq_rss_bot" / "seiyuu.toml"
REVIEW_OUT = ROOT / "data" / "birthday" / "seiyuu-review.md"
CST = timezone(timedelta(hours=8))

# 手写、脚本不生成的 section。重跑时原样带过去 —— 不加这个，手工数据会被
# 下一次 render 直接抹掉（render 只输出它自己知道的那几张表）。
EXTRA_SECTIONS = ("character_accounts", "officials")

ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.S)
TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S)
SPAN_RE = re.compile(
    r'<span class="idol-name"([^>]*)>(.*?)</span>'
    r'(?:<small class="date">\((.*?)\)</small>)?',
    re.S,
)
CHARACTER_ID_RE = re.compile(r'data-character-id="(\d+)"')
TITLE_RE = re.compile(r'title="([^"]*)"')
CV_TITLE_RE = re.compile(r"^(.+?)\(CV:(.+)\)$")


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_cv(page: str) -> tuple[dict[int, str], dict[int, set[str]], set[int]]:
    """→ (角色 id → 现任声优名, 角色 id → 前任声优名集合, 出现过的角色 id)。"""
    current: dict[int, str] = {}
    former: dict[int, set[str]] = defaultdict(set)
    seen_ids: set[int] = set()
    conflicts: dict[int, set[str]] = defaultdict(set)

    for row in ROW_RE.findall(page):
        if not CHARACTER_ID_RE.search(row):
            continue
        tds = TD_RE.findall(row)
        # 行首那个 td 是这一行的人；⚠️ 不能用 span 上的 data-person-id（那是现任）
        rb = re.search(r"<rb>(.*?)</rb>", tds[0], re.S) if tds else None
        person = _text(rb.group(1)) if rb else ""
        for span in SPAN_RE.finditer(tds[1] if len(tds) > 1 else row):
            attrs, _name, tenure = span.groups()
            cm = CHARACTER_ID_RE.search(attrs)
            if not cm:
                continue
            cid = int(cm.group(1))
            seen_ids.add(cid)
            tm = TITLE_RE.search(attrs)
            cv = CV_TITLE_RE.match(html.unescape(tm.group(1))) if tm else None
            if cv:
                who = cv.group(2).strip()
                conflicts[cid].add(who)
                current.setdefault(cid, who)
            ended = (tenure or "").split("〜")[-1].strip()
            if ended and person and person != current.get(cid):
                former[cid].add(person)

    bad = {cid: names for cid, names in conflicts.items() if len(names) > 1}
    if bad:
        # 同一个角色出现两个不同的现任 → 源的锅，别猜。交给人工。
        print(f"⚠️ {len(bad)} 个角色的 title 自相矛盾，已跳过：")
        for cid, names in list(bad.items())[:10]:
            print(f"   cid={cid} {'/'.join(sorted(names))}")
        for cid in bad:
            current.pop(cid, None)
    return current, former, seen_ids


def _q(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)


def build(days: list[dict], current: dict[int, str],
          former: dict[int, set[str]]) -> dict:
    """生日表 ⨝ 声优页。→ roles / former_roles / 缺 CV 的名单。"""
    roles: dict[str, str] = {}
    former_roles: dict[str, list[str]] = {}
    missing: list[tuple[str, str, int]] = []
    for day in days:
        for idol in day["idols"]:
            name = idol["name"]
            cid = idol.get("character_id")
            if cid is None:
                continue          # 品牌 / 系列条目，不是角色
            who = current.get(cid)
            if who is None:
                missing.append((name, idol["brand"], cid))
                continue
            roles[name] = who
            olds = sorted(former.get(cid, set()) - {who})
            if olds:
                former_roles[name] = olds
    persons = sorted(set(roles.values()) | {p for v in former_roles.values() for p in v})
    return {"roles": roles, "former_roles": former_roles,
            "missing": missing, "persons": persons}


def _inline(d: dict) -> str:
    return "{ " + ", ".join(f"{_q(k)} = {_q(str(v))}" for k, v in d.items()) + " }"


def render(*, persons: dict[str, dict], roles: dict[str, dict],
           former_roles: dict[str, list[str]], counts: dict[str, int],
           extra: dict[str, dict[str, dict]] | None = None,
           fetched_at: datetime) -> str:
    out = [
        "# 声优库 —— **角色 → 现任声优**。公开事实，进 git。",
        "#",
        f"# 由 scripts/fetch_seiyuu.py 生成（{CV_SOURCE} ⨝ 生日表）。",
        "# 只在 Mac 上手跑，**没有定时任务**；运行时只读这个文件，不联网。",
        "#",
        "# ⚠️ 要手改就把 source 改成 \"manual\" —— 否则重跑会被源数据覆盖。",
        "# ⚠️ [roles] 的 key 是**生日表里的角色名**。关联本身按 character_id 做，",
        "#    见 docs/adr/0001-join-by-character-id.md。别在这层按名字做 join。",
        "",
        "[meta]",
        f"source_cv = {_q(CV_SOURCE)}",
        f"fetched_at = {_q(fetched_at.isoformat(timespec='seconds'))}",
    ]
    out += [f"{k} = {v}" for k, v in counts.items()]
    out += ["", "[persons]"]
    for name in sorted(persons):
        out.append(f"{_q(name)} = {_inline(persons[name])}")
    out += ["", "[roles]"]
    for name in sorted(roles):
        out.append(f"{_q(name)} = {_inline(roles[name])}")
    out += ["", "# 只为留档，运行时**不用**", "[former_roles]"]
    for name in sorted(former_roles):
        olds = ", ".join(_q(p) for p in former_roles[name])
        out.append(f"{_q(name)} = [{olds}]")
    extra = extra or {}
    out += [
        "",
        "# ⚠️ 手写，脚本不生成、重跑原样保留。",
        "# 角色**自己**开的号（vα-liv 这类虚拟主播）—— 和 [persons]（配音的真人）",
        "# 是两个概念。key 是角色名，不是 handle。",
        "[character_accounts]",
    ]
    for name in sorted(extra.get("character_accounts") or {}):
        out.append(f"{_q(name)} = {_inline(extra['character_accounts'][name])}")
    out += [
        "",
        "# ⚠️ 手写，脚本不生成、重跑原样保留。",
        "# 企划 / 品牌 / 游戏的官号。last_post_at 是**事实**，只用于人工维护，",
        "# 运行时**不读** —— 别把它当成「活跃/不活跃」的判断。",
        "[officials]",
    ]
    for handle in sorted(extra.get("officials") or {}):
        out.append(f"{_q(handle)} = {_inline(extra['officials'][handle])}")
    out.append("")
    return "\n".join(out)


def merge(existing: dict, fresh: dict) -> int:
    """把 source = "manual" 的条目盖回新数据上。返回保留了几条。"""
    kept = 0
    for section in ("persons", "roles"):
        for key, old in (existing.get(section) or {}).items():
            if isinstance(old, dict) and old.get("source") == "manual":
                fresh[section][key] = old
                kept += 1
    for key, olds in (existing.get("former_roles") or {}).items():
        if key not in fresh["former_roles"]:
            fresh["former_roles"][key] = olds
            kept += 1
    return kept


def _render_review(built: dict, fetched_at: datetime) -> str:
    by_brand = Counter(b for _, b, _ in built["missing"])
    out = [
        "# 声优库：没有 CV 的角色",
        "",
        f"生成于 {fetched_at.isoformat(timespec='seconds')}。"
        f"共 {len(built['missing'])} 位角色在声优页上没有条目。",
        "",
        "**这不是脚本的漏抓** —— 声优页里根本没有这个 `character_id`。",
        "多数是灰姑娘的未付声角色。如果某个角色确实有 CV，就是源站漏了，",
        f"可以手工补进库（把 `source` 改成 `\"manual\"`，重跑不会覆盖）。",
        "",
        "## 按作品分布",
        "",
    ]
    out += [f"- `{b}`：{n}" for b, n in by_brand.most_common()]
    out += ["", "## 明细", ""]
    out += [f"- {name}（{brand}，character_id={cid}）"
            for name, brand, cid in sorted(built["missing"])]
    out.append("")
    return "\n".join(out)


def fetch(url: str, proxy: str) -> bytes:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    )
    req = urllib.request.Request(url, headers={"User-Agent": "qq-rss-bot/1.0"})
    with opener.open(req, timeout=30) as resp:
        return resp.read()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写盘")
    ap.add_argument("--proxy", default=None,
                    help="出站代理。不给他就依次看 .env 里的 MEDIA_PROXY，再退回直连")
    args = ap.parse_args()

    proxy = args.proxy or fb._env_proxy()
    print(f"抓生日页 {fb.SOURCE}" + (f"（经 {proxy}）" if proxy else "（直连）"))
    try:
        bd_page = fetch(fb.SOURCE, proxy).decode("utf-8", errors="replace")
        cv_page = fetch(CV_SOURCE, proxy).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 拉不到：{type(exc).__name__}: {exc}")
        print("   imas-db.jp 直连多半不通 —— 加 --proxy 再试。")
        return 1

    _brands, days = fb.parse(bd_page)
    if not days:
        print("❌ 生日页一条都没解析出来 —— 结构变了？别写盘。")
        return 1
    current, former, seen_ids = parse_cv(cv_page)
    if not current:
        print("❌ 声优页一条现任都没解析出来 —— 结构变了？别写盘。")
        return 1

    built = build(days, current, former)
    n_idol = sum(len(d["idols"]) for d in days)
    n_ids = sum(1 for d in days for i in d["idols"] if i.get("character_id"))
    print(f"  生日表 {len(days)} 天 / {n_idol} 条目（其中 {n_ids} 个角色带 id）")
    print(f"  声优页 {len(seen_ids)} 个角色 / {len(current)} 位现任")
    print(f"  → 关联上 {len(built['roles'])} 个角色，"
          f"没有 CV {len(built['missing'])} 个，"
          f"人物 {len(built['persons'])} 位，"
          f"有前任 {len(built['former_roles'])} 个")
    if built["former_roles"]:
        for name, olds in sorted(built["former_roles"].items()):
            print(f"     {name}：现任 {built['roles'][name]} ← 前任 {'、'.join(olds)}")

    if args.dry_run:
        print("\n--dry-run：没写盘。")
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fresh = {"persons": {n: {"handle": "", "source": "auto"} for n in built["persons"]},
             "roles": {n: {"current": p, "source": "auto"}
                       for n, p in built["roles"].items()},
             "former_roles": dict(built["former_roles"])}
    existing = tomllib.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    # ⚠️ 这两张表是手写的，脚本不生成 —— 不原样带过去的话，重跑就没了。
    extra = {s: dict(existing.get(s) or {}) for s in EXTRA_SECTIONS}
    if existing:
        kept = merge(existing, fresh)
        if kept:
            print(f"  保留 {kept} 条 source=manual 的条目")
    n_extra = sum(len(v) for v in extra.values())
    if n_extra:
        print(f"  原样保留手写的 "
              + " / ".join(f"[{s}] {len(extra[s])} 条" for s in EXTRA_SECTIONS if extra[s]))
    text = render(
        persons=fresh["persons"],
        roles=fresh["roles"],
        former_roles=fresh["former_roles"],
        counts={
            "idols": n_idol,
            "characters": n_ids,
            "with_cv": len(fresh["roles"]),
            "without_cv": n_ids - len(fresh["roles"]),
            "persons": len(fresh["persons"]),
            "former_cvs": sum(len(v) for v in fresh["former_roles"].values()),
            "character_accounts": len(extra["character_accounts"]),
            "officials": len(extra["officials"]),
        },
        extra=extra,
        fetched_at=datetime.now(CST),
    )
    tmp = out.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out)
    print(f"\n写出 {out}（{out.stat().st_size:,} 字节）")

    review = REVIEW_OUT
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text(_render_review(built, datetime.now(CST)), encoding="utf-8")
    print(f"写出 {review}（{len(built['missing'])} 位没有 CV 的角色，不进 git）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
