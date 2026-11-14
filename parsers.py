"""Turn a product page into {name, price, currency, in_stock}. Pure: HTML in, dict out.

Most shop platforms (Shopify, WooCommerce...) publish schema.org Product data for Google as JSON-LD,
so `jsonld` covers them without per-site code. Sites without it get their own parser in PARSERS."""

import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


def jsonld(html: str) -> dict:
    for tag in BeautifulSoup(html, "html.parser").find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "null")
        except json.JSONDecodeError:
            continue
        for node in data if isinstance(data, list) else [data]:
            for d in node.get("@graph", [node]) if isinstance(node, dict) else []:
                types = d.get("@type") if isinstance(d.get("@type"), list) else [d.get("@type")]
                offers = d.get("offers") or []
                offers = offers if isinstance(offers, list) else [offers]
                if "Product" not in types or not offers:
                    continue
                # one offer per variant (or one AggregateOffer): cheapest price, in stock if any variant is
                return {
                    "name": d["name"],
                    "price": min(float(o.get("price", o.get("lowPrice"))) for o in offers),
                    "currency": offers[0].get("priceCurrency", ""),
                    "in_stock": any(str(o.get("availability", "")).endswith("InStock") for o in offers),
                }
    raise ValueError("no schema.org Product data on the page")


def books_toscrape(html: str) -> dict:
    main = BeautifulSoup(html, "html.parser").select_one(".product_main")  # the page also lists other books
    if not main or not main.select_one(".price_color"):
        raise ValueError("product block not found")
    price = main.select_one(".price_color").get_text()
    stock = main.select_one(".availability").get_text(" ", strip=True)
    return {
        "name": main.h1.get_text(strip=True),
        "price": float(re.sub(r"[^\d.]", "", price)),
        "currency": "GBP",
        "in_stock": stock.lower().startswith("in stock"),
    }


PARSERS = {"books.toscrape.com": books_toscrape}  # add a site: write a function, map its domain here


def parse(url: str, html: str) -> dict:
    return PARSERS.get(urlsplit(url).hostname, jsonld)(html)
