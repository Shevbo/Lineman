"""Заметки агентов в индекс федерации: закрытое «не знаю» больше не повторяется.

Боря 2026-10-02: «пусть Клод после закрытия вопроса „не знаю“ — через греп или ещё
как-то — пишет в RAG, чтобы потом не было „не знаю“ на повторный близкий вопрос».

Почему это нужно именно так. Индекс собирается синком из `docs/` и `memory/` на
smain, прямой записи в него нет и быть не должно: демон переиндексирует корпус
целиком и штатно работает только с файлами. Поэтому заметка кладётся файлом в
`~/docs/knowledge/`, а следующий синк забирает её в корпус. Задержка до появления
в поиске — один прогон синка, и это нормальная цена за то, что индекс остаётся
воспроизводимым из файлов, а не копится в неизвестном состоянии.

Гейт секретов стоит ДО записи, а не после: заметку пишет агент, который только что
разбирался в проблеме и мог прихватить в текст значение из конфига или вывод команды.
Проверку делает Ключник — он единственный знает настоящие значения (решение Бориса
2026-09-18), наружу уходят только имена.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import unicodedata
import urllib.request
from typing import Any

NOTES_DIR = pathlib.Path(os.environ.get(
    "FEDRAG_NOTES_DIR", str(pathlib.Path.home() / "docs/knowledge")))
KEYMASTER = os.environ.get("KEYMASTER_URL", "http://127.0.0.1:9093")

MAX_TITLE = 120
MAX_TEXT = 20000
MIN_TEXT = 40          # короче — это не знание, а реплика в чат

_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_SLUG_OK = re.compile(r"[^a-z0-9а-яё-]+")


def slugify(title: str) -> str:
    """Имя файла из заголовка. Кириллицу сохраняем: так заметку видно глазами."""
    s = unicodedata.normalize("NFKC", (title or "").strip().lower())
    s = _SLUG_OK.sub("-", s).strip("-")
    return (s or "zametka")[:80]


def check_secrets(text: str) -> tuple[bool, list[str]]:
    """(чисто, имена найденных секретов). Ключник недоступен → считаем НЕ чисто.

    «Не смог проверить» это не «чисто»: заметка уйдёт в общий индекс, который читают
    все агенты, и один пропущенный проход обнуляет смысл гейта.
    """
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(KEYMASTER + "/keymaster/scan", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with _NOPROXY.open(req, timeout=30) as r:
            res = json.loads(r.read() or b"{}")
    except Exception:
        return False, ["<Ключник недоступен>"]
    # Ответ Ключника: {"checked": N, "flagged": [{"id": ..., "names": ["ИМЯ", ...]}]}.
    # names — это уже готовые строки, а не объекты: попытка взять из них ["name"]
    # роняла обработчик с TypeError и рвала соединение без ответа клиенту.
    hits: list[str] = []
    for item in (res.get("flagged") or []):
        if isinstance(item, dict):
            hits.extend(str(n) for n in (item.get("names") or []))
    return (not hits), sorted(set(hits))


def save_note(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Сохранить заметку. Возвращает (http-статус, тело ответа).

    Повторная запись с тем же заголовком перезаписывает файл: знание уточняется,
    а не плодит почти одинаковые заметки — иначе поиск начнёт выдавать по одному
    вопросу три разных ответа, и доверие к индексу кончится.
    """
    title = str(payload.get("title") or "").strip()
    text = str(payload.get("text") or "").strip()
    agent = str(payload.get("agent") or "unknown").strip()[:64]
    tags = payload.get("tags") or []
    if not isinstance(tags, list):
        tags = []

    if not title:
        return 400, {"error": "нужен title — по нему заметку найдут"}
    if len(title) > MAX_TITLE:
        return 400, {"error": "title длиннее %d символов" % MAX_TITLE}
    if len(text) < MIN_TEXT:
        return 400, {"error": "текст короче %d символов — это не знание" % MIN_TEXT}
    if len(text) > MAX_TEXT:
        return 400, {"error": "текст длиннее %d символов, разбей на части" % MAX_TEXT}

    clean, found = check_secrets(title + "\n" + text)
    if not clean:
        return 422, {
            "error": "в заметке есть значения секретов — не записал",
            "secrets": found,
            "hint": ("замени значение на «спроси Ключника: ИМЯ» и пришли снова; "
                     "если это ложное срабатывание — напиши Клоду"),
        }

    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    path = NOTES_DIR / (slugify(title) + ".md")
    existed = path.exists()
    today = datetime.date.today().isoformat()
    header = ["# " + title, "",
              "> Знание федерации. Записал агент `%s`, %s." % (agent, today)]
    if tags:
        header.append("> Метки: " + ", ".join(str(t)[:32] for t in tags[:8]) + ".")
    header += ["", text.rstrip(), ""]
    path.write_text("\n".join(header), encoding="utf-8")

    return (200 if existed else 201), {
        "ok": True,
        "path": str(path),
        "updated": existed,
        "note": ("в поиске появится после следующего прогона синка корпуса"),
    }


def list_notes() -> list[dict[str, Any]]:
    """Что уже записано. Нужно, чтобы агент не писал заново то же самое."""
    if not NOTES_DIR.exists():
        return []
    out = []
    for f in sorted(NOTES_DIR.glob("*.md")):
        try:
            first = f.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError):
            first = ""
        out.append({"file": f.name,
                    "title": first.lstrip("# ").strip(),
                    "size": f.stat().st_size})
    return out
