"""把 DNSPod 上的 A 记录更新成**当前真实的公网 IP**。

    uv run python scripts/ddns_update.py              # 查一次，不一致就更新
    uv run python scripts/ddns_update.py --dry-run    # 只看，不写
    uv run python scripts/ddns_update.py --show       # 打印现在的记录

## 为什么不用 DSM 自带的 DDNS

2026-10-22 排查：NAS 有两条宽带 —— 公网线接路由器（LAN 2），另一条 PPPoE
是 CGNAT。DSM 的 DDNS：

- 选「自动」→ 它去外部服务问 IP，**请求被透明代理接管**，拿回来的是代理出口
  （日本/香港），不是家里宽带；
- 选「局域网 2」→ 它直接把网卡地址 `192.168.0.6` 写上去了。

两条都不对。这个脚本绕开这两种行为，自己查一个**没被代理**的回显服务。

## `.env` 里要有

    DNSPOD_TOKEN_ID=123456
    DNSPOD_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    DDNS_DOMAIN=jacklin513.top
    DDNS_SUBDOMAIN=www

token 在 DNSPod 控制台 → 用户中心 → 密钥管理 里建，**只给改 DNS 的权限**。
⚠️ 绝不提交、绝不打印 —— 日志里只出现长度。

## NAS 上怎么定时跑

DSM「控制面板 → 任务计划 → 新增 → 计划的任务 → 用户定义的脚本」，每 5 分钟：

    /usr/local/bin/docker exec qq-rss-bot-bot-1 \\
        python /app/scripts/ddns_update.py

（用任务计划以 root 跑，`JackLin` 本人没有 docker 权限。）
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://dnsapi.cn/"

# 回显服务要挑**没被透明代理接管**的 —— 实测 ip.3322.net / ipip.net 返回的是
# 真实电信地址，而 api.ipify.org 走代理、返回的是代理出口。
IP_SOURCES = (
    "http://ip.3322.net",
    "https://myip.ipip.net",
    "https://4.ipw.cn",
)
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _is_public(ip: str) -> bool:
    """`is_global` 一并挡掉私网 / 回环 / 链路本地 / CGNAT（100.64/10）。"""
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return ""


def _get(url: str, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "qq-rss-bot-ddns/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def public_ip() -> str:
    """多问几家、取票数最多的。

    ⚠️ **不要「拿到第一个就用」**：被代理接管的服务会返回代理出口
    （实测 api.ipify.org 返回日本/香港）。几家不一致时宁可报错不更新，
    也别把域名指到一个错的地址上。
    """
    votes: dict[str, int] = {}
    for url in IP_SOURCES:
        try:
            text = _get(url)
        except Exception:  # noqa: BLE001
            continue
        for ip in IP_RE.findall(text):
            if _is_public(ip):
                votes[ip] = votes.get(ip, 0) + 1
    if not votes:
        raise RuntimeError("一个回显服务都没拿到公网 IP（都超时或被代理接管了）")
    best, n = max(votes.items(), key=lambda kv: kv[1])
    if len(votes) > 1:
        print(f"⚠️ 回显服务不一致：{votes}")
        if n == 1:
            raise RuntimeError("回显服务互相矛盾，这次不更新（免得写错）")
    return best


def dnspod(token_id: str, token: str, action: str, **params: str) -> dict:
    body = urllib.parse.urlencode({
        "login_token": f"{token_id},{token}",
        "format": "json",
        "lang": "cn",
        **params,
    }).encode()
    req = urllib.request.Request(
        API + action, data=body,
        headers={"User-Agent": "qq-rss-bot-ddns/1.0 (ddns)", "Content-Type":
                 "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _records(token_id: str, token: str, domain: str) -> list[dict]:
    data = dnspod(token_id, token, "Record.List", domain=domain, offset="0", length="200")
    status = data.get("status") or {}
    if str(status.get("code")) != "1":
        raise RuntimeError(f"Record.List 失败：{status.get('code')} {status.get('message')}")
    return data.get("records") or []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="只打印，不调修改接口")
    ap.add_argument("--show", action="store_true", help="只列出现有记录")
    args = ap.parse_args()

    token_id = _env("DNSPOD_TOKEN_ID")
    token = _env("DNSPOD_TOKEN")
    domain = _env("DDNS_DOMAIN")
    sub = _env("DDNS_SUBDOMAIN") or "@"
    if not domain:
        print("❌ .env 里没有 DDNS_DOMAIN")
        return 2
    if not token_id or not token:
        print("❌ .env 里没有 DNSPOD_TOKEN_ID / DNSPOD_TOKEN")
        return 2
    print(f"域 {sub}.{domain}；token_id {len(token_id)} 字符、token {len(token)} 字符")

    records = _records(token_id, token, domain)
    mine = [r for r in records if r.get("name") == sub and r.get("type") == "A"]
    if args.show or not mine:
        print(f"现有记录 {len(records)} 条，其中 {sub} 的 A 记录 {len(mine)} 条：")
        for r in records:
            print(f"  {r.get('type'):<6} {r.get('name'):<12} → {r.get('value')}  "
                  f"(id={r.get('id')}, line={r.get('line')})")
        if not mine:
            print("⚠️ 没有 A 记录 —— 需要先在 DNSPod 后台建一条，或改成 Record.Create")
    if args.show:
        return 0

    try:
        ip = public_ip()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ {exc}")
        return 1
    print(f"当前公网 IP：{ip}")
    if not mine:
        return 1
    cur = mine[0].get("value")
    if cur == ip:
        print("✅ 记录已经是对的，不用改")
        return 0
    if args.dry_run:
        print(f"--dry-run：会把 {cur} 改成 {ip}（没写）")
        return 0

    data = dnspod(
        token_id, token, "Record.Modify",
        domain=domain, record_id=str(mine[0]["id"]), sub_domain=sub,
        record_type="A", record_line=mine[0].get("line") or "默认",
        value=ip, ttl="600",
    )
    status = data.get("status") or {}
    if str(status.get("code")) != "1":
        print(f"❌ Record.Modify 失败：{status.get('code')} {status.get('message')}")
        return 1
    print(f"✅ 已更新：{cur} → {ip}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
