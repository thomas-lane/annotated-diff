# Inventory

Load a stock file and print a report.

    python -m inventory stock.json

`restock(items, name, qty)` adds `qty` units to an item and raises `KeyError` for an unknown item.
Totals are exact decimals, so `0.1 + 0.2` prices add up to `0.3`.
