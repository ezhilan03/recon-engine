"""Durable disjoint allocation groups for one-to-one, splits and batches.

The same settlement may support multiple ledger rows only inside one explicit,
balanced group. A row cannot belong to two different groups, even across runs.
"""
import hashlib
import json
from decimal import Decimal


def reserve(conn, transaction_ids, settlement_ids):
    txns, settlements = sorted(transaction_ids), sorted(settlement_ids)
    if not txns or not settlements or len(set(txns)) != len(txns) or len(set(settlements)) != len(settlements):
        raise ValueError("Allocation requires distinct nonempty source IDs")
    group = hashlib.sha256(json.dumps([txns, settlements]).encode()).hexdigest()
    # Caller owns one transaction containing reservations and audit records.
    conn.execute("SELECT pg_advisory_xact_lock(7131302)")
    amounts = conn.execute("SELECT amount FROM internal_ledger WHERE internal_txn_id=ANY(%s)", (txns,)).fetchall()
    gross = conn.execute("SELECT gross_amount FROM network_settlement WHERE settlement_line_id=ANY(%s)", (settlements,)).fetchall()
    if len(amounts) != len(txns) or len(gross) != len(settlements):
        raise ValueError("Unknown allocation source ID")
    values = [r[0] for r in amounts + gross]
    if not all(x.is_finite() and x > 0 for x in values):
        raise ValueError("Only positive finite payment allocations are supported")
    if abs(sum(r[0] for r in amounts) - sum(r[0] for r in gross)) > Decimal("0.01"):
        raise ValueError("Allocation group amounts do not balance")
    conn.execute("INSERT INTO allocation_groups(group_id) VALUES (%s) ON CONFLICT DO NOTHING", (group,))
    for table, column, ids in [("allocated_transactions", "internal_txn_id", txns),
                               ("allocated_settlements", "settlement_line_id", settlements)]:
        from psycopg import sql
        for source_id in ids:
            conn.execute(sql.SQL("INSERT INTO {} ({},group_id) VALUES (%s,%s) ON CONFLICT DO NOTHING")
                         .format(sql.Identifier(table), sql.Identifier(column)), (source_id, group))
            owner = conn.execute(sql.SQL("SELECT group_id FROM {} WHERE {}=%s")
                                 .format(sql.Identifier(table), sql.Identifier(column)), (source_id,)).fetchone()[0]
            if owner != group:
                raise ValueError("Source row is already allocated to another group")
    return group
