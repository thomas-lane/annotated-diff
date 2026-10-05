"""A tiny inventory module used to demonstrate annotated diffs."""
import json


def load(path):
    with open(path) as f:
        return json.load(f)


def total_value(items):
    total = 0
    for item in items:
        total += item["price"] * item["qty"]
    return total


def restock(items, name, qty):
    for item in items:
        if item["name"] == name:
            item["qty"] = qty
    return items


def report(items):
    lines = []
    for item in items:
        lines.append("%s: %d" % (item["name"], item["qty"]))
    return "\n".join(lines)
