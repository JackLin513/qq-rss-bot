"""事件探针：把 botpy 派发的每个下行事件打出来。

用途：机器人「连上了但收不到消息 / 定时任务不跑」时，先用这个确认
到底是平台没推事件，还是事件推了但代码没接住。

跑法（会占用一次 WebSocket 连接，先停掉正式实例）：
    uv run python scripts/probe_events.py [持续秒数]
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    force=True,  # 见 CLAUDE.md：不加 force，本项目日志会被 botpy 的 basicConfig 吃掉
)

import botpy  # noqa: E402
from botpy import Client  # noqa: E402


def say(*args: object) -> None:
    print(">>>", *args, flush=True)


class Probe(Client):
    async def _async_setup_hook(self) -> None:
        await super()._async_setup_hook()
        say(f"连接建立，{DURATION}s 后自动退出")
        self.loop.create_task(self._exit_later())

    async def _exit_later(self) -> None:
        await asyncio.sleep(DURATION)
        say("到时退出")
        os._exit(0)

    async def on_ready(self) -> None:
        say("on_ready 触发了（RssBot 在这里启动轮询器）")

    async def on_group_at_message_create(self, message) -> None:
        say(f"群消息 group_openid={message.group_openid!r} content={message.content!r}")

    async def on_group_add_robot(self, event) -> None:
        say(f"被拉进群 group_openid={getattr(event, 'group_openid', None)!r}")

    async def on_group_del_robot(self, event) -> None:
        say(f"被移出群 group_openid={getattr(event, 'group_openid', None)!r}")


_orig_dispatch = Client.ws_dispatch


def _patched_dispatch(self, event, *args, **kwargs):
    say(f"ws_dispatch event={event!r} 处理器存在={hasattr(self, 'on_' + event)}")
    return _orig_dispatch(self, event, *args, **kwargs)


Client.ws_dispatch = _patched_dispatch

from qq_rss_bot.config import load_config  # noqa: E402

_cfg = load_config()
say(f"sandbox={_cfg.is_sandbox}  appid={_cfg.appid[:3]}***")
Probe(
    intents=botpy.Intents(public_messages=True),
    is_sandbox=_cfg.is_sandbox,
    log_level=logging.INFO,
).run(appid=_cfg.appid, secret=_cfg.secret)
