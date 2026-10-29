"""Build data/uspto.db from annual backfile zips already on disk, parsing them in parallel.

Usage: python3 -I sync/load_local.py [--db PATH] [--workers N] [--cache-mb N] ZIP_OR_DIR [ZIP_OR_DIR ...]
A directory means every *.zip in it. It reads the zips in place and deletes nothing. setup.sh and
build_db.py do not use it.

Worker processes unzip and parse (the slow part: one core does ~8,000 case files a second) and send
batches to this process, the only SQLite writer. tm_parser.write_batch applies the same rule as
everywhere else: for each serial number the record with the latest transaction date wins.
Re-running skips files already recorded in processed_files, so it resumes after an interruption.
Memory: each worker holds one 5,000-record batch while parsing, and at most 2 per worker wait for the
writer; --workers 1 --cache-mb 64 runs in a few hundred MB. Afterwards run sync_daily.py to bring the
register up to date.
"""
import argparse
import multiprocessing as mp
import os
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))  # -I drops the script dir
import tm_parser  # noqa: E402
import sync_daily as sd  # noqa: E402


def iter_batches(path):
    """Yield (source, rows) batches and finally (source, None, total) for each XML in a zip."""
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".xml"):
                continue
            source = f"{Path(path).name}/{name}"
            rows, total, stack = [], 0, []
            with zf.open(name) as stream:
                for event, el in ET.iterparse(stream, events=("start", "end")):
                    if event == "start":
                        stack.append(el)
                        continue
                    stack.pop()
                    if el.tag != "case-file":
                        continue
                    total += 1
                    row = tm_parser.parse_case(el)
                    if row:
                        rows.append(row)
                    if len(rows) >= tm_parser.BATCH:
                        yield source, rows, total
                        rows = []
                    if stack:
                        stack[-1].remove(el)
            yield source, rows, total
            yield source, None, total


def worker(tasks, results):
    while True:
        path = tasks.get()
        if path is None:
            return
        try:
            for source, rows, total in iter_batches(path):
                results.put(("rows" if rows is not None else "done", path, source, rows, total))
        except Exception as exc:  # keep going; a rerun retries the file
            results.put(("error", path, None, f"{type(exc).__name__}: {exc}", 0))


def find_zips(args):
    zips = []
    for arg in args:
        p = Path(arg).expanduser()
        zips += sorted(p.glob("*.zip")) if p.is_dir() else [p]
    return zips


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--db", default=str(sd.DB_PATH))
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--cache-mb", type=int, default=512, help="SQLite page cache for the writer (default 512)")
    args = ap.parse_args()

    db_path = Path(args.db).expanduser()
    if db_path == sd.DB_PATH and not sd.take_lock():
        sys.exit("another build or sync is running")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = tm_parser.connect(db_path)
    db.execute("PRAGMA synchronous = OFF")
    db.execute(f"PRAGMA cache_size = -{args.cache_mb * 1024}")

    done = {row[0].split("/")[0] for row in db.execute("SELECT source_file FROM processed_files")}
    todo = [z for z in find_zips(args.inputs) if z.name not in done]
    if not todo:
        sys.exit("nothing to load: every file is already in processed_files")
    sd.log(f"{len(todo)} files, {args.workers} workers -> {db_path}")

    tasks, results = mp.Queue(), mp.Queue(2 * args.workers)  # bounds memory when the writer falls behind
    for z in todo:
        tasks.put(str(z))
    procs = [mp.Process(target=worker, args=(tasks, results), daemon=True) for _ in range(args.workers)]
    for _ in procs:
        tasks.put(None)
    for p in procs:
        p.start()

    start = time.time()
    finished, failed, cases = 0, [], 0
    while finished < len(todo):
        kind, path, source, payload, total = results.get()
        if kind == "rows":
            tm_parser.write_batch(db, payload, source)
        elif kind == "done":
            db.execute("INSERT OR REPLACE INTO processed_files VALUES (?, ?, ?, datetime('now'))",
                       (source, total, total))
            db.commit()
            finished += 1
            cases += total
            sd.log(f"[{finished}/{len(todo)}] {Path(path).name}: {total} case files "
                   f"(elapsed {(time.time() - start) / 60:.1f} min)")
        else:
            finished += 1
            failed.append(Path(path).name)
            sd.log(f"  FAILED {Path(path).name}: {payload}")
    for p in procs:
        p.join()

    if failed:
        db.close()
        sys.exit(f"INCOMPLETE: {len(failed)} files failed ({', '.join(failed)}). Run this again to retry them.")
    tm_parser.build_search_index(db)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if db_path == sd.DB_PATH:
        status = sd.write_status(db, [z.name for z in todo], failed)
        sd.log(f"COMPLETE: {status['case_files']:,} case files")
    db.close()


if __name__ == "__main__":
    main()
