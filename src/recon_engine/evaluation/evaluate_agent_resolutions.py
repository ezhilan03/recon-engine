"""
Scores a saved agent run against ground truth -- the same role RAGAS
played for Project 1's retrieval pipeline, but for the agent's final
resolutions.

Ground truth (data/output/ground_truth.csv) uses a different vocabulary
than the agent's resolution_type schema, since ground truth describes the
synthetic dataset's actual discrepancy mechanism and the agent describes
its own conclusion. AGENT_TO_EXPECTED_GT_TYPES below is the mapping
between them -- explicit and editable, not hidden in scoring logic.

'insufficient_evidence' is scored separately from right/wrong: it's the
agent honestly abstaining rather than committing to a guess, so it isn't
correct OR incorrect in the same sense. Reported as its own rate.

Usage:
    uv run python -m recon_engine.evaluation.evaluate_agent_resolutions [run_id]

If run_id is omitted, scores the most recent run in the database.
"""

import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "output"

# Which ground-truth discrepancy_type values count as a "correct" outcome
# for each agent resolution_type. Deliberately explicit and conservative --
# an empty set means "this resolution_type has no clean ground-truth
# equivalent, don't score it as right/wrong."
AGENT_TO_EXPECTED_GT_TYPES = {
    "confirmed_match": {"clean_match", "fee_variance", "descriptor_mangled", "timing_outlier"},
    "confirmed_batch": {"batched_settlement", "split_settlement"},
    "confirmed_orphan": {"orphan_internal"},
    "flagged_for_manual_review": {"duplicate_line", "batched_settlement", "hard_return"},
    "insufficient_evidence": set(),  # scored separately, see module docstring
}


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"])


def load_latest_run_id(conn: psycopg.Connection) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT run_id FROM agent_resolutions ORDER BY run_timestamp DESC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else None


def load_run_resolutions(conn: psycopg.Connection, run_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT internal_txn_id, resolution_type, matched_settlement_line_ids,
                   confidence, requires_human_approval, human_decision
            FROM agent_resolutions WHERE run_id = %s
        """, (run_id,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def load_ground_truth() -> dict[str, dict]:
    gt = {}
    with (DATA_DIR / "ground_truth.csv").open() as f:
        for row in csv.DictReader(f):
            if row["internal_txn_id"]:
                gt[row["internal_txn_id"]] = row
    return gt


def main() -> None:
    run_id = sys.argv[1] if len(sys.argv) > 1 else None
    conn = _connect()

    if run_id is None:
        run_id = load_latest_run_id(conn)
        if run_id is None:
            print("No saved runs found in agent_resolutions. Run recon_engine.graph.run_classification first.")
            return
        print(f"No run_id given -- scoring most recent run: {run_id}\n")

    resolutions = load_run_resolutions(conn, run_id)
    conn.close()
    if not resolutions:
        print(f"No resolutions found for run_id={run_id}")
        return

    gt = load_ground_truth()

    correct, incorrect, abstained, unscored = [], [], [], []

    for r in resolutions:
        gt_row = gt.get(r["internal_txn_id"])
        if gt_row is None:
            unscored.append(r)
            continue

        rtype = r["resolution_type"]
        if rtype == "insufficient_evidence":
            abstained.append(r)
            continue

        expected_types = AGENT_TO_EXPECTED_GT_TYPES.get(rtype, set())
        if not expected_types:
            unscored.append(r)
            continue

        if gt_row["discrepancy_type"] in expected_types:
            correct.append(r)
        else:
            incorrect.append((r, gt_row["discrepancy_type"]))

    total_scored = len(correct) + len(incorrect)
    print(f"Run: {run_id}")
    print(f"Total resolutions: {len(resolutions)}")
    print("-" * 60)
    if total_scored:
        print(f"Resolution accuracy (excl. abstentions): {len(correct)}/{total_scored} = {len(correct)/total_scored:.1%}")
    print(f"Abstained (insufficient_evidence):         {len(abstained)}/{len(resolutions)} = {len(abstained)/len(resolutions):.1%}")
    if unscored:
        print(f"No ground-truth mapping (not scored):      {len(unscored)}")

    if incorrect:
        print("\nIncorrect resolutions (agent said X, ground truth says Y):")
        for r, actual_type in incorrect:
            print(f"  {r['internal_txn_id']}: agent={r['resolution_type']!r} (conf={r['confidence']:.2f}) "
                  f"actual={actual_type!r}")

    # Confidence calibration: a well-calibrated agent should be more
    # confident when it's actually right than when it's wrong.
    if correct and incorrect:
        avg_conf_correct = sum(r["confidence"] for r in correct) / len(correct)
        avg_conf_incorrect = sum(r["confidence"] for r, _ in incorrect) / len(incorrect)
        print(f"\nConfidence calibration:")
        print(f"  avg confidence when correct:   {avg_conf_correct:.2f}")
        print(f"  avg confidence when incorrect: {avg_conf_incorrect:.2f}")
        if avg_conf_correct <= avg_conf_incorrect:
            print("  ^ WARNING: not well-calibrated -- confidence doesn't track correctness")

    # Human approval alignment: did the human's approve/reject decisions
    # during the interactive run actually track ground-truth correctness?
    correct_ids = {r["internal_txn_id"] for r in correct}
    incorrect_ids = {r["internal_txn_id"] for r, _ in incorrect}
    reviewed = [r for r in resolutions if r["human_decision"] in ("approved", "rejected")]
    if reviewed:
        print(f"\nHuman approval alignment ({len(reviewed)} cases reviewed by a human):")
        by_decision = defaultdict(lambda: {"correct": 0, "incorrect": 0, "unscored": 0})
        for r in reviewed:
            bucket = by_decision[r["human_decision"]]
            if r["internal_txn_id"] in correct_ids:
                bucket["correct"] += 1
            elif r["internal_txn_id"] in incorrect_ids:
                bucket["incorrect"] += 1
            else:
                bucket["unscored"] += 1
        for decision, counts in by_decision.items():
            total = sum(counts.values())
            print(f"  {decision:10s} -> correct={counts['correct']}, incorrect={counts['incorrect']}, "
                  f"unscored={counts['unscored']} (of {total})")
        print("  (ideally: 'approved' cases skew correct, 'rejected' cases skew incorrect --")
        print("   that would mean the human's judgment caught the agent's mistakes)")


if __name__ == "__main__":
    main()
