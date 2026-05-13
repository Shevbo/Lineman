#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

export DEEPSEEK_API_KEY="$(.venv/bin/python3 << 'PYEOF'
import json, os
d = json.load(open(os.path.expanduser('~/.openclaw/agents/main/agent/auth-profiles.json')))
print(d['profiles']['deepseek:default']['key'])
PYEOF
)"

export GEMINI_API_KEY="$(.venv/bin/python3 << 'PYEOF'
import json, os
d = json.load(open(os.path.expanduser('~/.openclaw/openclaw.json')))
print(d['models']['providers']['google']['apiKey'])
PYEOF
)"

export TELEGRAM_BOT_TOKEN="$(.venv/bin/python3 << 'PYEOF'
import json, os
try:
    d = json.load(open(os.path.expanduser('~/.openclaw/openclaw.json')))
    print(d['channels']['telegram']['accounts']['default']['botToken'])
except: print('')
PYEOF
)"

exec .venv/bin/python3 main.py
