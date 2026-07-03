#!/usr/bin/env python3
"""Daily lineman.db retention: null out request_body>7d, delete rows>90d, vacuum."""
import sqlite3, os, datetime, logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

DB = os.path.expanduser('~/workspaces/infra/lineman/lineman.db')
NOW = datetime.datetime.now(datetime.timezone.utc)
BODY_RETAIN_DAYS = 7
ROW_RETAIN_DAYS = 14

def run():
    body_cutoff = (NOW - datetime.timedelta(days=BODY_RETAIN_DAYS)).isoformat()
    row_cutoff  = (NOW - datetime.timedelta(days=ROW_RETAIN_DAYS)).isoformat()

    con = sqlite3.connect(DB)
    cur = con.cursor()

    cur.execute('SELECT COUNT(*) FROM request_log WHERE timestamp < ? AND request_body IS NOT NULL', (body_cutoff,))
    body_rows = cur.fetchone()[0]

    cur.execute('SELECT COUNT(*) FROM request_log WHERE timestamp < ?', (row_cutoff,))
    old_rows = cur.fetchone()[0]

    log.info(f'Nulling request_body for {body_rows} rows older than {BODY_RETAIN_DAYS}d')
    cur.execute('UPDATE request_log SET request_body=NULL WHERE timestamp < ? AND request_body IS NOT NULL', (body_cutoff,))

    log.info(f'Deleting {old_rows} rows older than {ROW_RETAIN_DAYS}d')
    cur.execute('DELETE FROM request_log WHERE timestamp < ?', (row_cutoff,))

    con.commit()

    # VACUUM блокирует БД на минуты (2026-06-26: 7м38с) — на это окно встаёт весь
    # Lineman и klod-dispatch сыпет tick error. Гоняем его только когда есть что
    # возвращать ОС: свободных страниц больше 20% файла. Иначе ежедневный VACUUM
    # стабильно экономил 0.0 MB (лог за июнь-июль) при полной блокировке.
    freelist = con.execute('PRAGMA freelist_count').fetchone()[0]
    pages = con.execute('PRAGMA page_count').fetchone()[0]
    if pages and freelist / pages > 0.2:
        size_before = os.path.getsize(DB)
        log.info(f'Running VACUUM (free {freelist}/{pages} pages, size {size_before/1024/1024:.1f} MB)...')
        con.execute('VACUUM')
        size_after = os.path.getsize(DB)
        log.info(f'Done. Size after: {size_after/1024/1024:.1f} MB (saved {(size_before-size_after)/1024/1024:.1f} MB)')
    else:
        log.info(f'VACUUM skipped: free pages {freelist}/{pages} ниже порога 20%')
    con.close()

if __name__ == '__main__':
    run()
