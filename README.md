# TMrex 🦖

TMrex is a Claude skill that runs a screening-level U.S. trademark clearance: it searches a local copy of the USPTO register, the web for unregistered (common-law) use, and domain names, then writes a report covering likelihood of confusion (the DuPont factors), distinctiveness and Section 2 refusal risks, and class coverage.

It is a screening tool, not legal advice. Reports say what was and wasn't searched, and should be reviewed by a trademark attorney.

## How it works

- **The register.** `data/uspto.db` is a SQLite copy of every USPTO trademark case file (about 14 million), with full-text indexes on mark text, owners, goods and services, and statements such as pseudo-marks, translations and disclaimers. It's built from the USPTO Open Data Portal's bulk files: the annual backfile (TRTYRAP) plus the daily application files (TRTDXFAP).
- **Sync.** `sync/sync_daily.py` runs at 6:30 a.m. and 6:30 p.m. It downloads any new daily files, loads them, and rebuilds the search indexes (about 12 minutes when there's something new). For each serial number, the record with the latest transaction date wins. It writes `data/sync_status.json`, and every report cites the date the register is current through.
- **Search.** `search/clearance_search.py` runs a battery of searches for a proposed mark (exact, all words, each word, spelling variants, compounds, pseudo-marks, near spellings) and ranks the candidates. `search/domain_check.py` checks domains with DNS and RDAP.
- **The skill.** `skill/SKILL.md` tells Claude how to run the searches, what to analyse, and how to write the report.

## Requirements

- macOS (the scheduler uses launchd; on Linux, use cron and the same scripts)
- Python 3.9 or later; standard library only, nothing to install
- A free USPTO Open Data Portal API key, from [data.uspto.gov](https://data.uspto.gov)
- About 100 GB of free disk space. The database is about 80 GB.
- Claude Code, or the Claude desktop app with this folder connected

## Install

1. Put this folder in your home folder, not under Desktop, Documents or Downloads (macOS blocks scheduled jobs there):
   ```
   git clone https://github.com/jordanteague/tmrex.git ~/tmrex && cd ~/tmrex
   ```
2. Run setup. It asks for your API key and schedules the sync:
   ```
   bash setup.sh
   ```
3. Build the register. This takes several hours; if it's interrupted, run it again and it picks up where it stopped:
   ```
   caffeinate -i python3 -I sync/build_db.py
   ```
4. Install the skill:
   - **Claude Code:** `mkdir -p ~/.claude/skills/tmrex && cp skill/SKILL.md ~/.claude/skills/tmrex/`
   - **Claude desktop app or claude.ai:** upload `skill/SKILL.md` as a skill in Settings. In the desktop app, connect `~/tmrex` as a folder in the session so the skill can reach the database.
5. Type `/tmrex` to see what to provide, or just ask: "Clear BLUE HERON for project management software."

If you already have the annual backfile zips on disk, `sync/load_local.py` rebuilds the register from them using all the CPU cores. `setup.sh` and `build_db.py` don't use it, so nothing changes unless you run it. It gives the same database as `tm_parser.py`. Loading speed, measured on four of the annual parts (508,768 case files), not counting the index build: `tm_parser.py` 6,400 case files per second, `load_local.py --workers 1` 7,200, and `load_local.py` with 16 workers 19,500. The whole register (13,993,768 case files) loaded on an 18-core Mac at 28,000 per second, in 8.3 minutes, plus about 4 minutes to build the search indexes. With one worker the gain is small, because parsing the XML is about 90% of the work; the rest comes from using more cores. `--workers N` sets the number of workers and `--cache-mb` limits memory.

```
python3 -I sync/load_local.py /path/to/annual/
```

The clearance search keeps the most similar marks of each search with a top-K selection that skips most similarity calculations, looks up each hit's details in a few batched queries instead of four per hit, and returns the same results as before. `python3 -I tests/test_scorer.py` checks the selection, and `python3 -I tests/bench_clearance.py record DIR` then `compare DIR` times a set of marks and checks the results haven't changed.

If you already have a TrademarkBench register, setup clones it instead of building one (instant, using an APFS copy-on-write copy): it looks for `~/Desktop/TrademarkBench/local/uspto_full.db`, or set `BENCH_DB` to its path.

## Day to day

| Task | Command |
| --- | --- |
| Clearance battery for a mark | `python3 -I search/clearance_search.py clearance --mark "BLUE HERON" --classes 009,042` |
| One custom register search | `python3 -I search/clearance_search.py query '{"mark_query": "heron*", "classes": ["009"], "limit": 20}'` |
| Full records | `python3 -I search/clearance_search.py record 99123456` |
| Domain status | `python3 -I search/domain_check.py "blue heron" --names blueheron-app` |
| See what a sync would load | `python3 -I sync/sync_daily.py --dry-run` |
| Run a sync now | `python3 -I sync/sync_daily.py` |
| Check how current the register is | `cat data/sync_status.json` |
| Read the sync log | `tail -50 logs/sync.log` |
| Stop the daily sync | `launchctl bootout gui/$(id -u)/com.tmrex.uspto-sync` |

A failed file is retried on the next run. Each run looks back 7 days before the newest loaded file, so a file USPTO posts late is still picked up.

## Limits

- The search matches words, spelling variants and near spellings. It can miss a word buried in a compound (GOWAVE for WAVE), sound-alikes that differ in their first letters, and foreign equivalents USPTO hasn't recorded; the skill covers these with follow-up searches and notes what's left.
- Words only: design marks and logos aren't compared.
- Prosecution history and design-search codes aren't stored.
- No state registrations or foreign registers.
- Reloading USPTO's next annual backfile once a year would correct any record a daily file missed.

## Credits

`search/search_register.py`, `search/normalize.py` and `sync/tm_parser.py` come from [TrademarkBench](https://github.com/jordanteague/trademarkbench). The trademark data is public USPTO data and isn't covered by this repository's license.

## License

[MIT](LICENSE)
