#!/usr/bin/env bash
# SessionStart hook: показывает Klod-Access новые сообщения inbox при старте сессии.
# Реализация — тонкий wrapper над python-скриптом, чтобы не возиться с heredoc-кавычками.

set -euo pipefail

exec /usr/bin/python3 /home/shectory/workspaces/infra/lineman/scripts/klod_session_start.py "$@"
