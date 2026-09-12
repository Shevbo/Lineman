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

exec ssh -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ConnectTimeout=10 \
  -o BatchMode=yes \
  -L "127.0.0.1:${PORT}:127.0.0.1:${PORT}" \
  "$REMOTE"
