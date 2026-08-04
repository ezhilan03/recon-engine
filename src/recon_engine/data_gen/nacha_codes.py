"""
NACHA ACH return reason codes.

These are the public standard return codes defined by Nacha (the ACH
network operator). They're not proprietary to any company, so they're
safe to use verbatim in a public portfolio project.

We use a subset that's representative of what actually shows up in
retail/fuel ACH settlement -- not the full ~80-code list, which
includes many codes that are effectively never seen outside specific
international/IAT contexts.

Each code maps to:
  - short_desc: what it means
  - category:   how it should route in the reconciliation graph
                 ("hard_return" = money is gone, needs write-off/dispute;
                  "soft_return" = may be retried;
                  "administrative" = data/format issue, not a funds issue)
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class NachaCode:
    code: str
    short_desc: str
    category: str  # hard_return | soft_return | administrative


NACHA_RETURN_CODES: dict[str, NachaCode] = {
    "R01": NachaCode("R01", "Insufficient Funds", "soft_return"),
    "R02": NachaCode("R02", "Account Closed", "hard_return"),
    "R03": NachaCode("R03", "No Account / Unable to Locate Account", "hard_return"),
    "R04": NachaCode("R04", "Invalid Account Number Structure", "administrative"),
    "R05": NachaCode("R05", "Unauthorized Debit to Consumer Account (corrected)", "hard_return"),
    "R07": NachaCode("R07", "Authorization Revoked by Customer", "hard_return"),
    "R08": NachaCode("R08", "Payment Stopped", "hard_return"),
    "R09": NachaCode("R09", "Uncollected Funds", "soft_return"),
    "R10": NachaCode("R10", "Customer Advises Not Authorized", "hard_return"),
    "R11": NachaCode("R11", "Customer Advises Entry Not in Accordance with Terms", "administrative"),
    "R16": NachaCode("R16", "Account Frozen", "hard_return"),
    "R20": NachaCode("R20", "Non-Transaction Account", "administrative"),
    "R29": NachaCode("R29", "Corporate Customer Advises Not Authorized", "hard_return"),
}

# Codes that are realistic to see repeated for the *same* account within
# a short window (useful when generating synthetic "problem" accounts).
RETRY_ELIGIBLE = {"R01", "R09"}
