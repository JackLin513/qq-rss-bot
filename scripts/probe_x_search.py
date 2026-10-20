"""X 话题搜索的传输探针 —— **这一条决定「誕生祭贺图」功能能不能做**。

    uv run python scripts/probe_x_search.py                    # 验 cookie + 试两条路
    uv run python scripts/probe_x_search.py --tag '#杜野凛世誕生祭2026'
    uv run python scripts/probe_x_search.py --save             # 原始响应存 /tmp 供 grep

## 2026-10-19 的实测结论（省得再走一遍）

| 传输 | 结果 |
|---|---|
| SSR（未登录） | ❌ 307 → 登录引导页 |
| SSR（带 cookie） | ⚠️ 200 / 307 KB，但 **HTML 里一条推文都没有**（登录态只有 JS 壳） |
| GraphQL **GET** | ❌ **404 空响应**，加什么头都一样 |
| GraphQL **POST** | ✅ **200**，20 条推文 + 点赞数 + 图 |

⚠️ **决定成败的是 POST。** 网上流传的示例几乎清一色是 GET —— 照抄就是 404，
而且和「queryId 不对」的症状一模一样，很难分辨。所以这个脚本现在**默认跑 POST**。

## 为什么要有这个脚本

「帶 cookie 去搜话题」这条路**从没验证过**。已知的只有：

- `x.com/<user>`（**未登录**）能读到服务端渲染的时间线，里面**带
  `"counts":{"favorite_count":…}`** —— 162 959 字节，实测过；
- `x.com/search?q=…&f=live`（**未登录**）307 跳到登录引导页，0 条推文。

⚠️ **加 cookie 不一定能让 SSR 变好，可能反而变差。** `x.com/<user>` 之所以
未登录能读，是因为走的是给爬虫/SEO 用的降级路径；登录态很可能是**只有 JS 壳、
推文全靠客户端再拉**。所以这里要同时试两条路：

| 传输 | 是什么 | 风险 |
|---|---|---|
| **ssr** | `GET /search?q=…&f=live` + cookie，正则扒 HTML | 登录态可能没有推文 |
| **graphql** | **`POST`** `/i/api/graphql/<queryId>/SearchTimeline` + cookie + csrf | `queryId` 随前端发版轮换 |

## ⚠️ 凭据只从 `.env` 读，绝不打印、绝不进命令行

读的是 `.env` 里的 `auth_token` / `ct0`（`X_AUTH_TOKEN` / `X_CT0` 也认）。
打出来的**只有长度**。这个脚本里**不许**出现把凭据写进 argv、写进日志、
写进异常的代码 —— 报错信息里也要把它剥掉。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# X 网页版自己的 JS 里写死的公开 bearer —— **不是我们的凭据**，网上到处都是。
# 写在这里是因为 GraphQL 那条路必须带它；真要换就从 bundle 里重新抄一份。
WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# 未登录时搜索页会 307 到这里。看到它就说明**cookie 没生效**。
ONBOARD_MARK = "i/jf/onboarding"

# SSR 页面里必须同时出现这些，才说明「HTML 里真有推文」。
MARKERS = {
    "推文 id": re.compile(r"status/(\d{15,25})"),
    "favorite_count": re.compile(r'"favorite_count"\s*:\s*(\d+)'),
    "counts 块": re.compile(r'"counts"\s*:\s*\{'),
    "图床 pbs": re.compile(r"pbs\.twimg\.com/media/"),
    "时间戳": re.compile(r'"timestamp"\s*:\s*(\d{13})'),
}

BUNDLE_RE = re.compile(r"https://abs\.twimg\.com/responsive-web/client-web[^\"'\\ ]+\.js")


def _arg(name: str, default: str | None = None) -> str | None:
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(f"{name}="):
            return a.split("=", 1)[1]
    return default


def _flag(name: str) -> bool:
    return name in sys.argv[1:]


def load_cookies() -> tuple[str, str]:
    """从 `.env` / 环境变量取两个 cookie。**只返回，不打印。**"""
    load_dotenv(ROOT / ".env")
    auth = (os.environ.get("auth_token") or os.environ.get("X_AUTH_TOKEN") or "").strip()
    ct0 = (os.environ.get("ct0") or os.environ.get("X_CT0") or "").strip()
    return auth, ct0


def _headers(auth: str, ct0: str, *, api: bool = False) -> dict[str, str]:
    """⚠️ **页面和 API 的头是两套，不能混。**

    往 `/home`、`/search` 这类 HTML 页面上发 `authorization: Bearer` +
    `x-twitter-auth-type` 会被直接 **401 空响应**（实测踩到），
    那几个头只在 `/i/api/*` 上成立。分开之后 cookie 才有机会被认。
    """
    head = {"User-Agent": UA, "Cookie": f"auth_token={auth}; ct0={ct0}"}
    if api:
        head |= {
            "x-csrf-token": ct0,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "authorization": f"Bearer {WEB_BEARER}",
            "content-type": "application/json",
        }
    return head


def _scrub(text: str, *secrets: str) -> str:
    """任何要打出来的文本都先过一遍 —— 万一服务端把 cookie 回显了。"""
    for s in secrets:
        if s:
            text = text.replace(s, "<cookie>")
    return text


SAVE_DIR = Path("/tmp/xprobe")
SAVE = False   # --save 打开


def _dump(name: str, resp: httpx.Response) -> None:
    """`--save` 时把原始响应落盘，方便事后 grep（**不进会话记录**）。"""
    if not SAVE:
        return
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    path = SAVE_DIR / f"{name}.txt"
    path.write_bytes(resp.content)
    print(f"      （原始响应已存 {path}，{len(resp.content)} 字节）")


async def _get(
    client: httpx.AsyncClient,
    url: str,
    auth: str,
    ct0: str,
    *,
    follow: bool = False,
    save_as: str = "",
) -> httpx.Response:
    resp = await client.get(url, headers=_headers(auth, ct0), follow_redirects=follow)
    if save_as:
        _dump(save_as, resp)
    return resp


def report_markers(html: str, label: str) -> int:
    """打印各标记的命中数，返回「推文 id」的命中数（判定成败的主指标）。"""
    hits = {name: len(rx.findall(html)) for name, rx in MARKERS.items()}
    for name, n in hits.items():
        print(f"      {name:<16} {n}")
    return hits["推文 id"]


async def check_cookie(client: httpx.AsyncClient, auth: str, ct0: str) -> bool:
    """`/home` 是登录态才有的页。307 到引导页 = cookie 无效。"""
    print("[1] cookie 有效性 —— GET https://x.com/home")
    resp = await _get(client, "https://x.com/home", auth, ct0, save_as="home")
    where = resp.headers.get("location", "")
    print(f"      HTTP {resp.status_code}  {len(resp.content)} 字节  跳转→ {where or '（无）'}")
    if resp.status_code in (301, 302, 303, 307, 308) and ONBOARD_MARK in where:
        print("      ❌ cookie 没生效（被当成未登录）")
        return False
    if resp.status_code == 200:
        n = report_markers(resp.text, "home")
        print(f"      {'✅' if n else '⚠️'} 200 —— cookie 有效；HTML 里推文 id {n} 个")
        return True
    if resp.status_code == 401:
        print("      ❌ 401 空响应 —— 头不对或 cookie 已失效")
    else:
        print(f"      ⚠️ 非预期状态 {resp.status_code}")
    return False


async def try_ssr(client: httpx.AsyncClient, tag: str, auth: str, ct0: str) -> bool:
    url = f"https://x.com/search?q={quote(tag)}&f=live&src=typed_query"
    print(f"[2] SSR 传输 —— GET {url}")
    resp = await _get(client, url, auth, ct0, save_as="search")
    where = resp.headers.get("location", "")
    print(f"      HTTP {resp.status_code}  {len(resp.content)} 字节  跳转→ {where or '（无）'}")
    if resp.status_code in (301, 302, 303, 307, 308):
        if ONBOARD_MARK in where:
            print("      ❌ 仍然被踢到登录引导页 —— SSR 这条路不通")
        else:
            print("      ⚠️ 有跳转，跟一次看看")
            resp = await _get(client, url, auth, ct0, follow=True)
            print(f"      跟随后 HTTP {resp.status_code}  {len(resp.content)} 字节")
    n = report_markers(resp.text, "search")
    ok = n > 0
    print(f"      {'✅' if ok else '❌'} HTML 里推文 id {n} 个 —— SSR {'可用' if ok else '拿不到推文'}")
    return ok


def _array_after(text: str, key: str) -> list[str]:
    m = re.search(r"%s:\[([^\]]*)\]" % key, text)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


def extract_operation(js: str, name: str) -> tuple[str, list[str], list[str]] | None:
    """从 X 自己的 bundle 里抠出某个 GraphQL 操作的 `(queryId, featureSwitches, fieldToggles)`。

    ⚠️ **`queryId` 随前端发版轮换，绝不能写死。** 每次刷新 ~5 MB，
    进程内缓存住就行。

    ⚠️ 匹配必须写成 `queryId:"…",operationName:"<name>"` 这个**紧邻**形态。
    实测松匹配会抓错：`SearchTimeline` 前面紧挨着的是
    `queryId:"VxJMx0-NXe-GO_4jC1lNhw",operationName:"BookmarkSearchTimeline"` ——
    少了 `,operationName:` 这一段就会把 BookmarkSearchTimeline 的 id 当成它的，
    然后 GraphQL 回 **404 空响应**（这个坑真踩过）。

    `featureSwitches` 是**必填**的 `features` 参数：X 会把每个开关名置 `true`
    拼成 JSON 发过去，漏了或少了会被服务端拒。
    """
    m = re.search(
        r'queryId:"([A-Za-z0-9_-]{18,30})",operationName:"%s"' % re.escape(name), js
    )
    if not m:
        return None
    tail = js[m.end() : m.end() + 6000]
    return m.group(1), _array_after(tail, "featureSwitches"), _array_after(tail, "fieldToggles")


async def discover_operation(
    client: httpx.AsyncClient, auth: str, ct0: str, name: str
) -> str | None:
    """从 X 自己的 JS bundle 里抠出操作的 queryId。

    ⚠️ **`queryId` 随前端发版轮换，绝不能写死。**
    ⚠️ 匹配必须写成 `queryId:"…",operationName:"<name>"` 这个**紧邻**形态 ——
    松匹配会抓到紧挨着的 `BookmarkSearchTimeline`，用它去打得到的是**同样的
    404 空响应**，和「方法不对」分不出来（真踩过，白绕一轮）。
    """
    print(f"[3] 从 JS bundle 里找 {name} 的 queryId")
    resp = await _get(client, "https://x.com/home", auth, ct0, save_as="home")
    bundles = list(dict.fromkeys(BUNDLE_RE.findall(resp.text)))
    print(f"      /home 里引用 {len(bundles)} 个 client-web bundle")
    pat = re.compile(
        r'queryId:"([A-Za-z0-9_-]{18,30})",operationName:"%s"' % re.escape(name)
    )
    for url in bundles:
        try:
            js = (await client.get(url, headers={"User-Agent": UA})).text
        except httpx.HTTPError as exc:
            print(f"      {url.rsplit('/', 1)[-1]}: 拉不下来 {type(exc).__name__}")
            continue
        m = pat.search(js)
        if m:
            print(f"      ✅ {url.rsplit('/', 1)[-1]} 命中：queryId={m.group(1)}")
            return m.group(1)
    print(f"      ⚠️ bundle 里没有 {name} —— 手工给一个：--query-id <id>")
    return None


async def try_graphql(
    client: httpx.AsyncClient,
    tag: str,
    auth: str,
    ct0: str,
    query_id: str | None,
    product: str = "Top",
) -> bool:
    """带 cookie 打 `SearchTimeline`。**这是「诞生祭贺图」唯一可能的数据来源。**

    ⚠️ `product` 是**网页上那两栏**，结果差一个数量级，而且写错了不报错：

        Top     = 热度栏，点赞高的全在这儿  ← **要的是这个**
        Latest  = 时间栏，按时间倒序，全是深夜新发的小号

    `main` 会把两栏都打一遍给你对照。第一版生产代码用的是 Latest，
    于是误判成「当天最高才 464 赞」。
    """
    qid = query_id or await discover_operation(client, auth, ct0, "SearchTimeline")
    if not qid:
        return False
    variables = {
        "rawQuery": tag,
        "count": 20,
        "querySource": "typed_query",
        "product": product,
    }
    params = {
        "variables": json.dumps(variables, separators=(",", ":"), ensure_ascii=False),
        # ⚠️ 传空对象就行 —— X 自己 JS 里那 40 个 featureSwitches **不是必须的**
        #    （实测）。所以不用去抠它们，要跟着发版变的只有 queryId 一个东西。
        "features": "{}",
    }
    url = f"https://x.com/i/api/graphql/{qid}/SearchTimeline"
    # ⚠️ **POST，不是 GET。** GET 无论加什么头（Referer / Origin / language /
    #    甚至补一个假的 x-client-transaction-id）**全是 404 空响应**；
    #    POST 用最朴素的头就通。网上流传的示例几乎清一色是 GET。
    print(f"[4] GraphQL 传输 —— POST {url}  product={product}")
    try:
        resp = await client.post(url, params=params, headers=_headers(auth, ct0, api=True))
    except httpx.HTTPError as exc:
        print(f"      ❌ {type(exc).__name__}: {exc}")
        return False
    print(f"      HTTP {resp.status_code}  {len(resp.content)} 字节")
    if resp.status_code != 200:
        print("      " + _scrub(resp.text[:400], auth, ct0).replace("\n", " ") or "（空响应）")
        return False
    if SAVE:
        _dump("graphql", resp)
    body = resp.text
    n_ids = len(re.findall(r'"rest_id"\s*:\s*"(\d{15,25})"', body))
    n_likes = len(re.findall(r'"favorite_count"\s*:\s*\d+', body))
    n_media = len(re.findall(r"pbs\.twimg\.com/media/", body))
    n_screen = len(re.findall(r'"screen_name"\s*:\s*"', body))
    likes = [int(x) for x in re.findall(r'"favorite_count"\s*:\s*(\d+)', body)]
    top = f" 最高赞 {max(likes)}" if likes else ""
    print(f"      rest_id {n_ids} / favorite_count {n_likes} / 图 {n_media} / "
          f"screen_name {n_screen}{top}")
    if n_ids == 0:
        print("      ⚠️ 有响应但没推文 —— 看看原始响应里的 errors")
        errs = re.findall(r'"message"\s*:\s*"([^"]{0,120})"', body)[:4]
        for e in errs:
            print(f"         error: {_scrub(e, auth, ct0)}")
        return False
    ok = n_likes > 0
    print(f"      {'✅ GraphQL 可用' if ok else '❌ GraphQL 拿不到点赞数'}")
    return ok


async def main() -> int:
    tag = _arg("--tag", "#杜野凛世誕生祭2026") or "#杜野凛世誕生祭2026"
    global SAVE
    SAVE = _flag("--save")
    auth, ct0 = load_cookies()
    print(f"cookie：auth_token {'已配置（%d 字符）' % len(auth) if auth else '**缺**'} / "
          f"ct0 {'已配置（%d 字符）' % len(ct0) if ct0 else '**缺**'}")
    if not auth or not ct0:
        print("❌ .env 里缺 auth_token / ct0，先去补上再跑")
        return 2
    print(f"话题：{tag}\n")

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        valid = await check_cookie(client, auth, ct0)
        print()
        ssr_ok = await try_ssr(client, tag, auth, ct0)
        print()
        # ⚠️ **两栏都打一遍** —— 只看一栏会得出完全错误的结论（我踩过）。
        gql_top = await try_graphql(client, tag, auth, ct0, _arg("--query-id"), "Top")
        print()
        gql_latest = await try_graphql(client, tag, auth, ct0, _arg("--query-id"), "Latest")
        print()
        if gql_top and gql_latest:
            print("   → 两栏都通。**生产代码用 Top**（Latest 的赞数低一个数量级）。")
        elif gql_top:
            print("   → Top 通、Latest 不通：生产代码用 Top。")
        elif gql_latest:
            print("   → ⚠️ 只有 Latest 通 —— 结果会全是低赞新推，别用。")
        gql_ok = gql_top or gql_latest

    print("\n" + "=" * 60)
    if gql_ok:
        print("结论：**走 GraphQL（POST + product=Top）**"
              " —— 见 src/qq_rss_bot/birthday_art.py")
    elif ssr_ok:
        print("结论：**SSR 也能出推文了** —— X 改版了，去看看能不能少走一跳")
    else:
        print("结论：**两条都不通** —— 停下来汇报，别硬写")
    print("=" * 60)
    return 0 if (ssr_ok or gql_ok) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
