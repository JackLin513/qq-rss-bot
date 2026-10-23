"""botpy 客户端：把 QQ 事件接到指令处理器和轮询器上。"""

from __future__ import annotations

import asyncio
import base64
import itertools
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import botpy
import httpx
from botpy.message import GroupMessage

from . import ai, bilibili, features, preview, repeat
from .autosub import AutoSubscriber
from .commands import (
    ADMIN_ROLES,
    CommandContext,
    CommandHandler,
    is_bot_mentioned,
    strip_mention,
)
from .config import Config
from .db import Database
from .notify import C2CNotifier
from .quota import GroupSender
from .scheduler import (
    IMAGE_FAILURE_KEY_PREFIX,
    FeedPoller,
    image_failure_message,
)
from . import monday as monday_module
from . import morning as morning_module
from .birthday import BirthdayPoller
from .birthday_art import BirthdayArtPoller
from . import imagelib, imgsrv
from .monday import MondayPoller
from .morning import MorningPoller
from .tickets import TicketPoller

_log = logging.getLogger("qq_rss_bot.bot")

# RSSHub 和很多源站都会拦默认 UA
_USER_AGENT = "qq-rss-bot/0.1 (+https://github.com/DIYgod/RSSHub)"


def _content_blocked(exc: BaseException) -> bool:
    """这条异常是不是 **QQ 内容审核** 拒绝的（`40034006 消息内容违规`）。

    ⚠️ botpy 的 `ServerError` **只带 message，不带 code**，所以只能按文案判。
    完整 body（含 `code` / `trace_id`）在 botpy 自己那行 `[botpy] 接口请求异常…`
    日志里。文案改了的话这里会漏判 —— 漏判的后果是退回纯文字，不是崩溃。
    """
    return "消息内容违规" in (str(exc) or "")


def _recall_reason(exc: BaseException) -> str:
    """撤回失败的人话原因。botpy 的异常同样只有 message，只能按文案映射。"""
    text = str(exc) or type(exc).__name__
    if "无操作权限" in text or "40062003" in text:
        return "无操作权限（机器人需要是群管理员）"
    if "撤回时限" in text or "40064004" in text:
        return "超过 2 分钟撤回时限"
    return text



class RssBot(botpy.Client):
    def __init__(self, cfg: Config) -> None:
        super().__init__(
            intents=botpy.Intents(public_messages=True),
            is_sandbox=cfg.is_sandbox,
            # 给 HTTP 留够时间，botpy 默认 5s 对抓 RSS 太短
            timeout=cfg.fetch_timeout_sec + 5,
            log_level=logging.INFO,
        )
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.rss_http = httpx.AsyncClient(
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        )
        # YouTube / X 专用的出站客户端 —— **只有它走代理**。
        # 别把两个合并：rss_http 必须直连（RSSHub 是容器内部服务名
        # `http://rsshub:1200`，代理解析不了；QQ 的接口直连也更快），
        # 而这两个站点**必须**走代理（容器直连全部超时）。
        # 为什么非走代理不可、为什么图要 base64 上传，见 preview.py 的模块说明。
        self.media_http = preview.make_client(
            cfg.media_proxy,
            ca_bundle=cfg.media_ca_bundle,
            insecure=cfg.media_insecure_tls,
        )
        # 主动消息的节流与配额。scheduler 用信号量**并发**跑多个源的推送循环，
        # 靠每个循环内部逐条 sleep 的间隔根本挡不住（实测 5 个源同时有内容时
        # 25 条全落在 1.6 秒内，而群维度频控是 20 条/分钟）。
        # 所以把 RssBot 包一层：群维度串行 + 令牌桶 + 每日配额记账。
        self.sender = GroupSender(
            self,
            self.db,
            per_minute=cfg.send_per_minute,
            daily_limit=cfg.daily_send_limit,
        )
        # 出错走私聊，不在群里报错。配了 NOTIFY_OPENIDS 就只发给名单上的人。
        self.notifier = C2CNotifier(self, self.db, allowlist=cfg.notify_openids)
        # ---- 图库 + 图床（**必须在所有轮询器之前建**）----
        # `imgsrv` 是自建图床：markdown 的图是 QQ 的服务器去抓的，而它在大陆机房
        # 够不到 pbs.twimg.com —— 所以图先落本地、用我们自己的公网地址给它抓。
        # 端口开不起来不拖垮机器人（`start()` 只打 WARNING）。
        lib_dir = cfg.imagelib_dir or (cfg.db_path.parent / "library")
        self.library = imagelib.Library(self.db, lib_dir, cfg.imgsrv_base)
        self.imgsrv = imgsrv.ImageServer(
            self.library.root, cfg.imgsrv_base, port=cfg.imgsrv_port)
        self.imgsrv.start()
        # 开关状态必须打得出来：镜像出问题时，第一个要回答的就是它开没开。
        _log.info(
            "第三方图镜像（MIRROR_IMAGES）：%s",
            "开 —— bilibili / 抽票 / RSS 的图会下载入库" if cfg.mirror_images
            else "关 —— 第三方图床的 URL 直接交给 QQ",
        )
        self.commands = CommandHandler(
            cfg,
            self.db,
            self.rss_http,
            self.sender,
            notifier=self.notifier,
            ai_http=self.media_http,
            library=self.library,
        )
        # 进群自动订阅。cfg.auto_subscribe_feed 留空时 enabled 为假，整个功能关掉。
        self.autosub = AutoSubscriber(cfg, self.db, self.sender)
        self.poller = FeedPoller(
            cfg,
            self.db,
            self.rss_http,
            self.sender,
            notifier=self.notifier,
            quota=self.sender,
            # 抓文章正文时直连失败才会用到它。**和 media_http 是同一个对象**：
            # 它本来就是「给被墙站点用的客户端」，语义一致，不必再建一个。
            ai_http=self.media_http,
            library=self.library,
        )
        self._poller_task: Any = None
        # 抽票提醒（ASOBI TICKET）。**单独一个 task**，不塞进 FeedPoller ——
        # 数据源、轮询节奏、失败语义都不一样。没配 TICKET_GROUPS 时
        # `run()` 会立刻返回，等于功能关闭。
        self.tickets = TicketPoller(cfg, self.db, self.rss_http, self.sender,
                                    library=self.library)
        self._ticket_task: Any = None
        # 「月曜が近いよ」每周播报。同样单独一个 task。没配 MONDAY_GROUPS 时
        # `run()` 立刻返回，等于功能关闭。
        self.monday = MondayPoller(cfg, self.db, self.sender, library=self.library)
        self._monday_task: Any = None
        # 每日早安语音。同样单独一个 task，没配 MORNING_GROUPS 时 `run()`
        # 立刻返回，等于功能关闭。
        self.morning = MorningPoller(cfg, self.db, self.sender, library=self.library)
        self._morning_task: Any = None
        # 偶像生日推送。同样单独一个 task。它和上面几个的**唯一结构差别**是
        # 要自己去抓推文，所以多收一个 **`media_http`**（带代理的那个）——
        # 容器直连 x.com / pbs.twimg.com 全不通，见 birthday.py 的模块说明。
        self.birthday = BirthdayPoller(cfg, self.db, self.sender, self.media_http,
                                       library=self.library)
        self._birthday_task: Any = None
        # 誕生祭贺图：**带 cookie 直接打 X 的 GraphQL**（不是 RSS，也不是爬 HTML），
        # 所以同样收 `media_http`（带代理的那个）。多一个 `notifier` 是因为
        # **cookie 失效的症状是「群里今天没图」**，和「今天真没人画」长得一样，
        # 只能走私聊告警。见 birthday_art.py 的模块说明。
        #
        self.birthday_art = BirthdayArtPoller(
            cfg, self.db, self.sender, self.media_http, notifier=self.notifier,
            library=self.library,
        )
        self._birthday_art_task: Any = None
        # 图库保留期清理（ADR 0003）。贺图永久，预览 / 订阅 90 天。
        # 每天一次就够，而且**启动时先跑一次** —— 停机几天攒下的过期引用不用等。
        self._cleanup_task: Any = None
        # 群复读机：连着两个人发同样的话，跟着复读一句。**没有 task** ——
        # 它挂在 `on_group_message_create` 那条路上，每条非 @ 群消息都会喂进来
        # 一次（`consider` 有副作用，不能只在「看起来像复读」时才调）。
        # ⚠️ 传的是**取值函数**而不是群列表 —— 群开关现在存在库里（features.py），
        # `/set` 改完要下一拍就生效，构造时定死就做不到。
        # `REPEAT_ENABLED=false` 时永远给空（= 关闭），此时一次库都不查。
        self.repeater = repeat.Repeater(
            lambda: (
                features.groups_for(self.db, "repeat")
                if cfg.repeat_enabled
                else ()
            ),
            max_len=cfg.repeat_max_len,
            gap_sec=cfg.repeat_gap_sec,
        )

    # ---------- 生命周期 ----------

    async def on_ready(self) -> None:
        _log.info("机器人「%s」已上线", self.robot.name)
        # 复读机没有 task（它挂在群消息事件上），所以状态在这里自报 ——
        # 别的功能都是在自己的 `run()` 里报的。
        # ⚠️ 这行**必须有**：群列表存在库里（`group_features` 表），`.env` 里
        # 一个群参数都没有 —— 所以「为什么这个功能没推」只能靠这行判断。
        # 群数不对 = 开关的问题（`/set` 或库里那一行），不是代码的问题。
        _log.info(
            "功能开关：群列表来自数据库 —— %s",
            features.summarize(self.db),
        )
        if self.repeater.enabled:
            _log.info("复读：已启用，跟这 %d 个群", len(self.repeater.groups))
        else:
            _log.info("复读：库里一个群都没开（或 REPEAT_ENABLED=false），功能关闭")
        await self._announce_previews()
        # 轮询器和 botpy 共用一个事件循环，在 on_ready 里挂上去最省事
        if self._poller_task is None:
            self._poller_task = self.loop.create_task(
                self.poller.run(), name="qq_rss_bot.poller"
            )
        if self._ticket_task is None:
            self._ticket_task = self.loop.create_task(
                self.tickets.run(), name="qq_rss_bot.tickets"
            )
        if self._monday_task is None:
            self._monday_task = self.loop.create_task(
                self.monday.run(), name="qq_rss_bot.monday"
            )
        if self._morning_task is None:
            self._morning_task = self.loop.create_task(
                self.morning.run(), name="qq_rss_bot.morning"
            )
        if self._birthday_task is None:
            self._birthday_task = self.loop.create_task(
                self.birthday.run(), name="qq_rss_bot.birthday"
            )
        if self._birthday_art_task is None:
            self._birthday_art_task = self.loop.create_task(
                self.birthday_art.run(), name="qq_rss_bot.birthday_art"
            )
        if self._cleanup_task is None:
            self._cleanup_task = self.loop.create_task(
                self._library_cleanup_loop(), name="qq_rss_bot.imagelib_clean"
            )
        if self.autosub.enabled:
            # **先挂 poller 再补订阅**：补订阅万一抛异常，不能连累轮询器起不来。
            # on_ready 在断线重连时会再跑一次，所以 backfill 必须幂等 ——
            # 它确实是（只补零订阅的群，且 add_subscription 本身幂等）。
            try:
                added = self.autosub.backfill()
            except Exception:  # noqa: BLE001
                _log.exception("启动补订阅失败")
            else:
                # 路由写错时这里看不出问题（要等首拍抓取失败才暴露），
                # 所以把**解析后**的 URL 明确打出来，配的时候好核对。
                _log.info(
                    "进群自动订阅已启用：%s；本次给 %d 个群补上了",
                    self.autosub.url,
                    len(added),
                )
                if not added and not self.db.known_groups():
                    # 全新部署最常见的情形：库是空的，一个群都不认识。
                    # 不说清楚的话，「补了 0 个群」看起来像功能坏了。
                    _log.info(
                        "（库里还没有任何已知的群，所以没得补。"
                        "在群里 @ 一次机器人让平台把群号报过来，之后重启就会自动订上。）"
                    )

    async def _library_cleanup_loop(self) -> None:
        """每天清一次图库：贺图永久保留，预览 / 订阅的图过期就清（ADR 0003）。

        **同一份内容只删一次** —— 它可能既当过 90 天前的预览、又是一张贺图，
        那条贺图引用会让文件活下来（引用按 sha 挂，见 `imagelib.cleanup`）。
        """
        while True:
            try:
                self.library.cleanup(keep_days=self.cfg.imagelib_keep_days)
            except Exception:  # noqa: BLE001 —— 清理失败不能连累别的功能
                _log.exception("图库清理失败")
            await asyncio.sleep(24 * 3600)

    async def _announce_previews(self) -> None:
        """启动时把「分享预览到底能不能用」写进日志。

        加这个是因为线上真的踩到了：群里发了链接**毫无反应**，而日志里
        也**毫无痕迹** —— 失败路径当时全是 DEBUG，INFO 下什么都看不到，
        排查只能靠猜。启动先探一次，把结论钉在日志里。

        bilibili 走直连的 `rss_http`，YouTube / X 走带代理的 `media_http` ——
        两条路的连通性完全独立，所以分开报，别把结论互相套用。
        """
        if self.cfg.bilibili_preview:
            # 探测最多 6 秒，放在 on_ready 里等它 —— 启动慢几秒没关系，
            # 换来的是一条能直接判读的结论。
            _log.info("bilibili 预览连通性自检：%s", await bilibili.probe(self.rss_http))
        else:
            _log.info("bilibili 预览已关闭（BILIBILI_PREVIEW=false）")

        # AI 摘要：**不在这里发请求**，每次启动花一次调用太浪费。
        # 只报「配没配、提取器是哪个」—— 「为什么这个源没有摘要」的答案
        # 基本就在这两句里。
        _log.info(
            "AI 摘要：%s",
            ai.probe(
                self.cfg.ai_api_key,
                configured=bool(self.cfg.ai_api_key),
                fetch_article=self.cfg.ai_fetch_article,
                model=self.cfg.ai_model,
            ),
        )
        if self.cfg.ai_api_key:
            _log.info(
                "AI 摘要用 %s（%s），日上限 %d 次",
                self.cfg.ai_model,
                self.cfg.ai_base_url,
                self.cfg.ai_daily_limit,
            )

        if not (self.cfg.youtube_preview or self.cfg.twitter_preview):
            _log.info("YouTube / X 预览已关闭")
            return
        # 代理没配 = **必然全挂**，而且挂得很安静（静默不回消息）。直接说破，
        # 省得又去猜「为什么群里发了 YouTube 链接没反应」。
        if not self.cfg.media_proxy:
            _log.warning(
                "YouTube / X 预览已启用，但 **MEDIA_PROXY 没配** —— "
                "容器直连这两个站点全部超时，必然全部解析失败。"
                "在 .env 里配上（如 MEDIA_PROXY=http://192.168.0.63:6152）再重启。"
            )
        else:
            _log.info("YouTube / X 预览走代理 %s", self.cfg.media_proxy)
        result = await preview.probe(self.media_http)
        _log.info("YouTube / X 预览连通性自检：%s", result)
        if "❌" in result:
            # 排查顺序写在这里，省得每次都要重新推一遍。
            _log.warning(
                "自检没过。排查顺序：① 代理本身通不通；"
                "② **代理若做 MITM**（Surge 就是），要用 MEDIA_CA_BUNDLE 指到它的"
                "根证书（或 MEDIA_INSECURE_TLS=true 兜底）—— 那个症状很有迷惑性："
                "**图片能下、接口全挂**，因为 MITM 通常只重签 API 域名，"
                "图床域名没碰。原始报错见上面 preview: 开头的 INFO 行。"
            )

    async def close(self) -> None:
        self.poller.stop()
        if self._poller_task is not None:
            self._poller_task.cancel()
        self.tickets.stop()
        if self._ticket_task is not None:
            self._ticket_task.cancel()
        self.monday.stop()
        if self._monday_task is not None:
            self._monday_task.cancel()
        self.morning.stop()
        if self._morning_task is not None:
            self._morning_task.cancel()
        self.birthday.stop()
        if self._birthday_task is not None:
            self._birthday_task.cancel()
        self.birthday_art.stop()
        if self._birthday_art_task is not None:
            self._birthday_art_task.cancel()
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
        self.imgsrv.stop()
        await self.rss_http.aclose()
        await self.media_http.aclose()
        self.db.close()
        await super().close()

    # ---------- 群事件 ----------

    async def on_group_add_robot(self, event) -> None:
        """机器人被拉进群。

        这是拿到 group_openid 的最佳时机 —— 主动推送必须要它，
        而且它不是群号，之前没记录过就只能等有人在群里 @ 一次。
        """
        group_openid = getattr(event, "group_openid", None)
        if not group_openid:
            _log.warning("group_add_robot 事件缺少 group_openid：%r", event)
            return
        self.db.touch_group(group_openid)
        _log.info("已记录新群 %s", group_openid)
        try:
            created = await self.autosub.on_join(group_openid)
        except Exception:  # noqa: BLE001 —— 自动订阅失败不该让进群事件炸掉
            _log.exception("群 %s 自动订阅失败", group_openid)
        else:
            if created:
                _log.info("群 %s 已自动订阅 %s", group_openid, self.autosub.route)

    async def on_group_del_robot(self, event) -> None:
        group_openid = getattr(event, "group_openid", None)
        if not group_openid:
            return
        # 连 groups 那一行一起删 —— 不然下次启动的补订阅会把这个群
        # 当成「零订阅的新群」重新订上，然后一直往一个机器人已经不在的群里推。
        removed = self.db.forget_group(group_openid)
        _log.info("已退出群 %s，清掉 %d 个订阅并忘掉该群", group_openid, removed)

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        """老协议：只有被 @ 时平台才推，所以进来就一定是在 @ 机器人。"""
        await self._handle_group_message(message)

    async def on_group_message_create(self, message: GroupMessage) -> None:
        """新协议（2026）：平台推的是**全量群消息**，连闲聊都推。

        所以必须自己筛 @，否则群里任何人说话都会触发机器人。
        见 compat.py 了解为什么需要单独解析这个事件。

        非 @ 的消息里**分享的 bilibili 链接要回一张卡片** —— 见
        `_preview_bilibili`。这是全量推送白送的能力（旧协议收不到这些消息）。
        """
        bot_name = getattr(self.robot, "name", None)
        if not is_bot_mentioned(message.content, message.mentions, bot_name):
            # 依次问两个预览。bilibili 在前 —— 它有四种分享形态（含没有链接
            # 的小程序卡片），而 YouTube/X 只认纯文本里的链接，顺序不影响结果。
            previewed = (
                await self._preview_bilibili(message)
                or await self._preview_media(message)
            )
            # 复读机**每条都要喂**（`consider` 有副作用，只在「看起来像复读」
            # 时才调的话状态里的「上一条」会错位）。预览处理过的传 skip=True。
            repeated = await self._maybe_repeat(message, skip=previewed)
            # 只有真的没做任何事才记「忽略」——这行日志是当初发现
            # 「平台在推全量群消息」的线索，语义别改。
            if not (previewed or repeated):
                _log.debug("忽略非 @ 消息: %r", message.content)
            return
        await self._handle_group_message(message)

    async def _handle_group_message(self, message: GroupMessage) -> None:
        self.db.touch_group(message.group_openid)
        text = strip_mention(message.content, getattr(self.robot, "name", None))
        # 把角色一起记下来：普通成员的实际取值目前还没实测到，
        # 这行日志能直接看出来（补 ADMIN_ROLES 时用得上）
        role = getattr(message, "member_role", None)
        _log.info("群 %s 指令: %r（角色=%r）", message.group_openid, text, role)

        # 顺手记下发起人的 openid —— 出错要私聊他，而 openid 只能从事件里拿
        # （它不是 QQ 号）。群消息里的 member_openid 和私聊的 openid 是同一个值。
        # **只记管理员**：普通成员发个 /list 不该收到机器人的错误私聊。
        # 角色拿不到时宁可不记 —— 通知少了看得见（日志会提示没有可通知对象），
        # 给无关的人发私聊则是打扰。
        #
        # 注意这条登记只在**没配 NOTIFY_OPENIDS** 时才有意义：配了白名单的话，
        # C2CNotifier 直接读名单、根本不看这张表（见 notify.C2CNotifier.targets）。
        member_openid = getattr(getattr(message, "author", None), "member_openid", None)
        if member_openid and role:
            if (role or "").strip().lower() in ADMIN_ROLES:
                self.db.set_notify_target(message.group_openid, member_openid)
            elif member_openid in self.db.notify_targets(message.group_openid):
                # 被撤了管理员还留在登记表里，就会继续收到私聊 —— 他下次说话时摘掉。
                # 只在角色**明确**不是管理员时才摘；role 拿不到就什么都不做，
                # 和「角色拿不到时放行」的全局约定一致。
                # 配了 NOTIFY_OPENIDS 时登记表本来就不参与投递，这段是给默认路径兜底的。
                _log.info("群 %s 摘掉已非管理员的通知对象 %s", message.group_openid, member_openid[:8])
                self.db.remove_notify_target(message.group_openid, member_openid)
        reply, reply_card = self._make_repliers(message)

        # 「不发」：人工否决预览，挡下这一条不进正式群。
        #
        # **必须在指令分发之前** —— 交给 `commands.handle` 的话会被当成未知指令，
        # 回一句帮助文本，你根本看不出到底挡没挡住。
        #
        # 只认管理员，且**只在预览窗口开着时才生效**（`veto_current` 负责判断）。
        # 窗口外喊「不发」返回 False，回一句说明 —— 不能因为你闲聊里带了这两个字，
        # 下一条正式推送就莫名其妙被吞掉。
        if (role or "").strip().lower() in ADMIN_ROLES and "不发" in text:
            # **两个轮询器都要问**，而且不能短路 —— RSS 和抽票各有自己的预览
            # 窗口，只问一个的话正在预览的是抽票、你喊「不发」却回「什么都没挡到」。
            rss_hit = self.poller.veto_current()
            ticket_hit = self.tickets.veto_current()
            if rss_hit or ticket_hit:
                _log.info(
                    "群 %s 喊「不发」，本条不进正式群（%s）",
                    message.group_openid,
                    "抽票" if ticket_hit else "RSS",
                )
                await reply("收到，这一条不发到正式群。")
            else:
                await reply("现在没有待确认的预览，什么都没挡到。")
            return

        ctx = CommandContext(
            group_openid=message.group_openid,
            reply=reply,
            member_role=role,
            reply_card=reply_card,
        )
        await self.commands.handle(ctx, text)

    # ---------- 内部工具 ----------

    async def _maybe_repeat(self, message: GroupMessage, *, skip: bool) -> bool:
        """群复读机：连着两个人发同样的话，跟着复读一句。返回是否**真跟了**。

        走**被动回复**（和预览同一条路）—— 不占主动消息配额，也不需要群里开
        「机器人主动在群聊内发言」。代价是客户端会把它渲染成**引用回复**，
        而不是一条光秃秃的复读。想改成普通消息就换 `self.sender.send_text`。

        ⚠️ 这里**新建**了一个 `_make_repliers`，`msg_seq` 从 1 开始。这只有在
        `skip=False`（没有任何预览回过这条消息）时才安全 —— 否则会和预览撞
        seq，后发的那条被**静默丢弃**。所以**别在 skip=True 的分支里发东西**。
        """
        if not self.repeater.enabled:
            return False
        sender = getattr(getattr(message, "author", None), "member_openid", "") or ""
        body = self.repeater.consider(
            message.group_openid,
            message.content or "",
            sender,
            now=time.monotonic(),
            skip=skip,
        )
        if body is None:
            return False
        reply, _ = self._make_repliers(message)
        try:
            await reply(body)
        except Exception as exc:  # noqa: BLE001
            # 复读失败**只记日志**，不往上抛 —— 它是个锦上添花的群功能，
            # 而这条路上还跑着预览和指令，不能因为复读发不出去就影响它们。
            _log.warning("复读：%s 发送失败：%s", message.group_openid[:8], exc)
            return False
        _log.info("复读：%s → %r", message.group_openid[:8], body[:30])
        return True

    async def _preview_bilibili(self, message: GroupMessage) -> bool:
        """非 @ 的群消息里如果有 bilibili 链接，回一张带封面和数据的卡片。

        返回是否**真的回了**（调用方据此决定要不要记「忽略非 @ 消息」）。

        **全程静默**：解析不出来就什么都不发。群里没人希望机器人因为认不出
        链接而回一句报错。异常也全在这里吃掉 —— 冒到 botpy 的 `_run_event`
        会打成 on_error，而这条路上的失败根本不值得报警。

        走**被动回复**：`msg_id` 就在手上，不占主动消息配额（配额要留给推送），
        也不需要群里开「机器人主动在群聊内发言」。
        """
        if not self.cfg.bilibili_preview:
            return False
        try:
            target = bilibili.find_target(
                message.content or "", getattr(message, "ark_data", None)
            )
            if target is None:
                # 看着像 bilibili 分享、却没认出链接 —— 这条必须留痕，
                # 否则和「消息压根没到机器人」长得一模一样，没法区分。
                # 只在带 bilibili 字样的消息上触发，不会刷日志。
                if bilibili.BILI_HINT.search(message.content or ""):
                    _log.info(
                        "群 %s：消息里有 bilibili 字样但没认出链接：%r",
                        message.group_openid,
                        (message.content or "")[:120],
                    )
                return False
            _log.info("群 %s：认出 bilibili 分享 %r", message.group_openid, target)
            data = await bilibili.lookup(self.rss_http, target)
            if data is None:
                # **这条必须看得见。** 具体原因由 bilibili.lookup 内部按 INFO 记，
                # 以前这些全是 DEBUG —— 结果线上「发了没反应」在日志里毫无痕迹。
                _log.warning(
                    "群 %s：认出了 bilibili 分享但没解析出视频，不会回消息",
                    message.group_openid,
                )
                return False
            # 非 @ 的消息不会有别人回，所以这条必定消耗 msg_seq=1，没有撞号风险
            _reply, reply_card = self._make_repliers(message)
            # 封面收进图库再用（ticket #13）。**失败只降级这一张** ——
            # `mirror` 原样返回第三方地址，卡片照发。
            cover = bilibili.cover_url(data.get("pic"))
            if cover and self.cfg.mirror_images:
                cover = await imagelib.mirror(
                    self.library, self.rss_http, cover,
                    kind=imagelib.KIND_PREVIEW,
                    event_date=datetime.now().date().isoformat(),
                    source_url=cover,
                    page_url=f"https://www.bilibili.com/video/{data.get('bvid', '')}",
                    source_id=str(data.get("bvid") or ""),
                )
            await reply_card(bilibili.render(data, cover=cover))
        except Exception:  # noqa: BLE001 —— 解析和发消息什么都可能抛
            # 对**群里**静默，但日志要留痕：没有这个栈，线上出问题只能靠猜
            _log.warning("bilibili 预览失败", exc_info=True)
            return False
        _log.info(
            "群 %s 分享了 bilibili %s，已回卡片", message.group_openid, data.get("bvid")
        )
        return True

    async def _recall_share(self, message: GroupMessage) -> str:
        """撤回触发了预览的那条原消息（内容审核拒绝时用）。

        返回**一句给私聊告警用的结果**，不抛异常 —— 撤回失败不该连累告警本身。

        ⚠️ 权限（官方文档，2026-10-23）：机器人是**群管理员**才能撤回普通群成员
        的消息；只是普通成员时只能撤回自己的。消息超过 **2 分钟**不可撤回。
        群主发的消息能否被管理员机器人撤回，**文档没有明确保证**，线上实测为准。
        """
        if not self.cfg.recall_blocked_share:
            return "自动撤回已关闭"
        try:
            await self.api.delete_group_message(message.group_openid, message.id)
        except Exception as exc:  # noqa: BLE001 —— 撤回失败只影响这一句话
            reason = _recall_reason(exc)
            _log.warning(
                "撤回原分享消息失败（群 %s，消息 %s）：%s",
                message.group_openid, message.id, reason,
            )
            return f"原消息未能撤回（{reason}）"
        _log.info(
            "已撤回被内容审核拦下的原分享消息（群 %s，消息 %s）",
            message.group_openid, message.id,
        )
        return "原消息已自动撤回"

    async def _preview_media(self, message: GroupMessage) -> bool:
        """非 @ 的群消息里有 YouTube / X 链接 → 回一条带封面的卡片。

        返回是否**真的回了**（调用方据此决定要不要记「忽略非 @ 消息」）。

        和 bilibili 那条路的关键区别在**发什么形态**：bilibili 走 markdown
        卡片、图由 QQ 自己去 i1.hdslb.com 下载；这两个站点的图 QQ 拉不到
        （实测 `850027 富媒体文件上传超时`），所以封面走「我们自己下载 →
        base64 上传」，发**富媒体消息**。拿不到封面就退回 markdown 卡片
        （没有图，但文字内容一样全）。

        同样**全程静默**：解析不出来就什么都不发，异常全在这里吃掉 ——
        冒到 botpy 的 `_run_event` 会打成 on_error，而这条路上的失败不值得报警。
        """
        try:
            target = preview.find_target(message.content or "")
            if target is None:
                return False
            if target.site == "youtube" and not self.cfg.youtube_preview:
                return False
            if target.site == "twitter" and not self.cfg.twitter_preview:
                return False

            _log.info("群 %s：认出 %s 分享 %s（消息 %s）",
                      message.group_openid, target.site, target.vid, message.id)
            card = await preview.lookup(self.media_http, target)
            if card is None:
                # **这条必须看得见。** 具体原因由 preview.lookup 内部按 INFO 记。
                _log.warning(
                    "群 %s：认出了 %s 分享但没解析出来，不会回消息"
                    "（多半是代理没配或不通，对照启动日志的自检结论）",
                    message.group_openid,
                    target.site,
                )
                return False

            _reply, reply_card = self._make_repliers(message)
            # **一条 markdown 带全部配图**（ticket #9）。以前是「首条带正文 +
            # 每图一条」，1+N 条消息：刷屏，还吃掉 N+1 份消息额度。
            #
            # ⚠️ 图必须先入图库再渲染 —— markdown 的 `![](url)` 是 **QQ 的服务器**
            # 去下载的，而它在大陆机房够不到 i.ytimg.com / pbs.twimg.com（实测）。
            # 走图库之后，图是我们自己托管的，也顺带摆脱了 base64 那条 3.5 MB 上限。
            urls = card.covers if self.cfg.preview_multi_image else card.covers[:1]
            # 下载并发（走代理，4 张串行要十几秒）；入库是本地写盘，很快。
            raws = await asyncio.gather(
                *(preview.fetch_image(self.media_http, u, max_bytes=imgsrv.MAX_BYTES)
                  for u in urls)
            )
            today = datetime.now().date().isoformat()
            images: list[tuple[str, int, int]] = []  # (图库地址, 宽, 高)
            failed: list[str] = []
            cover_bytes = 0
            for url, raw in zip(urls, raws):
                if not raw:
                    failed.append(url)  # 下载失败 / 超体积上限
                    continue
                ref = self.library.store(
                    raw,
                    kind=imagelib.KIND_PREVIEW,
                    event_date=today,
                    source_url=url,
                    page_url=card.url,
                    source_id=target.vid,
                )
                if ref is None:
                    failed.append(url)  # 超过图库的体积上限
                    continue
                cover_bytes += len(raw)
                images.append((ref.url, ref.width, ref.height))

            blocked = False
            recall = ""
            fallback = ""
            try:
                await reply_card(preview.render_markdown(card, images))
            except Exception as exc:  # noqa: BLE001
                # **只对能观测到的错误降级。** 业务错误码非 0（比如没有 markdown
                # 模板权限 304036）会抛出来；退回纯文字 + 链接，而不是退回另一种
                # markdown —— markdown 本身没权限时，那个兜底也是白的。
                # 「发出去但用户看不见」观测不到，不做自动降级。
                #
                # ⚠️ **`消息内容违规` 是 QQ 的内容审核**，不是 X 的风控，也
                # 不是图没传上去 —— 图早就在我们图库里了。
                #
                # ⚠️ **这种情况整条不发，也不退回纯文字。** 用户明确要求：
                # 一半文字一半缺图的残条比不发更麻烦，还得人工去群里解释。
                # 纯文字能过审是因为审核对象是图，但发出去只会制造困惑。
                fallback = str(exc) or type(exc).__name__
                failed.extend([u for u in urls])
                if _content_blocked(exc):
                    blocked = True
                    _log.warning(
                        "群 %s 分享了 %s %s，被 QQ 内容审核拒绝，整条不发：%s",
                        message.group_openid, target.site, target.vid, fallback,
                        exc_info=True,
                    )
                    # 群里的原分享还留着一条裸链接 —— 按配置尝试撤回。
                    recall = await self._recall_share(message)
                else:
                    _log.warning("markdown 卡片发不出去，退回纯文字：%s", fallback,
                                 exc_info=True)
                    await _reply(preview.render_text(card))
        except Exception:  # noqa: BLE001 —— 解析和发消息什么都可能抛
            _log.warning("YouTube/X 预览失败", exc_info=True)
            return False

        if blocked:
            # 整条没发，所以不用 `image_failure_message` 那套「N 张没发出去」——
            # 用户要的是「这条被审核拦了」，不是「图没上传成功」。
            await self.notifier.notify(
                message.group_openid,
                f"⚠️ 分享未发送：QQ 内容审核拒绝（{fallback}）。整条没有发出。\n"
                f"来源：{card.site} 分享\n条目：{card.title}\n链接：{card.url}\n"
                f"{recall}。",
                key=f"{IMAGE_FAILURE_KEY_PREFIX}{message.group_openid}",
            )
        elif failed:
            # 和定时推送共用冷却键：两个入口同时出问题时报一次就够。
            why = f"markdown 卡片被拒：{fallback}" if fallback else "上传或发送失败"
            await self.notifier.notify(
                message.group_openid,
                image_failure_message(
                    f"{len(failed)} 张没发出去（{why}）",
                    f"来源：{card.site} 分享\n条目：{card.title}",
                    failed,
                ),
                key=f"{IMAGE_FAILURE_KEY_PREFIX}{message.group_openid}",
            )

        # **封面字节数必须记。** 推文的图分 orig / large / medium / small 四档，
        # 943KB 和 70KB 差 13 倍，而「已回图文卡片」四种情况长得一模一样 ——
        # 只报结果不报尺寸的话，「发出去的到底是不是原图」这个问题在日志里
        # **无法回答**（这个已经发生过一次，只能靠重发来补证据）。
        # URL 也带上：档位就写在 `?name=` 里，扫一眼就能确认。
        if blocked:
            # 上面那条 WARNING 已经带群号 / 推文 id / 原因，这里不再重复一行。
            return False

        # **封面字节数必须记。** 推文的图分 orig / large / medium / small 四档，
        # 943KB 和 70KB 差 13 倍，而「已回图文卡片」四种情况长得一模一样 ——
        # 只报结果不报尺寸的话，「发出去的到底是不是原图」这个问题在日志里
        # **无法回答**（这个已经发生过一次，只能靠重发来补证据）。
        # URL 也带上：档位就写在 `?name=` 里，扫一眼就能确认。
        detail = (
            f"，配图 {len(images)} 张 {cover_bytes // 1024}KB"
            if images and not fallback
            else (f"，已下载 {len(images)} 张 {cover_bytes // 1024}KB 但未发出"
                  if images else "")
        )
        _log.info(
            "群 %s 分享了 %s %s，已回%s%s",
            message.group_openid,
            target.site,
            target.vid,
            f"纯文字卡片（markdown 被拒：{fallback}）" if fallback
            else ("markdown 卡片" if images else "纯文字卡片"),
            detail,
        )
        return True

    def _make_repliers(self, message: GroupMessage):
        """被动回复的两个出口：纯文本 / 带按钮的 markdown。

        同一条 msg_id 回复多次时 msg_seq 必须递增，否则后续消息会被丢弃，
        所以这里自己维护计数器，而不是用 message.reply()。

        **两个闭包共用同一个计数器。** 各自 `count(1)` 的话，先发卡片再发文本
        会在同一个 msg_id 下撞 seq，后一条被**静默丢弃** —— 症状是「偶尔不回复」，
        日志里什么都看不到。所以卡片退回纯文本时消耗掉的那个 seq 是**故意**的。

        为什么卡片走被动回复而不是主动消息：`/rss` 永远是为了回应某条群消息，
        被动回复**不占主动消息配额**（配额要留给推送），也不需要群里开
        「机器人主动在群聊内发言」。代价是本条 msg_id 的 5 次回复额度里用掉 1 次。
        """
        seq = itertools.count(1)

        async def reply(text: str) -> None:
            await self.api.post_group_message(
                group_openid=message.group_openid,
                msg_type=0,
                msg_id=message.id,
                msg_seq=next(seq),
                content=text,
            )

        async def reply_card(markdown: str, keyboard: dict | None = None) -> None:
            # keyboard 为 None 时**整个参数都不传**：塞一个空的
            # {"content": {"rows": []}} 有被平台静默丢弃的风险，而 bilibili
            # 预览卡片本来就没有按钮。
            resp = await self.api.post_group_message(
                group_openid=message.group_openid,
                msg_type=2,
                msg_id=message.id,
                msg_seq=next(seq),
                markdown={"content": markdown},
                # 必须是外层结构 {"content": {...}}，见 menu.keyboard() 的说明
                **({"keyboard": keyboard} if keyboard is not None else {}),
            )
            # botpy 只在 **HTTP** 非 2xx 时抛异常，业务错误码是 200 带 code 回来的。
            # 不判这一下，304036 / 40034127「无 Markdown 模板权限」会被当成发送成功，
            # 用户那边一片空白，而 _rss 的纯文本兜底永远不会触发。
            code = resp.get("code") if isinstance(resp, dict) else None
            if code:
                raise RuntimeError(f"发送按钮卡片失败 code={code}: {resp}")

        return reply, reply_card

    async def on_c2c_message_create(self, message) -> None:
        """私聊。

        主要作用是让用户**建立会话** —— 主动 C2C 消息通常要求用户先跟机器人
        有过会话，否则发过去会被拒。顺便告诉他这个通道通了、以及他管着哪些群。

        这里是**被动回复**（带 msg_id），不占主动消息额度。
        """
        openid = getattr(getattr(message, "author", None), "user_openid", None)
        text = strip_mention(message.content, getattr(self.robot, "name", None))
        _log.info("私聊消息 from %s: %r", (openid or "")[:8], text)

        groups = [g for g in self.db.known_groups() if self.notifier.is_target(g, openid)]
        lines = ["RSS 机器人私聊通道正常，群里的出错通知会发到这里。"]
        if self.notifier.allowlist:
            # 白名单模式：收件人写死在 NOTIFY_OPENIDS 里，和群角色无关
            if openid and openid in self.notifier.allowlist:
                lines.append(f"当前是白名单模式：{len(groups)} 个群的通知都会发给你。")
            else:
                lines.append(
                    "当前是白名单模式（NOTIFY_OPENIDS），你的 openid 不在名单里，收不到通知。"
                )
        elif groups:
            lines.append(f"你管理着 {len(groups)} 个已订阅的群。")
        else:
            lines.append("你在群里用一次 /sub 或 /unsub，那个群的通知就会发到这里。")
        lines.append("（群里不会报错，只有你会收到这些提醒。）")
        try:
            await message.reply(content="\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            _log.warning("私聊回复失败: %r", exc)

    # ---------- 推送出口（FeedPoller 通过这两个方法发消息）----------

    async def send_text(self, group_openid: str, content: str) -> None:
        """主动发纯文字。

        不带 msg_id 就是主动消息，需要群里开启「机器人主动在群聊内发言」权限，
        否则接口会报错（错误码见日志）。
        """
        await self.api.post_group_message(
            group_openid=group_openid,
            msg_type=0,
            content=content,
        )

    async def send_rich(self, group_openid: str, content: str, image_url: str) -> None:
        """主动发图文，两步走。

        一步只能带一张图：官方 media 字段是单个对象，传数组会 400。
        url 给的是图片地址，由 QQ 服务器自己去下载，不需要我们先下再传。
        """
        media = await self.api.post_group_file(
            group_openid=group_openid,
            file_type=1,  # 1=图片(png/jpg) 2=视频mp4 3=语音silk
            url=image_url,
            # False = 只上传拿 file_info，不单独发一条；要「文字+图」必须走两步
            srv_send_msg=False,
        )
        file_info = media.get("file_info") if isinstance(media, dict) else None
        if not file_info:
            raise RuntimeError(f"上传图片未拿到 file_info: {media!r}")

        await self.api.post_group_message(
            group_openid=group_openid,
            msg_type=7,  # 7=富媒体（图文）
            content=content,
            media={"file_info": file_info},
        )

    async def send_markdown(self, group_openid: str, content: str) -> None:
        """主动发 markdown（`msg_type=2`）。

        多图走这条：`msg_type=7` 的 `MediaInfo` 只有一个 `file_info`，一条只能
        带一张图，多图就得连发 N 条（N 条消息、N 份配额、还刷屏）。markdown
        正文里可以放任意多个 `![](url)`，一条搞定。

        语法和坑（**尺寸提示必填、比例必须对、URL 绝不能带 `_cb`**）见
        `multiimage.py` 的模块说明。
        """
        resp = await self.api.post_group_message(
            group_openid=group_openid,
            msg_type=2,  # 2=markdown
            markdown={"content": content},
        )
        # 和 reply_card 同理：botpy 只在 **HTTP** 非 2xx 时抛异常，业务错误码是
        # 200 带 code 回来的。不判这一下，「无 Markdown 模板权限」会被当成发送
        # 成功，而多图那条路的兜底（退回连发）永远不会触发。
        code = resp.get("code") if isinstance(resp, dict) else None
        if code:
            raise RuntimeError(f"发送 markdown 失败 code={code}: {resp}")

    async def send_video(self, group_openid: str, path: Path) -> None:
        """主动发一条视频（`file_type=2` + `msg_type=7`）。

        和发图一样是**两步**：先上传拿 `file_info`（`srv_send_msg=False`），
        再用 `msg_type=7` 发出去。

        ⚠️ **没有 `content` 参数，是故意的。** `msg_type=7` 带**图片**时
        `content` 会渲染，带**视频**时**客户端整个丢掉** —— 实测过：接口回
        HTTP 200、`file_info` 也拿到了，群里就是只有视频、一个字都没有。
        `MediaInfo` 里也只有 `file_info`，没有 caption/title 字段可挂。
        所以「文案 + 视频」只能拆成**两条消息**（文案那条跟图走，见
        `send_markdown`），别想着在这里塞文字。

        **走 base64 而不是 URL**：URL 形态要 QQ 的服务器自己去下载，而它连
        `i.ytimg.com` 都拉不到（`850027`）；本地素材更没有 URL 可给。

        ⚠️ **10 MiB 是实测出来的硬上限**（`400 40093011`），和时长无关。
        这条硬闸放在这里，超了直接抛 —— 不把注定被拒的 body 递出去。
        完整那张实测表在 `monday.py` 的模块说明里。
        """
        raw = Path(path).read_bytes()
        if len(raw) > monday_module.MAX_VIDEO_BYTES:
            raise RuntimeError(
                f"视频 {Path(path).name} 有 {len(raw) / 1048576:.2f} MiB，"
                f"超过 {monday_module.MAX_VIDEO_BYTES / 1048576:.0f} MiB 上限"
                "（QQ 会回 400 40093011），不发"
            )
        media = await self.api.post_group_file(
            group_openid=group_openid,
            file_type=2,  # 2=视频 mp4（1=图片 3=语音silk）
            file_data=base64.b64encode(raw).decode(),
            srv_send_msg=False,
        )
        file_info = media.get("file_info") if isinstance(media, dict) else None
        if not file_info:
            raise RuntimeError(f"上传视频未拿到 file_info: {media!r}")

        resp = await self.api.post_group_message(
            group_openid=group_openid,
            msg_type=7,  # 7=富媒体
            content="",  # 视频这条 content 客户端不渲染，见上面；留空而不是留文案
            media={"file_info": file_info},
        )
        # 和 send_markdown 同理：botpy 只在 **HTTP** 非 2xx 时抛异常，业务错误码
        # 是 200 带 code 回来的。不判这一下，超限/没权限会被当成发送成功。
        code = resp.get("code") if isinstance(resp, dict) else None
        if code:
            raise RuntimeError(f"发送视频失败 code={code}: {resp}")

    async def send_voice(self, group_openid: str, path: Path) -> None:
        """主动发一条语音（`file_type=3` + `msg_type=7`）。

        和发图/发视频一样是**两步**：先上传拿 `file_info`（`srv_send_msg=False`），
        再用 `msg_type=7` 发出去。

        ⚠️ **没有 `content` 参数，是故意的** —— 用户 2026-10-19 定的「只发语音、
        不要字幕」。附带说明：语音那条的 `content` 到底渲不渲染**没测过**
        （0:45 那条测试语音带了文字，用户只确认了「听到了」），所以这里留空，
        只发语音气泡。

        ⚠️ **这道 silk 魔数闸是这条路上唯一的把关。** 2026-10-19 实测：`file_type=3`
        的上传接口**完全不校验格式** —— mp3 / wav / m4a、甚至 `\x02#!SILK_V3`
        后面接两千个零字节的**纯垃圾**，全都 HTTP 200 拿到 `file_info`。
        **「上传成功」在这里一点信息量都没有**，不自己挡就等着群里出现一个
        点不开的语音气泡。视频那条至少还有 10 MiB 的硬边界，语音这条连格式都不挡。

        ⚠️ **没有体积闸** —— 语音这边**没测出**上限（也不该有：一条 3 秒的
        silk 只有几 KB）。真撞上官方 body 上限的话错误会和视频那条一样明显。
        """
        raw = Path(path).read_bytes()
        if raw[:10] != morning_module.SILK_MAGIC:
            raise RuntimeError(
                f"{Path(path).name} 不是 silk（文件头 {raw[:10]!r}，"
                f"应为 {morning_module.SILK_MAGIC!r}），不发 —— "
                "上传接口对格式完全不校验，传上去就是个点不开的气泡"
            )
        media = await self.api.post_group_file(
            group_openid=group_openid,
            file_type=3,  # 3=语音 silk（1=图片 2=视频 mp4）
            file_data=base64.b64encode(raw).decode(),
            srv_send_msg=False,
        )
        file_info = media.get("file_info") if isinstance(media, dict) else None
        if not file_info:
            raise RuntimeError(f"上传语音未拿到 file_info: {media!r}")

        resp = await self.api.post_group_message(
            group_openid=group_openid,
            msg_type=7,  # 7=富媒体
            content="",  # 只发语音气泡，不要字幕 —— 见上面
            media={"file_info": file_info},
        )
        # 和 send_markdown / send_video 同理：botpy 只在 **HTTP** 非 2xx 时抛异常，
        # 业务错误码是 200 带 code 回来的。不判这一下，超限/没权限会被当成发送成功。
        code = resp.get("code") if isinstance(resp, dict) else None
        if code:
            raise RuntimeError(f"发送语音失败 code={code}: {resp}")

    async def upload_image(self, group_openid: str, image_url: str) -> bool:
        """让 QQ 去抓一张图，只拿 `file_info`、**不发消息**。返回抓没抓到。

        多图那条路的**事前检查**：markdown 的图裂了是静默的（发送接口照样
        HTTP 200，没有任何回执），事后无从发现，只能先问一次「QQ 够不够得到」。
        顺带这张图就进了 QQ 那边的缓存。
        """
        try:
            resp = await self.api.post_group_file(
                group_openid=group_openid,
                file_type=1,  # 1=图片
                url=image_url,
                srv_send_msg=False,  # 只上传，不单独发一条
            )
        except Exception:  # noqa: BLE001 —— 抓不到只是少一张图，不该连累推送
            _log.info("多图：QQ 抓不到这张图 %s", image_url, exc_info=True)
            return False
        return bool(isinstance(resp, dict) and resp.get("file_info"))

    # ---------- 私聊出口（C2CNotifier 用它发出错通知）----------

    async def send_c2c_text(self, openid: str, content: str) -> None:
        """主动私聊。**不占群配额** —— 走的是用户维度的额度。

        任何失败由 C2CNotifier 吞掉并记日志：通知发不出去不该影响主流程。
        """
        await self.api.post_c2c_message(openid=openid, msg_type=0, content=content)
