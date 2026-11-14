from datetime import datetime, timedelta
from pathlib import Path

import pytest

import parsers
import rules
import tracker

T0, T1 = Path("fixtures/t0"), Path("fixtures/t1")
CANDY = "https://web-scraping.dev/product/1"
ATTIC = "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"
OLIO = "https://books.toscrape.com/catalogue/olio_984/index.html"
ITEMS = [{"url": CANDY, "below": 8.0}, {"url": ATTIC, "drop_pct": 10}, {"url": OLIO}]


def page(folder, url):
    return tracker.fixture_path(folder, url).read_text()


def test_jsonld_parser_reads_schema_org_product():
    assert parsers.parse(CANDY, page(T0, CANDY)) == {
        "name": "Box of Chocolate Candy", "price": 9.99, "currency": "USD", "in_stock": True}


def test_books_parser_reads_the_main_product_not_the_related_ones():
    assert parsers.parse(ATTIC, page(T0, ATTIC))["price"] == 51.77
    olio = parsers.parse(OLIO, page(T0, OLIO))
    assert olio["name"] == "Olio" and olio["in_stock"] is False


def test_jsonld_handles_graph_and_plain_offer():
    html = ('<script type="application/ld+json">{"@graph": [{"@type": "WebPage"}, {"@type": ["Product"], '
            '"name": "Mug", "offers": [{"price": "12.50", "priceCurrency": "EUR", '
            '"availability": "http://schema.org/OutOfStock"}]}]}</script>')
    assert parsers.jsonld(html) == {"name": "Mug", "price": 12.5, "currency": "EUR", "in_stock": False}


def test_jsonld_variants_take_the_cheapest_price_and_any_stock():
    html = ('<script type="application/ld+json">{"@type": "Product", "name": "Tee", "offers": ['
            '{"price": "30", "priceCurrency": "USD", "availability": "https://schema.org/OutOfStock"},'
            '{"price": "20", "priceCurrency": "USD", "availability": "https://schema.org/InStock"}]}</script>')
    assert parsers.jsonld(html) == {"name": "Tee", "price": 20.0, "currency": "USD", "in_stock": True}


def test_parser_fails_loudly_on_a_changed_page():
    with pytest.raises(ValueError):
        parsers.parse(CANDY, "<html><body>Under maintenance</body></html>")


def cur(price, in_stock=True):
    return {"name": "Mug", "price": price, "currency": "USD", "in_stock": in_stock}


@pytest.mark.parametrize("prev, now, fired", [
    (None, cur(7), ["below"]),               # first check already under target
    (cur(9), cur(7), ["below", "drop"]),     # crossed it, and fell 22%
    (cur(7), cur(6.9), []),                  # was already under: no repeat
    (cur(10), cur(8.9), ["drop"]),           # -11% with drop_pct 10
    (cur(10), cur(9.5), []),                 # -5%
    (cur(9, False), cur(9), ["restock"]),
    (cur(9), cur(9, False), []),             # going out of stock is not an alert
])
def test_rules_fire_on_the_change(prev, now, fired):
    item = {"below": 8, "drop_pct": 10}
    assert [rule for rule, _ in rules.check(item, prev, now)] == fired


def run(db, folder, when, fetch=None):
    sent = []
    tracker.run(db, ITEMS, fetch or (lambda url: page(folder, url)), sent.append, when)
    return sent


def test_end_to_end_t0_then_t1():
    db, t = tracker.connect(":memory:"), datetime(2026, 11, 20, 9)
    assert run(db, T0, t) == []
    sent = run(db, T1, t + timedelta(minutes=30))
    assert [m.split()[0] for m in sent] == ["🎯", "📉", "📦"]
    assert "51.77 → GBP 44.99" in sent[1] and sent[1].endswith(ATTIC)


def test_same_alert_is_not_resent_within_a_day_when_price_flaps():
    db, t = tracker.connect(":memory:"), datetime(2026, 11, 20, 9)
    for i, folder in enumerate([T0, T1, T0, T1]):
        sent = run(db, folder, t + timedelta(hours=i))
        assert (len(sent) == 3) == (i == 1)  # the drop and back-in-stock at hours 1 and 3 are the same alerts
    assert len(run(db, T0, t + timedelta(days=1, hours=2)) + run(db, T1, t + timedelta(days=1, hours=3))) == 3


def test_failing_page_alerts_once_after_three_failures():
    db, t = tracker.connect(":memory:"), datetime(2026, 11, 20, 9)
    run(db, T0, t)

    def broken(url):
        if url == OLIO:
            raise TimeoutError("timed out")
        return page(T0, url)

    alerts = [run(db, T0, t + timedelta(hours=h), broken) for h in range(1, 6)]
    assert [len(a) for a in alerts] == [0, 0, 1, 0, 0]
    assert alerts[2][0].startswith("⚠️ Olio: 3 failed checks in a row (TimeoutError")
