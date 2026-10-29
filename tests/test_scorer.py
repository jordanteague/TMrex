"""Scorer.top must keep exactly the marks a full sort by similarity would keep.

  python3 -I tests/test_scorer.py
"""
import random
import sys
import unittest
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "search"))
import clearance_search as cs  # noqa: E402
from normalize import normalize  # noqa: E402

WORDS = ["blue", "heron", "star", "smart", "home", "hub", "wave", "go", "apple", "lumina", "kwik", "stop", "pro", "x"]


def random_marks(rng, n):
    return [(str(i), " ".join(rng.choice(WORDS) + rng.choice(["", "s", "z", "e"]) for _ in range(rng.randint(1, 4))))
            for i in range(n)]


def brute_force(rows, target, k, keep=None):
    scored = [(SequenceMatcher(None, target, normalize(m)).ratio(), sn) for sn, m in rows
              if not keep or keep(m, normalize(m))]
    scored.sort(key=lambda x: -x[0])
    return min(len(scored), k), sorted((s for s, _ in scored[:k]), reverse=True)


class ScorerTest(unittest.TestCase):
    def check(self, seed, n, k, keep=None):
        rng = random.Random(seed)
        rows = random_marks(rng, n)
        target = normalize("blue heron")
        scorer = cs.Scorer(target)
        matches, serials = scorer.top(iter(rows), k, keep)
        by_serial = dict(rows)
        got = [scorer.score[by_serial[sn]] for sn in serials]
        want_matches, want_scores = brute_force(rows, target, k, keep)
        self.assertEqual(matches, want_matches)
        self.assertEqual(got, want_scores)  # same scores in the same order; ties may pick different marks
        return scorer

    def test_matches_full_sort(self):
        for seed in range(20):
            self.check(seed, n=400, k=25)

    def test_fewer_rows_than_k(self):
        self.check(1, n=10, k=50)

    def test_keep_filter(self):
        self.check(2, n=500, k=30, keep=lambda m, norm: "s" in norm)

    def test_prunes_most_of_the_work(self):
        scorer = self.check(3, n=5000, k=25)
        self.assertLess(scorer.stats["ratio_calls"], 5000 // 2)


if __name__ == "__main__":
    unittest.main()
