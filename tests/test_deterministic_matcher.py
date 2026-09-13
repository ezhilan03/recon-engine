"""Financial allocation regressions; no database, model, or network required."""

from datetime import date, timedelta
from itertools import permutations
import unittest

from recon_engine.matching.deterministic_matcher import (
    InternalRow, SettlementRow, run_matcher,
)

DAY = date(2026, 1, 1)


def transaction(key, amount=100):
    return InternalRow(key, DAY, amount, "1234", "test", "")


def settlement(key, amount=100, day=0):
    return SettlementRow(key, DAY + timedelta(days=day), amount, 0, amount,
                         "1234", "test", "batch", "")


class MatcherTests(unittest.TestCase):
    def test_single_match(self):
        result, = run_matcher([transaction("t")], [settlement("s")])
        self.assertEqual((result.match_type, result.matched_settlement_ids),
                         ("exact_one_to_one", ["s"]))

    def test_two_transactions_cannot_claim_one_line(self):
        results = run_matcher([transaction("a"), transaction("b")], [settlement("s")])
        self.assertEqual([r.match_type for r in results], ["ambiguous", "ambiguous"])

    def test_competing_exact_and_split_require_review(self):
        results = run_matcher([transaction("a", 60), transaction("b")],
                              [settlement("x", 60), settlement("y", 40)])
        self.assertTrue(all(r.match_type == "ambiguous" for r in results))

    def test_multiple_valid_splits_are_ambiguous(self):
        rows = [settlement("a", 60), settlement("b", 40),
                settlement("c", 70), settlement("d", 30)]
        result, = run_matcher([transaction("t")], rows)
        self.assertEqual(result.match_type, "ambiguous")
        self.assertEqual(result.matched_settlement_ids, ["a", "b", "c", "d"])

    def test_single_split(self):
        result, = run_matcher([transaction("t")],
                              [settlement("b", 40, 1), settlement("a", 60)])
        self.assertEqual((result.match_type, result.matched_settlement_ids),
                         ("split_two", ["a", "b"]))

    def test_split_dates_must_be_adjacent(self):
        result, = run_matcher([transaction("t")],
                              [settlement("a", 60), settlement("b", 40, 4)])
        self.assertEqual(result.match_type, "unresolved")

    def test_one_cent_boundary_is_inclusive(self):
        result, = run_matcher([transaction("t", 100)], [settlement("s", 100.01)])
        self.assertEqual(result.match_type, "exact_one_to_one")

    def test_larger_amount_difference_remains_unresolved(self):
        result, = run_matcher([transaction("t")], [settlement("s", 100.02)])
        self.assertEqual(result.match_type, "unresolved")

    def test_date_window_boundaries(self):
        for offset, expected in [(-1, "unresolved"), (0, "exact_one_to_one"),
                                 (5, "exact_one_to_one"), (6, "unresolved")]:
            with self.subTest(offset=offset):
                result, = run_matcher([transaction("t")], [settlement("s", day=offset)])
                self.assertEqual(result.match_type, expected)

    def test_duplicate_source_keys_are_rejected(self):
        for ledger, rows in [([transaction("t"), transaction("t")], [settlement("s")]),
                             ([transaction("t")], [settlement("s"), settlement("s")])]:
            with self.assertRaises(ValueError):
                run_matcher(ledger, rows)

    def test_non_finite_money_is_rejected(self):
        for amount in [float("nan"), float("inf"), float("-inf")]:
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                run_matcher([transaction("t", amount)], [settlement("s")])

    def test_permutations_preserve_decisions_and_unique_allocations(self):
        ledger = [transaction("a", 60), transaction("b"), transaction("c", 200)]
        rows = [settlement("x", 60), settlement("y", 40), settlement("z", 200)]
        expected = None
        for txns in permutations(ledger):
            for settlements in permutations(rows):
                results = run_matcher(list(txns), list(settlements))
                actual = sorted((r.internal_txn_id, r.match_type, r.matched_settlement_ids)
                                for r in results)
                if expected is None:
                    expected = actual
                self.assertEqual(actual, expected)
                allocated = [sid for r in results if r.match_type in
                             ("exact_one_to_one", "split_two")
                             for sid in r.matched_settlement_ids]
                self.assertEqual(len(allocated), len(set(allocated)))


if __name__ == "__main__":
    unittest.main()
