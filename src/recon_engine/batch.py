"""Local/container batch entry point: ingest, match, reserve, emit evidence.

No model is called. Unresolved/ambiguous cases remain a review queue; no
speculative batch or agent proposal is committed by this job.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time

import psycopg

from recon_engine.db.loader import load_dataset
from recon_engine.db.repository import load_data_from_db
from recon_engine.db.save_resolutions import save_run
from recon_engine.matching.deterministic_matcher import run_matcher


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        f.write(text)
    temporary.replace(path)


def run(data_dir, output_dir, run_id, inject_failure=False):
    started = time.monotonic()
    output_dir = Path(output_dir)
    result = {"run_id": run_id, "status": "failed"}
    last_success = 0
    prior = output_dir / "status.json"
    if prior.exists():
        last_success = json.loads(prior.read_text()).get("last_success_timestamp", 0)
    try:
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            imported = load_dataset(conn, data_dir)
            ledger, settlement = load_data_from_db()
            matches = run_matcher(ledger, settlement)
            if inject_failure:
                raise RuntimeError("Injected failure before allocation commit")
            save_run(run_id, "none", "none", [], matches=matches, conn=conn)
        counts = dict(Counter(m.match_type for m in matches))
        last_success = int(time.time())
        result.update(status="succeeded", ingestion=imported, counts=counts,
                      transactions=len(ledger), settlement_lines=len(settlement))
        atomic_write(output_dir / "review-queue.json", json.dumps([
            {"transaction_id": m.internal_txn_id, "match_type": m.match_type,
             "candidate_settlement_ids": m.matched_settlement_ids}
            for m in matches if m.match_type in ("ambiguous", "unresolved")
        ], indent=2))
    except Exception as exc:
        # Avoid DSNs, source contents, model prompts and raw DB error strings in logs.
        result["error_type"] = type(exc).__name__
        raise
    finally:
        result["duration_seconds"] = round(time.monotonic() - started, 4)
        result["last_success_timestamp"] = last_success
        result["completed_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write(prior, json.dumps(result, indent=2, sort_keys=True))
        atomic_write(output_dir / "metrics.prom", "\n".join([
            "# HELP recon_batch_success Whether the latest batch succeeded.",
            "# TYPE recon_batch_success gauge",
            f"recon_batch_success {int(result['status'] == 'succeeded')}",
            "# HELP recon_batch_last_success_timestamp_seconds Last successful completion.",
            "# TYPE recon_batch_last_success_timestamp_seconds gauge",
            f"recon_batch_last_success_timestamp_seconds {last_success}",
            "# HELP recon_batch_duration_seconds Duration of the latest batch.",
            "# TYPE recon_batch_duration_seconds gauge",
            f"recon_batch_duration_seconds {result['duration_seconds']}",
            "",
        ]))
        print(json.dumps(result, sort_keys=True))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True, help="Stable ID for retries of this input run")
    parser.add_argument("--inject-failure", action="store_true")
    args = parser.parse_args()
    try:
        run(args.data_dir, args.output_dir, args.run_id, args.inject_failure)
    except Exception:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
