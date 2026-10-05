"""A tiny inventory module used to demonstrate annotated diffs."""
import json
from decimal import Decimal


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def total_value(items):
    total = Decimal("0")
    for item in items:
        total += Decimal(str(item["price"])) * item["qty"]
    return total


def restock(items, name, qty):
    if qty < 0:
        raise ValueError(f"cannot restock {name} by a negative amount: {qty}")
    for item in items:
        if item["name"] == name:
            item["qty"] += qty
            return items
    raise KeyError(f"no item named {name!r}")


def report(items):
    lines = []
    for item in items:
        lines.append("%s: %d" % (item["name"], item["qty"]))
    return "\n".join(lines)
