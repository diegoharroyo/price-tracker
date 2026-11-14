"""Web page with every tracked product, its price history and recent alerts, plus /health for uptime checks.
Run: uv run dashboard.py   (http://localhost:8000)
     uv run dashboard.py --static site/index.html   (write the page once, e.g. for GitHub Pages)"""

import argparse
import os
import sqlite3
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, render_template_string

import tracker

DB = os.environ.get("PRICES_DB", "prices.db")
CHECK_EVERY_MIN = int(os.environ.get("CHECK_EVERY_MIN", "30"))  # keep in sync with deploy/price-tracker.timer

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Price tracker</title>
<style>
  :root { --ink:#1c2321; --muted:#6b7672; --line:#e3e8e5; --bg:#f7f9f8; --down:#1f7a52; --up:#b4432f; }
  body { margin:0; padding:32px 20px; background:var(--bg); color:var(--ink);
         font:15px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif; }
  main { max-width:980px; margin:auto; }
  h1 { font-size:26px; margin:0 0 4px; } h2 { font-size:17px; margin:36px 0 10px; }
  .sub { color:var(--muted); margin:0 0 24px; }
  .pill { display:inline-block; padding:1px 9px; border-radius:99px; font-size:13px; font-weight:600; }
  .ok { background:#dff1e7; color:var(--down); } .bad { background:#f8e1dc; color:var(--up); }
  .wrap { overflow-x:auto; background:#fff; border:1px solid var(--line); border-radius:12px; }
  table { border-collapse:collapse; width:100%; min-width:720px; }
  th, td { padding:12px 14px; text-align:left; border-bottom:1px solid var(--line); vertical-align:middle; }
  th { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); font-weight:600; }
  tr:last-child td { border-bottom:0; }
  a { color:inherit; } td.num { font-variant-numeric:tabular-nums; white-space:nowrap; }
  .price { font-weight:650; } .down { color:var(--down); font-weight:600; } .up { color:var(--up); font-weight:600; }
  .muted { color:var(--muted); } .err { color:var(--up); font-size:13px; }
  ul { list-style:none; padding:0; margin:0; background:#fff; border:1px solid var(--line); border-radius:12px; }
  li { padding:10px 14px; border-bottom:1px solid var(--line); } li:last-child { border-bottom:0; }
  footer { color:var(--muted); font-size:13px; margin-top:28px; }
</style></head><body><main>
<h1>Price tracker</h1>
<p class="sub">
  {% if last %}{% if healthy is not none %}<span class="pill {{ 'ok' if healthy else 'bad' }}">{{ 'Running' if healthy else 'Stalled' }}</span>{% endif %}
  Last check {{ last|when }} · {{ rows|length }} products · checked every {{ every }} min
  {% else %}No checks yet.{% endif %}
</p>
<div class="wrap"><table>
<tr><th>Product</th><th>Price</th><th>Change</th><th>History</th><th>Stock</th><th>Alert when</th></tr>
{% for r in rows %}<tr>
  <td><a href="{{ r.url }}">{{ r.name }}</a>{% if r.error %}<div class="err">Last check failed: {{ r.error }}</div>{% endif %}</td>
  <td class="num price">{% if r.price is not none %}{{ r.currency }} {{ "%.2f"|format(r.price) }}{% else %}<span class="muted">—</span>{% endif %}</td>
  <td class="num">{% if r.change is none or r.change == 0 %}<span class="muted">—</span>
      {% elif r.change < 0 %}<span class="down">▼ {{ "%.0f"|format(-r.change) }}%</span>
      {% else %}<span class="up">▲ {{ "%.0f"|format(r.change) }}%</span>{% endif %}</td>
  <td>{{ r.spark|safe }}</td>
  <td>{% if r.in_stock is none %}<span class="muted">—</span>{% elif r.in_stock %}In stock{% else %}<span class="up">Out of stock</span>{% endif %}</td>
  <td class="muted">{{ r.rule }}</td>
</tr>{% endfor %}
</table></div>
<h2>Recent alerts</h2>
{% if alerts %}<ul>{% for a in alerts %}<li><span class="muted">{{ a.sent_at|when }}</span> · {{ a.message }}</li>{% endfor %}</ul>
{% else %}<p class="muted">None yet.</p>{% endif %}
<footer>{{ note }}Times are the server's local time ({{ tz }}).</footer>
</main></body></html>"""

app = Flask(__name__)
app.jinja_env.filters["when"] = lambda ts: datetime.fromisoformat(ts).strftime("%d %b %Y, %H:%M")


def sparkline(prices: list[float], w: int = 120, h: int = 30) -> str:
    if len(prices) < 2:
        return '<span class="muted">—</span>'
    lo, hi = min(prices), max(prices)
    step = w / (len(prices) - 1)
    pts = [(i * step, h - 4 - (p - lo) / ((hi - lo) or 1) * (h - 8) if hi > lo else h / 2)
           for i, p in enumerate(prices)]
    color = "#1f7a52" if prices[-1] < prices[0] else "#b4432f" if prices[-1] > prices[0] else "#9aa5a1"
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    x, y = pts[-1]
    return (f'<svg width="{w + 4}" height="{h}" viewBox="-2 0 {w + 4} {h}" role="img" aria-label="price history">'
            f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/></svg>')


def rule_text(item: dict) -> str:
    parts = [f"≤ {item['below']:.2f}"] if item.get("below") is not None else []
    parts += [f"drops {item['drop_pct']:g}%+"] if item.get("drop_pct") else []
    return " · ".join(parts + ["back in stock"])


def is_healthy(db: sqlite3.Connection) -> tuple[str | None, bool]:
    last = db.execute("select max(checked_at) from checks where error is null").fetchone()[0]
    return last, bool(last) and datetime.fromisoformat(last) > datetime.now() - timedelta(minutes=2 * CHECK_EVERY_MIN)


def render(db: sqlite3.Connection, watchlist: str = "watchlist.toml", note: str = "", static: bool = False) -> str:
    items = {i["url"]: i for i in tomllib.loads(Path(watchlist).read_text())["item"]}
    rows = []
    for url, item in items.items():
        ok = db.execute("select * from checks where url = ? and error is null order by rowid", (url,)).fetchall()
        last = db.execute("select error from checks where url = ? order by rowid desc limit 1", (url,)).fetchone()
        prices = [r["price"] for r in ok]
        rows.append({
            "url": url, "name": url, "price": None, "currency": "", "in_stock": None,  # never read successfully
            **(dict(ok[-1]) if ok else {}), "spark": sparkline(prices[-60:]), "rule": rule_text(item),
            "change": 100 * (prices[-1] / prices[-2] - 1) if len(prices) > 1 else None,
            "error": last and last["error"],
        })
    last, healthy = is_healthy(db)
    alerts = db.execute("select * from alerts order by rowid desc limit 10").fetchall()
    # a static page can't know if checks are still running later, so it only shows the date
    return render_template_string(PAGE, rows=rows, alerts=alerts, last=last, healthy=None if static else healthy,
                                  every=CHECK_EVERY_MIN, note=note, tz=datetime.now().astimezone().tzname())


@app.get("/")
def index():
    return render(tracker.connect(DB))


@app.get("/health")
def health():
    """200 while checks keep coming in, 503 once they stop: point an uptime monitor here."""
    healthy = is_healthy(tracker.connect(DB))[1]
    return {"ok": healthy}, 200 if healthy else 503


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", metavar="FILE", help="write the page to FILE and exit")
    ap.add_argument("--note", default="", help="extra footer text for the static page")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    if args.static:
        with app.app_context():
            Path(args.static).parent.mkdir(parents=True, exist_ok=True)
            Path(args.static).write_text(render(tracker.connect(DB), note=args.note, static=True))
    else:
        app.run(host="127.0.0.1", port=args.port)
