"""小偶像的生日推送 —— **每天一条图文**。

数据分两半，来源完全不同：

| | 来源 | 什么时候取 |
|---|---|---|
| **名单**（谁生日） | `data/birthday/birthdays.toml`（冷数据，见 `scripts/fetch_birthday_source.py`） | 一次性扒下来，运行时只读本地文件 |
| **配图** | `https://x.com/imas_ch` 当天的生日推 | 每天实时抓 |

## 为什么图要实时抓，名单却是冷数据

名单是**角色设定**，不会变 —— 366 天几百 KB，扒一次存着，少一个「imas-db
挂了今天就没推送」的依赖。

配图是**当天才发的**：imas_ch 在北京时间 **23:00**（= 日本 0 点）发一条推，
列出当天的偶像、配一张**多人合并在内**的图。

## ⚠️ 推送时刻是 23:05，推的是**第二天**的生日

用户定的：生日推送的「正日子」是**日本时间当天 0 点**，而 imas_ch 正好在那个
时刻发推，所以我们 **23:05** 起跑去抓那张图。

于是 **10-18 晚上 23:05 推的是 10-19 的生日**（那时日本已经是 10-19 了）。
`slot_for()` 给推送时刻、`birthday_date()` 把它 +1 天，**库里记的是被祝福的
那天**（`10-19`），不是推送那天 —— 这样「同一天的生日」在库里只有一行，
重试、重启、跨零点都不会串。见 `birthday_date()` 的说明。

## ⚠️ 这条链路容器直连全不通，必须走代理

实测（2026-10-19，容器内）：

| 目标 | 直连 | 经 `MEDIA_PROXY` |
|---|---|---|
| `x.com/imas_ch` | ❌ SSL 握手超时（墙） | ✅ 200 / 163 KB |
| `pbs.twimg.com/media/…` | ❌ `Errno 99`（DNS 被污染） | ✅ 200 / 115 KB |

**没有证书错误** —— Surge 没 MITM 这两个域名，所以 `MEDIA_CA_BUNDLE` /
`MEDIA_INSECURE_TLS` 在这里都用不上（虽然线上配着）。
客户端一律用 `preview.make_client(cfg.media_proxy, …)`，别另起一个。

## ⚠️ 图片必须我们自己下、走 base64 上传

不能把 `pbs.twimg.com` 的 URL 直接交给 QQ 去抓 —— **QQ 的服务器在国内，
同样够不到 Twitter 的图床**。所以走 `preview.py` 那条老路：我们下载字节 →
base64 → `post_group_file`。

## 名单不做任何过滤

961プロ（反派）、Xenoglossia（机甲 AU）、その他 全都发。用户明确要求
「能从 imasdb 抓下来的都要显示」。
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import re
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, time, timedelta
from pathlib import Path

from . import features, preview

_log = logging.getLogger("qq_rss_bot.birthday")

# 用户定的：日本时间当天 0 点 = 北京时间前一天 23:00，imas_ch 正好在那时发推，
# 所以 23:05 起跑。**推的是第二天的生日**，见 birthday_date()。
DEFAULT_TIME = "23:05"
# ⚠️ **和早安那个 180 秒不是一个意思。** 早安是纯本地文件，窗口只覆盖调度抖动；
# 这里要**实时抓推文**，所以窗口同时是「抓图的补发窗口」：
# 窗口内每拍重试，到窗口末尾还没抓到就**发纯文字**（名单从来不依赖推文）。
DEFAULT_WINDOW_SEC = 300
CHECK_SEC = 60
CALENDAR_NAME = "birthdays.toml"

X_SCREEN_NAME = "imas_ch"
X_PROFILE_URL = f"https://x.com/{X_SCREEN_NAME}"
# 正文里认这一句。imas_ch 除了生日推还发别的，只按「最新一条」取会取错。
BIRTHDAY_MARK = "お誕生日おめでとう"
X_ID_RE = re.compile(r"\d{15,25}")

#  pessoas 名字单元格里剖出来的角色：`土屋翔 (舞台SideM 伊瀬谷四季役)`
_ROLE_RE = re.compile(r"^(.*?)\s*\((.*?)役\)\s*$")


@dataclass(frozen=True)
class Person:
    """名单里的一个人。`role` 只有声优有（`舞田類` 这种）。"""

    name: str
    brand: str
    role: str = ""


@dataclass(frozen=True)
class Day:
    idols: tuple[Person, ...] = ()
    people: tuple[Person, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.idols and not self.people

    @property
    def total(self) -> int:
        return len(self.idols) + len(self.people)


@dataclass(frozen=True)
class Calendar:
    brands: dict[str, str]
    days: dict[str, Day]  # "MM-DD" → Day

    def for_date(self, d: Date) -> Day:
        """取某天的名单。查不到就是空（46 天没人过生日）。

        ⚠️ **不需要判闰年**：`datetime.date` 本身就构造不出平年的 02-29，
        所以 `"02-29"` 这个键只在闰年会被查到。别在这里加 `isleap` 分支 ——
        那是走不到的死代码。
        """
        return self.days.get(f"{d.month:02d}-{d.day:02d}", Day())


def load_calendar(path: Path) -> Calendar:
    """读冷数据。文件不在 / 读不动就抛 —— 宁可炸在启动，也别静默变成「今天没人过生日」。"""
    with Path(path).open("rb") as fh:
        raw = tomllib.load(fh)
    days: dict[str, Day] = {}
    for entry in raw.get("days", []):
        key = entry.get("date")
        if not key:
            continue
        days[key] = Day(
            # ⚠️ 没人过生日的那天**根本没有这两个键**（不是空数组），必须 .get
            idols=tuple(
                Person(i["name"], i.get("brand", "")) for i in entry.get("idols", [])
            ),
            people=tuple(
                Person(p["name"], p.get("brand", ""), p.get("role", ""))
                for p in entry.get("people", [])
            ),
        )
    brands = {str(k): str(v) for k, v in (raw.get("brands") or {}).items()}
    return Calendar(brands=brands, days=days)


# --------------------------------------------------------------------------
# x.com 的 HTML
# --------------------------------------------------------------------------

# 时间线条目：`data-href="/imas_ch/status/<id>"` 起一个 <li>
_ENTRY_RE = re.compile(r'data-href="/[A-Za-z0-9_]+/status/')
_TS_RE = re.compile(r'"timestamp":(\d{13})')
_MEDIA_RE = re.compile(r"https://pbs\.twimg\.com/media/([A-Za-z0-9_-]+)")
_TEXT_DIV_RE = re.compile(
    r'whitespace-pre-wrap[^>]*dir="auto">(.*?)</div>', re.S
)


@dataclass(frozen=True)
class Tweet:
    id: str
    posted_at: datetime | None
    text: str
    images: tuple[str, ...]  # pbs.twimg.com 的 media id

    def image_url(self, index: int = 0, name: str = "orig") -> str:
        return (
            f"https://pbs.twimg.com/media/{self.images[index]}"
            f"?format=jpg&name={name}"
        )


def _plain(chunk: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", chunk))).strip()


def parse_timeline(page: str) -> list[Tweet]:
    """从 `https://x.com/imas_ch` 的 HTML 里剖出时间线。

    ⚠️ **x.com 对未登录用户返回的是服务端渲染的完整时间线**（2026-10-19 实测，
    约 160 KB），`/imas_ch/status/<id>`、时间戳、正文、配图都在里面。
    这是目前**唯一**免登录能拿到它的路 —— 官方 syndication 接口已死、
    fxtwitter 只有 `/status/<id>` 没有时间线、Nitter 镜像全挂。
    页面结构调整的话这个函数会返回空列表，上层按「今天没抓到图」处理（纯文字发）。

    一页大概给 6 条（含置顶），够我们找当天那条了。
    """
    parts = _ENTRY_RE.split(page)
    if len(parts) < 2:
        return []
    # 分隔符被吃掉了，把它拼回去：每段开头就是 id
    tweets: list[Tweet] = []
    seen: set[str] = set()
    for part in parts[1:]:
        m = X_ID_RE.match(part)
        if not m:
            continue
        tid = m.group(0)
        # ⚠️ 一条推被另一条**引用成卡片**时会再出现一次（实测撞到过），
        # 不去重的话同一条生日推会解析出两遍、图片发两张。
        if tid in seen:
            continue
        seen.add(tid)
        ts = _TS_RE.search(part)
        texts = [_plain(t) for t in _TEXT_DIV_RE.findall(part)]
        images = list(dict.fromkeys(_MEDIA_RE.findall(part)))
        tweets.append(
            Tweet(
                id=tid,
                posted_at=(
                    datetime.fromtimestamp(int(ts.group(1)) / 1000) if ts else None
                ),
                text=" ".join(t for t in texts if t),
                images=tuple(images),
            )
        )
    return tweets


def pick_birthday_tweets(tweets: list[Tweet], d: Date) -> list[Tweet]:
    """挑出「某天」的生日推。

    判据是**正文里的日期 + 那句祝福语**，不是「最新一条」——
    imas_ch 除了生日推还会发配信、周边之类，盲取最新会取错。
    """
    stamp = f"{d.month}/{d.day}"
    return [
        t
        for t in tweets
        if BIRTHDAY_MARK in t.text and re.search(rf"(?<!\d){re.escape(stamp)}(?!\d)", t.text)
    ]


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def render(d: Date, day: Day) -> str:
    """当天的文案。**名单一位不落**，偶像在前、声优在后。"""
    lines = [f"🎂 {d:%Y/%m/%d} 本日のお誕生日:"]
    lines += [p.name for p in day.idols]
    lines += [f"{p.name} ({p.role}役)" if p.role else p.name for p in day.people]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 调度
# --------------------------------------------------------------------------


def parse_time(raw: str) -> time:
    """`"23:05"` → `time(23, 5)`。写错就退出 —— 和 `morning.parse_time` 同款。"""
    try:
        return datetime.strptime((raw or "").strip(), "%H:%M").time()
    except ValueError as exc:
        raise SystemExit(f"BIRTHDAY_TIME 必须是 HH:MM，当前值：{raw!r}") from exc


def slot_for(now: datetime, at: time | None = None) -> datetime:
    """`now` 之前（含）最近的那个推送时刻。**每天一次，不是每周。**

    含 —— 正好 23:05 时返回**今天** 23:05；否则 tick 恰好落在整点那一下会
    算出昨天、然后被窗口判掉，白白错过一天。（`morning.slot_for` 同一套逻辑。）
    """
    at = at or parse_time(DEFAULT_TIME)
    slot = datetime.combine(now.date(), at)
    if slot > now:
        slot -= timedelta(days=1)
    return slot


def birthday_date(slot: datetime) -> Date:
    """推送时刻 → **被祝福的那天**。

    ⚠️ **这是整个功能最容易写错的一行。** 用户定 23:05 是因为 imas_ch 在北京
    时间 23:00（= 日本 0 点）发当天的祝贺推，所以 10-18 晚上 23:05 推的是
    **10-19** 的生日。

    写成 `slot.date()` 的话每天的推送会整体**早一天**，而且从日志上完全看不出来
    —— 名单和推文都取自同一份数据，只是整体错位。库里也按被祝福的那天记，
    所以「同一天的生日」只有一行，重试 / 重启 / 跨零点都不会串。
    """
    return (slot + timedelta(days=1)).date()


# --------------------------------------------------------------------------
# 抓推文
# --------------------------------------------------------------------------

# 抓时间线的超时。它在轮询里，别设太长 —— 窗口只有 300 秒。
TIMELINE_TIMEOUT = 20.0


async def fetch_timeline(client, url: str = X_PROFILE_URL) -> list[Tweet]:
    """抓一次 `x.com/imas_ch` 的时间线。**失败会抛**，调用方按「这一拍没图」处理。

    解析出 0 条要打 WARNING：页面结构变了、或者 x.com 改成登录墙，都会落到
    这里。静默的话症状是「每天都没图」，看着像 imas_ch 没发。
    """
    resp = await client.get(url, timeout=TIMELINE_TIMEOUT)
    resp.raise_for_status()
    tweets = parse_timeline(resp.text)
    if not tweets:
        _log.warning("生日：%s 解析出 0 条推文（%d 字节）", url, len(resp.text))
    return tweets


async def fetch_day_image(
    client, tweets: list[Tweet], d: Date
) -> tuple[str, bytes] | None:
    """在时间线里找当天的生日推、下它第一张图。返回 `(推文 id, 图片字节)`。

    拿不到就返回 `None` —— **只影响有没有图，不影响这条推发不发**：
    名单是冷数据，和推文一点关系都没有。
    """
    hits = pick_birthday_tweets(tweets, d)
    if not hits:
        _log.info("生日：时间线里没有 %d/%d 那条推", d.month, d.day)
        return None
    tweet = hits[0]
    if not tweet.images:
        _log.warning("生日：推文 %s 里没有配图", tweet.id)
        return None
    raw = await preview.fetch_image(client, tweet.image_url())
    if raw is None:
        _log.warning("生日：推文 %s 的配图下载失败", tweet.id)
        return None
    return tweet.id, raw


# --------------------------------------------------------------------------
# 轮询器
# --------------------------------------------------------------------------


class BirthdayPoller:
    """每天一次的生日推送。和 `MorningPoller` / `MondayPoller` 同形。

    单独一个 task，不塞进 `FeedPoller` —— 数据源、节奏、失败语义都不一样。

    ## 和早安那条**最关键的区别：这里会重试**

    早安是纯本地文件，一拍就成。生日要**实时抓推文**：imas_ch 23:00 发，
    我们 23:05 起跑，正常第一拍就有；但它偶尔晚几分钟，或者网络抖一下。
    所以窗口（默认 300 秒）内**每拍都重试抓图**，到窗口末尾还没抓到就发纯文字。

    **绝不因为没图就不发** —— 名单是冷数据，推文挂了只是少一张配图。
    """

    def __init__(self, cfg, db, sender, media_http) -> None:
        self.cfg = cfg
        self.db = db
        self.sender = sender
        # ⚠️ 走 `media_http`（带代理的那个）。容器直连 x.com / pbs.twimg.com
        # **全不通**（实测：SSL 握手超时 / Errno 99），别顺手换成 rss_http。
        self.http = media_http
        self._stopped = asyncio.Event()
        self._calendar: Calendar | None = None
        self._load_failed = False

    @property
    def groups(self) -> tuple[str, ...]:
        """这个功能该发到哪些群。**每次重查库** —— `/set` 改完下一拍就生效。

        群开关存在库里（见 `features.py`），`.env` 只用于**首次灌入**。
        """
        return features.groups_for(self.db, "birthday", self.cfg.birthday_groups)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.birthday_enabled and self.groups)

    @property
    def calendar_path(self) -> Path:
        return Path(self.cfg.birthday_dir) / CALENDAR_NAME

    def stop(self) -> None:
        self._stopped.set()

    def load(self) -> Calendar | None:
        """读冷数据（只读一次，之后走缓存）。

        ⚠️ **读不到只打 ERROR 并让功能静默关闭，不抛。** 它是锦上添花的功能，
        不该让整个机器人起不来（`data/birthday/` 是 gitignore 的，全新部署本来
        就没有这个文件）。但**一定要能看见** —— 每天少一条推送，从群里是
        完全看不出来的。日志里写清楚了怎么修。
        """
        if self._calendar is None:
            try:
                self._calendar = load_calendar(self.calendar_path)
            except Exception as exc:  # noqa: BLE001
                if not self._load_failed:
                    self._load_failed = True
                    _log.error(
                        "生日：冷数据读不了（%s）：%s。功能关闭 —— 先跑 "
                        "scripts/fetch_birthday_source.py，再把 data/birthday/ 同步到 NAS",
                        self.calendar_path,
                        exc,
                    )
                return None
        return self._calendar

    async def run(self) -> None:
        if not self.enabled:
            _log.info("生日：未配置 BIRTHDAY_GROUPS，功能关闭")
            return
        _log.info(
            "生日：已启用，每天 %s（本地时间）推送**第二天**的生日，目标 %d 个群，名单 %s",
            self.cfg.birthday_time,
            len(self.groups),
            self.calendar_path,
        )
        while not self._stopped.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 和早安 / 月曜一样：出问题只记日志，**不往群里报错**。
                _log.exception("生日：本轮检查异常，跳过")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=CHECK_SEC)
            except asyncio.TimeoutError:
                pass
        _log.info("生日：已停止")

    async def poll_once(self, *, now: datetime | None = None, tweets=None) -> int:
        """跑一轮，返回这轮发出去的条数。

        `tweets` **只给测试用**：传进来就完全不碰网络（smoke 全靠这个跑离线）。
        """
        if not self.enabled:
            return 0

        now = now or datetime.now()
        slot = slot_for(now, parse_time(self.cfg.birthday_time))
        late = (now - slot).total_seconds()
        if late > self.cfg.birthday_window_sec:
            return 0

        d = birthday_date(slot)
        day_key = d.isoformat()
        done = self.db.birthday_posted(day_key)
        targets = [g for g in self.groups if g not in done]
        if not targets:
            return 0

        calendar = self.load()
        if calendar is None:
            return 0
        day = calendar.for_date(d)
        if day.empty:
            # 366 天里有 46 天没人过生日。**这是正常情况，不是错误**，不打日志
            # 也不记账 —— 窗口内每拍都会走到这里，打日志就是刷屏。
            return 0

        text = render(d, day)

        # ---- 配图：能抓就抓，抓不到也得发 ----
        found = await self._find_image(d, tweets)
        if found is None and late + CHECK_SEC < self.cfg.birthday_window_sec:
            # 还没到窗口末尾 —— **留着下一拍再试**。imas_ch 23:00 发推、我们
            # 23:05 起跑，正常第一拍就有；偶尔晚几分钟也等得起。
            return 0
        tweet_id, image = found if found else ("", None)
        if image is None:
            _log.warning(
                "生日：%s 没抓到配图，等满 %d 秒后发纯文字",
                day_key,
                self.cfg.birthday_window_sec,
            )

        sent = 0
        # 图得先落盘 —— `send_local_image` 收的是路径（当初是给 data/monday/
        # 的表情包写的）。**落临时目录、用完就删**：这里只是要个路径，
        # 不是素材，不该在 NAS 上留痕。
        with tempfile.TemporaryDirectory(prefix="birthday-") as tmp:
            path = Path(tmp) / "birthday.jpg"
            if image is not None:
                path.write_bytes(image)
            for group in targets:
                # 逐群隔离：一个群发不出去不能连累别的群。失败**不记账**，
                # 窗口内下一次 tick 会重试；窗口过了就当今天没发。
                try:
                    if image is None:
                        await self.sender.send_text(group, text)
                    else:
                        await self.sender.send_local_image(group, text, path)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("生日：发送失败 → %s: %s", group[:8], exc)
                    continue
                self.db.mark_birthday_posted(day_key, group, tweet_id)
                sent += 1
                _log.info(
                    "生日：已推送 %s（%d 位，%s）→ %s",
                    day_key,
                    day.total,
                    f"推文 {tweet_id}" if image is not None else "无图",
                    group[:8],
                )
        return sent

    async def _find_image(self, d: Date, tweets):
        """`tweets` 是测试注入口；为 None 才真去抓。"""
        if tweets is None:
            try:
                tweets = await fetch_timeline(self.http)
            except Exception as exc:  # noqa: BLE001
                _log.info("生日：抓时间线失败：%s: %s", type(exc).__name__, exc)
                return None
        return await fetch_day_image(self.http, tweets, d)
