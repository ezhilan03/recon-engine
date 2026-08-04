# Settlement File Structure — Field Mapping

This dataset's schema is grounded in a real external network settlement
report format, genericized per InfoSec policy (no vendor name, no field
names copied verbatim where they'd identify the source). The mapping below
documents which real-world report sections and fields this project's
schema represents, and which it deliberately omits.

## Real file sections (typical external settlement report)

A production settlement file from a card network is not a single flat
table — it's organized into report sections:

1. **General Report Header** — report-level metadata (processor/issuer
   identifiers, currency, platform, report ID, IIN range, card product).
   Not modeled here — this project operates at transaction-line
   granularity; header metadata doesn't affect matching logic.

2. **Detail Transactions** — one row per transaction. **This is what
   `network_settlement.csv` represents.**

3. **Summary of Deposit & Adjustment Transactions** — aggregated totals
   by transaction code. Not modeled — this is a downstream accounting
   view, not part of the transaction-matching problem.

4. **Summary of Issuer Fees / Assessments**, **Summary of Issuer
   Corrections**, **Summary of Issuer Recalculated Interchange** — GL/
   accounting-level reconciliation categories, separate from transaction
   matching. Out of scope for this project.

5. **Issuer Net Settlement Amount Total** — grand totals across all
   sections. Not modeled.

## Detail Transaction field mapping

| Real report field (genericized) | This project's field | Notes |
|---|---|---|
| Card Number | `card_number_masked` | PCI-masked format: first-6 (BIN) + mask + last-4 |
| Transaction Amount / Cash at Checkout | `gross_amount` | |
| Interchange Amount | `fee_amount` | conceptually the same thing: network-assessed fee |
| MCC | `mcc` | ISO 18245 merchant category code |
| Merchant Number | `merchant_number` | per-merchant ID, distinct from the descriptor text |
| Transaction Description | `descriptor` | |
| (transaction/settlement timing fields) | `settlement_date` | |
| — | `batch_id` | not a literal real-file field; represents batch/sequence grouping |
| — | `return_code` | modeled via real NACHA R-codes (ACH side), not the card-network return taxonomy |

## Why `card_number_masked` matters for matching

This is the field that changes the matching problem. Without it, the only
usable anchor between the two files is `account_last4` — weak, and useless
for batched settlement (see `sql/schema.sql` comments and the matcher's
own docstring). A masked card number is a much stronger candidate key:
narrower collision risk than last-4 alone, and present on both sides of a
real settlement file.

**This project's matcher does not yet use `card_number_masked`.** It's
included in the schema for realism and as groundwork for a future
matching upgrade, but the current deterministic matcher and graph
classification logic are unchanged and still key off `account_last4` only.
Using `card_number_masked` (or a real disguised identifier like an RRN/
auth code) as the primary matching anchor is exactly the kind of
composite-scoring upgrade documented as deliberately deferred — see the
project's open technical thread on greedy vs. weighted/optimal matching.
