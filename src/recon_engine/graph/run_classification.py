"""
Loads ledger + settlement data (from Postgres if DATABASE_URL is set,
otherwise falls back to the CSVs for local testing), builds the exception
queue, runs it through the graph, and reports how far rule-based
classification + batch resolution shrank the queue -- before any LLM
call exists in this pipeline.

Run:
    uv run python -m recon_engine.graph.run_classification
"""

import asyncio
import csv
import logging
import os
import uuid
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from langgraph.types import Command

from recon_engine.graph.graph import build_graph
from recon_engine.graph.nodes import build_exception_queue
from recon_engine.matching.deterministic_matcher import DATA_DIR

load_dotenv()  # reads .env in the project root, so DATABASE_URL persists across terminal sessions

# litellm's default logger renders through rich, which reflows output based
# on detected terminal width -- this has been producing garbled, hard-to-read
# output all session. Quieting it to WARNING removes the noise; our own
# plain print() progress markers below (not routed through this logger)
# take over for visibility into what's actually happening.
logging.getLogger("LiteLLM").setLevel(logging.WARNING)


def load_ledger_and_settlement():
    if os.environ.get("DATABASE_URL"):
        from recon_engine.db.repository import load_data_from_db
        print("Loading from Postgres...")
        return load_data_from_db()
    else:
        from recon_engine.matching.deterministic_matcher import load_data
        print("DATABASE_URL not set -- falling back to CSV for local testing.")
        return load_data()


def load_ground_truth_by_type() -> dict[str, str]:
    """internal_txn_id -> discrepancy_type, for scoring only."""
    gt = {}
    with (DATA_DIR / "ground_truth.csv").open() as f:
        for row in csv.DictReader(f):
            if row["internal_txn_id"]:
                gt[row["internal_txn_id"]] = row["discrepancy_type"]
    return gt


async def main() -> None:
    ledger, settlement = load_ledger_and_settlement()
    print(f"Loaded {len(ledger)} internal, {len(settlement)} settlement rows.\n")

    initial_state = build_exception_queue(ledger, settlement)
    n_exceptions = len(initial_state["exceptions"])
    print(f"Exception queue after deterministic matching: {n_exceptions} cases\n")

    app = build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    result = await app.ainvoke(initial_state, config=config)
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        print("\n" + "=" * 60)
        print("HUMAN APPROVAL NEEDED")
        print(f"  transaction:      {payload['internal_txn_id']}")
        print(f"  amount:           ${payload['amount']:.2f}")
        print(f"  proposed:         {payload['resolution_type']}")
        print(f"  settlement lines: {payload['matched_settlement_line_ids']}")
        print(f"  confidence:       {payload['confidence']:.2f}")
        print(f"  reasoning:        {payload['reasoning']}")
        print(f"  flagged because:  {payload['flagged_reason']}")
        print("=" * 60)
        decision = input("Approve this resolution? [y/n]: ").strip().lower()
        decision_value = "approved" if decision == "y" else "rejected"
        result = await app.ainvoke(Command(resume=decision_value), config=config)

    final_state = result

    print("Classification buckets after rule-based pass + batch resolution:")
    print("-" * 60)
    for label, count in sorted(final_state["bucket_counts"].items(), key=lambda x: -x[1]):
        print(f"  {label:30s} {count}")

    still_needs_llm = final_state["bucket_counts"].get("needs_llm_investigation", 0) \
        + final_state["bucket_counts"].get("duplicate_needs_review", 0) \
        + final_state["bucket_counts"].get("no_batch_match_found", 0)
    print(f"\nGenuinely left for the agent layer: {still_needs_llm} / {n_exceptions} "
          f"({still_needs_llm/n_exceptions:.1%} of the original exception queue)")

    # score correctness against ground truth, since we can here
    gt_by_type = load_ground_truth_by_type()

    exception_ids = {c["internal_txn_id"] for c in final_state["exceptions"]}
    overlap = exception_ids & gt_by_type.keys()
    if len(overlap) < len(exception_ids) * 0.5:
        print(
            f"\n⚠️  WARNING: only {len(overlap)}/{len(exception_ids)} exception-queue "
            f"transaction IDs were found in data/output/ground_truth.csv. This almost "
            f"always means the local CSVs and the loaded database are out of sync -- "
            f"regenerate AND reload together:\n"
            f"    uv run python -m recon_engine.data_gen.generate_dataset\n"
            f"    uv run python -m recon_engine.db.loader\n"
            f"Accuracy numbers below are not meaningful until this is fixed.\n"
        )

    correct_by_label = defaultdict(lambda: [0, 0])
    label_to_expected_type = {
        "hard_return": "hard_return",
        "timing_outlier": "timing_outlier",
        "batched_settlement_resolved": "batched_settlement",
        "duplicate_needs_review": "duplicate_line",
        "no_batch_match_found": "orphan_internal",  # scoring only -- the node never asserts this
    }
    for case in final_state["exceptions"]:
        expected_type = gt_by_type.get(case["internal_txn_id"])
        label = case["classification"]
        if label in label_to_expected_type:
            correct_by_label[label][1] += 1
            if label_to_expected_type[label] == expected_type:
                correct_by_label[label][0] += 1

    print("\nClassification accuracy vs ground truth (where checkable):")
    for label, (correct, total) in correct_by_label.items():
        print(f"  {label:30s} {correct}/{total} = {correct/total:.1%}")

    investigation_errors = [
        c for c in final_state["exceptions"]
        if "investigation_error" in c["evidence"]
    ]
    if investigation_errors:
        print(f"\n{len(investigation_errors)} case(s) failed during investigation (see evidence['investigation_error']):")
        for c in investigation_errors:
            print(f"  {c['internal_txn_id']}: {c['evidence']['investigation_error']}")

    proposals = [
        c for c in final_state["exceptions"]
        if "resolution_proposal" in c["evidence"]
    ]
    errors = [
        c for c in final_state["exceptions"]
        if "proposal_error" in c["evidence"]
    ]
    if errors:
        print(f"\n{len(errors)} case(s) failed during resolution proposal (see evidence['proposal_error']):")
        for c in errors:
            print(f"  {c['internal_txn_id']}: {c['evidence']['proposal_error']}")
    if proposals:
        print(f"\nAgent produced {len(proposals)} resolution proposals:")
        decisions = defaultdict(int)
        for c in proposals:
            decisions[c["evidence"].get("human_decision", "no_decision_recorded")] += 1
        for decision, count in decisions.items():
            print(f"  {decision:20s} {count}")
        avg_confidence = sum(c["evidence"]["resolution_proposal"]["confidence"] for c in proposals) / len(proposals)
        print(f"  average confidence: {avg_confidence:.2f}")

    if proposals and os.environ.get("DATABASE_URL"):
        from recon_engine.agent.investigator import INVESTIGATOR_MODEL
        from recon_engine.agent.proposer import PROPOSER_MODEL
        from recon_engine.db.save_resolutions import new_run_id, save_run

        run_id = new_run_id()
        n_saved = save_run(run_id, INVESTIGATOR_MODEL, PROPOSER_MODEL, final_state["exceptions"])
        print(f"\nSaved {n_saved} resolutions to agent_resolutions (run_id={run_id})")
        print(f"Score this run with:")
        print(f"  uv run python -m recon_engine.evaluation.evaluate_agent_resolutions {run_id}")


if __name__ == "__main__":
    asyncio.run(main())
