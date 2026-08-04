"""
Same evaluation as evaluate_baseline.py, but reads from Postgres instead
of CSV -- confirms the DB migration didn't silently change any data.

Run:
    uv run python -m recon_engine.matching.evaluate_baseline_db
"""

from recon_engine.db.repository import load_data_from_db
from recon_engine.matching.deterministic_matcher import run_matcher
from recon_engine.matching.evaluate_baseline import is_correct, load_ground_truth
from collections import defaultdict


def main() -> None:
    ledger, settlement = load_data_from_db()
    print(f"Loaded from DB: {len(ledger)} internal, {len(settlement)} settlement rows")

    results = run_matcher(ledger, settlement)
    gt, orphan_settlement_ids = load_ground_truth()  # ground truth still read from CSV -- eval-only, never in the app's runtime path

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

    by_type_total["orphan_settlement"] = len(orphan_settlement_ids)
    by_type_correct["orphan_settlement"] = sum(
        1 for sid in orphan_settlement_ids if sid not in all_claimed_settlement_ids
    )

    total_correct = sum(by_type_correct.values())
    total = sum(by_type_total.values())
    print(f"\nOVERALL (DB-backed): {total_correct}/{total} = {total_correct/total:.1%}")
    print("Matches CSV-backed run? Compare against the 83.1% from evaluate_baseline.py")


if __name__ == "__main__":
    main()
