#!/usr/bin/env python3
"""ocr_batch — батч OCR изображений/PDF через LM Studio (Lazy Queue).

Использование:
    # PDF:
    python3 ocr_batch.py --pdf /path/to/textbook.pdf --agent eshkola@smain

    # Директория с PNG/JPG:
    python3 ocr_batch.py --images /path/to/pages/ --agent eshkola@smain

    # Несколько PDF сразу:
    python3 ocr_batch.py --pdf book1.pdf book2.pdf --agent eshkola@smain

Опции:
    --pdf FILE [FILE ...]    PDF-файл(ы) для конвертации и OCR
    --images DIR            директория с PNG/JPG файлами
    --agent AGENT           имя агента (для трассировки), e.g. eshkola@smain
    --system PROMPT         системный промпт (по умолчанию: "Extract all text…")
    --out FILE              путь к выходному JSON (по умолчанию: stdout)
    --dpi N                 разрешение при конвертации PDF (по умолчанию: 150)
    --max-tokens N          макс. токенов в ответе на страницу (по умолчанию: 2000)
    --workers N             кол-во параллельных submits (по умолчанию: 4)
    --timeout N             таймаут ожидания одного job в секундах (по умолчанию: 300)
    --lineman URL           адрес Lineman (по умолчанию: http://127.0.0.1:9090)

Результат (stdout или --out):
    {"pages": [{"source": "book.pdf", "page": 1, "text": "...", "job_id": 42}, ...]}
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

LINEMAN = os.environ.get("LINEMAN_URL", "http://127.0.0.1:9090")
DEFAULT_SYSTEM = (
    "You are an OCR engine. Extract ALL text from the image exactly as written. "
    "Preserve paragraph structure. Output only the extracted text, no commentary."
)
POLL_INTERVAL = 3   # секунды между проверками статуса одного job
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ─────────────────────────────── helpers ────────────────────────────────────

def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{LINEMAN}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Agent-Name": "ocr-batch"},
    )
    with _NOPROXY.open(req, timeout=30) as r:
        return json.loads(r.read())


def _get(path: str) -> dict:
    req = urllib.request.Request(
        f"{LINEMAN}{path}",
        headers={"X-Agent-Name": "ocr-batch"},
    )
    with _NOPROXY.open(req, timeout=30) as r:
        return json.loads(r.read())


def img_to_b64(img_bytes: bytes, fmt: str = "PNG") -> str:
    return base64.b64encode(img_bytes).decode()


def vision_user_prompt(b64: str, mime: str, text: str) -> str:
    """Возвращает JSON-строку vision-контента (OpenAI format)."""
    parts = [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        {"type": "text", "text": text},
    ]
    return json.dumps(parts, ensure_ascii=False)


# ─────────────────────────────── PDF → pages ────────────────────────────────

def pdf_to_images(pdf_path: str, dpi: int) -> list[tuple[int, bytes, str]]:
    """Конвертирует PDF в список (page_num, png_bytes, mime)."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        sys.exit("PyMuPDF не установлен. Запусти: python3 -m pip install pymupdf")
    doc = fitz.open(pdf_path)
    pages = []
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    for i, page in enumerate(doc, start=1):
        pix = page.get_pixmap(matrix=mat, alpha=False)
        png_bytes = pix.tobytes("png")
        pages.append((i, png_bytes, "image/png"))
        print(f"  PDF → page {i}/{len(doc)}", end="\r", flush=True)
    print()
    return pages


def images_from_dir(dirpath: str) -> list[tuple[str, bytes, str]]:
    """Читает PNG/JPG из директории. Возвращает (filename, bytes, mime)."""
    exts = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
    result = []
    for p in sorted(Path(dirpath).iterdir()):
        mime = exts.get(p.suffix.lower())
        if mime:
            result.append((p.name, p.read_bytes(), mime))
    return result


# ─────────────────────────────── Lazy Queue I/O ─────────────────────────────

def submit_ocr(b64: bytes, mime: str, label: str,
               agent: str, system: str, max_tokens: int) -> int:
    prompt = vision_user_prompt(b64, mime, "Extract all text from this page.")
    resp = _post("/api/queue/lazy", {
        "kind": "ocr",
        "from_agent": agent,
        "from_node": agent.split("@")[-1] if "@" in agent else "smain",
        "user_prompt": prompt,
        "system_prompt": system,
        "max_tokens": max_tokens,
        "priority": 2,
        "deadline_hint_minutes": 60,
    })
    job_id = resp.get("job_id") or resp.get("id")
    if not job_id:
        raise RuntimeError(f"submit failed: {resp}")
    return int(job_id)


def wait_for_job(job_id: int, timeout: int) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = _get(f"/api/queue/lazy/{job_id}")
        status = data.get("status", "?")
        if status == "done":
            return data.get("output") or ""
        if status == "failed":
            raise RuntimeError(f"job {job_id} failed: {data.get('error', '?')}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"job {job_id} не завершился за {timeout}s")


# ─────────────────────────────── main ───────────────────────────────────────

def process_pages(
    pages: list[dict],   # [{"label": str, "bytes": bytes, "mime": str, "source": str}]
    agent: str,
    system: str,
    max_tokens: int,
    n_workers: int,
    timeout: int,
) -> list[dict]:
    results = [None] * len(pages)
    total = len(pages)

    def handle(idx: int, p: dict) -> tuple[int, dict]:
        b64 = img_to_b64(p["bytes"])
        job_id = submit_ocr(b64, p["mime"], p["label"], agent, system, max_tokens)
        print(f"  [{idx+1}/{total}] submitted job={job_id} label={p['label']}")
        text = wait_for_job(job_id, timeout)
        print(f"  [{idx+1}/{total}] done job={job_id} chars={len(text)}")
        return idx, {"source": p["source"], "label": p["label"],
                     "job_id": job_id, "text": text}

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = {pool.submit(handle, i, p): i for i, p in enumerate(pages)}
        for fut in as_completed(futs):
            try:
                idx, rec = fut.result()
                results[idx] = rec
            except Exception as e:
                idx = futs[fut]
                p = pages[idx]
                print(f"  [{idx+1}/{total}] ERROR {p['label']}: {e}", file=sys.stderr)
                results[idx] = {"source": p["source"], "label": p["label"],
                                "job_id": None, "text": None, "error": str(e)}
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch OCR через LM Studio / Lazy Queue")
    ap.add_argument("--pdf", nargs="+", help="PDF файл(ы)")
    ap.add_argument("--images", help="Директория с PNG/JPG")
    ap.add_argument("--agent", default="eshkola@smain", help="Имя агента")
    ap.add_argument("--system", default=DEFAULT_SYSTEM, help="System prompt")
    ap.add_argument("--out", help="Выходной JSON файл (иначе stdout)")
    ap.add_argument("--dpi", type=int, default=150, help="DPI для PDF конвертации")
    ap.add_argument("--max-tokens", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=4,
                    help="Параллельные submits (рекомендовано 4)")
    ap.add_argument("--timeout", type=int, default=300, help="Таймаут на job (сек)")
    ap.add_argument("--lineman", default=LINEMAN)
    args = ap.parse_args()

    global LINEMAN
    LINEMAN = args.lineman

    if not args.pdf and not args.images:
        ap.print_help()
        return 1

    pages: list[dict] = []

    if args.pdf:
        for pdf_path in args.pdf:
            print(f"Конвертирую PDF: {pdf_path} (dpi={args.dpi})")
            for page_num, png_bytes, mime in pdf_to_images(pdf_path, args.dpi):
                pages.append({
                    "source": os.path.basename(pdf_path),
                    "label": f"{os.path.basename(pdf_path)}:p{page_num}",
                    "bytes": png_bytes,
                    "mime": mime,
                })

    if args.images:
        print(f"Читаю изображения из: {args.images}")
        for fname, img_bytes, mime in images_from_dir(args.images):
            pages.append({
                "source": args.images,
                "label": fname,
                "bytes": img_bytes,
                "mime": mime,
            })

    if not pages:
        print("Нет страниц для обработки.", file=sys.stderr)
        return 1

    print(f"Всего страниц: {len(pages)}. Workers: {args.workers}. Отправляю в Lazy Queue...")
    results = process_pages(pages, args.agent, args.system,
                            args.max_tokens, args.workers, args.timeout)

    # Считаем статистику
    ok = sum(1 for r in results if r and r.get("text"))
    err = sum(1 for r in results if r and r.get("error"))
    total_chars = sum(len(r.get("text") or "") for r in results if r)
    print(f"\nГотово: {ok} успешно, {err} ошибок, ~{total_chars} символов текста")

    output = {"pages": results, "stats": {"ok": ok, "errors": err, "chars": total_chars}}
    out_str = json.dumps(output, ensure_ascii=False, indent=2)

    if args.out:
        Path(args.out).write_text(out_str, encoding="utf-8")
        print(f"Результат сохранён: {args.out}")
    else:
        print(out_str)

    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
