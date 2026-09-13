# Local operations and release checkpoint

## Run

Use synthetic fixtures in data/output. Start an isolated PostgreSQL instance;
never aim integration tests or fixture imports at the personal finance database.
The compose file uses a local-only demo password and binds PostgreSQL to loopback.

```sh
docker compose up -d postgres
docker compose run --build --rm batch
```

Run it a second time: ingestion should report replayed=true and no inserted rows.
The named volume keeps status.json, review-queue.json and metrics.prom. To inspect:

```sh
docker compose run --rm --no-deps batch cat /app/reports/status.json
docker compose run --rm --no-deps batch cat /app/reports/metrics.prom
```

For an existing isolated database, set DATABASE_URL locally and run:

```sh
uv sync --frozen
uv run python -m recon_engine.batch --data-dir data/output --output-dir /tmp/recon-report --run-id demo-v1
```

Run IDs identify immutable payloads. Repeat the same ID for retry; use a new ID
for a changed input run. Raw source IDs are immutable in this slice: a changed row
under an existing ID is rejected and requires a designed correction/version path.
The CLI does not replace or clear old source tables. Ground truth is omitted from
normal ingestion; --include-ground-truth is an explicit evaluation-only loader option.

## Failure exercise

Run the batch command with --inject-failure and a new run ID. It deliberately
fails after ingestion and before allocation commit. Expect exit 1, status=failed
and recon_batch_success=0. The last-success timestamp must remain unchanged.
Retry without the flag, using the same run ID: the input replay is harmless, the
allocation commit succeeds and metrics return to success. Failed output contains
an error type, not database credentials or source data. A failed run may leave the
previous successful review queue; consumers must inspect status.json first.

metrics.prom exposes success, duration and last-success gauges. monitoring/alerts.yml
defines failed, stale (25-hour daily schedule assumption), and missing-metric rules.
These rules are configuration only until a Prometheus scraper/evaluator and an
alert destination have been deployed and exercised. No external notification was sent.

## Tests

```sh
RECON_TEST_DATABASE_URL=postgresql://USER:PASSWORD@localhost:PORT/TEST_DATABASE \
  uv run python -m unittest discover -s tests -v
uv run python -m tests.test_human_approval
```

Database tests create/drop a unique schema per test and require the explicitly
named test variable. If that variable is absent they skip; the CI job supplies it.
Never count a skipped integration suite as a passed database check.

## Current verified evidence

September 13: 27 tests passed locally on Python 3.11 with a disposable PostgreSQL
16 container, including concurrency, rollback, run replay, metrics and recovery.
The separate existing human-approval pause/resume test passed earlier. The new
image built locally as recon-engine:local-v02. All 27 tests also passed inside that
image against the disposable database, with container exit code 0. Docker Desktop's
streaming attach stalled before startup; creating the container, copying tests,
then using docker start without -a and reading docker logs worked. Hosted CI remains
pending. Cloud resources were not provisioned.

## Boundaries before a full release

- The durable reservation layer validates disjoint ownership and positive balanced
  amounts, not the business truth of a match. Same-last-four matching can still be
  ambiguous or wrong; wider business identity and currency support remain work.
- Existing historical agent_resolutions rows are evaluation/audit records, not
  retrospectively validated allocations. Backfill/review must be explicit.
- save_run atomically commits approved confirmed_match proposals and deterministic
  matches. Model confirmed_batch proposals without complete validated group membership
  fail closed. The explicit reserve API supports balanced batch groups; the agent UI
  now supports complete deterministic candidate groups through explicit review.
- PostgreSQL checkpoints and source-aware caches are implemented in the restart
  milestone below; cloud runtime and model-quality evaluation remain separate gates.
- Backups/restores, actual alert delivery, authenticated runtime, Terraform/cloud
  deployment and release publication remain separate gates.

The SQL bootstrap is additive; there is no automatic down migration. Back up before
an actual shared-database upgrade. Do not use compose down --volumes as a recovery
procedure: it removes the local database/report volumes.

## Restart and review milestone — September 13

The operational agent CLI now requires DATABASE_URL and a stable RECON_RUN_ID.
It stores LangGraph checkpoints in PostgreSQL. Restart with the same run ID to
redisplay pending approval or continue interrupted work; completed runs are reused.
A database lease prevents two workers operating the same run simultaneously. Changed
source snapshots, model settings or implementation reject reuse of that run ID.

```sh
RECON_RUN_ID=review-001 uv run python -m recon_engine.graph.run_classification
```

Investigations use content-addressed cache keys covering the source tables, case,
model, endpoint and investigator/tool implementation. Proposals include the findings
and proposer implementation. Corrupt JSON caches are ignored; replacement is atomic.
Full source-table hashing is suitable for this declared small synthetic workload.
Sources are checked before and after investigation and before approval/persistence;
this is change detection, not a distributed immutable snapshot of every MCP query.

Batch candidates are enumerated before selection. Competing groups remain review
cases; the search stops safely at 10,000 combinations. An unambiguous amount/date
candidate still requires one human review showing all members. Approval/rejection is
applied to the complete group. Persistence validates every member's agreement and the
database amount balance before committing audit rows and reservations together.
Model-only batch proposals without a complete group still fail closed.

Verification now includes a real subprocess exit before PostgreSQL-backed approval
resume, changed-input rejection, concurrent-run exclusion, cache invalidation,
complete/partial batch approval and a pg_dump/pg_restore drill. The restore test uses
an explicitly named disposable container, a unique source schema and a new target
database; it checks replay and conflicting reservations after restore, then cleans up.

```sh
RECON_TEST_PG_CONTAINER=recon-local-test-0913 \
RECON_TEST_DATABASE_URL=postgresql://USER:PASSWORD@localhost:PORT/TEST_DATABASE \
  uv run python -m unittest discover -s tests -v
```

Prometheus v3.5.0 rule tests cover failed, missing, stale and recovered metrics.
Compose includes a metrics-only HTTP exporter and a loopback-only Prometheus UI.
The exporter exposes /metrics, never report files. Alert delivery to an external
recipient is not configured or tested. Use the Prometheus Alerts view for local
inspection; rules alone do not constitute delivered notifications.

The AWS foundation under infra/aws passed validation, mock policy assertions and an
authenticated plan (8 additions). It remains unapplied and excludes compute/database
hosting. Hosted CI, publication, scoped cloud identities, complete cloud runtime,
notification routing and cost approval remain release gates.

Latest verified checkpoint: 34/34 local tests passed with the restore drill enabled.
The image suite passed 33 application tests; the host-driven Docker restore drill
is deliberately skipped inside the application container. Prometheus's live API
showed the injected failure transition to firing, then returned an empty alert list
after healthy metrics were restored. This validates scrape/evaluation/recovery using
synthetic telemetry, not notification delivery to a person.
