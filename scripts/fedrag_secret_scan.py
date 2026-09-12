#!/usr/bin/env python3
"""Шлюз секретов для корпуса fedrag.

Запускается ПЕРЕД тем, как документация уедет в индекс на sdev. Ищет не упоминания
слов вроде "token", а похожие на настоящие значения: длинные строки высокой энтропии
в присваивании, приватные ключи, известные префиксы провайдеров.

Найденное значение секрета в индексе означает, что его сможет вытащить поиском любой
агент федерации. Поэтому ненулевой код возврата обязан останавливать синк.

Использование:
    python3 fedrag_secret_scan.py <путь> [<путь> ...]
    python3 fedrag_secret_scan.py --stdin-list < files.txt

Код возврата: 0 — чисто, 1 — есть подозрения (синк не продолжать).
"""
import math
import os
import re
import sys

# Известные префиксы провайдеров: почти нулевой шанс ложного срабатывания.
HARD_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "ключ Anthropic"),
    (re.compile(r"sk-[A-Za-z0-9]{32,}"), "ключ OpenAI-совместимый"),
    (re.compile(r"AIza[A-Za-z0-9_\-]{30,}"), "ключ Google API"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "токен GitHub"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "токен Slack"),
    (re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"), "приватный ключ"),
    (re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{30,}"), "токен Telegram-бота"),
]

# Присваивание секретоподобному имени длинного литерала.
ASSIGN = re.compile(
    r"""(?ix)
    \b (?P<name> [A-Za-z0-9_\-]* (?: api[_-]?key | secret | token | passwo?r?d | credential )
                 [A-Za-z0-9_\-]* )
    \s* [:=] \s*
    (?P<quote>["']) (?P<val> [^"'\n]{16,}) (?P=quote)
    """
)

# Значения, которые выглядят как секрет, но им не являются.
PLACEHOLDER = re.compile(
    r"(?i)^(\$|<|\{\{|%|x{6,}|\.{3}|your|example|placeholder|changeme|dummy|test|"
    r"sample|redacted|hidden|скрыт|значение|см\.|see |none|null|true|false)"
)
# Ссылка на переменную окружения или подстановка команды — это указатель, а не значение.
INDIRECTION = re.compile(r"[$`]|\{\{|os\.environ|getenv|keymaster|cat /")


def entropy(s):
    """Шеннон на символ. Осмысленный текст ~3.5 и ниже, случайный ключ ~4.5 и выше."""
    if not s:
        return 0.0
    freq = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def looks_secret(val):
    """Похоже ли значение на настоящий секрет, а не на прозу или указатель."""
    if PLACEHOLDER.match(val.strip()) or INDIRECTION.search(val):
        return False
    if " " in val.strip():          # секреты не содержат пробелов
        return False
    if not re.fullmatch(r"[A-Za-z0-9_\-\.:+/=]{16,}", val):
        return False
    # Длинное и бессистемное: ключ. Длинное и читаемое: скорее slug вроде
    # "all-agents-via-lineman".
    return entropy(val) >= 3.6 and len(val) >= 20


def scan_file(path):
    hits = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                if len(line) > 4000:        # минифицированное или бинарное — пропускаем
                    continue
                for rx, what in HARD_PATTERNS:
                    if rx.search(line):
                        hits.append((lineno, what, "по сигнатуре провайдера"))
                for m in ASSIGN.finditer(line):
                    if looks_secret(m.group("val")):
                        hits.append((lineno, m.group("name"),
                                     "длинное значение высокой энтропии в присваивании"))
    except OSError as exc:
        print("  не прочитан %s: %s" % (path, exc), file=sys.stderr)
    return hits


def main(argv):
    if argv and argv[0] == "--stdin-list":
        paths = [ln.strip() for ln in sys.stdin if ln.strip()]
    else:
        paths = argv
    if not paths:
        sys.exit(__doc__)

    files = []
    for p in paths:
        if os.path.isdir(p):
            for dirpath, _, names in os.walk(p):
                files.extend(os.path.join(dirpath, n) for n in names
                             if n.rsplit(".", 1)[-1] in ("md", "txt", "json"))
        else:
            files.append(p)

    total = 0
    for f in sorted(set(files)):
        for lineno, what, why in scan_file(f):
            total += 1
            # Само значение НИКОГДА не печатается: путь, строка и причина.
            print("НАЙДЕНО  %s:%d  [%s]  %s" % (f, lineno, what, why))

    print("\nпросмотрено файлов: %d, подозрений: %d" % (len(files), total))
    if total:
        print("Синк остановлен. Убрать значения из файлов, оставить имя переменной "
              "и путь, значение брать через Ключника.")
        return 1
    print("Чисто, корпус можно синкать.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
