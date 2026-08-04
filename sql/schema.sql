-- Reconciliation engine schema (Postgres 16, matches Project 1's version).
--
-- internal_ledger and network_settlement are the two tables the matcher
-- and, later, the MCP investigator agent query at runtime.
--
-- ground_truth is eval-only. It is never queried by the matcher or agent --
-- exposing it at runtime would let the system "cheat" the way a leaked
-- test set would. It exists purely for evaluate_baseline.py-style scoring.

DROP TABLE IF EXISTS ground_truth;
DROP TABLE IF EXISTS network_settlement;
DROP TABLE IF EXISTS internal_ledger;

CREATE TABLE internal_ledger (
    internal_txn_id      TEXT PRIMARY KEY,
    transaction_date     DATE NOT NULL,
    amount                NUMERIC(10, 2) NOT NULL,
    account_last4        TEXT NOT NULL,
    merchant_descriptor  TEXT NOT NULL,
    nacha_return_code    TEXT,
    card_number_masked   TEXT NOT NULL,
    mcc                   TEXT NOT NULL,
    merchant_number       TEXT NOT NULL
);

CREATE TABLE network_settlement (
    settlement_line_id  TEXT PRIMARY KEY,
    settlement_date      DATE NOT NULL,
    gross_amount          NUMERIC(10, 2) NOT NULL,
    fee_amount            NUMERIC(10, 2) NOT NULL,
    net_amount            NUMERIC(10, 2) NOT NULL,
    account_last4        TEXT NOT NULL,
    descriptor            TEXT NOT NULL,
    batch_id              TEXT NOT NULL,
    return_code           TEXT,
    card_number_masked    TEXT NOT NULL,
    mcc                    TEXT NOT NULL,
    merchant_number        TEXT NOT NULL
);

-- Eval-only. Not part of the application schema the agent queries.
CREATE TABLE ground_truth (
    internal_txn_id       TEXT,
    settlement_line_ids   TEXT,
    discrepancy_type      TEXT NOT NULL,
    expected_cardinality  TEXT NOT NULL
);

-- These indexes are what make the matcher's "candidates for this account
-- within a date window" query fast instead of a full scan -- the same
-- role the HNSW index played for vector search in Project 1.
CREATE INDEX idx_ledger_account_date ON internal_ledger (account_last4, transaction_date);
CREATE INDEX idx_settlement_account_date ON network_settlement (account_last4, settlement_date);
