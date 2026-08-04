"""
Scores the deterministic matcher against ground_truth.csv, broken out by
discrepancy type. This is the number that justifies the agent layer: it
should be high on the "easy" types and ~zero on the ones that structurally
require investigation (batching, timing outliers, hard returns, orphans).

Run:
    uv run python -m recon_engine.matching.evaluate_baseline
"""

import csv
from collections import defaultdict
from pathlib import Path

from recon_engine.matching.deterministic_matcher import DATA_DIR, load_data, run_matcher


def load_ground_truth() -> tuple[dict[str, dict], list[str]]:
    gt = {}
    orphan_settlement_ids = []
    with (DATA_DIR / "ground_truth.csv").open() as f:
        for row in csv.DictReader(f):
            if row["internal_txn_id"]:
                gt[row["internal_txn_id"]] = row
            else:
                # orphan_settlement rows: no internal txn, keyed by settlement line instead
                orphan_settlement_ids.extend(row["settlement_line_ids"].split("|"))
    return gt, orphan_settlement_ids


def is_correct(result, gt_row: dict) -> bool:
    expected_ids = set(gt_row["settlement_line_ids"].split("|")) if gt_row["settlement_line_ids"] else set()
    actual_ids = set(result.matched_settlement_ids)

    if gt_row["discrepancy_type"] in ("orphan_internal",):
        return result.match_type == "unresolved"

    if gt_row["discrepancy_type"] == "duplicate_line":
        # correct if flagged ambiguous with both of the true duplicate lines surfaced
        return result.match_type == "ambiguous" and actual_ids == expected_ids

    if gt_row["discrepancy_type"] == "split_settlement":
        return result.match_type == "split_two" and actual_ids == expected_ids

    # clean_match, fee_variance, descriptor_mangled, timing_outlier, hard_return, batched_settlement
    return result.match_type == "exact_one_to_one" and actual_ids == expected_ids


def main() -> None:
    ledger, settlement = load_data()
    results = run_matcher(ledger, settlement)
    gt, orphan_settlement_ids = load_ground_truth()

    results_by_id = {r.internal_txn_id: r for r in results}
    all_claimed_settlement_ids = {sid for r in results for sid in r.matched_settlement_ids}

    by_type_total: dict[str, int] = defaultdict(int)
    by_type_correct: dict[str, int] = defaultdict(int)

    for txn_id, gt_row in gt.items():
        dtype = gt_row["discrepancy_type"]
        result = results_by_id[txn_id]
        by_type_total[dtype] += 1
        if is_correct(result, gt_row):
            by_type_correct[dtype] += 1

    # orphan_settlement: correct if the matcher never claimed that settlement
    # line for any internal transaction (i.e. correctly left it unmatched)
    by_type_total["orphan_settlement"] = len(orphan_settlement_ids)
    by_type_correct["orphan_settlement"] = sum(
        1 for sid in orphan_settlement_ids if sid not in all_claimed_settlement_ids
    )

    print(f"{'discrepancy_type':22s} {'correct/total':>15s} {'accuracy':>10s}")
    print("-" * 50)
    total_correct = total = 0
    for dtype in sorted(by_type_total.keys()):
        c, t = by_type_correct[dtype], by_type_total[dtype]
        total_correct += c
        total += t
        print(f"{dtype:22s} {c:>6d}/{t:<8d} {c/t:>9.1%}")
    print("-" * 50)
    print(f"{'OVERALL':22s} {total_correct:>6d}/{total:<8d} {total_correct/total:>9.1%}")

    unresolved = [r for r in results if r.match_type == "unresolved"]
    ambiguous = [r for r in results if r.match_type == "ambiguous"]
    print(f"\nRouted to exception queue: {len(unresolved)} unresolved, {len(ambiguous)} ambiguous "
          f"({(len(unresolved)+len(ambiguous))/len(results):.1%} of all transactions)")
    print("^ this is the volume the LangGraph agent layer needs to handle")


if __name__ == "__main__":
    main()
