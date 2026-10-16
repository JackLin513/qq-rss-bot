#!/usr/bin/env bash
#
# 机器人的起停。
#
# 存在理由：macOS 上 Python 的可执行文件叫 `Python`（**大写**），所以
# `pgrep -f 'python -m qq_rss_bot'` 匹配不到 —— 这个坑已经造成过两次
# 「以为重启了、其实旧的还在跑」。两个实例同时连网关会**重复回复、
# 重复推送**，而且旧进程的日志还被新进程覆盖掉，现场基本没法还原。
# 本脚本统一用 `pgrep -if -- '-m qq_rss_bot'`。
#
# 用法：
#   scripts/botctl.sh start      后台启动，日志写 $BOT_LOG（默认 /tmp/qq-rss-bot.log）
#   scripts/botctl.sh stop       SIGTERM，最多等 10s，还没退才 SIGKILL
#   scripts/botctl.sh restart
#   scripts/botctl.sh status     列出所有实例（**多于 1 个就是出问题了**）
#   scripts/botctl.sh log [-f]   看日志（-f 跟随）
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

PY=".venv/bin/python"
LOG="${BOT_LOG:-/tmp/qq-rss-bot.log}"
# 必须带前导 `-m`：只匹配真实进程的命令行，不会误伤 grep、
# 编辑器、或者这个脚本自己（脚本的 cmdline 只是个路径）。
PATTERN='-m qq_rss_bot'

pids()  { pgrep -if -- "$PATTERN" 2>/dev/null || true; }
alive() { [ -n "$(pids)" ]; }

cmd_status() {
  local list n
  list="$(pids)"
  if [ -z "$list" ]; then
    echo "未运行"
    return 1
  fi
  n="$(printf '%s\n' "$list" | wc -l | tr -d ' ')"
  for p in $list; do
    printf '  PID %s  启动于 %s\n' "$p" "$(ps -o lstart= -p "$p" 2>/dev/null | sed 's/^ *//')"
  done
  if [ "$n" -gt 1 ]; then
    echo "⚠️  $n 个实例同时在跑 —— 都会接网关，会重复回复。先 stop 再 start。"
    return 1
  fi
  return 0
}

cmd_stop() {
  local list p
  list="$(pids)"
  if [ -z "$list" ]; then echo "本来就没在跑"; return 0; fi
  for p in $list; do kill -TERM "$p" 2>/dev/null && echo "  已发 SIGTERM 给 $p"; done
  for _ in $(seq 1 20); do
    alive || { echo "已退出"; return 0; }
    sleep 0.5
  done
  for p in $(pids); do kill -KILL "$p" 2>/dev/null && echo "  SIGTERM 没退，强杀 $p"; done
  sleep 1
  if alive; then echo "✗ 还有进程没退掉"; return 1; fi
  echo "已退出"
}

cmd_start() {
  if alive; then
    echo "已经在跑了，先 stop："
    cmd_status
    return 1
  fi
  : > "$LOG"
  nohup "$PY" -m qq_rss_bot >>"$LOG" 2>&1 &
  sleep 4
  if alive; then
    echo "已启动："
    cmd_status
    echo "日志：$LOG"
  else
    echo "✗ 没起来，最后 20 行日志："
    tail -20 "$LOG"
    return 1
  fi
}

case "${1:-status}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop && cmd_start ;;
  status)  cmd_status ;;
  log)     shift
           if [ "${1:-}" = "-f" ]; then tail -f -n 60 "$LOG"; else tail -n 60 "$LOG"; fi ;;
  *)       echo "用法: $0 {start|stop|restart|status|log [-f]}"; exit 2 ;;
esac
