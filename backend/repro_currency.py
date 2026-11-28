"""REPRO (Stream A, issue 1) — run BEFORE the fix: shows the currency
regex truncating Indian-grouped prices and misreading European decimals.

Usage: python3 repro_currency.py   (from backend/)
"""
import sys
sys.path.insert(0, ".")

from app.extractors.research import disambiguate_prices
from app.monitor_check import _amount_from_text, _parse_amount

CASES = [
    # (text, expected current)
    ("Price: ₹1,29,999", 129999.0),      # Indian grouping
    ("Price: ₹89999", 89999.0),          # ungrouped
    ("Price: ₹4,199.50", 4199.50),       # decimals
    ("Price: $1,299.99", 1299.99),
    ("Price: €1.299,99", 1299.99),       # European dot-thousands, comma-decimal
    ("Price: €1,299.99", 1299.99),
    ("Price: £999", 999.0),
    ("Price: ₹1,00,000", 100000.0),      # Indian grouping, lakh
    ("Price: ₹99", 99.0),
    ("Price: ₹12,34,567", 1234567.0),    # Indian grouping, 7 digits
    ("Price: ₹1,29,999.50", 129999.50),  # Indian grouping + decimals
]

fails = 0
for text, expected in CASES:
    got = disambiguate_prices(text).get("current")
    status = "OK  " if got == expected else "FAIL"
    if got != expected:
        fails += 1
    print(f"{status} disambiguate {text!r:28} -> {got} (want {expected})")

# monitor_check's first-amount helper (price-classed / microdata path)
for text, expected in [("₹1,29,999", 129999.0), ("€1.299,99", 1299.99),
                       ("₹4,199.50", 4199.50)]:
    got, cur = _amount_from_text(text)
    status = "OK  " if got == expected else "FAIL"
    if got != expected:
        fails += 1
    print(f"{status} _amount_from_text {text!r:15} -> {got} {cur} (want {expected})")

print(f"\n{fails} failures" if fails else "\nall pass")
sys.exit(1 if fails else 0)
