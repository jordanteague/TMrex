"""Search the local USPTO register (data/uspto.db) for a trademark clearance.

Three commands, each printing JSON:

  clearance  Run a fixed battery of searches for a proposed mark and return one de-duplicated list of
             candidate conflicts, each tagged with the searches that found it and a similarity score.
               python3 -I search/clearance_search.py clearance --mark "BLUE HERON" --classes 009,042
               [--include-dead] [--per-search 200] [--max-candidates 150]
  query      One search with the full search_register syntax (Boolean queries on mark, goods, owner and
             statements; class, status, date and header filters). See docs/search_tool.md in TrademarkBench.
               python3 -I search/clearance_search.py query '{"mark_query": "heron*", "classes": ["009"], "limit": 20}'
  record     Full records for serial numbers.
               python3 -I search/clearance_search.py record 99123456 98765432

search_register.py and normalize.py are TrademarkBench's, copied unchanged. Here they run with no
excluded application and no viewing budget. Every result carries register_current_through, from
data/sync_status.json, which the report must cite.

What the clearance battery covers, and what it doesn't: exact marks, marks containing all the words,
marks sharing any distinctive word, the words joined or split, generated spelling variants
(c/k/q, ph/f, s/z, i/y, doubled letters, plurals, number words), USPTO pseudo-marks and translations,
compounds that start with a word or its variant (BLUEWAVE for BLUE), and near spellings that share a
word's first three letters. When a search has more matches than --per-search, it keeps the ones most
similar to the proposed mark. It can miss a word buried inside a compound (MYBLUE for BLUE), sound-alikes
that differ in their first letters beyond the generated variants, foreign-language equivalents USPTO
hasn't recorded, and design marks whose word portion differs.
"""
import argparse
import heapq
import json
import os
import re
import sqlite3
import sys
import time
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))  # -I drops the script dir
import search_register as sr  # noqa: E402
from normalize import normalize  # noqa: E402

ROOT = SCRIPT_DIR.parent
DB_PATH = Path(os.environ.get("TMC_DB", ROOT / "data" / "uspto.db"))
STATUS_PATH = DB_PATH.parent / "sync_status.json"
STOPWORDS = {"the", "a", "an", "and", "of", "for", "by", "to", "in", "on", "at", "with", "co", "inc", "llc", "ltd",
             "company", "corp", "corporation", "group", "brand", "brands"}
NUMBER_WORDS = {"1": ["one", "won"], "2": ["two", "to", "too"], "4": ["four", "for"], "8": ["eight", "ate"],
                "one": ["1"], "two": ["2"], "to": ["2"], "too": ["2"], "four": ["4"], "for": ["4"], "eight": ["8"]}
# Letter swaps that sound alike, tried in both directions.
SWAPS = [("ph", "f"), ("ck", "k"), ("c", "k"), ("q", "k"), ("kw", "qu"), ("x", "ks"), ("z", "s"), ("y", "i"),
         ("ee", "i"), ("ie", "y"), ("oo", "u"), ("ou", "u"), ("ght", "t"), ("gh", "g"), ("wr", "r"), ("kn", "n"),
         ("tion", "shun"), ("er", "or"), ("er", "ar"), ("s", "c")]
MAX_VARIANTS = 40
FUZZY_MIN = 0.75     # word similarity needed to keep a near spelling
GOODS_SNIPPET = 400
CACHE_SIZE = 1 << 20  # the register repeats words heavily
IN_CHUNK = 500        # serial numbers per IN (...) list


def connect():
    if not DB_PATH.exists():
        sys.exit(f"{DB_PATH} not found: run setup.sh first")
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def register_date():
    try:
        return json.loads(STATUS_PATH.read_text()).get("daily_files_through")
    except (OSError, ValueError):
        return None


def words_of(mark):
    return [w for w in re.findall(r"[a-z0-9]+", normalize_spaces(mark)) if w]


def normalize_spaces(mark):
    return (mark or "").casefold().replace("&", " and ")


def distinctive(words):
    return [w for w in words if w not in STOPWORDS and len(w) >= 3]


@lru_cache(maxsize=CACHE_SIZE)
def sound_key(word):
    """A rough sound-alike key: similar-sounding spellings of a word usually share it."""
    w = word.lower()
    for a, b in [("ph", "f"), ("ck", "k"), ("q", "k"), ("x", "ks"), ("z", "s"), ("gh", "g"), ("wr", "r"),
                 ("kn", "n"), ("dg", "j")]:
        w = w.replace(a, b)
    w = re.sub(r"c(?=[eiy])", "s", w).replace("c", "k")
    head, tail = w[:1], re.sub(r"[aeiouyhw]", "", w[1:])
    return re.sub(r"(.)\1+", r"\1", head + tail)


def spelling_variants(word):
    out = set()
    for a, b in SWAPS:
        for x, y in ((a, b), (b, a)):
            if x in word:
                out.add(word.replace(x, y))
    out.add(re.sub(r"(.)\1", r"\1", word))                    # doubled letters collapsed
    out.add(re.sub(r"([bcdfgklmnprst])", r"\1\1", word, count=1))  # first consonant doubled
    out.update([word + "s", word + "z", word[:-1]] if word.endswith("s") else [word + "s", word + "z"])
    out.add(word[:-1] if word.endswith("e") else word + "e")      # silent e
    out.update(NUMBER_WORDS.get(word, []))
    out.discard(word)
    return sorted(v for v in out if len(v) >= 2)


@lru_cache(maxsize=CACHE_SIZE)
def close_spelling(mark, word):
    key = sound_key(word)
    return int(any(w != word and (SequenceMatcher(None, word, w).ratio() >= FUZZY_MIN or
                                  (len(w) >= 3 and sound_key(w) == key)) for w in words_of(mark)))


def fts_terms(words):
    return " ".join(f'"{w}"' for w in words)


class Scorer:
    """The K marks most similar to the proposed mark, without scoring every match.

    A common word matches hundreds of thousands of marks, and only the best --per-search are kept. Once
    K are held, a mark can only get in if its score beats the worst one held, and two cheap upper bounds
    on SequenceMatcher.ratio() (from the lengths, then from the letter counts) rule most marks out
    before the expensive ratio() runs. Scores and normalized names are cached across searches.
    """

    def __init__(self, target):
        self.target = target
        self.sm = SequenceMatcher(None, target)
        self.norm, self.score = {}, {}
        self.stats = {"rows": 0, "skipped_by_length": 0, "skipped_by_letters": 0, "ratio_calls": 0}

    def similarity(self, mark):
        """The score of a mark, from the cache when a search already computed it."""
        score = self.score.get(mark)
        if score is None:
            self.sm.set_seq2(self.norm.get(mark) or normalize(mark or ""))
            score = self.score[mark] = self.sm.ratio()
        return score

    def top(self, rows, k, keep=None, deadline=None):
        """rows: (serial, mark_text) pairs. keep(mark_text) -> bool filters. Returns (matches, serials)."""
        heap, seen, matches = [], 0, 0
        for serial, mark in rows:
            self.stats["rows"] += 1
            seen += 1
            if deadline and seen % 2048 == 0 and time.monotonic() > deadline:
                raise sqlite3.OperationalError("interrupted")
            norm = self.norm.get(mark)
            if norm is None:
                norm = self.norm[mark] = normalize(mark or "")
            full = len(heap) >= k
            if full and mark not in self.score:
                self.sm.set_seq2(norm)
                if self.sm.real_quick_ratio() <= heap[0][0]:
                    self.stats["skipped_by_length"] += 1
                    continue
                if self.sm.quick_ratio() <= heap[0][0]:
                    self.stats["skipped_by_letters"] += 1
                    continue
            if keep and not keep(mark, norm):
                continue
            matches += 1
            score = self.score.get(mark)
            if score is None:
                self.sm.set_seq2(norm)
                score = self.score[mark] = self.sm.ratio()
                self.stats["ratio_calls"] += 1
            if not full:
                heapq.heappush(heap, (score, -seen, serial))
            elif score > heap[0][0]:
                heapq.heapreplace(heap, (score, -seen, serial))
        return min(matches, k), [sn for _, _, sn in sorted(heap, reverse=True)]


class Searcher:
    def __init__(self, db, classes, include_dead, per_search, scorer, deadline=None):
        self.db, self.classes, self.include_dead, self.per_search = db, classes, include_dead, per_search
        self.scorer, self.deadline = scorer, deadline
        self.hits = {}      # serial -> set of search labels
        self.searches = []  # what was run, for the report's search-scope section

    def _filters(self, use_classes):
        where, params = [], []
        if not self.include_dead:
            where.append(f"NOT {sr.DEAD_SQL}")
        if use_classes and self.classes:
            where.append(f"EXISTS (SELECT 1 FROM classes c WHERE c.serial_number = cf.serial_number "
                         f"AND c.intl_class IN ({','.join('?' * len(self.classes))}))")
            params += self.classes
        return where, params

    def mark_search(self, label, match, use_classes, keep=None, query_note=None):
        """keep(mark_text, normalized) -> bool drops matches the full-text query can't rule out itself."""
        where, params = self._filters(use_classes)
        sql = ("SELECT cf.serial_number, cf.mark_text FROM mark_fts JOIN case_files cf ON cf.rowid = mark_fts.rowid "
               f"WHERE mark_fts MATCH ? {''.join(' AND ' + w for w in where)}")
        rows = self.db.execute(sql, [f"mark_text : ({match})"] + params)
        found, kept = self.scorer.top(rows, self.per_search + 1, keep, self.deadline)
        self._record(label, query_note or match, use_classes, found, kept[:self.per_search])

    def statement_search(self, label, match, use_classes):
        where, params = self._filters(use_classes)
        sql = ("SELECT DISTINCT cf.serial_number FROM statements_fts JOIN statements st ON st.rowid = statements_fts.rowid "
               "JOIN case_files cf ON cf.serial_number = st.serial_number "
               "WHERE statements_fts MATCH ? AND SUBSTR(st.type_code, 1, 2) IN ('PM', 'TR', 'TL') "
               f"{''.join(' AND ' + w for w in where)} LIMIT ?")
        rows = self.db.execute(sql, [f"text : ({match})"] + params + [self.per_search + 1]).fetchall()
        self._record(label, match, use_classes, len(rows), [sn for (sn,) in rows[:self.per_search]])

    def near_spellings(self, word, use_classes):
        """Marks with a word that shares this word's first three letters and is spelled or sounds close to it."""
        self.mark_search(f"near spelling of {word.upper()}", f'"{word[:3]}"*', use_classes,
                         lambda mark, _norm: close_spelling(mark, word) == 1,
                         query_note=f'"{word[:3]}"*, then a word with spelling similarity >= {FUZZY_MIN} or the same sound key')

    def _record(self, label, query, use_classes, found, kept):
        cap = self.per_search
        self.searches.append({"search": label, "query": query, "classes": self.classes if use_classes else "all",
                              "matches": f"more than {cap}" if found > cap else found, "kept": len(kept)})
        for sn in kept:
            self.hits.setdefault(sn, set()).add(label)


def clearance(args):
    db = connect()
    classes = [c.strip().zfill(3) for c in (args.classes or "").split(",") if c.strip()]
    words = words_of(args.mark)
    if not words:
        sys.exit("--mark has no letters or digits")
    key_words = distinctive(words) or words
    target = normalize(args.mark)
    # Deadline, as in search_register: a runaway query stops instead of hanging the skill.
    deadline = time.monotonic() + args.timeout
    scorer = Scorer(target)
    s = Searcher(db, classes, args.include_dead, args.per_search, scorer, deadline)
    db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 100000)
    try:
        s.mark_search("exact mark (all classes)", fts_terms(words), False, lambda _mark, norm: norm == target)
        s.mark_search("all words, any order", fts_terms(words), bool(classes))
        if len(words) > 1:
            s.mark_search("words joined", f'"{"".join(words)}"', False)
        for w in key_words:
            s.mark_search(f"shares the word {w.upper()}", f'"{w}"', bool(classes))
            variants = spelling_variants(w)[:MAX_VARIANTS]
            if variants:
                s.mark_search(f"spelling variants of {w.upper()}", " OR ".join(f'"{v}"' for v in variants), bool(classes))
            s.mark_search(f"word starting with {w.upper()} or a variant (compounds)",
                          " OR ".join(f'"{v}"*' for v in [w] + variants), bool(classes))
            s.statement_search(f"pseudo-mark or translation contains {w.upper()}", f'"{w}"', bool(classes))
            s.near_spellings(w, bool(classes))
    except sqlite3.OperationalError as exc:
        if "interrupted" not in str(exc):
            raise
        s.searches.append({"search": "STOPPED", "query": f"timed out after {args.timeout}s; results are partial"})
    finally:
        db.set_progress_handler(None, 0)
    if os.environ.get("TMC_STATS"):
        print(json.dumps(scorer.stats), file=sys.stderr)

    # Ranking needs only the header row and the classes, so fetch those for every hit in a few batched queries,
    # rank, and fetch the long goods and statement text for the candidates that are actually printed.
    serials = list(s.hits)
    headers, class_lists = fetch_headers(db, serials), fetch_classes(db, serials)
    candidates = [summary(sn, sorted(labels), headers[sn], class_lists[sn], scorer.similarity, classes)
                  for sn, labels in s.hits.items()]
    strong = {"exact mark (all classes)", "all words, any order", "words joined"}
    candidates.sort(key=lambda c: (c["status"] == "dead", not strong & set(c["found_by"]), c["class_overlap"] == 0,
                                   -c["similarity"], c["serial_number"]))
    shown = candidates[:args.max_candidates]
    add_details(db, shown)
    print(json.dumps({
        "register_current_through": register_date(),
        "proposed_mark": args.mark, "classes_searched": classes or "all", "include_dead": args.include_dead,
        "searches_run": s.searches,
        "candidates_found": len(candidates), "candidates_shown": len(shown),
        "candidates": shown,
        "note": "Candidates are sorted live first; then exact, all-words and joined-words matches; then those sharing a "
                "searched class; then by mark similarity. Similarity is a "
                "spelling score from 0 to 1, not a legal judgment. Use `record` for full records.",
    }, indent=2, ensure_ascii=False))


def _batched(db, sql, serials, tail=""):
    """Yield rows of `sql` (which ends in `serial_number IN (%s)`) for the serials, IN_CHUNK at a time."""
    for i in range(0, len(serials), IN_CHUNK):
        part = serials[i:i + IN_CHUNK]
        yield from db.execute(sql % ",".join("?" * len(part)) + tail, part)


def fetch_headers(db, serials):
    return {row[0]: row[1:] for row in _batched(
        db, "SELECT serial_number, registration_number, mark_text, filing_date, registration_date, owner_name, "
            f"({sr.STATUS_SQL}) FROM case_files cf WHERE serial_number IN (%s)", serials)}


def fetch_classes(db, serials):
    out = {sn: [] for sn in serials}
    for sn, c in _batched(db, "SELECT serial_number, intl_class FROM classes WHERE serial_number IN (%s)",
                          serials, " ORDER BY serial_number, intl_class"):
        out[sn].append(c)
    return out


def summary(sn, labels, header, its_classes, similarity, classes):
    """One candidate, without the goods and statements text (add_details fills those in for printed candidates)."""
    reg, mark, fdate, rdate, owner, status = header
    return {
        "serial_number": sn, "registration_number": reg if reg and reg.strip("0") else None, "mark": mark,
        "status": status, "filing_date": fdate, "registration_date": rdate, "owner": owner,
        "classes": its_classes, "class_overlap": len(set(its_classes) & set(classes)),
        "similarity": round(similarity(mark), 2),
        "found_by": labels,
        "goods_services": None,
        "statements": None,
    }


def add_details(db, candidates):
    serials = [c["serial_number"] for c in candidates]
    goods = {sn: [] for sn in serials}
    for sn, c, t in _batched(db, "SELECT serial_number, intl_class, text FROM goods_services WHERE serial_number IN (%s)",
                             serials, " ORDER BY serial_number, intl_class, type_code"):
        if t:
            goods[sn].append(f"[{c}] {t}")
    extra = {sn: [] for sn in serials}
    for sn, tc, t in _batched(db, "SELECT serial_number, type_code, text FROM statements WHERE serial_number IN (%s) "
                                  "AND SUBSTR(type_code, 1, 2) IN ('PM', 'TR', 'TL', 'D0', 'D1', 'DS')",
                              serials, " ORDER BY serial_number, type_code"):
        if t:
            extra[sn].append(f"{sr.STATEMENT_TYPES.get(tc[:2], tc)}: {t}")
    for c in candidates:
        text = " || ".join(goods[c["serial_number"]])
        c["goods_services"] = text[:GOODS_SNIPPET] + ("…" if len(text) > GOODS_SNIPPET else "")
        c["statements"] = extra[c["serial_number"]]


def query(args):
    session = sr.Session(connect(), "", max_records=10 ** 9)
    result = session.run(json.loads(args.json))
    result.pop("records_viewable_remaining", None)
    print(json.dumps({"register_current_through": register_date(), **result}, indent=2, ensure_ascii=False))


def record(args):
    session = sr.Session(connect(), "", max_records=10 ** 9)
    records = [session._record(sn) for sn in args.serials
               if session.db.execute("SELECT 1 FROM case_files WHERE serial_number = ?", (sn,)).fetchone()]
    missing = [sn for sn in args.serials if sn not in {r["serial_number"] for r in records}]
    print(json.dumps({"register_current_through": register_date(), "records": records, "not_found": missing},
                     indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("clearance", help="battery of searches for a proposed mark")
    c.add_argument("--mark", required=True)
    c.add_argument("--classes", help="comma-separated international classes, e.g. 009,042 (include related classes)")
    c.add_argument("--include-dead", action="store_true", help="also return abandoned, cancelled and expired marks")
    c.add_argument("--per-search", type=int, default=200, help="records kept per search (default 200)")
    c.add_argument("--max-candidates", type=int, default=150, help="candidates printed (default 150)")
    c.add_argument("--timeout", type=int, default=180, help="seconds before the battery stops (default 180)")
    c.set_defaults(fn=clearance)
    q = sub.add_parser("query", help="one search_register query, as JSON")
    q.add_argument("json")
    q.set_defaults(fn=query)
    r = sub.add_parser("record", help="full records by serial number")
    r.add_argument("serials", nargs="+")
    r.set_defaults(fn=record)
    args = p.parse_args()
    sr.DB_PATH = DB_PATH
    args.fn(args)


if __name__ == "__main__":
    main()
