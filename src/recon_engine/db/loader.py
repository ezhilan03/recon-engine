"""
Loads the generated CSVs into Postgres using COPY (much faster than
row-by-row INSERT, and the technique you'd actually use for a real
settlement file import).

Requires a DATABASE_URL environment variable pointing at your Neon
Postgres instance, e.g.:

    export DATABASE_URL="postgresql://user:pass@ep-xxxx.neon.tech/reconengine?sslmode=require"

Run:
    uv run python -m recon_engine.db.loader
"""

import csv
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "output"
SCHEMA_PATH = Path(__file__).resolve().parents[3] / "sql" / "schema.sql"


def get_connection() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print(
            "DATABASE_URL is not set. Create a Neon project, copy the "
            "connection string, and:\n"
            "    export DATABASE_URL=\"postgresql://...\"\n",
            file=sys.stderr,
        )
        sys.exit(1)
    return psycopg.connect(dsn)


def apply_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_PATH.read_text())
    conn.commit()
    print("Schema applied.")


def copy_csv(conn: psycopg.Connection, table: str, csv_path: Path, columns: list[str]) -> int:
    col_list = ", ".join(columns)
    n = 0
    with conn.cursor() as cur:
        with cur.copy(f"COPY {table} ({col_list}) FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')") as copy:
            with csv_path.open("rb") as f:
                copy.write(f.read())
        # cursor.rowcount isn't reliably set after COPY in all drivers; count manually
        with csv_path.open() as f:
            n = sum(1 for _ in f) - 1  # minus header
    conn.commit()
    return n


def main() -> None:
    conn = get_connection()
    apply_schema(conn)

    n_ledger = copy_csv(
        conn, "internal_ledger", DATA_DIR / "internal_ledger.csv",
        ["internal_txn_id", "transaction_date", "amount", "account_last4",
         "merchant_descriptor", "nacha_return_code", "card_number_masked",
         "mcc", "merchant_number"],
    )
    n_settlement = copy_csv(
        conn, "network_settlement", DATA_DIR / "network_settlement.csv",
        ["settlement_line_id", "settlement_date", "gross_amount", "fee_amount",
         "net_amount", "account_last4", "descriptor", "batch_id", "return_code",
         "card_number_masked", "mcc", "merchant_number"],
    )
    n_gt = copy_csv(
        conn, "ground_truth", DATA_DIR / "ground_truth.csv",
        ["internal_txn_id", "settlement_line_ids", "discrepancy_type", "expected_cardinality"],
    )

    print(f"Loaded: internal_ledger={n_ledger}, network_settlement={n_settlement}, ground_truth={n_gt}")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM internal_ledger")
        db_ledger_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM network_settlement")
        db_settlement_count = cur.fetchone()[0]
    print(f"Verified in DB: internal_ledger={db_ledger_count}, network_settlement={db_settlement_count}")

    conn.close()


if __name__ == "__main__":
    main()
