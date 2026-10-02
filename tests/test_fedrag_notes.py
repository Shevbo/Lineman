"""Заметки агентов в индекс федерации.

Боря 2026-10-02: «пусть Клод после закрытия вопроса „не знаю“ пишет в RAG, чтобы
потом не было „не знаю“ на повторный близкий вопрос».

Две вещи здесь важнее остального, и обе про доверие к индексу:

* гейт секретов стоит ДО записи и «не смог проверить» считается отказом. Заметку
  пишет агент, который только что разбирался в проблеме и мог прихватить в текст
  значение из конфига. Один пропущенный проход — и секрет в общем индексе, который
  читают все агенты;
* повторная запись с тем же заголовком ПЕРЕЗАПИСЫВАЕТ файл. Иначе по одному вопросу
  поиск начнёт выдавать три слегка разных ответа, и индексу перестанут верить.
"""
import json

import pytest

import fedrag_notes


@pytest.fixture
def notes_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fedrag_notes, "NOTES_DIR", tmp_path / "knowledge")
    return tmp_path / "knowledge"


@pytest.fixture
def clean_keymaster(monkeypatch):
    """Ключник отвечает «чисто»."""
    monkeypatch.setattr(fedrag_notes, "check_secrets", lambda text: (True, []))


def good(**kw):
    base = {"title": "Где лежат логи Lineman",
            "text": "Логи в PM2: ~/.pm2/logs/lineman-gateway-error.log, "
                    "смотреть pm2 logs lineman-gateway --nostream.",
            "agent": "klod-access"}
    base.update(kw)
    return base


# --------------------------------------------------------------------- запись
def test_заметка_сохраняется_файлом(notes_dir, clean_keymaster):
    code, res = fedrag_notes.save_note(good())
    assert code == 201 and res["ok"] is True
    saved = (notes_dir / "где-лежат-логи-lineman.md").read_text(encoding="utf-8")
    assert saved.startswith("# Где лежат логи Lineman")
    assert "pm2 logs lineman-gateway" in saved


def test_в_заметке_видно_кто_и_когда_записал(notes_dir, clean_keymaster):
    """Без автора знание нельзя перепроверить, а в индексе это важнее всего."""
    fedrag_notes.save_note(good(agent="eshkola"))
    saved = next(notes_dir.glob("*.md")).read_text(encoding="utf-8")
    assert "`eshkola`" in saved


def test_метки_попадают_в_текст(notes_dir, clean_keymaster):
    fedrag_notes.save_note(good(tags=["lineman", "логи"]))
    saved = next(notes_dir.glob("*.md")).read_text(encoding="utf-8")
    assert "lineman" in saved and "логи" in saved


def test_повтор_с_тем_же_заголовком_перезаписывает(notes_dir, clean_keymaster):
    fedrag_notes.save_note(good(text="Первая версия знания, достаточно длинная строка."))
    code, res = fedrag_notes.save_note(
        good(text="Уточнённая версия знания, тоже достаточно длинная строка."))
    assert code == 200 and res["updated"] is True
    assert len(list(notes_dir.glob("*.md"))) == 1, "дублей быть не должно"
    assert "Уточнённая" in next(notes_dir.glob("*.md")).read_text(encoding="utf-8")


# ------------------------------------------------------------ гейт секретов
def test_секрет_в_заметке_не_записывается(notes_dir, monkeypatch):
    monkeypatch.setattr(fedrag_notes, "check_secrets",
                        lambda text: (False, ["TELEGRAM_BOT_TOKEN"]))
    code, res = fedrag_notes.save_note(good(text="В заметке случайно оказался токен бота целиком, вот такая длинная строка для проверки гейта."))
    assert code == 422
    assert "TELEGRAM_BOT_TOKEN" in res["secrets"]
    assert not list(notes_dir.glob("*.md")), "файл не должен появиться"


def test_недоступный_ключник_это_отказ(notes_dir, monkeypatch):
    """«Не смог проверить» не равно «чисто»: заметка уйдёт в общий индекс."""
    def boom(url, *a, **kw):
        raise OSError("Ключник недоступен")

    monkeypatch.setattr(fedrag_notes._NOPROXY, "open", boom, raising=False)
    clean, found = fedrag_notes.check_secrets("любой текст")
    assert clean is False and found


def test_в_ответе_об_отказе_есть_что_делать(notes_dir, monkeypatch):
    monkeypatch.setattr(fedrag_notes, "check_secrets", lambda t: (False, ["X"]))
    _, res = fedrag_notes.save_note(good())
    assert "спроси Ключника" in res["hint"]


# ------------------------------------------------------------------ проверки
def test_без_заголовка_отказ(notes_dir, clean_keymaster):
    code, res = fedrag_notes.save_note(good(title=""))
    assert code == 400 and "title" in res["error"]


def test_слишком_короткий_текст_отказ(notes_dir, clean_keymaster):
    """Реплика в чат — не знание; индекс не должен заполняться обрывками."""
    code, _ = fedrag_notes.save_note(good(text="ок"))
    assert code == 400


def test_слишком_длинный_текст_отказ(notes_dir, clean_keymaster):
    code, res = fedrag_notes.save_note(good(text="я" * 20001))
    assert code == 400 and "разбей" in res["error"]


def test_слишком_длинный_заголовок_отказ(notes_dir, clean_keymaster):
    code, _ = fedrag_notes.save_note(good(title="з" * 121))
    assert code == 400


# ------------------------------------------------------------------- имена
def test_кириллица_в_имени_файла_сохраняется():
    """Заметку должно быть видно глазами в каталоге, а не по хешу."""
    assert fedrag_notes.slugify("Где логи Lineman") == "где-логи-lineman"


def test_опасные_символы_не_уезжают_в_путь():
    for bad in ("../../etc/passwd", "a/b/c", "имя: с? знаками*"):
        slug = fedrag_notes.slugify(bad)
        assert "/" not in slug and ".." not in slug, bad


def test_пустой_заголовок_даёт_запасное_имя():
    assert fedrag_notes.slugify("") == "zametka"


# -------------------------------------------------------------------- список
def test_список_показывает_заголовки(notes_dir, clean_keymaster):
    fedrag_notes.save_note(good())
    fedrag_notes.save_note(good(title="Как перезапустить Клода"))
    titles = [n["title"] for n in fedrag_notes.list_notes()]
    assert "Где лежат логи Lineman" in titles
    assert "Как перезапустить Клода" in titles


def test_список_пуст_когда_заметок_нет(notes_dir):
    assert fedrag_notes.list_notes() == []


def test_разбор_ответа_ключника(monkeypatch):
    """Регрессия: names — список строк, а не объектов.

    Попытка взять из них ["name"] роняла обработчик с TypeError, и клиент получал
    обрыв соединения вместо внятного отказа. Поймано живой пробой 2026-10-02.
    """
    import io, json as _json

    class _Resp:
        def __init__(self, payload):
            self._p = _json.dumps(payload).encode()

        def read(self):
            return self._p

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        fedrag_notes._NOPROXY, "open",
        lambda *a, **kw: _Resp({"checked": 1, "flagged": [
            {"id": "text", "names": ["TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY"]}]}),
        raising=False)
    clean, found = fedrag_notes.check_secrets("текст с секретом")
    assert clean is False
    assert found == ["GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN"]


def test_чистый_ответ_ключника_пропускает(monkeypatch):
    class _Resp:
        def read(self):
            return b'{"checked": 1, "flagged": []}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(fedrag_notes._NOPROXY, "open",
                        lambda *a, **kw: _Resp(), raising=False)
    assert fedrag_notes.check_secrets("чистый текст") == (True, [])
