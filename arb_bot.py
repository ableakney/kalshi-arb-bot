
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


# ---------------- SETTINGS: safe to edit ----------------

BASE = "https://external-api.kalshi.com/trade-api/v2"

# (label, nominee event, winner event)
PAIRINGS = [
    ("President - Democratic", "KXPRESNOMD-28", "KXPRESPERSON-28"),
    ("President - Republican", "KXPRESNOMR-28", "KXPRESPERSON-28"),
    ("VP - Democratic", "KXVPRESNOMD-28", "KXVPRESPERSON-29"),
    ("VP - Republican", "KXVPRESNOMR-28", "KXVPRESPERSON-29"),
]

MIN_ANNUALIZED_RETURN = 0.04   # 4% per year after fees
MIN_CONTRACTS = 25             # Minimum number of pairs
MIN_TOTAL_PROFIT = 1.00        # Minimum profit in dollars
FEE_RATE = 0.07                # Estimated taker fee rate

FALLBACK_SETTLE = "2030-01-06T00:00:00+00:00"
PAUSE = 0.2                    # Seconds between API requests


# --------------------------------------------------------


NL = chr(10)


def get(path, params=None):
    """Make a GET request to the Kalshi API."""

    url = BASE + path

    if params:
        url += "?" + urllib.parse.urlencode(params)

    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "arb-alert-bot/1.0",
                    "Accept": "application/json",
                },
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)

            time.sleep(PAUSE)
            return data

        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            raise

        except urllib.error.URLError:
            time.sleep(2 * (attempt + 1))

    raise RuntimeError("Kalshi request failed: " + url)


def list_markets(event):
    """Retrieve all markets belonging to an event."""

    out = []
    cursor = None

    while True:
        params = {
            "event_ticker": event,
            "limit": 200,
        }

        if cursor:
            params["cursor"] = cursor

        data = get("/markets", params)

        out += data.get("markets", [])
        cursor = data.get("cursor")

        if not cursor:
            break

    done = ("closed", "settled", "finalized", "determined")

    return [
        m for m in out
        if m.get("status", "").lower() not in done
    ]


def num(x):
    """Convert a value to a float."""

    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def key(m):
    """Extract the final ticker component for matching."""

    return m["ticker"].split("-")[-1].upper()


def name(m):
    """Get the market's display name."""

    return (
        m.get("yes_sub_title")
        or m.get("title")
        or m["ticker"]
    ).strip()


def parse_time(s):
    """Convert an ISO timestamp into a timezone-aware datetime."""

    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))

        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)

        return d

    except (AttributeError, ValueError):
        return None


def settle_date(*markets):
    """Find the expected settlement date."""

    dates = []

    for m in markets:
        for f in (
            "expiration_time",
            "latest_expiration_time",
            "expected_expiration_time",
            "close_time",
        ):
            d = parse_time(m.get(f))

            if d:
                dates.append(d)
                break

    return max(dates) if dates else parse_time(FALLBACK_SETTLE)


def book(ticker):
    """Retrieve the order book for a market."""

    data = get("/markets/" + urllib.parse.quote(ticker) + "/orderbook")

    fp = data.get("orderbook_fp")

    if fp is None:
        print(
            "  WARNING: unexpected order book format for "
            + ticker + ": " + str(list(data.keys()))
        )
        fp = {}

    return (
        fp.get("yes_dollars", []),
        fp.get("no_dollars", []),
    )


def asks_from(opposite_bids):
    """
    Convert opposite-side bids into equivalent asks.

    A NO bid at 0.93 is equivalent to a YES ask at 0.07.
    """

    levels = [
        (round(1 - float(p), 4), float(q))
        for p, q in opposite_bids
    ]

    levels.sort()

    return levels


def ceil_cent(x):
    """Round a fee upwards to the nearest cent."""

    return math.ceil(round(x * 100, 6)) / 100


def walk(yes_asks, no_asks, years):
    """
    Walk through both order books.

    Buy pairs at the cheapest available levels while each
    additional pair meets the minimum annualised return.
    """

    A = [list(x) for x in yes_asks]
    B = [list(x) for x in no_asks]

    legs = []

    while A and B:

        pa = A[0][0]
        pb = B[0][0]

        fee = FEE_RATE * (
            pa * (1 - pa) + pb * (1 - pb)
        )

        capital = pa + pb + fee
        net = 1 - capital

        if net <= 0:
            break

        if net / capital / years < MIN_ANNUALIZED_RETURN:
            break

        q = min(A[0][1], B[0][1])

        legs.append((pa, pb, q))

        A[0][1] -= q
        B[0][1] -= q

        if A[0][1] <= 1e-9:
            A.pop(0)

        if B[0][1] <= 1e-9:
            B.pop(0)

    return legs


def summarize(legs, years):
    """Calculate the total cost, fees and potential profit."""

    n = sum(q for _, _, q in legs)

    if n <= 0:
        return None

    cost_a = sum(
        pa * q for pa, _, q in legs
    )

    cost_b = sum(
        pb * q for _, pb, q in legs
    )

    fee_a = ceil_cent(
        FEE_RATE * sum(
            q * pa * (1 - pa)
            for pa, _, q in legs
        )
    )

    fee_b = ceil_cent(
        FEE_RATE * sum(
            q * pb * (1 - pb)
            for _, pb, q in legs
        )
    )

    capital = cost_a + cost_b + fee_a + fee_b
    net = n - capital

    return {
        "pairs": n,
        "avg_cost": (cost_a + cost_b) / n,
        "capital": capital,
        "net": net,
        "ret": net / capital,
        "ann": net / capital / years,
    }


def notify(topic, title, body):
    """Send a notification through ntfy."""

    if not topic:
        print(
            "NTFY_TOPIC is not set. Would have sent: "
            + title + NL + body
        )
        return

    req = urllib.request.Request(
        "https://ntfy.sh/" + urllib.parse.quote(topic, safe=""),
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Title": title.encode("ascii", "ignore").decode(),
            "Priority": "high",
            "Tags": "moneybag",
        },
    )

    urllib.request.urlopen(req, timeout=30).read()


def main():

    topic = os.environ.get("NTFY_TOPIC", "").strip()

    # Optional notification test
    if os.environ.get("SEND_TEST") == "1":
        notify(
            topic,
            "Kalshi arb bot test",
            "Test alert: your phone is connected.",
        )

    now = datetime.now(timezone.utc)

    alerts = 0
    failures = 0

    for label, nom_event, win_event in PAIRINGS:

        print("=== " + label + " ===")

        # Retrieve nominee and winner markets
        try:
            noms = list_markets(nom_event)
            wins = list_markets(win_event)

        except Exception as e:
            print("Could not load markets: " + str(e))
            failures += 1
            continue

        # Create lookup dictionaries for matching markets
        by_name = {
            name(m).lower(): m
            for m in wins
        }

        by_key = {
            key(m): m
            for m in wins
        }

        matched = []
        unmatched = []

        for nm in noms:

            wm = (
                by_name.get(name(nm).lower())
                or by_key.get(key(nm))
            )

            if wm:
                matched.append((nm, wm))
            else:
                unmatched.append(name(nm))

        print("Matched pairs: " + str(len(matched)))

        print(
            "No winner-market match for: "
            + (", ".join(unmatched) or "none")
        )

        closest = []

        # Analyse each matched nominee/winner market
        for nm, wm in matched:

            a = num(nm.get("yes_ask_dollars"))
            b = num(wm.get("yes_bid_dollars"))

            # Skip markets where the indicative pair cost
            # is clearly too expensive to offer an arbitrage.
            if (
                a is not None
                and b is not None
                and a + (1 - b) > 1.02
            ):
                continue

            # Calculate time until expected settlement
            settlement = settle_date(nm, wm)

            seconds = max(
                (settlement - now).total_seconds(),
                30 * 86400,
            )

            years = seconds / (365 * 86400)

            # Retrieve both order books
            try:
                _, nom_no_bids = book(nm["ticker"])

                win_yes_bids, _ = book(wm["ticker"])

            except Exception as e:
                print(
                    "  Book error for "
                    + name(nm) + ": " + str(e)
                )
                continue

            # Convert bids into equivalent asks
            yes_asks = asks_from(nom_no_bids)
            no_asks = asks_from(win_yes_bids)

            if not yes_asks or not no_asks:
                continue

            # Record the cheapest observed pair cost
            closest.append(
                (
                    yes_asks[0][0] + no_asks[0][0],
                    name(nm),
                )
            )

            # Walk order books and calculate potential returns
            res = summarize(
                walk(yes_asks, no_asks, years),
                years,
            )

            # Check alert conditions
            if (
                res
                and res["pairs"] >= MIN_CONTRACTS
                and res["net"] >= MIN_TOTAL_PROFIT
            ):

                body = NL.join([
                    label + ": " + name(nm),

                    "Buy YES on the nominee market "
                    "+ NO on the winner market",

                    "%d pairs, avg cost %.1fc (pays at least 100c)"
                    % (
                        res["pairs"],
                        res["avg_cost"] * 100,
                    ),

                    "Net about $%.2f on $%.2f "
                    "(%.1f%% total, %.1f%% a year)"
                    % (
                        res["net"],
                        res["capital"],
                        res["ret"] * 100,
                        res["ann"] * 100,
                    ),

                    "Nominee: " + nm["ticker"],
                    "Winner: " + wm["ticker"],
                ])

                print("ALERT " + body)

                try:
                    notify(
                        topic,
                        "Kalshi arb: " + name(nm),
                        body,
                    )

                    alerts += 1

                except Exception as e:
                    print("Notification failed: " + str(e))

        # Display the three closest opportunities
        for top, who in sorted(closest)[:3]:

            print(
                "  closest: %s top-of-book pair cost %.1fc"
                % (who, top * 100)
            )

    print("Done. Alerts sent: " + str(alerts))

    if failures == len(PAIRINGS):
        sys.exit(1)


if __name__ == "__main__":
    main()

