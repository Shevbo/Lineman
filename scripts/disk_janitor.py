#!/usr/bin/env python3
"""Disk Janitor — безопасная очистка МЁРТВОЙ ГЕНЕРАЦИИ на smain.

Диск smain критично заполнен (96%). Скрипт удаляет ТОЛЬКО регенерируемый/устаревший хлам
по явному allowlist категорий. Dry-run по умолчанию.

НИКОГДА не трогает (denylist): *.db (lineman.db и пр.), credentials/секреты, *.env, исходный код,
~/genai_dl (рабочие данные Бори), .git. Логи не удаляет, а ОБРЕЗАЕТ (truncate) — чтобы сервисы,
держащие файл открытым, не сломались.

Запуск:
  python3 disk_janitor.py                 # dry-run: что и сколько освободится
  python3 disk_janitor.py --apply         # реально почистить
  python3 disk_janitor.py --apply --if-above 85   # для крона: чистить только если диск > 85%
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from pathlib import Path

HOME = Path.home()
NOW = time.time()
DAY = 86400.0

# Пути/паттерны, которые НИКОГДА не трогаем (подстрока в абсолютном пути).
_NEVER = (
    "/.git/", "/genai_dl", "/credentials", "/.keymaster/credentials",
    "/.ssh", "/.gnupg",
)


def _disk_pct(path: str = "/") -> int:
    st = os.statvfs(path)
    used = (st.f_blocks - st.f_bfree) / st.f_blocks
    return round(used * 100)


def _safe(p: Path) -> bool:
    s = str(p.resolve()) if p.exists() else str(p)
    if any(n in s for n in _NEVER):
        return False
    low = s.lower()
    if low.endswith(".db") or low.endswith(".env"):
        return False
    if "secret" in low or "token" in low or "credential" in low or low.endswith(".key"):
        return False
    return True


def _size(p: Path) -> int:
    try:
        if p.is_file():
            return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


class Janitor:
    def __init__(self, apply: bool):
        self.apply = apply
        self.freed = 0
        self.report: list[tuple[str, int]] = []

    def _do_delete(self, p: Path) -> int:
        if not _safe(p) or not p.exists():
            return 0
        sz = _size(p)
        if self.apply:
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
            except OSError:
                return 0
        self.freed += sz
        return sz

    def _truncate(self, p: Path, keep_bytes: int) -> int:
        """Обрезать лог до последних keep_bytes (файл остаётся, сервис не ломается)."""
        if not _safe(p) or not p.is_file():
            return 0
        try:
            sz = p.stat().st_size
        except OSError:
            return 0
        if sz <= keep_bytes:
            return 0
        saved = sz - keep_bytes
        if self.apply:
            try:
                with open(p, "rb") as f:
                    f.seek(sz - keep_bytes)
                    tail = f.read()
                with open(p, "wb") as f:
                    f.write(tail)
            except OSError:
                return 0
        self.freed += saved
        return saved

    def cat(self, name: str, fn) -> None:
        before = self.freed
        fn()
        self.report.append((name, self.freed - before))

    # --- категории мёртвой генерации ---

    def npm_cache(self):
        self._do_delete(HOME / ".npm" / "_cacache")  # регенерируется npm-ом

    def journald(self):
        if self.apply:
            try:
                subprocess.run(["journalctl", "--user", "--vacuum-size=150M"],
                               capture_output=True, timeout=60)
            except Exception:
                pass
        # оценка экономии до vacuum
        try:
            out = subprocess.run(["journalctl", "--user", "--disk-usage"],
                                 capture_output=True, text=True, timeout=20).stdout
            import re
            m = re.search(r"([0-9.]+)([MG])", out)
            if m:
                v = float(m.group(1)) * (1024**3 if m.group(2) == "G" else 1024**2)
                self.freed += max(0, int(v) - 150 * 1024**2)
        except Exception:
            pass

    def rotated_logs(self):
        # ротированные pm2-логи с таймштампом в имени — старше 2 дней
        for p in (HOME / ".pm2" / "logs").glob("*__*.log"):
            try:
                if NOW - p.stat().st_mtime > 2 * DAY:
                    self._do_delete(p)
            except OSError:
                pass

    def truncate_active_logs(self):
        # активные логи обрезаем до последних 3 МБ
        targets = list((HOME / ".pm2" / "logs").glob("*.log"))
        targets += list(Path("/home/shectory/logs").glob("*.log")) if Path("/home/shectory/logs").exists() else []
        for p in targets:
            if "__" not in p.name:  # ротированные уже обработаны
                self._truncate(p, 3 * 1024 * 1024)

    def openclaw_archives(self):
        # архивные сессии агентов (.reset.*) и трасы старше 7 дней
        base = HOME / ".openclaw" / "agents"
        if not base.exists():
            return
        for pat in ("*.jsonl.reset.*", "*.trajectory.jsonl"):
            for p in base.rglob(pat):
                try:
                    if NOW - p.stat().st_mtime > 7 * DAY:
                        self._do_delete(p)
                except OSError:
                    pass

    def tmp_old(self):
        # /tmp: наши файлы старше 3 дней
        for p in Path("/tmp").glob("*"):
            try:
                if p.stat().st_uid == os.getuid() and NOW - p.stat().st_mtime > 3 * DAY:
                    self._do_delete(p)
            except OSError:
                pass

    def gemini_live_audio(self):
        # временное аудио голосовых аппов старше 1 дня
        roots = [Path("/home/shectory/workspaces/projects/gemini-live-service")]
        cfg = HOME / ".config" / "gemini-live"
        if cfg.exists():
            roots.append(cfg)
        for root in roots:
            if not root.exists():
                continue
            for ext in ("*.wav", "*.pcm", "*.mp3", "*.ogg"):
                for p in root.rglob(ext):
                    try:
                        if NOW - p.stat().st_mtime > 1 * DAY:
                            self._do_delete(p)
                    except OSError:
                        pass

    def old_baks(self):
        # *.bak / *.bak.* старше 7 дней (вне .git)
        for root in (HOME, Path("/home/shectory/workspaces/infra/lineman")):
            for pat in ("*.bak", "*.bak.*"):
                for p in root.glob(pat):
                    try:
                        if NOW - p.stat().st_mtime > 7 * DAY:
                            self._do_delete(p)
                    except OSError:
                        pass
        # бэкапы ключника/openclaw
        for p in list((HOME / ".keymaster").glob("*.bak.*")) + list((HOME / ".openclaw").glob("*.bak.*")):
            try:
                if NOW - p.stat().st_mtime > 7 * DAY:
                    self._do_delete(p)
            except OSError:
                pass

    def run(self):
        self.cat("npm cache", self.npm_cache)
        self.cat("journald vacuum→150M", self.journald)
        self.cat("rotated logs >2d", self.rotated_logs)
        self.cat("truncate active logs→3M", self.truncate_active_logs)
        self.cat("openclaw archives >7d", self.openclaw_archives)
        self.cat("/tmp >3d", self.tmp_old)
        self.cat("gemini-live audio >1d", self.gemini_live_audio)
        self.cat("old .bak >7d", self.old_baks)


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}TB"


def main():
    ap = argparse.ArgumentParser(description="Disk Janitor — очистка мёртвой генерации smain")
    ap.add_argument("--apply", action="store_true", help="реально удалять (иначе dry-run)")
    ap.add_argument("--if-above", type=int, default=0,
                    help="чистить только если диск занят > N%% (для крона)")
    args = ap.parse_args()

    pct = _disk_pct("/")
    if args.if_above and pct <= args.if_above:
        print(f"Диск {pct}% ≤ порога {args.if_above}% — очистка не нужна.")
        return

    j = Janitor(apply=args.apply)
    j.run()

    mode = "УДАЛЕНО" if args.apply else "БУДЕТ ОСВОБОЖДЕНО (dry-run)"
    print(f"Диск до: {pct}% занято")
    print(f"--- {mode} по категориям ---")
    for name, sz in j.report:
        if sz:
            print(f"  {name:28} {_human(sz)}")
    print(f"ИТОГО: {_human(j.freed)}")
    if args.apply:
        print(f"Диск после: {_disk_pct('/')}% занято")


if __name__ == "__main__":
    main()
