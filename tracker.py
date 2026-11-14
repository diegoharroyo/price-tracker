"""Check every page in watchlist.toml once: store the result, send the alerts that apply.
Meant to be run on a timer (see deploy/). Run: uv run --env-file .env tracker.py

--fixtures DIR reads saved pages instead of the network (tests, demos)."""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timedelta
from pathlib import Path

import parsers
import rules

UA = "price-tracker-demo/0.1 (+https://github.com/diegoharroyo/price-tracker)"
DELAY = 2  # seconds between requests, more if the site's robots.txt asks for it

SCHEMA = """
create table if not exists checks (url text, checked_at text, name text, price real, currency text,
                                   in_stock integer, error text);
create table if not exists alerts (url text, sent_at text, rule text, price real, message text);
"""


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def fixture_path(fixtures: str, url: str) -> Path:
    return Path(fixtures, re.sub(r"[^\w-]", "_", url.split("://", 1)[1]) + ".html")


class Fetcher:
    """Polite HTTP: obeys robots.txt (allow and crawl-delay), waits between requests to the same site."""

    def __init__(self):
        self.robots, self.last = {}, {}

    def __call__(self, url: str) -> str:
        host = urllib.parse.urlsplit(url).netloc
        if host not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                rp.parse(get(f"https://{host}/robots.txt").splitlines())
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) or e.code >= 500:  # blocked or down: not a robots.txt answer
                    raise
                rp.parse([])  # no robots.txt: everything is allowed
            self.robots[host] = rp
        rp = self.robots[host]
        if not rp.can_fetch(UA, url):
            raise PermissionError("robots.txt disallows this page")
        wait = max(DELAY, rp.crawl_delay(UA) or 0) - (time.monotonic() - self.last.get(host, -1e9))
        time.sleep(max(0, wait))
        self.last[host] = time.monotonic()
        return get(url)


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")


def telegram(text: str) -> None:
    print(text, flush=True)
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        # if Telegram fails the run crashes before committing this page, so the alert is retried next run
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30).close()


def run(db: sqlite3.Connection, items: list[dict], fetch, notify=telegram, now: datetime | None = None) -> int:
    """Check each item once. Returns how many checks failed."""
    now = now or datetime.now()
    failed = 0
    for item in items:
        url = item["url"]
        prev = db.execute("select * from checks where url = ? and error is null order by rowid desc limit 1",
                          (url,)).fetchone()
        try:
            cur, error = parsers.parse(url, fetch(url)), None
        except Exception as e:  # noqa: BLE001 -- one broken page must not stop the others
            cur, error = None, f"{type(e).__name__}: {e}"
            failed += 1
            print(f"failed: {url}: {error}", file=sys.stderr)
        db.execute("insert into checks values (?, ?, ?, ?, ?, ?, ?)",
                   (url, now.isoformat(timespec="seconds"), cur and cur["name"], cur and cur["price"],
                    cur and cur["currency"], cur and cur["in_stock"], error))
        if cur:
            found = [(rule, cur["price"], msg) for rule, msg in rules.check(item, prev and dict(prev), cur)]
        else:
            recent = db.execute("select error from checks where url = ? order by rowid desc limit ?",
                                (url, rules.FAIL_ALERT + 1)).fetchall()
            streak = next((i for i, r in enumerate(recent) if r["error"] is None), len(recent))
            name = prev["name"] if prev else url
            msg = f"⚠️ {name}: {streak} failed checks in a row ({error}). The page may have moved or changed."
            found = [("failing", None, msg)] if streak == rules.FAIL_ALERT else []
        for rule, price, msg in found:
            # the same alert at the same price at most once a day, in case a price flaps up and down
            if db.execute("select 1 from alerts where url = ? and rule = ? and price is ? and sent_at > ?",
                          (url, rule, price, (now - timedelta(days=1)).isoformat())).fetchone():
                continue
            notify(f"{msg}\n{url}")
            db.execute("insert into alerts values (?, ?, ?, ?, ?)",
                       (url, now.isoformat(timespec="seconds"), rule, price, msg))
        db.commit()
    return failed


def to_sheet(db: sqlite3.Connection, since: str) -> None:
    """Optional: 'Latest' tab = current price of every item, 'Alerts' tab = every alert sent."""
    if not os.environ.get("SHEET_ID"):
        return
    import gspread

    book = gspread.service_account(filename=os.environ["GOOGLE_CREDENTIALS"]).open_by_key(os.environ["SHEET_ID"])

    def tab(name, header):
        try:
            return book.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(name, rows=1000, cols=len(header))
            ws.append_row(header)
            return ws

    # sqlite returns the other columns from the row that has the max()
    latest = db.execute("""select url, max(rowid), checked_at, name, price, currency, in_stock, error
                           from checks group by url order by name""").fetchall()
    header = ["name", "price", "currency", "in_stock", "checked_at", "error", "url"]
    ws = tab("Latest", header)
    ws.clear()
    ws.append_rows([header] +
                   [[r["name"], r["price"], r["currency"], bool(r["in_stock"]) if r["in_stock"] is not None else "",
                     r["checked_at"], r["error"] or "", r["url"]] for r in latest])
    new = db.execute("select sent_at, message, url from alerts where sent_at >= ?", (since,)).fetchall()
    if new:
        tab("Alerts", ["sent_at", "message", "url"]).append_rows([list(r) for r in new])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--watchlist", default="watchlist.toml")
    ap.add_argument("--db", default="prices.db")
    ap.add_argument("--fixtures", help="read pages from this folder instead of the network")
    ap.add_argument("--at", type=datetime.fromisoformat, help="pretend the check happens at this time (demos)")
    ap.add_argument("--notify", metavar="TEXT", help="just send TEXT to Telegram and exit (used by OnFailure)")
    args = ap.parse_args()
    if args.notify:
        telegram(args.notify)
        return

    items = tomllib.loads(Path(args.watchlist).read_text())["item"]
    db = connect(args.db)
    fetch = (lambda url: fixture_path(args.fixtures, url).read_text()) if args.fixtures else Fetcher()
    now = args.at or datetime.now()
    failed = run(db, items, fetch, now=now)
    to_sheet(db, now.isoformat(timespec="seconds"))
    print(json.dumps({"checked": len(items), "failed": failed}))


if __name__ == "__main__":
    main()
