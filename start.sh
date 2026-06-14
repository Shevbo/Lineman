#!/bin/bash
source /home/shectory/keymaster/.lineman-proxy.env
cd /home/shectory/workspaces/infra/lineman

# Override TELEGRAM_BOT_TOKEN with live value from openclaw.json (lineman-proxy.env has stale token)
export TELEGRAM_BOT_TOKEN="$(.venv/bin/python3 -c "
import json, os
try:
    d = json.load(open(os.path.expanduser('~/.openclaw/openclaw.json')))
    print(d['channels']['telegram']['accounts']['default']['botToken'])
except: print('')
")"

export DEEPSEEK_API_KEY="$(.venv/bin/python3 -c "
import json, os
try:
    d = json.load(open(os.path.expanduser('~/.openclaw/agents/main/agent/auth-profiles.json')))
    print(d['profiles']['deepseek:default']['key'])
except: print('')
")"

export GEMINI_API_KEY="$(.venv/bin/python3 -c "
import json, os
try:
    d = json.load(open(os.path.expanduser('~/.openclaw/openclaw.json')))
    print(d['models']['providers']['google']['apiKey'])
except: print('')
")"

# Эксклюзивный ключ Gemini ТОЛЬКО у Lineman (Боря заводит через Ключника). Если задан —
# reverse_proxy инжектит ИМЕННО его на /proxy/google; агенты без ключа ходят только через шлюз.
# Источник: .lineman-proxy.env (sourced выше) ИЛИ credential-файл Ключника (любое из имён).
export GEMINI_LINEMAN_API_TOKEN="${GEMINI_LINEMAN_API_TOKEN:-$(cat ~/.keymaster/credentials/gemini_lineman_api_token 2>/dev/null || cat ~/.keymaster/credentials/gemini-lineman-api-token 2>/dev/null)}"

exec .venv/bin/python3 main.py
