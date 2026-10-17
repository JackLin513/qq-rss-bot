"""入口：uv run qq-rss-bot"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .bot import RssBot
from .compat import install_new_protocol_support
from .config import Config, load_config

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


def setup_logging(cfg: Config) -> RotatingFileHandler | None:
    """装日志。返回文件 handler（没配 LOG_FILE 就是 None），方便测试断言。

    `force=True` 不可省：`import botpy` 时 `botpy/logging.py` 在模块级就
    `basicConfig()` 过一次，此后再调因 root 已有 handler 而**变成空操作**，
    本项目的 INFO 日志会全部被静默丢弃。

    **文件 handler 必须加在 `RssBot(...)` 构造之前**，且加在 root 上：
    botpy 的 `configure_logging` 会遍历它自己注册表里的 logger 改等级，
    所以我们要保证自己这份在任何 botpy 代码跑之前就已经挂好。
    """
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, force=True)
    if not cfg.log_file:
        return None
    # 容器场景下这份文件日志是**唯一**看得到的：`docker logs` 要 root，
    # 而群晖上非 root 连 /volume1/@docker 都进不去。路径指向挂载卷，
    # 宿主上直接 cat 就行。用轮转以免长期运行撑爆共享文件夹。
    log_path = Path(cfg.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logging.getLogger().addHandler(handler)
    return handler


def main() -> None:
    cfg = load_config()
    setup_logging(cfg)
    # 显式设置本包 logger 等级，不依赖 root 继承：
    # botpy 的 configure_logging 会遍历并改写它自己注册表里的 logger 等级。
    logging.getLogger("qq_rss_bot").setLevel(cfg.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # 必须在建 Client 之前装：解析表是在 ConnectionState 实例化时一次性构建的
    install_new_protocol_support()
    logging.getLogger("qq_rss_bot").info(
        "启动中：sandbox=%s rsshub=%s db=%s",
        cfg.is_sandbox,
        cfg.rsshub_base,
        cfg.db_path,
    )
    RssBot(cfg).run(appid=cfg.appid, secret=cfg.secret)


if __name__ == "__main__":
    main()
