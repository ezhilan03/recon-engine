"""
Saves the final resolutions from a run_classification.py run into the
agent_resolutions table, so results are durable and scoreable after the
process exits -- currently the graph's final state only ever gets
printed to stdout, then discarded.

Also applies agent_resolutions_schema.sql if the table doesn't exist yet
(safe to call every run -- CREATE TABLE IF NOT EXISTS).

Usage as a library:
    from recon_engine.db.save_resolutions import save_run
    save_run(run_id, investigator_model, proposer_model, final_state["exceptions"])
"""

import os
import uuid
import hashlib
import json
import math
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from recon_engine.db.allocations import reserve
from recon_engine.db.loader import apply_schema

load_dotenv()

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "sql" / "agent_resolutions_schema.sql"


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"])


def ensure_schema(conn: psycopg.Connection) -> None:
    apply_schema(conn)
    with conn.cursor() as cur:
        cur.execute(SCHEMA_PATH.read_text())


def save_run(run_id: str, investigator_model: str, proposer_model: str, exceptions: list[dict],
             matches=(), conn=None) -> int:
    """Writes one row per case that reached a resolution proposal.
    Returns the number of rows written."""
    rows = []
    groups = []
    seen = set()
    cases_by_id = {c["internal_txn_id"]: c for c in exceptions}
    for case in exceptions:
        proposal = case["evidence"].get("resolution_proposal")
        if not proposal:
            continue
        if case["internal_txn_id"] in seen:
            raise ValueError("Duplicate case in resolution run")
        seen.add(case["internal_txn_id"])
        if proposal["internal_txn_id"] != case["internal_txn_id"]:
            raise ValueError("Proposal transaction identity mismatch")
        decision = case["evidence"].get("human_decision")
        confidence = float(proposal["confidence"])
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("Invalid proposal confidence")
        if decision not in (None, "approved", "rejected", "auto_approved"):
            raise ValueError("Invalid human decision")
        if decision == "auto_approved" and (confidence < 0.85 or proposal["requires_human_approval"] or case["amount"] >= 100):
            raise ValueError("Proposal requires explicit human approval")
        if decision in ("approved", "auto_approved") and proposal["resolution_type"] in ("confirmed_match", "confirmed_batch"):
            if proposal["resolution_type"] == "confirmed_batch":
                group = case["evidence"].get("allocation_group")
                if not group or decision != "approved" or case["internal_txn_id"] not in group["transaction_ids"]:
                    raise ValueError("Batch proposals require an explicit human-approved complete group")
                for member in group["transaction_ids"]:
                    evidence = cases_by_id.get(member, {}).get("evidence", {})
                    other = evidence.get("resolution_proposal", {})
                    if (evidence.get("allocation_group") != group or evidence.get("human_decision") != "approved"
                            or other.get("resolution_type") != "confirmed_batch"
                            or sorted(other.get("matched_settlement_line_ids", [])) != sorted(group["settlement_ids"])):
                        raise ValueError("Every batch member must approve the same complete group")
                groups.append((group["transaction_ids"], group["settlement_ids"]))
            else:
                groups.append(([case["internal_txn_id"]], proposal["matched_settlement_line_ids"]))
        rows.append((
            run_id,
            investigator_model,
            proposer_model,
            case["internal_txn_id"],
            proposal["resolution_type"],
            "|".join(proposal["matched_settlement_line_ids"]),
            proposal["confidence"],
            proposal["reasoning"],
            proposal["requires_human_approval"],
            case["evidence"].get("human_decision"),
        ))

    for match in matches:
        if match.match_type in ("exact_one_to_one", "split_two"):
            groups.append(([match.internal_txn_id], match.matched_settlement_ids))
    rows.sort(key=lambda row: row[3])
    groups = sorted(set((tuple(sorted(txns)), tuple(sorted(settlements))) for txns, settlements in groups))
    payload_hash = hashlib.sha256(json.dumps([rows, groups], sort_keys=True, default=str).encode()).hexdigest()
    owned = conn is None
    conn = conn or _connect()
    try:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(7131302)")
            ensure_schema(conn)
            previous = conn.execute("SELECT payload_hash, saved_rows FROM reconciliation_runs WHERE run_id=%s", (run_id,)).fetchone()
            if previous:
                if previous[0] != payload_hash:
                    raise ValueError("Run ID already exists with a different payload")
                return 0
            for txns, settlements in groups:
                reserve(conn, txns, settlements)
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO agent_resolutions
                        (run_id, investigator_model, proposer_model, internal_txn_id,
                         resolution_type, matched_settlement_line_ids, confidence,
                         reasoning, requires_human_approval, human_decision)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
            conn.execute("INSERT INTO reconciliation_runs(run_id,payload_hash,saved_rows) VALUES (%s,%s,%s)",
                         (run_id, payload_hash, len(rows)))
    finally:
        if owned:
            conn.close()
    return len(rows)


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:10]}"
