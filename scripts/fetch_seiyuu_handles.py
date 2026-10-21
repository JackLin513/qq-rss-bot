"""给声优库里的每个人查 X 账号 → 写回 `src/qq_rss_bot/seiyuu.toml`。

    uv run python scripts/fetch_seiyuu_handles.py            # 查 + 写盘
    uv run python scripts/fetch_seiyuu_handles.py --dry-run  # 只看结果，不写
    uv run python scripts/fetch_seiyuu_handles.py --only 浅倉杏美 --only 郁原ゆう
    uv run python scripts/fetch_seiyuu_handles.py --proxy http://192.168.0.63:6152

只在 Mac 上手跑，**没有定时任务**。

## 三级回退

1. **Wikidata 的 `P2002`**（X 用户名）—— 最可靠
2. **日文维基条目的外部链接** —— 兜住那些没填 P2002 的
3. **跟随重定向后的条目名** —— 改名过的声优（`長谷優里奈` → `友利花`）靠这一级

每命中一条就停，并把**是哪一级查到的**记进 `source`，方便日后复查。

## ⚠️ 宁可留空，也不猜

自动填一个**错**的账号，比留空坏得多 —— 贺图会把别人的图当成这位声优发的，
而且从群里看不出来。所以：

- Wikidata 只认**名字完全一致**（label 或 alias 去空格后相等）且 `P31 = Q5`（人）的条目
- 维基只认**恰好一个** X 外链；两个及以上 → 进复核清单，不猜
- 查到之后一律做 **x.com 存活校验**（200 才算数），死号不算
- 拿不准的一律进 `data/birthday/seiyuu-handles-review.md`，按
  「没查到 / 多个候选 / 疑似但账号不存在」分组

## 重跑不会把查到的洗掉

`source = "wikidata"` / `"wikipedia"` 和 `"manual"` 一样，`fetch_seiyuu.py`
重跑时整条保留（见那边的 `merge`）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_seiyuu as fs  # noqa: E402

LIB = ROOT / "src" / "qq_rss_bot" / "seiyuu.toml"
REVIEW = ROOT / "data" / "birthday" / "seiyuu-handles-review.md"

WD_API = "https://www.wikidata.org/w/api.php"
WP_API = "https://ja.wikipedia.org/w/api.php"
UA = "qq-rss-bot/1.0 (seiyuu handle lookup; +https://github.com/JackLin513/qq-rss-bot)"
X_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
TW_LINK_RE = re.compile(r"^https?://(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]+)")
# 这些不是「某个人」的账号，出现在外链里也不能用
BAD_HANDLES = {
    "intent", "share", "home", "hashtag", "search", "i", "twitter", "x",
    "prtimes_jp", "prtimes", "status", "settings", "login", "signup",
}


def _opener(proxy: str) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    )


def _get_json(opener, base: str, params: dict) -> dict:
    q = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
    req = urllib.request.Request(f"{base}?{q}", headers={"User-Agent": UA})
    with opener.open(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wd_search(opener, name: str) -> list[str]:
    d = _get_json(opener, WD_API, {
        "action": "wbsearchentities", "search": name,
        "language": "ja", "uselang": "ja", "type": "item", "limit": 7,
    })
    return [h["id"] for h in d.get("search", [])][:7]


def wd_entities(opener, qids: list[str]) -> dict:
    out: dict = {}
    for i in range(0, len(qids), 45):
        chunk = qids[i:i + 45]
        d = _get_json(opener, WD_API, {
            "action": "wbgetentities", "ids": "|".join(chunk),
            "props": "claims|labels|aliases", "languages": "ja|en",
        })
        out.update(d.get("entities", {}))
        time.sleep(0.2)
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def matches_name(ent: dict, name: str) -> bool:
    """label 或 alias 去掉空白后**完全相等**。不做模糊匹配。"""
    ent = ent.get("missing") and {} or ent
    labels = [v.get("value", "") for v in (ent.get("labels") or {}).values()]
    aliases = [a.get("value", "") for lang in ("ja", "en")
               for a in (ent.get("aliases") or {}).get(lang, [])]
    return _norm(name) in {_norm(x) for x in labels + aliases if x}


def is_human(ent: dict) -> bool:
    for c in (ent.get("claims") or {}).get("P31", []):
        if (c.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")) == "Q5":
            return True
    return False


def wd_handles(ent: dict) -> list[str]:
    out = []
    for c in (ent.get("claims") or {}).get("P2002", []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, str) and HANDLE_RE.match(v):
            out.append(v)
    return sorted(set(out))


def wp_handles(opener, name: str) -> tuple[str | None, list[str]]:
    """→ (跟随重定向后的条目名, 候选 handle 列表)。"""
    try:
        d = _get_json(opener, WP_API, {
            "action": "query", "titles": name, "redirects": "1",
            "prop": "extlinks", "ellimit": "500",
        })
    except Exception:  # noqa: BLE001
        return None, []
    pages = (d.get("query") or {}).get("pages") or []
    if not pages or pages[0].get("missing"):
        return None, []
    page = pages[0]
    hs = set()
    for e in page.get("extlinks", []):
        m = TW_LINK_RE.match(e.get("*", ""))
        if m and m.group(1).lower() not in BAD_HANDLES:
            hs.add(m.group(1))
    return page.get("title"), sorted(hs)


def x_status(opener, handle: str) -> tuple[int, str]:
    """→ (HTTP 状态, 页面标题里的显示名)。只读前 60 KB —— 标题在 <head> 里。"""
    req = urllib.request.Request(f"https://x.com/{handle}", headers={"User-Agent": X_UA})
    try:
        with opener.open(req, timeout=20) as resp:
            head = resp.read(60000).decode("utf-8", "replace")
            m = re.search(r"<title>(.*?)</title>", head, re.S)
            import html as _html
            return resp.status, (_html.unescape(m.group(1)).strip() if m else "")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:  # noqa: BLE001
        return 0, ""


def name_hint(name: str, title: str) -> bool:
    """昵称里有没有名字的痕迹（整名，或任意连续两个字）。"""
    n, t = _norm(name), _norm(title)
    if not n or not t:
        return False
    if n in t:
        return True
    return any(n[i:i + 2] in t for i in range(len(n) - 1))


def collect_candidates(opener, name: str, qids: list[str],
                       ents: dict) -> tuple[list[tuple[str, str, str]], str]:
    """→ (候选 [(handle, source, 出处)], 「没查到」时的 detail)。不碰网络以外的东西。"""
    matched = [q for q in qids
               if q in ents and matches_name(ents[q], name) and is_human(ents[q])]
    cands: list[tuple[str, str, str]] = []
    for q in matched:
        for h in wd_handles(ents[q]):
            cands.append((h, "wikidata", f"Wikidata {q}"))
    if cands:
        return cands, ""
    title, hs = wp_handles(opener, name)
    for h in hs:
        cands.append((h, "wikipedia", f"维基条目「{title}」"))
    return cands, (f"维基条目「{title}」没有 X 外链" if title else "Wikidata / 维基都没有")


def decide(cands: list[tuple[str, str, str]], alive: dict[str, tuple[int, str]],
           name: str) -> dict:
    if not cands:
        return {"handle": "", "reason": "没查到", "detail": ""}
    uniq = sorted({(h.lower(), h, s, d) for h, s, d in cands})
    if len(uniq) > 1:
        return {"handle": "", "reason": "多个候选",
                "detail": "、".join(f"@{h}（{s}）" for _, h, s, _ in uniq)}
    _, handle, source, detail = uniq[0]
    code, title = alive.get(handle.lower(), (0, ""))
    if code != 200:
        return {"handle": "", "reason": "疑似但账号不存在",
                "detail": f"@{handle} → HTTP {code}（{detail}）"}
    return {"handle": handle, "source": source, "reason": "", "detail": detail,
            "title": title, "hint": name_hint(name, title)}


def _render_review(results: dict[str, dict], fetched_at: datetime) -> str:
    groups = {"没查到": [], "多个候选": [], "疑似但账号不存在": []}
    ok, warn = [], []
    for name in sorted(results):
        r = results[name]
        if r.get("handle"):
            (ok if r.get("hint") else warn).append((name, r))
        else:
            groups.setdefault(r["reason"], []).append((name, r))
    out = [
        "# 声优 X 账号：需要人工确认的",
        "",
        f"生成于 {fetched_at.isoformat(timespec='seconds')}。",
        f"自动确认 **{len(ok)}** 人；**昵称对不上、建议重点抽查 {len(warn)}** 人；"
        f"待确认 **{sum(len(v) for v in groups.values())}** 人。",
        "",
        "确认后直接改 `src/qq_rss_bot/seiyuu.toml`，把 `source` 改成 `\"manual\"`",
        "（重跑不会覆盖）。**宁可留空也别填猜的** —— 填错了群里看不出来。",
        "",
    ]
    if warn:
        out += [f"## 已自动填上、但昵称里找不到名字（{len(warn)}）", "",
                "这些账号**存在**，只是昵称和本名对不上（可能用昵称做号名）。"
                "抽查一遍，不对就把 handle 清空或改掉。", ""]
        out += [f"- `{n}` → @{r['handle']}（昵称「{r.get('title', '')[:40]}」）"
                for n, r in warn]
        out.append("")
    for title, rows in groups.items():
        out += [f"## {title}（{len(rows)}）", ""]
        if not rows:
            out += ["_无_", ""]
            continue
        for name, r in rows:
            note = f" — {r['detail']}" if r.get("detail") else ""
            out.append(f"- `{name}`{note}")
        out.append("")
    out += ["## 自动确认且昵称对得上（抽查用）", ""]
    out += [f"- `{name}` → @{r['handle']}（{r['source']}）" for name, r in ok]
    out.append("")
    return "\n".join(out)


def _write_library(path: Path, data: dict, persons: dict, now: datetime) -> None:
    meta = data.get("meta") or {}
    counts = {k: v for k, v in meta.items() if isinstance(v, int)}
    extra = {s: dict(data.get(s) or {}) for s in fs.EXTRA_SECTIONS}
    text = fs.render(
        persons=persons,
        roles=data.get("roles") or {},
        former_roles=data.get("former_roles") or {},
        counts=counts,
        extra=extra,
        fetched_at=datetime.fromisoformat(meta["fetched_at"]) if "fetched_at" in meta else now,
    )
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", action="append", default=[], help="只查这些人（可重复）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--limit", type=int, default=0, help="只查前 N 个（调试用）")
    args = ap.parse_args()

    proxy = args.proxy or fs.fb._env_proxy()
    opener = _opener(proxy)
    data = tomllib.loads(LIB.read_text(encoding="utf-8"))
    persons = {k: dict(v) for k, v in (data.get("persons") or {}).items()}

    todo = [n for n, v in persons.items() if not v.get("handle")]
    if args.only:
        todo = [n for n in args.only if n in persons]
    if args.limit:
        todo = todo[:args.limit]
    print(f"要查 {len(todo)} 人（库里共 {len(persons)} 人，已有 handle "
          f"{sum(1 for v in persons.values() if v.get('handle'))} 个）")

    print("  ① Wikidata 搜索…")
    qids: dict[str, list[str]] = {}

    def _search(name: str) -> tuple[str, list[str]]:
        try:
            return name, wd_search(opener, name)
        except Exception as exc:  # noqa: BLE001
            print(f"     {name}: 搜索失败 {type(exc).__name__}")
            return name, []

    with ThreadPoolExecutor(max_workers=6) as pool:
        for i, (name, found) in enumerate(pool.map(_search, todo), 1):
            qids[name] = found
            if i % 50 == 0:
                print(f"     {i}/{len(todo)}")

    all_qids = sorted({q for v in qids.values() for q in v})
    print(f"  ② 批量取实体（{len(all_qids)} 个候选）…")
    ents = wd_entities(opener, all_qids) if all_qids else {}

    print("  ③ 汇总候选（Wikidata 优先，维基兜底）…")
    cands: dict[str, list[tuple[str, str, str]]] = {}
    no_detail: dict[str, str] = {}

    def _cands(name: str):
        c, detail = collect_candidates(opener, name, qids.get(name, []), ents)
        return name, c, detail

    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, c, detail in pool.map(_cands, todo):
            cands[name] = c
            no_detail[name] = detail

    unique = sorted({h for c in cands.values() for h, _, _ in c})
    print(f"  ④ x.com 存活校验（{len(unique)} 个账号）…")
    alive: dict[str, tuple[int, str]] = {}

    def _check(h: str) -> tuple[str, tuple[int, str]]:
        return h.lower(), x_status(opener, h)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for key, res in pool.map(_check, unique):
            alive[key] = res

    results: dict[str, dict] = {}
    for name in todo:
        r = decide(cands[name], alive, name)
        if not r.get("handle") and r["reason"] == "没查到" and no_detail.get(name):
            r["detail"] = no_detail[name]
        results[name] = r
        if r.get("handle"):
            print(f"     @{r['handle']:<18} {name}  ← {r['source']}")

    filled = {n: r for n, r in results.items() if r.get("handle")}
    for name, r in filled.items():
        persons[name]["handle"] = r["handle"]
        persons[name]["source"] = r["source"]
    print(f"\n  自动确认 {len(filled)} / {len(todo)}；"
          f"待人工 {len(todo) - len(filled)}")

    if args.dry_run:
        print("--dry-run：没写盘。")
        return 0

    _write_library(LIB, data, persons, datetime.now(fs.CST))
    REVIEW.parent.mkdir(parents=True, exist_ok=True)
    REVIEW.write_text(_render_review(results, datetime.now(fs.CST)), encoding="utf-8")
    print(f"写出 {LIB}")
    print(f"写出 {REVIEW}（不进 git）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
