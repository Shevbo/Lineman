#!/usr/bin/env python3
"""Генератор справочника репозиториев федерации (Боря: единый каталог служб/навыков/агентов).

Авто-инвентаризация (чтобы не устаревал): сканит настоящие git-репо в ~/workspaces,
навыки ~/skills + ~/.openclaw/skills, агентов ~/.openclaw/agents. Для каждого пишет путь,
git-корень, own_repo (свой ли репо — критично для Билдера: PR-флоу только на own_repo),
remote, node, keywords. Результат → federation_registry.json (источник истины для резолвера).

Запуск: python3 scripts/build_registry.py  (по таймеру/при изменениях).
"""
import json
import os
import subprocess
from datetime import datetime, timezone, timedelta

HOME = os.path.expanduser("~")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "federation_registry.json")
MSK = timezone(timedelta(hours=3))

AGENTS = {"eshkola", "guilya", "inbox", "interview-coach", "jobsearch-scanner", "keymaster",
          "main", "nurse", "qaper", "resume-editor", "selfcoder", "titan", "virtual-boris"}

# Курируемые ключевые слова (имя + синонимы/русские формы) для резолвинга задачи → репо.
KEYWORDS = {
    "voice-profiles": ["голос", "голосов", "voice", "tts", "озвучк", "озвучива", "профил голос"],
    "voice-parser": ["распознав", "транскрипц", "voice parse", "голосовое сообщение", "расшифров"],
    "polar-accesslink": ["тренировк", "треня", "трен ", "polar", "пульс", "accesslink", "workout", "кардио"],
    "polar-link": ["polar ссылк", "share", "flow.polar", "polar link", "polarlink"],
    "titan": ["титан", "тренер", "фитнес", "тренировк", "вес", "питание", "polar"],
    "nurse": ["nurse", "медсестра", "здоров", "лекарств", "таблетк", "приём"],
    "keymaster": ["keymaster", "ключник", "секрет", "ключ", "токен", "credential"],
    "lineman": ["lineman", "лайнмен", "прокси", "proxy", "шлюз", "gateway", "дашборд", "dashboard",
                "миниап", "миниапп", "reverse proxy", "роутинг", "вотчдог", "бэклог"],
    "censor": ["censor", "цензор", "мониторинг токен", "ретрай", "поведение агент"],
    "klod-foreman": ["klod", "клод", "форман", "медик", "medic", "билдер", "builder", "диспетчер"],
    "jobsearch-scanner": ["ваканс", "job", "поиск работ", "jobscanner", "скан работ"],
    "resume-editor": ["резюме", "resume", "cv "],
    "interview-coach": ["интервью", "interview", "собеседов"],
    "eshkola": ["эшкол", "eshkola", "школ"],
    "guilya": ["гуля", "guilya", "гуйля"],
    "virtual-boris": ["виртуальн борис", "vboris", "virtual boris", "двойник"],
    "selfcoder": ["selfcoder", "селфкодер", "автокодер"],
    "qaper": ["qa", "тест", "qaper", "проверк качеств"],
    "inbox": ["инбокс", "inbox", "входящ"],
    "career-bot": ["career", "карьер", "карьерный бот"],
    "gemini-live-service": ["gemini live", "джемини лайв", "голосовой gemini"],
    "openclaw": ["openclaw", "опенкло", "гейтвей агент", "ядро агент"],
    "youtube": ["youtube", "ютуб", "видео поиск"],
    "image-gen": ["image gen", "генерац картин", "картинк", "image"],
    "screenshot-reader": ["скриншот", "screenshot", "чтение экран"],
    "github-repo": ["github", "гитхаб", "репозитори"],
    "gog": ["gog", "google cli", "гугл cli"],
    "summarize-master": ["саммари", "summar", "выжимк", "сжать текст"],
    "meeting": ["встреч", "meeting", "созвон"],
    "shectory-infra": ["infra", "инфра", "сервер", "деплой", "nginx", "systemd"],
}


def _git(path, *args):
    try:
        r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _kw(cid, name):
    base = KEYWORDS.get(cid, [])
    toks = [t for t in cid.replace("-", " ").replace("_", " ").lower().split() if len(t) > 2]
    return sorted(set(base + toks))


def _entry(cid, ctype, path, node="smain", desc=""):
    root = _git(path, "rev-parse", "--show-toplevel")
    own = bool(root) and os.path.realpath(root) == os.path.realpath(path)
    remote = _git(path, "remote", "get-url", "origin") if own else ""
    return {"id": cid, "type": ctype, "name": cid, "path": path,
            "git_root": root or None, "own_repo": own, "git_remote": remote or None,
            "node": node, "desc": desc, "keywords": _kw(cid, cid)}


def scan():
    out = []
    seen = set()

    # 1) Настоящие git-репо под ~/workspaces (службы/проекты)
    ws = os.path.join(HOME, "workspaces")
    for dirpath, dirs, _ in os.walk(ws):
        depth = dirpath[len(ws):].count(os.sep)
        if depth >= 3:
            dirs[:] = []
            continue
        if "node_modules" in dirpath or "/.git" in dirpath:
            continue
        if ".git" in dirs:
            cid = os.path.basename(dirpath)
            ctype = "agent" if cid in AGENTS else "service"
            out.append(_entry(cid, ctype, dirpath))
            seen.add(os.path.realpath(dirpath))
            dirs[:] = [d for d in dirs if d != ".git"]

    # 2) Воркспейсы агентов (под ~/workspaces, git-корень = home, без своего репо)
    for ag in sorted(AGENTS):
        p = os.path.join(ws, ag)
        if os.path.isdir(p) and os.path.realpath(p) not in seen:
            out.append(_entry(ag, "agent", p))

    # 3) Навыки
    for skroot in (os.path.join(HOME, "skills"), os.path.join(HOME, ".openclaw", "skills")):
        if not os.path.isdir(skroot):
            continue
        for name in sorted(os.listdir(skroot)):
            p = os.path.join(skroot, name)
            if os.path.isdir(p) and name != "shared":
                out.append(_entry(name, "skill", p))
    return out


def main():
    entries = sorted(scan(), key=lambda e: (e["type"], e["id"]))
    reg = {"generated": datetime.now(MSK).strftime("%Y-%m-%d %H:%M MSK"),
           "count": len(entries), "components": entries}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    by = {}
    for e in entries:
        by[e["type"]] = by.get(e["type"], 0) + 1
    own = sum(1 for e in entries if e["own_repo"])
    print(f"registry: {len(entries)} компонентов {by}, own_repo={own} → {OUT}")


if __name__ == "__main__":
    main()
