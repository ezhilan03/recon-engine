"""Atomic immutable-source imports. Identical replays are no-ops.

Changed rows under existing IDs are rejected, not silently overwritten. Source
correction/version workflows are outside v0.2. Ground truth is opt-in and never
used by the application runtime.
"""
import argparse
import csv
import io
import hashlib
import json
import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from dotenv import load_dotenv

load_dotenv()
DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "output"
SQL_DIR = Path(__file__).resolve().parents[3] / "sql"
SCHEMA_PATH = SQL_DIR / "schema.sql"
TABLES = {
    "internal_ledger": ["internal_txn_id", "transaction_date", "amount", "account_last4",
                        "merchant_descriptor", "nacha_return_code", "card_number_masked",
                        "mcc", "merchant_number"],
    "network_settlement": ["settlement_line_id", "settlement_date", "gross_amount",
                           "fee_amount", "net_amount", "account_last4", "descriptor",
                           "batch_id", "return_code", "card_number_masked", "mcc",
                           "merchant_number"],
}


def get_connection():
    return psycopg.connect(os.environ["DATABASE_URL"])


def apply_schema(conn):
    # Caller owns commit/rollback. Bootstrap is not a data reset.
    conn.execute(SCHEMA_PATH.read_text())
    conn.execute((SQL_DIR / "operations.sql").read_text())


def load_dataset(conn, directory=DATA_DIR, include_ground_truth=False):
    paths = {name: Path(directory) / f"{name}.csv" for name in TABLES}
    if include_ground_truth:
        paths["ground_truth"] = Path(directory) / "ground_truth.csv"
    # Snapshot bytes once: manifest and imported bytes cannot diverge if a file changes.
    inputs = {name: path.read_bytes() for name, path in paths.items()}
    digest = hashlib.sha256(json.dumps({
        name: hashlib.sha256(data).hexdigest() for name, data in inputs.items()
    }, sort_keys=True).encode()).hexdigest()
    with conn.transaction():
        # Serialise imports and reservations; uniqueness constraints also protect allocations.
        conn.execute("SELECT pg_advisory_xact_lock(7131302)")
        apply_schema(conn)
        previous = conn.execute("SELECT counts FROM ingestion_batches WHERE digest=%s",
                                (digest,)).fetchone()
        if previous:
            return {"digest": digest, "replayed": True, "inserted": {k: 0 for k in previous[0]}}
        counts = {}
        for table, columns in TABLES.items():
            header = next(csv.reader(io.StringIO(inputs[table].decode("utf-8-sig"))), None)
            if header != columns:
                raise ValueError(f"Unexpected CSV contract: {table}")
            target, stage = sql.Identifier(table), sql.Identifier(f"stage_{table}")
            cols = sql.SQL(", ").join(map(sql.Identifier, columns))
            conn.execute(sql.SQL("CREATE TEMP TABLE {} (LIKE {} INCLUDING DEFAULTS) ON COMMIT DROP")
                         .format(stage, target))
            with conn.cursor().copy(sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')")
                                    .format(stage, cols)) as copy:
                copy.write(inputs[table])
            money_columns = ["amount"] if table == "internal_ledger" else ["gross_amount", "fee_amount", "net_amount"]
            for money_column in money_columns:
                invalid = conn.execute(sql.SQL("SELECT 1 FROM {} WHERE {}::text IN ('NaN','Infinity','-Infinity') LIMIT 1")
                                       .format(stage, sql.Identifier(money_column))).fetchone()
                if invalid:
                    raise ValueError(f"Non-finite monetary value in {table}")
            key = sql.Identifier(columns[0])
            # Reject conflicting duplicates; identical deliveries may collapse safely.
            duplicate = conn.execute(sql.SQL(
                "SELECT {key} FROM (SELECT DISTINCT * FROM {stage}) s "
                "GROUP BY {key} HAVING count(*) > 1 LIMIT 1"
            ).format(key=key, stage=stage)).fetchone()
            if duplicate:
                raise ValueError(f"Conflicting duplicate keys in {table}")
            conflict = conn.execute(sql.SQL(
                "SELECT 1 FROM {stage} s JOIN {target} t USING ({key}) "
                "WHERE ROW(s.*) IS DISTINCT FROM ROW(t.*) LIMIT 1"
            ).format(stage=stage, target=target, key=key)).fetchone()
            if conflict:
                raise ValueError(f"Source correction requires a new version: {table}")
            result = conn.execute(sql.SQL(
                "INSERT INTO {} ({}) SELECT DISTINCT {} FROM {} ON CONFLICT ({}) DO NOTHING"
            ).format(target, cols, cols, stage, key))
            counts[table] = result.rowcount
        if include_ground_truth:
            # Evaluation fixtures accumulate without clearing unrelated prior fixtures.
            conn.execute("CREATE TEMP TABLE stage_truth (LIKE ground_truth) ON COMMIT DROP")
            with conn.cursor().copy("COPY stage_truth FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')") as copy:
                copy.write(inputs["ground_truth"])
            counts["ground_truth"] = conn.execute(
                "INSERT INTO ground_truth SELECT DISTINCT * FROM stage_truth EXCEPT SELECT * FROM ground_truth"
            ).rowcount
        conn.execute("INSERT INTO ingestion_batches(digest,counts) VALUES (%s,%s)", (digest, Jsonb(counts)))
    return {"digest": digest, "replayed": False, "inserted": counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--include-ground-truth", action="store_true")
    args = parser.parse_args()
    with get_connection() as conn:
        print(json.dumps(load_dataset(conn, args.data_dir, args.include_ground_truth), sort_keys=True))


if __name__ == "__main__":
    main()
