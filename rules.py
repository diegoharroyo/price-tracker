"""Alert rules. Pure: previous and current check in, alerts out.

Every rule fires on the change, not on the state: a price that stays under the target
alerts once, not on every check."""

FAIL_ALERT = 3  # consecutive failed checks before warning that a page may have changed


def check(item: dict, prev: dict | None, cur: dict) -> list[tuple[str, str]]:
    """(rule, message) pairs. prev is the last successful check of this item, or None on the first."""
    name, price, money = cur["name"], cur["price"], f"{cur['currency']} {cur['price']:.2f}"
    alerts = []
    below = item.get("below")
    if below is not None and price <= below and (prev is None or prev["price"] > below):
        alerts.append(("below", f"🎯 {name} is down to {money} (your target: {below:.2f})"))
    drop = item.get("drop_pct")
    if drop and prev and price <= prev["price"] * (1 - drop / 100):
        pct = 100 * (1 - price / prev["price"])
        alerts.append(("drop", f"📉 {name} dropped {pct:.0f}%: {prev['price']:.2f} → {money}"))
    if prev and not prev["in_stock"] and cur["in_stock"]:
        alerts.append(("restock", f"📦 {name} is back in stock at {money}"))
    return alerts
