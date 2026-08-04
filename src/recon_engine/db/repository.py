"""
Loads internal_ledger and network_settlement from Postgres instead of CSV.
Same InternalRow/SettlementRow shapes as deterministic_matcher.load_data(),
so run_matcher() doesn't need to change at all -- only where the rows
come from changes. This is the seam where CSVs stop being the source of
truth and the database takes over.
"""

import os

import psycopg
from dotenv import load_dotenv

from recon_engine.matching.deterministic_matcher import InternalRow, SettlementRow

load_dotenv()


def get_connection() -> psycopg.Connection:
    dsn = os.environ["DATABASE_URL"]
    return psycopg.connect(dsn)


def load_data_from_db() -> tuple[list[InternalRow], list[SettlementRow]]:
    conn = get_connection()
    ledger = []
    settlement = []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT internal_txn_id, transaction_date, amount, account_last4,
                   merchant_descriptor, nacha_return_code, card_number_masked,
                   mcc, merchant_number
            FROM internal_ledger
            ORDER BY internal_txn_id
        """)
        for row in cur.fetchall():
            ledger.append(InternalRow(
                internal_txn_id=row[0],
                transaction_date=row[1],
                amount=float(row[2]),
                account_last4=row[3],
                merchant_descriptor=row[4],
                nacha_return_code=row[5] or "",
                card_number_masked=row[6] or "",
                mcc=row[7] or "",
                merchant_number=row[8] or "",
            ))

        cur.execute("""
            SELECT settlement_line_id, settlement_date, gross_amount, fee_amount,
                   net_amount, account_last4, descriptor, batch_id, return_code,
                   card_number_masked, mcc, merchant_number
            FROM network_settlement
            ORDER BY settlement_line_id
        """)
        for row in cur.fetchall():
            settlement.append(SettlementRow(
                settlement_line_id=row[0],
                settlement_date=row[1],
                gross_amount=float(row[2]),
                fee_amount=float(row[3]),
                net_amount=float(row[4]),
                account_last4=row[5],
                descriptor=row[6],
                batch_id=row[7],
                return_code=row[8] or "",
                card_number_masked=row[9] or "",
                mcc=row[10] or "",
                merchant_number=row[11] or "",
            ))
    conn.close()
    return ledger, settlement
