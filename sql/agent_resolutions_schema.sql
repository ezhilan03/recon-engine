-- Stores the agent pipeline's final resolutions for each run. Kept
-- separate from schema.sql (which DROP+CREATEs on every data load) since
-- this table should accumulate across runs -- including runs with
-- different model configs, so results are comparable over time.
--
-- Apply once, independent of data reloads:
--   uv run python -m recon_engine.db.save_resolutions --init-schema-only

CREATE TABLE IF NOT EXISTS agent_resolutions (
    id                        SERIAL PRIMARY KEY,
    run_id                    TEXT NOT NULL,
    run_timestamp             TIMESTAMP NOT NULL DEFAULT now(),
    investigator_model        TEXT NOT NULL,
    proposer_model             TEXT NOT NULL,
    internal_txn_id           TEXT NOT NULL,
    resolution_type            TEXT NOT NULL,
    matched_settlement_line_ids TEXT,  -- pipe-delimited, same format as ground_truth.csv
    confidence                 NUMERIC(4, 3) NOT NULL,
    reasoning                  TEXT,
    requires_human_approval    BOOLEAN NOT NULL,
    human_decision              TEXT  -- 'approved' | 'rejected' | 'auto_approved' | NULL if not reached
);

CREATE INDEX IF NOT EXISTS idx_agent_resolutions_run ON agent_resolutions (run_id);
CREATE INDEX IF NOT EXISTS idx_agent_resolutions_txn ON agent_resolutions (internal_txn_id);
