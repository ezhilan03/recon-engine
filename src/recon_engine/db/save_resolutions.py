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
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "sql" / "agent_resolutions_schema.sql"


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"])


def ensure_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_PATH.read_text())
    conn.commit()


def save_run(run_id: str, investigator_model: str, proposer_model: str, exceptions: list[dict]) -> int:
    """Writes one row per case that reached a resolution proposal.
    Returns the number of rows written."""
    conn = _connect()
    ensure_schema(conn)

    rows = []
    for case in exceptions:
        proposal = case["evidence"].get("resolution_proposal")
        if not proposal:
            continue
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

    if not rows:
        conn.close()
        return 0

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
    conn.commit()
    conn.close()
    return len(rows)


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:10]}"
