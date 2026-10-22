"""图床：把**图库里已有的图**通过 HTTP 发出去，专门喂给 QQ 的 markdown。

## 只管传输，不管存储

存图、去重、索引都在 `imagelib.py`。这里只做一件事：把
`<库根>/blobs/<sha 前两位>/<sha>.<扩展名>` 映射成 `<base>/i/<sha>.<扩展名>`。

## 为什么非要有它

markdown 里的 `![](url)` 不是我们下载的 —— 是 **QQ 的服务器去抓**。
2026-10-22 实测：`pbs.twimg.com` 在**大陆机房根本够不到**，预热两遍群里的图
一张都没有；而 `idolmaster-official.jp` 的图一直显示得好。所以图得我们自己托管。

## 只读，而且窄

- 只认 `/i/<64 位小写十六进制>.<白名单扩展名>`，其它一律 404 —— 路径穿越在正则
  这一层就断了。
- 没有目录列表，没有任何写入口。
- **加目录列表、加别的路由 = 给公网开洞。** 要加功能先问「这需不需要对公网暴露」。

## 访问日志是这套方案最值钱的东西

「群里没图」时第一个要回答的是 **QQ 到底有没有来抓**：有这条日志 = 它抓到了
（问题在渲染），没有 = 它根本没找过来（转发 / DNS / URL 打错）。
⚠️ 来源 IP 会被 Docker 的网桥改写成一个固定值，所以**只能看出有没有人抓，
看不出是不是 QQ**。
"""

from __future__ import annotations

import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

_log = logging.getLogger("qq_rss_bot.imgsrv")

DEFAULT_PORT = 18080

# 文件名 = <sha256><ext>。sha 只认小写十六进制，长度锁死 64 —— 越界一律 404。
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

_EXT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# 单张图的上限。超过的不服务 —— 家宽上行和 QQ 的抓取都有成本。
MAX_BYTES = 12 * 1024 * 1024


def suffix_of(raw: bytes) -> str:
    """按魔数定扩展名 —— 源站的 URL 后缀经常骗人（推特尤其）。"""
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def mime_of(ext: str) -> str:
    return _EXT_TYPES.get(ext.lower(), "application/octet-stream")


class _Handler(BaseHTTPRequestHandler):
    server_version = "qq-rss-bot-imgsrv"
    sys_version = ""
    root = Path(".")

    def do_GET(self) -> None:  # noqa: N802
        self._serve(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(send_body=False)

    def _serve(self, *, send_body: bool) -> None:
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        # 只认 /i/<sha><ext> 这一个形态
        if len(parts) != 2 or parts[0] != "i":
            self._deny(404, "not found")
            return
        name = parts[1]
        stem, ext = os.path.splitext(name)
        ext = ext.lower()
        if not _SHA_RE.match(stem) or ext not in _EXT_TYPES:
            self._deny(404, "not found")
            return
        full = (self.root / stem[:2] / f"{stem}{ext}").resolve()
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
        self.send_header("Content-Type", _EXT_TYPES[ext])
        self.send_header("Content-Length", str(size))
        # 地址是不变的（内容哈希），让它长缓存。
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
        有这一行 = 抓了但渲染/尺寸/缓存有问题；没有 = 它根本没找到我们。
        """
        short = urlsplit(self.path).path
        parts = [p for p in short.split("/") if p]
        where = f"/{parts[1][:8]}…{os.path.splitext(parts[1])[1]}" if len(parts) == 2 else short
        _log.info("imgsrv %s %s %s %s", self.address_string(), where, code, size)

    def log_message(self, fmt: str, *args) -> None:
        # 解析失败之类（400/414）和请求结束那行分开处理：前者是罕见事件，
        # 但**也不该 DEBUG** —— 挖掘类请求正是「图不显示」的一种成因。
        _log.info("imgsrv %s - %s", self.address_string(), fmt % args)


class ImageServer:
    """后台线程里跑一个只读 HTTP 服务，把图库里的图发出去。"""

    def __init__(self, root: str | Path, base: str, *,
                 port: int = DEFAULT_PORT) -> None:
        self.root = Path(root)
        self.base = (base or "").rstrip("/")
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base)

    def start(self) -> bool:
        """开端口。**端口被占 / 没权限时不抛异常** —— 图床挂了不该拖垮机器人。"""
        if not self.enabled or self._httpd is not None:
            return self._httpd is not None
        self.root.mkdir(parents=True, exist_ok=True)
        handler = type("_BoundHandler", (_Handler,), {"root": self.root})
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        except OSError as exc:
            _log.warning("imgsrv：端口 %d 开不了（%s）—— 图床没起来，"
                         "markdown 里的图会抓不到", self.port, exc)
            return False
        httpd.daemon_threads = True
        self.port = httpd.server_address[1]  # 传 0 时由内核分配（测试用）
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="imgsrv", daemon=True)
        self._thread.start()
        _log.info("imgsrv：已在 :%d 提供服务，对外地址 %s/i/<sha>", self.port, self.base)
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
