"""botpy 客户端：把 QQ 事件接到指令处理器和轮询器上。"""

from __future__ import annotations

import itertools
import logging
import os
from typing import Any

import botpy
import httpx
from botpy.message import GroupMessage

from . import bilibili
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
from .scheduler import FeedPoller

_log = logging.getLogger("qq_rss_bot.bot")

# RSSHub 和很多源站都会拦默认 UA
_USER_AGENT = "qq-rss-bot/0.1 (+https://github.com/DIYgod/RSSHub)"


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
        self.commands = CommandHandler(cfg, self.db, self.rss_http, self.sender)
        # 进群自动订阅。cfg.auto_subscribe_feed 留空时 enabled 为假，整个功能关掉。
        self.autosub = AutoSubscriber(cfg, self.db, self.sender)
        self.poller = FeedPoller(
            cfg,
            self.db,
            self.rss_http,
            self.sender,
            notifier=self.notifier,
            quota=self.sender,
        )
        self._poller_task: Any = None

    # ---------- 生命周期 ----------

    async def on_ready(self) -> None:
        _log.info("机器人「%s」已上线", self.robot.name)
        await self._announce_bilibili()
        # 轮询器和 botpy 共用一个事件循环，在 on_ready 里挂上去最省事
        if self._poller_task is None:
            self._poller_task = self.loop.create_task(
                self.poller.run(), name="qq_rss_bot.poller"
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

    async def _announce_bilibili(self) -> None:
        """启动时把「bilibili 预览到底能不能用」写进日志。

        加这个是因为线上真的踩到了：群里发了链接**毫无反应**，而日志里
        也**毫无痕迹** —— 失败路径当时全是 DEBUG，INFO 下什么都看不到，
        排查只能靠猜。启动先探一次，把结论钉在日志里。

        顺带把出站代理环境变量打出来。容器的网络和宿主机不是一回事，
        实测宿主机上那个 Python 的 urllib 会莫名走 Surge，而容器不会 ——
        所以别拿宿主机的结果推断容器，看这里的实测输出。
        """
        if not self.cfg.bilibili_preview:
            _log.info("bilibili 预览已关闭（BILIBILI_PREVIEW=false）")
            return
        proxied = sorted(k for k in os.environ if k.lower().endswith("_proxy"))
        _log.info(
            "bilibili 预览已启用；出站代理环境变量 %s",
            {k: os.environ[k] for k in proxied} or "（无）",
        )
        # 探测最多 6 秒，放在 on_ready 里等它 —— 启动慢几秒没关系，
        # 换来的是一条能直接判读的结论。
        _log.info("bilibili 预览连通性自检：%s", await bilibili.probe(self.rss_http))

    async def close(self) -> None:
        self.poller.stop()
        if self._poller_task is not None:
            self._poller_task.cancel()
        await self.rss_http.aclose()
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
            # 只有真的没做任何事才记「忽略」——这行日志是当初发现
            # 「平台在推全量群消息」的线索，语义别改。
            if not await self._preview_bilibili(message):
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
        ctx = CommandContext(
            group_openid=message.group_openid,
            reply=reply,
            member_role=role,
            reply_card=reply_card,
        )
        await self.commands.handle(ctx, text)

    # ---------- 内部工具 ----------

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
            await reply_card(bilibili.render(data))
        except Exception:  # noqa: BLE001 —— 解析和发消息什么都可能抛
            # 对**群里**静默，但日志要留痕：没有这个栈，线上出问题只能靠猜
            _log.warning("bilibili 预览失败", exc_info=True)
            return False
        _log.info(
            "群 %s 分享了 bilibili %s，已回卡片", message.group_openid, data.get("bvid")
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

    # ---------- 私聊出口（C2CNotifier 用它发出错通知）----------

    async def send_c2c_text(self, openid: str, content: str) -> None:
        """主动私聊。**不占群配额** —— 走的是用户维度的额度。

        任何失败由 C2CNotifier 吞掉并记日志：通知发不出去不该影响主流程。
        """
        await self.api.post_c2c_message(openid=openid, msg_type=0, content=content)
