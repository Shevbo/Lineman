# Telegram Rate Limiter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `POST /api/tg/send` endpoint to Lineman with per-account 15-second rate limiting, and migrate all Telegram sendMessage callers to use it.

**Architecture:** ProxyServer gets a new `_tg_rate: dict[str, float]` (account → last_send_ts) and a `_raw_api_tg_send` async method. The method reads bot tokens directly from `~/.openclaw/openclaw.json`, enforces the cooldown, and sends via the existing `_upstream_session`. Callers (keymaster, voice scripts) switch from direct urllib/requests Telegram API calls to `POST http://127.0.0.1:9090/api/tg/send`.

**Tech Stack:** Python 3.12, asyncio, aiohttp (already in Lineman), urllib (keymaster), requests (voice-manager)

---

## Files

| File | Change |
|------|--------|
| `workspaces/infra/lineman/proxy_server.py` | Add `_tg_rate`, `_tg_oc_path`, `_raw_api_tg_send`, wire route |
| `keymaster/keymaster.py` | Replace `_notify_boris()` impl |
| `scripts/voice-manager.py` | Replace `send_telegram_text()` impl |
| `scripts/voices_bot.py` | Add `tg_send()` helper, use for sendMessage only |
| `scripts/voice_wizard.py` | Replace `tg("sendMessage", ...)` calls |
| `~/.openclaw/openclaw.json` | Remove invalid accounts: virtual-boris, titan, keymaster |

---

## Task 1: Add rate-limiter state and `/api/tg/send` endpoint to ProxyServer

**Files:**
- Modify: `workspaces/infra/lineman/proxy_server.py`

- [ ] **Step 1: Add `_tg_rate` and `_tg_oc_path` to `__init__`**

In `ProxyServer.__init__`, after the `_pool` line, add:

```python
# Telegram rate limiter: account → last send timestamp
self._tg_rate: dict[str, float] = {}
self._tg_oc_path = Path.home() / ".openclaw" / "openclaw.json"
```

- [ ] **Step 2: Add the `_raw_api_tg_send` method**

Add after `_raw_api_agent_message` (before `_send_json_response`):

```python
async def _raw_api_tg_send(
    self,
    rd: asyncio.StreamReader,
    wr: asyncio.StreamWriter,
) -> None:
    """POST /api/tg/send — rate-limited Telegram sendMessage.

    Body: {"account": "default", "chat_id": "...", "text": "...", "parse_mode": "Markdown"}
    Rate limit: 1 message per 15 seconds per account.
    """
    headers: dict[str, str] = {}
    while True:
        hdr = await asyncio.wait_for(rd.readline(), timeout=5)
        if hdr in (b"\r\n", b"\n", b""):
            break
        decoded = hdr.decode("utf-8", errors="replace").strip()
        if ": " in decoded:
            k, v = decoded.split(": ", 1)
            headers[k.lower()] = v

    content_length = int(headers.get("content-length", "0") or "0")
    body_bytes = b""
    if content_length > 0:
        body_bytes = await asyncio.wait_for(
            rd.read(min(content_length, 65536)), timeout=10
        )

    try:
        req = json.loads(body_bytes)
    except json.JSONDecodeError:
        self._send_json_response(wr, 400, {"ok": False, "error": "invalid JSON"})
        await wr.drain()
        wr.close()
        return

    account = req.get("account", "default")
    chat_id = str(req.get("chat_id", ""))
    text = req.get("text", "")
    parse_mode = req.get("parse_mode", "")

    if not chat_id or not text:
        self._send_json_response(wr, 400, {"ok": False, "error": "chat_id and text required"})
        await wr.drain()
        wr.close()
        return

    # Rate limit check
    RATE_LIMIT_S = 15.0
    now = time.time()
    last = self._tg_rate.get(account, 0.0)
    since = now - last
    if since < RATE_LIMIT_S:
        retry_after = round(RATE_LIMIT_S - since, 1)
        self._send_json_response(wr, 429, {"ok": False, "retry_after": retry_after})
        await wr.drain()
        wr.close()
        logger.warning("tg_rate_limited", account=account, retry_after=retry_after)
        return

    # Load token from openclaw.json
    try:
        with open(self._tg_oc_path) as f:
            oc = json.load(f)
        token = (
            oc.get("channels", {})
            .get("telegram", {})
            .get("accounts", {})
            .get(account, {})
            .get("botToken", "")
        )
    except Exception as e:
        self._send_json_response(wr, 503, {"ok": False, "error": f"config read error: {e}"})
        await wr.drain()
        wr.close()
        return

    if not token:
        self._send_json_response(wr, 400, {"ok": False, "error": f"unknown account: {account}"})
        await wr.drain()
        wr.close()
        return

    # Send to Telegram
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    tg_url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with self._upstream_session.post(
            tg_url, json=payload, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            tg_body = await resp.json()
            if tg_body.get("ok"):
                self._tg_rate[account] = time.time()
                self._send_json_response(wr, 200, {
                    "ok": True,
                    "message_id": tg_body.get("result", {}).get("message_id"),
                })
            else:
                self._send_json_response(wr, 503, {
                    "ok": False,
                    "error": f"telegram error: {tg_body.get('description', 'unknown')}",
                })
    except asyncio.TimeoutError:
        self._send_json_response(wr, 504, {"ok": False, "error": "telegram timeout"})
    except Exception as e:
        self._send_json_response(wr, 503, {"ok": False, "error": str(e)})

    await wr.drain()
    wr.close()
```

- [ ] **Step 3: Wire the route in `_raw_handler`**

In the `_raw_handler` inside `start()`, add this block right before the `elif request_path_only.startswith("/api/agent/"):` block:

```python
            elif request_path_only == "/api/tg/send" and method == "POST":
                await self._raw_api_tg_send(rd, wr)
                return
```

- [ ] **Step 4: Smoke-test the endpoint manually**

```bash
# Restart first
npx pm2 restart lineman-gateway

# Wait 3s then test with known-good account
curl -s -X POST http://127.0.0.1:9090/api/tg/send \
  -H "Content-Type: application/json" \
  -d '{"account":"default","chat_id":"36910539","text":"Lineman tg/send test ✅"}'
```

Expected: `{"ok": true, "message_id": <N>}` and a message appears in Telegram.

- [ ] **Step 5: Test rate limit**

```bash
# Second call immediately — should be blocked
curl -s -X POST http://127.0.0.1:9090/api/tg/send \
  -H "Content-Type: application/json" \
  -d '{"account":"default","chat_id":"36910539","text":"should be blocked"}'
```

Expected: `{"ok": false, "retry_after": <~15.0>}` with HTTP 429.

- [ ] **Step 6: Test unknown account**

```bash
curl -s -X POST http://127.0.0.1:9090/api/tg/send \
  -H "Content-Type: application/json" \
  -d '{"account":"nonexistent","chat_id":"36910539","text":"x"}'
```

Expected: `{"ok": false, "error": "unknown account: nonexistent"}` with HTTP 400.

---

## Task 2: Remove invalid Telegram bot accounts from openclaw.json

**Files:**
- Modify: `~/.openclaw/openclaw.json`

The accounts `virtual-boris`, `titan`, and `keymaster` return HTTP 404 from Telegram (tokens deleted or invalid). Remove them to keep config clean.

- [ ] **Step 1: Remove the three invalid accounts**

```bash
python3 - << 'EOF'
import json, os
path = os.path.expanduser("~/.openclaw/openclaw.json")
with open(path) as f:
    d = json.load(f)
accts = d.get("channels", {}).get("telegram", {}).get("accounts", {})
for name in ("virtual-boris", "titan", "keymaster"):
    if name in accts:
        del accts[name]
        print(f"removed: {name}")
with open(path, "w") as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
print("done")
EOF
```

Expected output:
```
removed: virtual-boris
removed: titan
removed: keymaster
done
```

- [ ] **Step 2: Verify remaining accounts are valid**

```bash
python3 - << 'EOF'
import json, os, urllib.request
with open(os.path.expanduser("~/.openclaw/openclaw.json")) as f:
    d = json.load(f)
accts = d.get("channels", {}).get("telegram", {}).get("accounts", {})
proxy = urllib.request.ProxyHandler({"http": "http://127.0.0.1:9090", "https": "http://127.0.0.1:9090"})
opener = urllib.request.build_opener(proxy)
for name, cfg in accts.items():
    token = cfg.get("botToken", "")
    try:
        r = opener.open(f"https://api.telegram.org/bot{token}/getMe", timeout=8)
        info = json.loads(r.read())
        print(f"  {name}: ✅ @{info['result']['username']}")
    except Exception as e:
        print(f"  {name}: ❌ {e}")
EOF
```

Expected: all remaining accounts show ✅.

---

## Task 3: Migrate keymaster.py

**Files:**
- Modify: `keymaster/keymaster.py`

Replace the `_notify_boris()` function to POST to Lineman instead of calling Telegram directly.

- [ ] **Step 1: Replace `_notify_boris()`**

Replace the entire function (lines ~41–60) with:

```python
def _notify_boris(message: str) -> None:
    """Уведомить Бориса через Lineman /api/tg/send (с rate limiting)."""
    try:
        payload = json.dumps({
            "account": "default",
            "chat_id": BORIS_TG,
            "text": message,
        }).encode()
        req = urllib.request.Request(
            f"{LINEMAN}/api/tg/send",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # уведомление не критично
```

- [ ] **Step 2: Test**

```bash
# Trigger a query (will call _notify_boris internally)
python3 ~/keymaster/keymaster.py --requester test_migration query GEMINI_API_KEY
```

Expected: command succeeds (prints metadata), Telegram message appears in Boris's chat (if >15s since last message).

---

## Task 4: Migrate voice-manager.py

**Files:**
- Modify: `scripts/voice-manager.py`

Replace `send_telegram_text()` to use Lineman endpoint.

- [ ] **Step 1: Find the account used by voice-manager**

```bash
grep -n "token\|account\|BOT_TOKEN\|botToken" ~/scripts/voice-manager.py | head -20
```

Note which bot account name is used — it will be needed in the next step.

- [ ] **Step 2: Replace `send_telegram_text()`**

Replace the function with:

```python
def send_telegram_text(token, text, chat_id=None):
    """Отправить текст через Lineman /api/tg/send (rate-limited)."""
    import urllib.request as _ur
    s = _load_secrets()
    cid = str(chat_id or s['chat_id'])
    # Определяем account по токену
    account = _token_to_account(token)
    payload = json.dumps({
        "account": account,
        "chat_id": cid,
        "text": text,
        "parse_mode": "HTML",
    }).encode()
    req = _ur.Request(
        "http://127.0.0.1:9090/api/tg/send",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        r = _ur.urlopen(req, timeout=15)
        resp = json.loads(r.read())
        return resp.get("ok", False)
    except Exception:
        return False
```

Also add this helper (find where secrets/tokens are loaded in the file and add near there):

```python
def _token_to_account(token: str) -> str:
    """Найти имя account в openclaw.json по токену."""
    try:
        with open(os.path.expanduser("~/.openclaw/openclaw.json")) as f:
            d = json.load(f)
        accts = d.get("channels", {}).get("telegram", {}).get("accounts", {})
        for name, cfg in accts.items():
            if cfg.get("botToken", "") == token:
                return name
    except Exception:
        pass
    return "default"
```

- [ ] **Step 3: Quick test**

```bash
python3 ~/scripts/voice-manager.py 2>&1 | head -5
```

Expected: script starts without import errors.

---

## Task 5: Migrate voices_bot.py and voice_wizard.py

**Files:**
- Modify: `scripts/voices_bot.py`
- Modify: `scripts/voice_wizard.py`

Only `sendMessage` calls go through the rate limiter. Other Telegram methods (`pinChatMessage`, `editMessageText`, etc.) keep using the existing `tg()` function.

- [ ] **Step 1: Add `tg_send()` helper to voices_bot.py**

After the `tg()` function definition (around line 34), add:

```python
def tg_send(chat_id: str, text: str, parse_mode: str = "Markdown") -> dict:
    """Отправить сообщение через Lineman /api/tg/send (rate-limited, 1/15s)."""
    import urllib.request as _ur
    payload = json.dumps({
        "account": "main-sdev",
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": parse_mode,
    }).encode()
    req = _ur.Request(
        "http://127.0.0.1:9090/api/tg/send",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        r = _ur.urlopen(req, timeout=15)
        return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}
```

- [ ] **Step 2: Replace `tg("sendMessage", ...)` calls in voices_bot.py**

Find all `tg("sendMessage", {...})` calls and replace with `tg_send(...)`:

```python
# Before (example around line 79):
resp = tg("sendMessage", {
    "chat_id": CHAT_ID,
    "text": text,
    "parse_mode": "Markdown",
    "reply_markup": json.dumps(keyboard),
})

# After — note: reply_markup not supported by tg_send (no buttons needed for plain sends)
# For calls WITH reply_markup, keep tg("sendMessage", ...) as-is
# For calls WITHOUT reply_markup, replace with:
resp = tg_send(CHAT_ID, text, "Markdown")
```

Check each `tg("sendMessage", ...)` call:
- If it has `reply_markup` → keep using `tg("sendMessage", ...)` (buttons require direct API)
- If plain text only → replace with `tg_send()`

- [ ] **Step 3: Add `tg_send()` helper to voice_wizard.py**

Find where `tg()` is defined in `voice_wizard.py` (or imported), add the same `tg_send()` helper nearby, using whichever `account` matches the bot token used by that script.

Check which account:
```bash
grep -n "account\|BOT_TOKEN\|botToken\|default\|guilya" ~/scripts/voice_wizard.py | head -10
```

Then add `tg_send()` using the correct account name (likely `"default"` or `"guilya"`).

- [ ] **Step 4: Replace plain `tg("sendMessage", ...)` calls in voice_wizard.py**

Same rule as Step 2: calls without `reply_markup` → `tg_send()`, calls with → keep `tg("sendMessage", ...)`.

- [ ] **Step 5: Test both scripts start cleanly**

```bash
python3 ~/scripts/voices_bot.py 2>&1 | head -5
python3 ~/scripts/voice_wizard.py 2>&1 | head -5
```

Expected: no import errors, no crashes on startup.

---

## Task 6: Final restart and end-to-end check

- [ ] **Step 1: Restart Lineman**

```bash
npx pm2 restart lineman-gateway
sleep 4
curl -s http://127.0.0.1:9090/health | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])"
```

Expected: `ok`

- [ ] **Step 2: Full rate limit test sequence**

```bash
# Send 1 — should succeed
curl -s -X POST http://127.0.0.1:9090/api/tg/send \
  -H "Content-Type: application/json" \
  -d '{"account":"default","chat_id":"36910539","text":"Rate limit test 1/2 ✅"}' | python3 -m json.tool

# Send 2 immediately — should be blocked
curl -s -X POST http://127.0.0.1:9090/api/tg/send \
  -H "Content-Type: application/json" \
  -d '{"account":"default","chat_id":"36910539","text":"should be blocked"}' | python3 -m json.tool

# Wait 16s, send 3 — should succeed
sleep 16
curl -s -X POST http://127.0.0.1:9090/api/tg/send \
  -H "Content-Type: application/json" \
  -d '{"account":"default","chat_id":"36910539","text":"Rate limit test 2/2 ✅"}' | python3 -m json.tool
```

Expected:
- Send 1: `{"ok": true, "message_id": N}`
- Send 2: `{"ok": false, "retry_after": ~15.0}` (HTTP 429)
- Send 3: `{"ok": true, "message_id": M}`

- [ ] **Step 3: Verify keymaster migration**

```bash
# Wait >15s from last TG send, then:
python3 ~/keymaster/keymaster.py --requester test query GEMINI_API_KEY 2>&1 | head -5
```

Expected: metadata returned, Telegram message received by Boris.

- [ ] **Step 4: Commit**

```bash
cd ~/workspaces/infra/lineman
git add proxy_server.py
git commit -m "feat(tg): POST /api/tg/send endpoint with 15s per-account rate limit"

git add ~/keymaster/keymaster.py ~/scripts/voice-manager.py ~/scripts/voices_bot.py ~/scripts/voice_wizard.py
git commit -m "feat(tg): migrate all sendMessage callers to Lineman /api/tg/send"
```
