"""入口：uv run qq-rss-bot"""

from __future__ import annotations

import logging
import subprocess
import time
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


def code_revision(pkg: Path) -> str | None:
    """正在跑的代码是哪个 commit。拿不到返回 `None`（调用方退回 mtime）。

    **三条路，按可靠性从高到低** —— 三个环境里能用的东西不一样：

    1. **git**（Mac 上 `uv run`）—— `git -C <仓库> rev-parse --short HEAD`，
       工作区脏就加 `-dirty`。
    2. **`data/DEPLOYED`**（群晖容器里）—— 容器**没装 git、也没挂 `.git`**
       （compose 只挂了 `./src` `./scripts` `./data`），所以这条是主路径。
       文件由 `scripts/deploy.sh` 部署成功后写下；`data/` 是挂载卷、又被
       gitignore，所以容器读得到，也不会弄脏仓库。
    3. 都没有 → `None`。

    ⚠️ 为什么非要有 SHA：**mtime 不够用了。** 2026-10-21 在 NAS 上做 initial
    checkout 时，`src/*.py` 的 mtime 被整棵树刷成同一秒 —— 从此时间戳分不清
    「我改了一行」和「git 重写了整棵树」。SHA 才能回答「这是哪个版本、能不能
    退回去」。
    """
    repo = pkg.parents[1]
    try:
        rev = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if rev.returncode == 0 and rev.stdout.strip():
            dirty = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain",
                 "--untracked-files=no"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return rev.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        line = (repo / "data" / "DEPLOYED").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return (line.splitlines()[0].strip() or None) if line else None


def code_identity() -> str:
    """报出「正在跑的是哪份代码」：包目录 + 版本 + 最新一次改动的时间。

    **这不是装饰。** 出过一次很难查的事故：镜像从早上 09:56 就没再重建过，
    而源码 13:34 就换过了 —— 连着三次「重建」实际都只是用旧镜像重开容器，
    日志看起来完全正常（`已上线`、`轮询启动` 一个不少），新代码一行都没生效，
    而且**没有任何地方提示**。这条日志就是那个提示。

    报的是包内 .py 的**最新** mtime（不是某个固定文件），所以改哪个模块都会
    变 —— 一眼就能看出「我刚改的那行到底进去了没有」：

      * 挂了 `./src` 卷（热更新）→ 这里就是你刚保存的时间
      * 跑的是烤进镜像的代码 → 这里是镜像构建时的时间

    两者对不上，就说明代码没生效，不用再去猜日志了。
    """
    pkg = Path(__file__).resolve().parent
    newest = max((f.stat().st_mtime for f in pkg.glob("*.py")), default=0.0)
    stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(newest)) if newest else "?"
    rev = code_revision(pkg)
    inside = f"{rev}，最新改动 {stamp}" if rev else f"最新改动 {stamp}"
    return f"{pkg}（{inside}）"


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
    logging.getLogger("qq_rss_bot").info("运行代码 %s", code_identity())
    RssBot(cfg).run(appid=cfg.appid, secret=cfg.secret)


if __name__ == "__main__":
    main()
