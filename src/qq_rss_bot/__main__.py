"""入口：uv run qq-rss-bot"""

from __future__ import annotations

import logging

from .bot import RssBot
from .compat import install_new_protocol_support
from .config import load_config


def main() -> None:
    # force=True 不可省：import botpy 时 botpy/logging.py 已经 basicConfig 过一次，
    # 不覆盖的话这里的设置会被当成空操作丢弃，本项目的 INFO 日志会全部消失。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        force=True,
    )
    cfg = load_config()
    # 显式设置本包 logger 等级，不依赖 root 继承：
    # botpy 的 configure_logging 会遍历并改写它自己注册表里的 logger 等级。
    logging.getLogger("qq_rss_bot").setLevel(cfg.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # 必须在建 Client 之前装：解析表是在 ConnectionState 实例化时一次性构建的
    install_new_protocol_support()
    logging.getLogger("qq_rss_bot").info(
        "启动中：sandbox=%s rsshub=%s db=%s", cfg.is_sandbox, cfg.rsshub_base, cfg.db_path
    )
    RssBot(cfg).run(appid=cfg.appid, secret=cfg.secret)


if __name__ == "__main__":
    main()
