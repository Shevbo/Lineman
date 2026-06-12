"""Тесты резолвера репо федерации."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from federation_registry import resolve, score, load_registry

REG = {"components": [
    {"id": "voice-profiles", "type": "skill", "name": "voice-profiles",
     "path": "/home/shectory/skills/voice-profiles",
     "keywords": ["голос", "голосов", "voice", "tts", "озвучк"]},
    {"id": "voice-parser", "type": "skill", "name": "voice-parser",
     "path": "/home/shectory/skills/voice-parser",
     "keywords": ["распознав", "транскрипц", "расшифров"]},
    {"id": "lineman", "type": "service", "name": "lineman",
     "path": "/home/shectory/workspaces/infra/lineman",
     "keywords": ["lineman", "прокси", "proxy", "шлюз", "дашборд", "миниапп"]},
    {"id": "polar-accesslink", "type": "skill", "name": "polar-accesslink",
     "path": "/home/shectory/skills/polar-accesslink",
     "keywords": ["тренировк", "polar", "пульс", "accesslink"]},
]}


def test_voice_task_resolves_voice_profiles():
    r = resolve("починить генерацию голосовых сообщений всем агентам", REG)
    assert r["best"]["id"] == "voice-profiles"
    assert r["confident"] is True


def test_lineman_task_confident():
    r = resolve("дашборд миниапп лайнмен не грузится", REG)
    assert r["best"]["id"] == "lineman" and r["confident"] is True


def test_polar_task():
    r = resolve("polar тренировки приходят неполными", REG)
    assert r["best"]["id"] == "polar-accesslink"


def test_unknown_not_confident():
    r = resolve("сделай мне бутерброд с колбасой", REG)
    assert r["confident"] is False
    assert r["best_score"] == 0


def test_ambiguous_returns_candidates():
    r = resolve("voice голос распознавание озвучка транскрипция", REG)
    # оба voice-* набирают очки → есть кандидаты
    ids = {c["id"] for c in r["candidates"]}
    assert "voice-profiles" in ids and "voice-parser" in ids


def test_real_registry_loads():
    reg = load_registry()
    assert isinstance(reg.get("components"), list)
