#!/usr/bin/env bash
# Туннель smain -> sdev к демону ragkit.
#
# Демон слушает только 127.0.0.1:8791 на своём узле — так задумано его автором
# («never exposed off-box»), и ломать это ради удобства не нужно. Туннель даёт Lineman
# локальный порт, сохраняя ограничение: снаружи sdev порт по-прежнему закрыт.
#
# Запускается под PM2 (тем же оркестратором, что держит Lineman):
#   pm2 start scripts/fedrag_tunnel.sh --name fedrag-tunnel
#
# ExitOnForwardFailure держит процесс честным: если порт занят или проброс не встал,
# ssh падает, PM2 перезапускает, и мы не получаем тихо мёртвый туннель.
set -euo pipefail

REMOTE="${FEDRAG_REMOTE:-sdev}"
PORT="${FEDRAG_PORT:-8791}"
# Пауза перед выходом, если ssh упал сразу. PM2 перезапускает мгновенно, поэтому
# недоступный sdev или занятый порт превращаются в шторм: к 2026-09-29 счётчик
# показывал 15877 перезапусков. Сам туннель при этом рабочий — шторм копился в те
# часы, когда его не удавалось поднять, и грузил и smain, и sdev попытками ssh.
#
# Ждём только на быстром падении: если туннель прожил дольше минуты, значит он
# работал и упал по делу — такой перезапуск задерживать незачем.
MIN_LIFETIME_S="${FEDRAG_MIN_LIFETIME_S:-60}"
RETRY_PAUSE_S="${FEDRAG_RETRY_PAUSE_S:-30}"
started_at=$SECONDS

on_exit() {
    code=$?
    lived=$(( SECONDS - started_at ))
    if [ "$lived" -lt "$MIN_LIFETIME_S" ]; then
        echo "[fedrag-tunnel] прожил ${lived}с (код $code), жду ${RETRY_PAUSE_S}с" >&2
        sleep "$RETRY_PAUSE_S"
    fi
    exit "$code"
}
trap on_exit EXIT

# Порт уже слушают — туннель поднят другим экземпляром. Это не ошибка, а гонка
# перезапуска: падать из-за неё в цикл незачем.
if ss -ltn 2>/dev/null | grep -q "127.0.0.1:${PORT}[[:space:]]"; then
    echo "[fedrag-tunnel] порт ${PORT} уже слушают — ухожу тихо" >&2
    exit 0
fi


ssh -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ConnectTimeout=10 \
  -o BatchMode=yes \
  -L "127.0.0.1:${PORT}:127.0.0.1:${PORT}" \
  "$REMOTE"
