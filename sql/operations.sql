-- Additive operational metadata. Apply after schema.sql.
CREATE TABLE IF NOT EXISTS ingestion_batches (
    digest TEXT PRIMARY KEY,
    counts JSONB NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    run_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    saved_rows INTEGER NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS allocation_groups (
    group_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS allocated_transactions (
    internal_txn_id TEXT PRIMARY KEY REFERENCES internal_ledger(internal_txn_id),
    group_id TEXT NOT NULL REFERENCES allocation_groups(group_id)
);
CREATE TABLE IF NOT EXISTS allocated_settlements (
    settlement_line_id TEXT PRIMARY KEY REFERENCES network_settlement(settlement_line_id),
    group_id TEXT NOT NULL REFERENCES allocation_groups(group_id)
);
