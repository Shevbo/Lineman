#!/usr/bin/env python3
"""Рендер машинных реестров федерации в markdown для индекса fedrag.

Индексатор ragkit берёт только md и txt (`include_ext`), а самые частые вопросы агентов
как раз про машинные данные: на каком узле живёт агент, по какому адресу ему отвечать,
какой компонент за что отвечает. JSON в индексе бесполезен — чанкуется и ищется плохо.
Поэтому реестр и карта узлов превращаются в таблицы markdown.

Запуск на smain:
    python3 fedrag_render_registry.py <каталог-назначения>

Секреты: из config.json берётся ТОЛЬКО agents.node_map. Секции proxy_pool, upstreams
и любые credentials не читаются и не выводятся. Этого оказалось НЕ достаточно: значение
секрета приехало в реестр через поле desc компонента (sms-gateway, адрес LAN-шлюза), и
гейт Ключника выбросил federation_registry.md из корпуса. Файл исчез из индекса молча,
recall упал 0.867 -> 0.800, перестал находиться даже ответ «кто отвечает за бэкапы».
Поэтому каждый готовый текст теперь проверяется Ключником ПЕРЕД записью: помеченный
файл не пишется вовсе, и это видно сразу здесь, а не через потерю в поиске.
"""
import json
import os
import sys
import urllib.error
import urllib.request

KEYMASTER = os.environ.get("KEYMASTER_URL", "http://127.0.0.1:9093")

LINEMAN = os.path.expanduser("~/workspaces/infra/lineman")


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        print("  пропуск %s: %s" % (path, exc), file=sys.stderr)
        return None


def secret_free(text, label):
    """Отдать текст Ключнику и сказать, можно ли его писать.

    Недоступный Ключник трактуется как ОТКАЗ, не как «чисто»: «не смог проверить» не
    равно «проверено» — то же правило, что в гейте синка корпуса.
    """
    try:
        req = urllib.request.Request(
            KEYMASTER + "/keymaster/scan",
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            flagged = json.loads(resp.read()).get("flagged") or []
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("  ОТКАЗ %s: Ключник недоступен (%s) — не смог проверить, значит не пишу"
              % (label, str(exc)[:80]), file=sys.stderr)
        return False
    if flagged:
        names = sorted({n for f in flagged for n in (f.get("names") or [])})
        print("  ОТКАЗ %s: внутри значения секретов %s. Замени на '<спроси Ключника: ИМЯ>' "
              "в исходном реестре." % (label, ", ".join(names) or "?"), file=sys.stderr)
        return False
    return True


def render_node_map(cfg):
    """Карта агент -> узел. Только node_map, ничего из соседних секций.

    В config.json карта хранится как узел -> список агентов. Агент спрашивает наоборот
    («на каком я узле», «где живёт сосед»), поэтому таблица разворачивается.
    """
    node_map = (cfg.get("agents") or {}).get("node_map") or {}
    if not node_map:
        return None

    pairs = []           # (агент, узел)
    for key, val in node_map.items():
        if isinstance(val, (list, tuple)):          # узел -> [агенты]
            pairs.extend((str(a), key) for a in val)
        elif isinstance(val, dict):                 # агент -> {node: ...}
            pairs.append((key, val.get("node") or val.get("host") or
                          json.dumps(val, ensure_ascii=False)))
        else:                                       # агент -> узел
            pairs.append((key, str(val)))

    out = ["# Карта агентов федерации по узлам",
           "",
           "Источник: `config.json` -> `agents.node_map` на smain (Lineman).",
           "Отвечает на вопрос «на каком узле живёт агент X и куда ему писать».",
           "",
           "| Агент | Узел |",
           "|---|---|"]
    for agent, node in sorted(pairs):
        out.append("| `%s` | %s |" % (agent, node))
    out += ["",
            "## Адреса узлов",
            "",
            "| Узел | Как достучаться |",
            "|---|---|",
            "| smain | `127.0.0.1` локально |",
            "| sdev, hoster, cloud | `10.66.0.1:9090` через WireGuard |",
            "| vibe (Windows) | `127.0.0.1:19090`, обратный SSH-туннель |",
            ""]
    return "\n".join(out)


def render_registry(reg):
    """Реестр компонентов федерации: агенты и сервисы с назначением и ключевыми словами."""
    items = reg if isinstance(reg, list) else (
        reg.get("components") or reg.get("nodes") or reg.get("entries") or [])
    if isinstance(items, dict):
        items = [dict(v, id=k) if isinstance(v, dict) else {"id": k, "value": v}
                 for k, v in items.items()]
    if not items:
        return None

    out = ["# Реестр компонентов федерации",
           "",
           "Источник: `federation_registry.json` на smain (Lineman).",
           "Отвечает на вопрос «какой компонент за это отвечает и где он живёт».",
           ""]
    for it in items:
        if not isinstance(it, dict):
            continue
        name = it.get("id") or it.get("name") or it.get("component") or "без имени"
        out.append("## %s" % name)
        for key in ("kind", "type", "node", "host", "purpose", "description", "desc",
                    "path", "repo", "port", "endpoint", "owner", "status"):
            val = it.get(key)
            if val not in (None, "", [], {}):
                out.append("- **%s**: %s" % (key, val))
        kw = it.get("keywords") or it.get("tags")
        if kw:
            out.append("- **keywords**: %s" % (", ".join(map(str, kw)) if isinstance(kw, list) else kw))
        out.append("")
    return "\n".join(out)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    dest = sys.argv[1]
    os.makedirs(dest, exist_ok=True)

    written = 0
    cfg = load(os.path.join(LINEMAN, "config.json"))
    if cfg:
        text = render_node_map(cfg)
        if text and secret_free(text, "federation_node_map.md"):
            with open(os.path.join(dest, "federation_node_map.md"), "w", encoding="utf-8") as fh:
                fh.write(text)
            written += 1
            print("  federation_node_map.md")

    reg = load(os.path.join(LINEMAN, "federation_registry.json"))
    if reg:
        text = render_registry(reg)
        if text and secret_free(text, "federation_registry.md"):
            with open(os.path.join(dest, "federation_registry.md"), "w", encoding="utf-8") as fh:
                fh.write(text)
            written += 1
            print("  federation_registry.md")

    print("сгенерировано файлов: %d" % written)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
