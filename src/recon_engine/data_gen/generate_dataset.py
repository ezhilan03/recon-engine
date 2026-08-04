"""
Generates three files:

  internal_ledger.csv     -- Spire's internal transaction record
  network_settlement.csv  -- the external settlement file (stands in for
                              a card network's settlement feed; deliberately
                              has NO field that maps 1:1 to internal_txn_id)
  ground_truth.csv         -- internal_txn_id -> settlement_line_id(s) +
                              discrepancy_type. NOT an input to the matcher.
                              Used only afterward to score match accuracy,
                              the same role RAGAS's ground truth played in
                              Project 1.

Run:
    uv run python -m recon_engine.data_gen.generate_dataset
"""

import csv
import hashlib
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from recon_engine.data_gen.discrepancy_types import DISTRIBUTION, DiscrepancyType
from recon_engine.data_gen.nacha_codes import NACHA_RETURN_CODES

SEED = 42
N_TRANSACTIONS = 500
OUTPUT_DIR = Path(__file__).resolve().parents[3] / "data" / "output"

MERCHANT_DESCRIPTORS = [
    "FUEL STOP #4471", "QUIKMART 0092", "HWY60 FUEL CTR", "TRAVEL PLAZA 12",
    "EXPRESS FUEL 8", "CORNER STORE 3", "GAS N GO 214", "PANTRY PLUS 77",
]

# Merchant category codes -- real ISO 18245 codes, fuel/convenience-weighted
# to match Spire's actual client profile (fuel retail, c-stores).
MERCHANT_MCC = {
    "FUEL STOP #4471": "5541",   # Service Stations
    "QUIKMART 0092": "5499",     # Misc. Food Stores / Convenience
    "HWY60 FUEL CTR": "5542",    # Automated Fuel Dispensers
    "TRAVEL PLAZA 12": "5541",
    "EXPRESS FUEL 8": "5542",
    "CORNER STORE 3": "5499",
    "GAS N GO 214": "5541",
    "PANTRY PLUS 77": "5499",
}
MERCHANT_NUMBER = {desc: f"MID{700000 + i}" for i, desc in enumerate(MERCHANT_DESCRIPTORS)}

random.seed(SEED)


def make_card_number(account_last4: str) -> str:
    """Masked card number: first-6 (BIN) + mask + last4, standard PCI display
    format. BIN is derived deterministically from account_last4 so the same
    account always gets the same masked number within this dataset.

    Uses hashlib, NOT Python's built-in hash(). Built-in hash() is
    intentionally randomized per-process for strings/tuples (PYTHONHASHSEED,
    a security feature since Python 3.3) -- using it here silently broke
    reproducibility even after the uuid4() fix: transaction/settlement IDs
    became correctly seeded, but this field kept changing on every run.
    hashlib functions are not affected by PYTHONHASHSEED, so this is stable
    across processes, runs, and machines."""
    digest = hashlib.md5(account_last4.encode()).hexdigest()
    bin6 = str(100000 + (int(digest, 16) % 900000))
    return f"{bin6}******{account_last4}"


def make_id(prefix: str) -> str:
    """Generates an ID using the seeded `random` module, not uuid.uuid4().
    uuid.uuid4() pulls from OS randomness and completely ignores
    random.seed() -- using it here silently broke the "seed=42 means
    reproducible dataset" claim: every regeneration produced identical
    discrepancy-type COUNTS (those come from random.choices, which IS
    seeded) but entirely different transaction/settlement IDs every time.
    That's what caused ground_truth.csv to fall out of sync with whatever
    was already loaded in Postgres, silently zeroing out every accuracy
    score. This generator is fully seeded, so the same seed now produces
    byte-for-byte identical IDs across machines and runs."""
    hex_chars = "0123456789ABCDEF"
    suffix = "".join(random.choices(hex_chars, k=10))
    return f"{prefix}-{suffix}"


@dataclass
class InternalTxn:
    internal_txn_id: str
    transaction_date: date
    amount: float
    account_last4: str
    merchant_descriptor: str
    discrepancy_type: DiscrepancyType
    nacha_return_code: str | None = None
    card_number_masked: str = ""
    mcc: str = ""
    merchant_number: str = ""


@dataclass
class SettlementLine:
    settlement_line_id: str
    settlement_date: date
    gross_amount: float
    fee_amount: float
    net_amount: float
    account_last4: str
    descriptor: str
    batch_id: str
    return_code: str | None = None
    card_number_masked: str = ""
    mcc: str = ""
    merchant_number: str = ""


def weighted_choice() -> DiscrepancyType:
    types, weights = zip(*DISTRIBUTION)
    return random.choices(types, weights=weights, k=1)[0]


def mangle_descriptor(desc: str) -> str:
    """Simulate how descriptors get truncated/reformatted by a network."""
    variants = [
        desc[:12],                                   # truncated
        desc.replace(" ", "").upper()[:15],          # no spaces, truncated
        desc.split()[0] + " " + desc.split()[-1][:4] if " " in desc else desc,
    ]
    return random.choice(variants)


def make_internal_ledger() -> list[InternalTxn]:
    txns = []
    base_date = date(2026, 6, 1)
    for _ in range(N_TRANSACTIONS):
        dtype = weighted_choice()
        txn_date = base_date + timedelta(days=random.randint(0, 29))
        amount = round(random.uniform(15.0, 180.0), 2)
        last4 = f"{random.randint(0, 9999):04d}"
        descriptor = random.choice(MERCHANT_DESCRIPTORS)

        return_code = None
        if dtype == DiscrepancyType.HARD_RETURN:
            return_code = random.choice(list(NACHA_RETURN_CODES.keys()))

        txns.append(InternalTxn(
            internal_txn_id=make_id("TXN"),
            transaction_date=txn_date,
            amount=amount,
            account_last4=last4,
            merchant_descriptor=descriptor,
            discrepancy_type=dtype,
            nacha_return_code=return_code,
            card_number_masked=make_card_number(last4),
            mcc=MERCHANT_MCC[descriptor],
            merchant_number=MERCHANT_NUMBER[descriptor],
        ))
    return txns


def build_settlement_and_ground_truth(
    txns: list[InternalTxn],
) -> tuple[list[SettlementLine], list[dict]]:
    settlement: list[SettlementLine] = []
    ground_truth: list[dict] = []
    batch_counter = 1000

    # group internal txns for BATCHED_SETTLEMENT (needs >1 source txn per line)
    batch_pool: list[InternalTxn] = []

    for txn in txns:
        dtype = txn.discrepancy_type
        matched_line_ids: list[str] = []

        if dtype == DiscrepancyType.CLEAN_MATCH:
            line = _settle_line(txn, lag_days=random.randint(1, 2))
            settlement.append(line)
            matched_line_ids = [line.settlement_line_id]

        elif dtype == DiscrepancyType.FEE_VARIANCE:
            # fee is larger/irregular vs. the standard schedule -- net_amount
            # doesn't reconcile against a flat fee assumption
            line = _settle_line(txn, lag_days=random.randint(1, 2),
                                 fee_override=round(txn.amount * random.uniform(0.04, 0.09), 2))
            settlement.append(line)
            matched_line_ids = [line.settlement_line_id]

        elif dtype == DiscrepancyType.SPLIT_SETTLEMENT:
            part1 = round(txn.amount * 0.6, 2)
            part2 = round(txn.amount - part1, 2)
            l1 = _settle_line(txn, lag_days=1, amount_override=part1)
            l2 = _settle_line(txn, lag_days=2, amount_override=part2)
            settlement.extend([l1, l2])
            matched_line_ids = [l1.settlement_line_id, l2.settlement_line_id]

        elif dtype == DiscrepancyType.BATCHED_SETTLEMENT:
            batch_pool.append(txn)
            if len(batch_pool) < 3:
                continue  # wait until we have 3 to batch, then flush below
            line = _batched_settle_line(batch_pool, batch_counter)
            batch_counter += 1
            settlement.append(line)
            for t in batch_pool:
                ground_truth.append(_gt_row(t, [line.settlement_line_id]))
            batch_pool = []
            continue  # already wrote ground truth rows for this group

        elif dtype == DiscrepancyType.TIMING_OUTLIER:
            line = _settle_line(txn, lag_days=random.randint(10, 20))
            settlement.append(line)
            matched_line_ids = [line.settlement_line_id]

        elif dtype == DiscrepancyType.DUPLICATE_LINE:
            l1 = _settle_line(txn, lag_days=1)
            l2 = _settle_line(txn, lag_days=1)  # exact duplicate, different line id
            settlement.extend([l1, l2])
            matched_line_ids = [l1.settlement_line_id, l2.settlement_line_id]

        elif dtype == DiscrepancyType.ORPHAN_INTERNAL:
            pass  # never settles -- no settlement line at all

        elif dtype == DiscrepancyType.HARD_RETURN:
            line = _settle_line(txn, lag_days=random.randint(3, 6), amount_override=0.0)
            line.return_code = txn.nacha_return_code
            settlement.append(line)
            matched_line_ids = [line.settlement_line_id]

        elif dtype == DiscrepancyType.DESCRIPTOR_MANGLED:
            line = _settle_line(txn, lag_days=random.randint(1, 2))
            line.descriptor = mangle_descriptor(txn.merchant_descriptor)
            settlement.append(line)
            matched_line_ids = [line.settlement_line_id]

        else:
            raise ValueError(
                f"No handler for discrepancy_type={dtype!r}. "
                "Every value assignable via DISTRIBUTION must have a branch here."
            )

        ground_truth.append(_gt_row(txn, matched_line_ids))

    # flush any leftover partial batch as clean matches so nothing is dropped
    for t in batch_pool:
        line = _settle_line(t, lag_days=1)
        settlement.append(line)
        ground_truth.append(_gt_row(t, [line.settlement_line_id]))

    # ORPHAN_SETTLEMENT: inject settlement lines with no internal source at all
    n_orphans = max(1, int(N_TRANSACTIONS * 0.04))
    for _ in range(n_orphans):
        orphan_last4 = f"{random.randint(0, 9999):04d}"
        orphan_descriptor = random.choice(MERCHANT_DESCRIPTORS)
        fake = SettlementLine(
            settlement_line_id=make_id("SETL"),
            settlement_date=date(2026, 6, random.randint(1, 30)),
            gross_amount=round(random.uniform(15.0, 180.0), 2),
            fee_amount=0.0,
            net_amount=0.0,
            account_last4=orphan_last4,
            descriptor=orphan_descriptor,
            batch_id=f"BATCH-{batch_counter}",
            card_number_masked=make_card_number(orphan_last4),
            mcc=MERCHANT_MCC[orphan_descriptor],
            merchant_number=MERCHANT_NUMBER[orphan_descriptor],
        )
        fake.net_amount = round(fake.gross_amount - fake.fee_amount, 2)
        settlement.append(fake)
        ground_truth.append({
            "internal_txn_id": "",
            "settlement_line_ids": fake.settlement_line_id,
            "discrepancy_type": DiscrepancyType.ORPHAN_SETTLEMENT.value,
            "expected_cardinality": "unmatched_settlement",
        })
        batch_counter += 1

    return settlement, ground_truth


def _settle_line(txn: InternalTxn, lag_days: int, amount_override=None, fee_override=None) -> SettlementLine:
    gross = amount_override if amount_override is not None else txn.amount
    fee = fee_override if fee_override is not None else round(gross * 0.025, 2)
    return SettlementLine(
        settlement_line_id=make_id("SETL"),
        settlement_date=txn.transaction_date + timedelta(days=lag_days),
        gross_amount=gross,
        fee_amount=fee,
        net_amount=round(gross - fee, 2),
        account_last4=txn.account_last4,
        descriptor=txn.merchant_descriptor,
        batch_id=f"BATCH-{txn.transaction_date.strftime('%Y%m%d')}",
        card_number_masked=txn.card_number_masked,
        mcc=txn.mcc,
        merchant_number=txn.merchant_number,
    )


def _batched_settle_line(txns: list[InternalTxn], batch_id_num: int) -> SettlementLine:
    total_gross = round(sum(t.amount for t in txns), 2)
    fee = round(total_gross * 0.025, 2)
    latest_txn = max(txns, key=lambda t: t.transaction_date)
    return SettlementLine(
        settlement_line_id=make_id("SETL"),
        settlement_date=latest_txn.transaction_date + timedelta(days=2),
        gross_amount=total_gross,
        fee_amount=fee,
        net_amount=round(total_gross - fee, 2),
        account_last4=txns[0].account_last4,
        descriptor=txns[0].merchant_descriptor,
        batch_id=f"BATCH-{batch_id_num}",
        # NOTE: like account_last4, a single card_number_masked can't represent
        # a batch of transactions from potentially different cards -- borrows
        # the first member's, same documented limitation as account_last4.
        card_number_masked=txns[0].card_number_masked,
        mcc=txns[0].mcc,
        merchant_number=txns[0].merchant_number,
    )


def _gt_row(txn: InternalTxn, matched_line_ids: list[str]) -> dict:
    from recon_engine.data_gen.discrepancy_types import EXPECTED_MATCH_CARDINALITY
    return {
        "internal_txn_id": txn.internal_txn_id,
        "settlement_line_ids": "|".join(matched_line_ids),
        "discrepancy_type": txn.discrepancy_type.value,
        "expected_cardinality": EXPECTED_MATCH_CARDINALITY[txn.discrepancy_type],
    }


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    txns = make_internal_ledger()
    settlement, ground_truth = build_settlement_and_ground_truth(txns)

    ledger_rows = [{
        "internal_txn_id": t.internal_txn_id,
        "transaction_date": t.transaction_date.isoformat(),
        "amount": t.amount,
        "account_last4": t.account_last4,
        "merchant_descriptor": t.merchant_descriptor,
        "nacha_return_code": t.nacha_return_code or "",
        "card_number_masked": t.card_number_masked,
        "mcc": t.mcc,
        "merchant_number": t.merchant_number,
    } for t in txns]

    settlement_rows = [{
        "settlement_line_id": s.settlement_line_id,
        "settlement_date": s.settlement_date.isoformat(),
        "gross_amount": s.gross_amount,
        "fee_amount": s.fee_amount,
        "net_amount": s.net_amount,
        "account_last4": s.account_last4,
        "descriptor": s.descriptor,
        "batch_id": s.batch_id,
        "return_code": s.return_code or "",
        "card_number_masked": s.card_number_masked,
        "mcc": s.mcc,
        "merchant_number": s.merchant_number,
    } for s in settlement]

    write_csv(OUTPUT_DIR / "internal_ledger.csv", ledger_rows,
              ["internal_txn_id", "transaction_date", "amount", "account_last4",
               "merchant_descriptor", "nacha_return_code", "card_number_masked",
               "mcc", "merchant_number"])

    write_csv(OUTPUT_DIR / "network_settlement.csv", settlement_rows,
              ["settlement_line_id", "settlement_date", "gross_amount", "fee_amount",
               "net_amount", "account_last4", "descriptor", "batch_id", "return_code",
               "card_number_masked", "mcc", "merchant_number"])

    write_csv(OUTPUT_DIR / "ground_truth.csv", ground_truth,
              ["internal_txn_id", "settlement_line_ids", "discrepancy_type", "expected_cardinality"])

    print(f"internal_ledger.csv:    {len(ledger_rows)} rows")
    print(f"network_settlement.csv: {len(settlement_rows)} rows")
    print(f"ground_truth.csv:       {len(ground_truth)} rows")

    from collections import Counter
    counts = Counter(row["discrepancy_type"] for row in ground_truth)
    print("\nDiscrepancy type distribution:")
    for dtype, count in counts.most_common():
        print(f"  {dtype:22s} {count}")


if __name__ == "__main__":
    main()
