# Deterministic reconciliation correctness

September 13, 2026. Local work on top of dc99a8ba6f69c1085a9faf6618a724feaedbd6df.

Two internal payments could previously each accept the same settlement row.
Multiple valid split pairs also silently selected the first pair in file order.
The baseline now collects candidates before accepting allocations. Competing
claims go to review for every affected transaction; multiple split pairs are
ambiguous. Exact matches retain precedence over split candidates.

This deliberately favours review over a greedy winner. It is not an optimal
global assignment solver. Input ordering does not decide which payment wins.
Ambiguous IDs are candidate evidence, not approved allocations.

The existing same/adjacent-day rule for splits is now enforced. Decimal
comparison preserves the inclusive one-cent tolerance. Duplicate source IDs
and non-finite matching amounts fail before producing results. This does not
change monetary representation throughout the other pipeline modules.

## Verification

Run `PYTHONPATH=src python3 -m unittest tests.test_deterministic_matcher`.
The suite includes contention, split ambiguity, 36 input permutations, date and
cent boundaries, duplicate keys and invalid amounts. Tests were run against the
old implementation first and exposed failures, then passed with the fix.

Run `PYTHONPATH=src python3 -m recon_engine.matching.evaluate_baseline`.
The frozen synthetic sample returned 433/520 correct under the existing scoring
definition (83.3%). This aggregate includes correctly identified exceptions;
it is not automatic-match precision. Batch, hard-return and timing-outlier
cases remain outside this baseline. The new regressions cover adversarial cases
that the sample does not cover.

The new GitHub Actions workflow runs allocation tests, the existing isolated
human-approval test, and a container build/test. The approval test now exits
unsuccessfully if the expected interrupt never occurs. Workflow configuration
alone does not mean hosted CI or deployment has run.

## Remaining release work

- Durable allocation constraints across runs and across deterministic, agent,
  and human-approved paths; this patch only protects one baseline invocation.
- Richer ambiguity reasons (cross-transaction contention versus duplicate
  settlements), propagated through the investigation and review interfaces.
- Idempotent file/batch ingestion and database integration tests.
- Match precision/coverage by discrepancy type, separate from review accuracy.
- Runtime metrics, infrastructure, hosted CI and a verified cloud job release.

Learning checkpoint: explain why a consumed-ID set used greedily would avoid
double use but still make file order choose the winner. Demonstrate both
transactions entering review and the unchanged valid single-match case.
