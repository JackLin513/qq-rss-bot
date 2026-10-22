"""自建图床：把图存到本地，用一个**随机路径段**对外提供，专门喂给 QQ 的 markdown。

## 为什么需要它

markdown 里的 `![](url)` 不是我们下载的 —— 是 **QQ 的服务器去抓**。
实测预热两遍（45 秒 / 180 秒）群里**都没有图**，根因是 `pbs.twimg.com`
（= X / Twitter）在**大陆机房根本够不到**。`imas_ch` 的官方 CMS 图是能显示的，
差别就在这儿：**不是格式问题、不是预热问题，是 QQ 抓不到那个域名。**

所以诞祭贺图的图必须**我们自己托管一份**，给 QQ 一个它抓得到的 URL。

## 形态

- 每张图一个 **32 位十六进制随机路径段**：`http://<host>:<port>/<token>/<file>.jpg`。
  猜不到、也不能靠枚举翻别人的图（目录列表一律不给）。
- 只认 `[0-9a-f]{32}` 的路径段 + 白名单扩展名，其它一律 404 —— 路径穿越在正则
  这一层就断了，不用再拼 `..` 检查。
- **TTL 自动清**（默认 7 天）：贺图发完就没人看了，不该在 NAS 上常驻。
- 只读：除了 `publish()` 往目录里写，HTTP 侧没有任何写入口。

## ⚠️ 对公网开一个端口，别把它做成通用 web 服务

这里只服务「随机名图片」这一件事。加目录列表、加别的路由 = 给自己开洞。
要加功能先问一句「这需不需要对公网暴露」。
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

_log = logging.getLogger("qq_rss_bot.imgsrv")

DEFAULT_PORT = 18080
DEFAULT_TTL_DAYS = 7

# 路径段只认 32 位小写十六进制；文件名只认安全字符。
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_EXT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

MAX_BYTES = 12 * 1024 * 1024


def suffix_of(raw: bytes) -> str:
    """按魔数定扩展名 —— 推特的图不一定是 jpg，且 URL 上的后缀经常骗人。"""
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


class _Handler(BaseHTTPRequestHandler):
    server_version = "qq-rss-bot-imgsrv"
    sys_version = ""
    root = Path(".")

    def do_GET(self) -> None:  # noqa: N802
        self._serve(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(send_body=False)

    def _serve(self, *, send_body: bool) -> None:
        path = urlsplit(self.path).path
        parts = [p for p in path.split("/") if p]
        if len(parts) != 2 or not _TOKEN_RE.match(parts[0]) or not _NAME_RE.match(parts[1]):
            self._deny(404, "not found")
            return
        token, name = parts
        ext = os.path.splitext(name)[1].lower()
        ctype = _EXT_TYPES.get(ext)
        if ctype is None:
            self._deny(404, "not found")
            return
        full = (self.root / token / name).resolve()
        try:
            full.relative_to(self.root.resolve())
        except ValueError:
            self._deny(404, "not found")
            return
        try:
            size = full.stat().st_size
        except OSError:
            self._deny(404, "not found")
            return
        if size > MAX_BYTES:
            self._deny(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        # QQ 的图代理只抓一次；给它一个长缓存，我们的地址本来也是不可变的。
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        if not send_body:
            return
        try:
            with full.open("rb") as fh:
                while chunk := fh.read(64 * 1024):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _deny(self, code: int, text: str) -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """**每次取图都留一行 INFO** —— 这是自建图床唯一换得来的东西。

        「群里没图」的时候，第一个要回答的问题是**QQ 到底有没有来抓**：
        日志里有这一行 = 抓了但渲染/尺寸/缓存有问题；没有 = 它根本没找到我们
        （转发、DDNS、或 QQ 侧拒绝）。走 nginx 就只能翻它的 access.log 了。

        路径只打 token 前 8 位 —— 完整 token 是这张图的钥匙，没必要进日志。
        """
        short = urlsplit(self.path).path
        parts = [p for p in short.split("/") if p]
        where = f"/{parts[0][:8]}…/{parts[1]}" if len(parts) == 2 else short
        _log.info("imgsrv %s %s %s %s", self.address_string(), where, code, size)

    def log_message(self, fmt: str, *args) -> None:
        # 解析失败之类（400/414）和请求结束那行分开处理：前者是罕见事件，
        # 但**也不该 DEBUG** —— 挖掘类请求正是「图不显示」的一种成因。
        _log.info("imgsrv %s - %s", self.address_string(), fmt % args)


class ImageServer:
    """把图写进目录 + 在后台线程开一个只读 HTTP 服务。

    `base` 是**对外的**根 URL（含端口），例如 `http://www.example.com:18080`。
    为空时 `enabled` 为假：`publish()` 返回 `None`，调用方自己回退。
    """

    def __init__(
        self,
        root: str | Path,
        base: str,
        *,
        port: int = DEFAULT_PORT,
        ttl_days: int = DEFAULT_TTL_DAYS,
    ) -> None:
        self.root = Path(root)
        self.base = base.rstrip("/")
        self.port = port
        self.ttl_sec = max(1, ttl_days) * 86400
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._last_cleanup = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.base)

    # ---------- 写入 ----------

    def publish(self, raw: bytes, stem: str) -> str | None:
        """存一张图，返回**公网 URL**；没配 `base` 时返回 None。

        `stem` 只用来让文件名可读（推文 id），不参与寻址 —— 寻址靠随机 token。
        """
        if not self.enabled:
            return None
        if len(raw) > MAX_BYTES:
            _log.warning("imgsrv：%s 有 %d 字节，超过 %d，不收",
                         stem, len(raw), MAX_BYTES)
            return None
        token = secrets.token_hex(16)
        safe_stem = re.sub(r"[^A-Za-z0-9._-]", "", stem)[:48] or "img"
        name = safe_stem + suffix_of(raw)
        d = self.root / token
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(raw)
        return f"{self.base}/{token}/{name}"

    def cleanup(self, *, force: bool = False) -> int:
        """删掉超过 TTL 的 token 目录，返回删了几个。

        按**目录 mtime** 判 —— 目录里只有一个文件，两者同义。
        """
        now = time.time()
        if not force and now - self._last_cleanup < 3600:
            return 0
        self._last_cleanup = now
        if not self.root.is_dir():
            return 0
        n = 0
        for d in self.root.iterdir():
            if not d.is_dir() or not _TOKEN_RE.match(d.name):
                continue
            try:
                if now - d.stat().st_mtime > self.ttl_sec:
                    for f in d.iterdir():
                        f.unlink(missing_ok=True)
                    d.rmdir()
                    n += 1
            except OSError:
                continue
        if n:
            _log.info("imgsrv：清掉 %d 个过期目录", n)
        return n

    # ---------- 服务 ----------

    def start(self) -> bool:
        """开端口。**端口被占 / 没权限时不抛异常** —— 图床挂了不该拖垮机器人。"""
        if not self.enabled or self._httpd is not None:
            return self._httpd is not None
        self.root.mkdir(parents=True, exist_ok=True)
        self.cleanup(force=True)

        handler = type("_BoundHandler", (_Handler,), {"root": self.root})
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        except OSError as exc:
            _log.warning("imgsrv：端口 %d 开不了（%s）—— 图床没起来，"
                         "贺图的 markdown 图会退回不可达的原始 URL", self.port, exc)
            return False
        httpd.daemon_threads = True
        self.port = httpd.server_address[1]  # 传 0 时由内核分配（测试用）
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="imgsrv", daemon=True)
        self._thread.start()
        _log.info("imgsrv：已在 :%d 提供服务，对外地址 %s", self.port, self.base)
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
