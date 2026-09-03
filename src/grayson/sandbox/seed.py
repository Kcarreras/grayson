"""Sandbox warehouse seeding: a small retail warehouse with the texture of a
real one, and planted, verifiable problems at two levels of difficulty.

The point of the texture is evaluation validity, not disguise: the agent
always knows it is in a sandbox (the connection is named `sandbox`, the
catalog is `SANDBOX`). What it must not be able to do is shortcut the
investigation — reproduce a uniform generator and read every deviation from
it as a defect, or treat any structure at all as suspicious. So the
background has the shape real data has: heavy-tailed order values and
customer activity, weekly and seasonal volume with a Black Friday spike,
ids that grow with signup date, emails that look like emails, promo codes
with validity windows, payments that reconcile to their orders, timestamps
with times in them. Several of those patterns are **decoys**: real
structure a careful run investigates and rules out, and a careless one
reports as a defect. The answer key lists them.

Generation is deterministic (fixed RNG seed) and returns the exact ground
truth, which `grayson sandbox init` renders into an answer key for the human
and `grayson sandbox score` uses to grade a session's findings. The planted
problems map onto built-in workflows:

Tier 1 (the fundamentals):
- table-health      → CUSTOMERS: an email-NULL regression after a cutoff, on
                      one signup channel; duplicated customer ids with
                      conflicting emails; impossible birthdates.
- bug-hunter        → ORDERS_ENRICHED: join fan-out from re-issued promo codes
                      (the join ignores the code's validity window).
- migration-parity  → PAYMENTS vs PAYMENTS_V2: refunded rows dropped, EUR
                      amounts truncated to whole units.

Tier 2 (subtler, needing a join or a second look):
- bug-hunter        → ORDERS: one channel recorded amounts in minor units
                      (×100) for a two-week release window; the payments for
                      those orders are right, so the join gives it away.
- semantic-rule-qa  → ORDERS: WELCOME10 is a first-order-only code, and the
                      partner import path skips that check.
- pipeline-qa       → ORDERS_DAILY: the daily rollup drops the last day of
                      every month (an exclusive date boundary), and inherits
                      the fan-out inflation from upstream.
- migration-parity  → PAYMENTS_V2: PAID_AT lost its time part (typed as DATE).
"""

from __future__ import annotations

import math
import random
import sqlite3
from bisect import bisect_right
from datetime import date, datetime, timedelta
from pathlib import Path

CATALOG = "SANDBOX"
SCHEMA = "SHOP"

#: the data window: orders run from START to END; signups begin earlier
START = date(2025, 9, 1)
END = date(2026, 8, 18)
SIGNUP_START = date(2024, 1, 8)
IOS_LAUNCH = date(2025, 10, 6)  # the iOS app ships; the channel exists from here
BLACK_FRIDAY = date(2025, 11, 28)
CYBER_MONDAY = date(2025, 12, 1)

#: when each table was last written, per the catalog — a pipeline that runs
#: in a plausible order, not one constant
LAST_ALTERED = {
    "CUSTOMERS": "2026-08-18 06:05:12",
    "PROMOS": "2026-05-14 15:40:03",
    "ORDERS": "2026-08-18 06:12:47",
    "ORDERS_ENRICHED": "2026-08-18 06:31:20",
    "ORDERS_DAILY": "2026-08-18 06:40:55",
    "PAYMENTS": "2026-08-18 06:14:02",
    "PAYMENTS_V2": "2026-08-11 22:10:31",
}

# -- planted problems -------------------------------------------------------

_NULL_EMAIL_CUTOFF = date(2026, 7, 15)
_NULL_EMAIL_CHANNEL = "ios"
_NULL_EMAIL_RATE = 0.78
_DUP_CUSTOMER_IDS = [1187, 2604, 3311]
_FUTURE_BIRTHDATE_IDS = [418, 1733, 3960]
_DUP_PROMO_CODES = ["SUMMER25", "FLASH5"]
_CENTS_CHANNEL = "android"
_CENTS_WINDOW = (date(2026, 3, 4), date(2026, 3, 19))
_WELCOME_LEAK_CHANNEL = "partner"
_WELCOME_LEAK_RATE = 0.05

# -- background --------------------------------------------------------------

_FIRST = [
    "James",
    "Mary",
    "John",
    "Patricia",
    "Robert",
    "Jennifer",
    "Michael",
    "Linda",
    "William",
    "Elizabeth",
    "David",
    "Barbara",
    "Richard",
    "Susan",
    "Joseph",
    "Jessica",
    "Thomas",
    "Sarah",
    "Charles",
    "Karen",
    "Daniel",
    "Lisa",
    "Matthew",
    "Nancy",
    "Anthony",
    "Betty",
    "Mark",
    "Sandra",
    "Paul",
    "Ashley",
    "Emma",
    "Liam",
    "Olivia",
    "Noah",
    "Ava",
    "Lucas",
    "Mia",
    "Sophie",
    "Leon",
    "Hannah",
    "Amelie",
    "Louis",
    "Chloe",
    "Hugo",
    "Ines",
    "Mateo",
    "Lucia",
    "Pablo",
    "Sofia",
    "Daan",
    "Julia",
    "Sem",
    "Fleur",
    "Aoife",
    "Cian",
]
_LAST = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
    "Wilson",
    "Anderson",
    "Taylor",
    "Thomas",
    "Moore",
    "Jackson",
    "Martin",
    "Lee",
    "Thompson",
    "White",
    "Harris",
    "Clark",
    "Lewis",
    "Walker",
    "Hall",
    "Muller",
    "Schmidt",
    "Schneider",
    "Fischer",
    "Weber",
    "Meyer",
    "Wagner",
    "Bernard",
    "Dubois",
    "Moreau",
    "Laurent",
    "Simon",
    "Garcia",
    "Fernandez",
    "Lopez",
    "Sanchez",
    "Perez",
    "Jansen",
    "de-Vries",
    "Bakker",
    "Visser",
    "Murphy",
    "Kelly",
    "O'Brien",
    "Byrne",
    "Ryan",
    "Walsh",
]
_DOMAINS = (
    ("gmail.com", 44),
    ("outlook.com", 9),
    ("hotmail.com", 8),
    ("yahoo.com", 7),
    ("icloud.com", 6),
    ("gmx.de", 4),
    ("web.de", 3),
    ("orange.fr", 3),
    ("proton.me", 2),
    ("live.co.uk", 2),
    ("btinternet.com", 2),
    ("ziggo.nl", 1),
    ("company.com", 1),
    ("aol.com", 1),
)
_COUNTRIES = (
    ("US", 37),
    ("GB", 17),
    ("DE", 12),
    ("FR", 9),
    ("ES", 6),
    ("NL", 5),
    ("CA", 4),
    ("AU", 3),
    ("IE", 3),
    ("IT", 2),
    ("BE", 1),
    ("AT", 1),
)
_EUR = {"DE", "FR", "ES", "NL", "IE", "IT", "BE", "AT"}
_CURRENCY = {"GB": ("GBP", 0.79), "CA": ("CAD", 1.36), "AU": ("AUD", 1.52)}
_METHODS = (("card", 62), ("paypal", 21), ("apple_pay", 9), ("google_pay", 5), ("bank", 3))
#: share of a day's payments per hour, 00..23 — quiet nights, a lunch bump, an evening peak
_HOURS = (1, 1, 1, 1, 1, 1, 2, 3, 5, 6, 6, 7, 8, 7, 6, 6, 6, 7, 9, 10, 10, 9, 6, 3)
_WEEKDAY = (1.0, 1.05, 1.02, 1.0, 1.1, 0.86, 0.8)  # Mon..Sun


def _fq(name: str) -> str:
    return f"{CATALOG}.{SCHEMA}.{name}"


def table_names() -> list[str]:
    """Every seeded table, fully qualified — LAST_ALTERED is the inventory."""
    return [_fq(t) for t in LAST_ALTERED]


def _weighted(rng: random.Random, table: tuple[tuple[str, int], ...]) -> str:
    return rng.choices([t[0] for t in table], weights=[t[1] for t in table])[0]


def _poisson(rng: random.Random, lam: float) -> int:
    if lam > 400:  # normal approximation keeps Knuth's loop short
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    threshold, k, p = math.exp(-lam), 0, 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= threshold:
            return k - 1


def _days(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def _price(rng: random.Random) -> float:
    """A catalogue price: log-normal, with retail endings."""
    raw = math.exp(rng.gauss(3.3, 0.75))  # median ~27, long right tail
    if raw < 10:
        return round(math.floor(raw) + rng.choice((0.49, 0.99, 0.99, 0.0)), 2)
    if raw < 100:
        return round(math.floor(raw) + rng.choice((0.99, 0.99, 0.95, 0.5, 0.0)), 2)
    return round(math.floor(raw / 5) * 5 + rng.choice((0.0, 0.0, 4.99, 4.0)), 2)


# -- tables -------------------------------------------------------------------


def seed_sandbox(db_path: Path) -> dict:
    """(Re)create the sandbox warehouse. Returns the exact ground truth."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    try:
        return seed_connection(con)
    finally:
        con.close()


def seed_connection(con: sqlite3.Connection) -> dict:
    """Seed an open connection (a file, or :memory: to recompute the truth
    without touching any warehouse — how `sandbox score` gets its key)."""
    rng = random.Random(42)
    customers = _seed_customers(con, rng)
    orders, promos = _seed_orders(con, rng, customers["_rows"])
    enriched = _seed_enriched(con, orders["_rows"], promos)
    daily = _seed_daily(con)
    payments = _seed_payments(con, rng, orders["_rows"], customers["_rows"])
    _write_meta(con)
    con.commit()
    for section in (customers, orders):
        section.pop("_rows")
    return {
        "customers": customers,
        "orders": orders,
        "orders_enriched": enriched,
        "orders_daily": daily,
        "payments": payments,
    }


def _seed_customers(con: sqlite3.Connection, rng: random.Random) -> dict:
    con.execute(
        f'CREATE TABLE "{_fq("CUSTOMERS")}" ('
        "CUSTOMER_ID INTEGER, EMAIL TEXT, FULL_NAME TEXT, SIGNUP_DATE TEXT, "
        "SIGNUP_CHANNEL TEXT, COUNTRY TEXT, BIRTH_DATE TEXT)"
    )
    # signups per day: slow growth, weekday shape, the iOS launch and Black
    # Friday as spikes — ids are then assigned in signup order, as a real
    # sequence would be
    days = list(_days(SIGNUP_START, END))
    span = len(days)
    signups: list[date] = []
    for i, d in enumerate(days):
        lam = 2.2 + 4.3 * i / span
        lam *= _WEEKDAY[d.weekday()]
        if d == IOS_LAUNCH or d == IOS_LAUNCH + timedelta(days=1):
            lam *= 3.2
        if d == BLACK_FRIDAY:
            lam *= 2.6
        if d.month == 12 and d.day >= 24:
            lam *= 0.6
        signups.extend([d] * _poisson(rng, lam))
    seen_emails: set[str] = set()
    rows = []
    null_email_count = 0
    for cid, signup in enumerate(signups, start=1):
        first, last = rng.choice(_FIRST), rng.choice(_LAST)
        email = _email(rng, first, last, seen_emails)
        channels = (("web", 52), ("ios", 24), ("android", 16), ("partner", 8))
        if signup < IOS_LAUNCH:
            channels = (("web", 64), ("android", 24), ("partner", 12))
        channel = _weighted(rng, channels)
        # Planted: the iOS signup form's release on the cutoff date stopped
        # sending the email field. Nothing before the cutoff is NULL.
        if (
            signup >= _NULL_EMAIL_CUTOFF
            and channel == _NULL_EMAIL_CHANNEL
            and rng.random() < _NULL_EMAIL_RATE
        ):
            email = None
            null_email_count += 1
        country = _weighted(rng, _COUNTRIES)
        birth = _birthdate(rng, signup)
        if cid in _FUTURE_BIRTHDATE_IDS:
            birth = f"2031-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"  # planted
        rows.append((cid, email, f"{first} {last}", signup.isoformat(), channel, country, birth))
    # Planted: a re-import produced second rows for three ids — same person,
    # a different email on file.
    by_id = {r[0]: r for r in rows}
    for cid in _DUP_CUSTOMER_IDS:
        orig = by_id[cid]
        first, last = orig[2].split(" ", 1)
        rows.append(
            (
                cid,
                _email(rng, first, last, seen_emails),
                orig[2],
                orig[3],
                orig[4],
                orig[5],
                orig[6],
            )
        )
    con.executemany(f'INSERT INTO "{_fq("CUSTOMERS")}" VALUES (?, ?, ?, ?, ?, ?, ?)', rows)
    return {
        "table": _fq("CUSTOMERS"),
        "total_rows": len(rows),
        "distinct_customers": len(signups),
        "null_email_count": null_email_count,
        "null_email_cutoff": _NULL_EMAIL_CUTOFF.isoformat(),
        "null_email_channel": _NULL_EMAIL_CHANNEL,
        "duplicate_customer_ids": _DUP_CUSTOMER_IDS,
        "future_birthdate_ids": _FUTURE_BIRTHDATE_IDS,
        "null_birthdate_share": round(
            sum(1 for r in rows if r[6] is None) / len(rows), 3
        ),  # a decoy: BIRTH_DATE is optional
        "_rows": rows[: len(signups)],
    }


def _email(rng: random.Random, first: str, last: str, seen: set[str]) -> str:
    f, surname = first.lower(), last.lower().replace("'", "").replace("-", "")
    style = rng.random()
    if style < 0.42:
        local = f"{f}.{surname}"
    elif style < 0.64:
        local = f"{f[0]}{surname}"
    elif style < 0.80:
        local = f"{f}{surname}{rng.randint(1, 99)}"
    elif style < 0.92:
        local = f"{f}_{surname}{rng.randint(70, 99)}"
    else:
        local = f"{surname}.{f}"
    email = f"{local}@{_weighted(rng, _DOMAINS)}"
    while email in seen:
        email = f"{local}{rng.randint(100, 999)}@{_weighted(rng, _DOMAINS)}"
    seen.add(email)
    return email


def _birthdate(rng: random.Random, signup: date) -> str | None:
    if rng.random() < 0.06:
        return None  # optional field
    age = rng.triangular(18, 78, 31)
    year = signup.year - int(age)
    month = rng.randint(1, 12)
    day = rng.randint(1, [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return f"{year}-{month:02d}-{day:02d}"


#: code, description, discount, validity window — SUMMER25 and FLASH5 were
#: re-issued for a second window with a different discount: two rows each,
#: the same CODE. The join that builds ORDERS_ENRICHED ignores the window.
_PROMOS = (
    ("WELCOME10", "New customer welcome", 10.0, "2024-01-01", None),
    ("SUMMER25", "Summer sale", 25.0, "2025-06-01", "2025-08-31"),
    ("VIP15", "VIP tier discount", 15.0, "2024-01-01", None),
    ("FLASH5", "Flash weekend", 5.0, "2025-11-28", "2025-12-01"),
    ("LOYAL20", "Loyalty reward", 20.0, "2024-01-01", None),
    ("SUMMER25", "Summer sale (extended)", 20.0, "2026-06-01", "2026-08-31"),
    ("FLASH5", "Flash weekend (relaunch)", 7.5, "2026-05-15", "2026-05-18"),
)


def _valid_codes(d: date) -> list[str]:
    out = []
    for code, _desc, _pct, lo, hi in _PROMOS:
        if lo <= d.isoformat() and (hi is None or d.isoformat() <= hi):
            out.append(code)
    return out


def _seed_orders(con: sqlite3.Connection, rng: random.Random, customers: list[tuple]) -> tuple:
    con.execute(
        f'CREATE TABLE "{_fq("PROMOS")}" ('
        "CODE TEXT, DESCRIPTION TEXT, DISCOUNT_PCT REAL, VALID_FROM TEXT, VALID_TO TEXT)"
    )
    con.executemany(f'INSERT INTO "{_fq("PROMOS")}" VALUES (?, ?, ?, ?, ?)', _PROMOS)
    con.execute(
        f'CREATE TABLE "{_fq("ORDERS")}" ('
        "ORDER_ID INTEGER, CUSTOMER_ID INTEGER, ORDER_DATE TEXT, CHANNEL TEXT, "
        "ITEM_COUNT INTEGER, AMOUNT REAL, PROMO_CODE TEXT, STATUS TEXT)"
    )
    # customers become eligible on their signup date (ids grow with signup,
    # so the eligible set is a prefix); activity is heavy-tailed, so a few
    # customers order often and most order once or twice
    signup_by_id = [date.fromisoformat(r[3]) for r in customers]
    weights = [rng.lognormvariate(0.0, 1.1) for _ in customers]
    cum: list[float] = []
    total = 0.0
    for w in weights:
        total += w
        cum.append(total)
    ids = [r[0] for r in customers]
    channel_of = {r[0]: r[4] for r in customers}

    days = list(_days(START, END))
    span = len(days)
    orders = []
    first_order_seen: set[int] = set()
    cents_affected = 0
    welcome_violations = 0
    welcome_orders = 0
    oid = 0
    for i, d in enumerate(days):
        lam = 38 + 40 * i / span
        lam *= _WEEKDAY[d.weekday()]
        if d.month == 11 and d.day >= 10:
            lam *= 1.15
        if d.month == 12 and d.day <= 20:
            lam *= 1.3
        if d.month == 12 and d.day >= 24:
            lam *= 0.5
        if d.month == 1:
            lam *= 0.85
        if d == BLACK_FRIDAY:
            lam *= 3.8
        if d == CYBER_MONDAY:
            lam *= 2.2
        n = _poisson(rng, lam)
        k = bisect_right(signup_by_id, d)
        if k == 0 or n == 0:
            continue
        buyers = rng.choices(ids[:k], cum_weights=cum[:k], k=n)
        valid = _valid_codes(d)
        for cid in buyers:
            oid += 1
            items = min(6, 1 + int(rng.expovariate(1.1)))
            amount = round(sum(_price(rng) for _ in range(items)), 2)
            channel = _weighted(
                rng,
                (("web", 55), ("ios", 27), ("android", 18))
                if d >= IOS_LAUNCH
                else (("web", 70), ("android", 30)),
            )
            promo = None
            is_first = cid not in first_order_seen
            first_order_seen.add(cid)
            if rng.random() < 0.22:
                choices = [c for c in valid if c != "WELCOME10"]
                if is_first and rng.random() < 0.6:
                    promo = "WELCOME10"
                elif choices:
                    promo = rng.choice(choices)
            # Planted (semantic rule): WELCOME10 is first-order-only, but the
            # partner import path never runs that check
            if (
                promo is None
                and not is_first
                and channel_of[cid] == _WELCOME_LEAK_CHANNEL
                and rng.random() < _WELCOME_LEAK_RATE
            ):
                promo = "WELCOME10"
                welcome_violations += 1
            if promo == "WELCOME10":
                welcome_orders += 1
            age_days = (END - d).days
            if age_days <= 2:
                status = rng.choices(("pending", "completed"), (60, 40))[0]
            else:
                status = rng.choices(("completed", "refunded", "cancelled"), (90, 4.5, 5.5))[0]
            true_amount = amount
            # Planted (bug-hunter, tier 2): an Android release sent totals in
            # minor units for two weeks; the payment provider charged the
            # right amount, so PAYMENTS disagrees with ORDERS for these rows
            if channel == _CENTS_CHANNEL and _CENTS_WINDOW[0] <= d <= _CENTS_WINDOW[1]:
                amount = round(amount * 100, 2)
                cents_affected += 1
            orders.append(
                (oid, cid, d.isoformat(), channel, items, amount, promo, status, true_amount)
            )
    con.executemany(
        f'INSERT INTO "{_fq("ORDERS")}" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        [o[:8] for o in orders],
    )
    truth = {
        "table": _fq("ORDERS"),
        "total_rows": len(orders),
        "cents_channel": _CENTS_CHANNEL,
        "cents_window": [_CENTS_WINDOW[0].isoformat(), _CENTS_WINDOW[1].isoformat()],
        "cents_affected_orders": cents_affected,
        "cents_root_cause": (
            f"the {_CENTS_CHANNEL} app release of {_CENTS_WINDOW[0]} sent order totals in "
            "minor units (cents); payments carry the correct amount"
        ),
        "welcome_orders": welcome_orders,
        "welcome_violations": welcome_violations,
        "welcome_leak_channel": _WELCOME_LEAK_CHANNEL,
        "welcome_root_cause": (
            "WELCOME10 is first-order-only; orders imported through the partner channel "
            "skip that check"
        ),
        "black_friday": BLACK_FRIDAY.isoformat(),
        "_rows": orders,
    }
    return truth, _PROMOS


def _seed_enriched(con: sqlite3.Connection, orders: list[tuple], promos: tuple) -> dict:
    # ORDERS_ENRICHED is the *result* of the buggy join: it matches on CODE
    # alone, so an order using a re-issued code matches both rows.
    con.execute(
        f'CREATE TABLE "{_fq("ORDERS_ENRICHED")}" AS '
        f"SELECT o.ORDER_ID, o.CUSTOMER_ID, o.ORDER_DATE, o.CHANNEL, o.ITEM_COUNT, o.AMOUNT, "
        f"o.PROMO_CODE, o.STATUS, p.DESCRIPTION AS PROMO_DESCRIPTION, p.DISCOUNT_PCT "
        f'FROM "{_fq("ORDERS")}" o LEFT JOIN "{_fq("PROMOS")}" p ON o.PROMO_CODE = p.CODE'
    )
    enriched = con.execute(f'SELECT COUNT(*) FROM "{_fq("ORDERS_ENRICHED")}"').fetchone()[0]
    affected = sum(1 for o in orders if o[6] in _DUP_PROMO_CODES)
    return {
        "table": _fq("ORDERS_ENRICHED"),
        "orders_rows": len(orders),
        "enriched_rows": enriched,
        "extra_rows": enriched - len(orders),
        "duplicated_promo_codes": _DUP_PROMO_CODES,
        "affected_orders": affected,
        "root_cause": (
            "re-issued promo codes make CODE non-unique in PROMOS; the join matches on CODE "
            "alone instead of CODE plus the validity window, so those orders fan out"
        ),
    }


def _seed_daily(con: sqlite3.Connection) -> dict:
    # ORDERS_DAILY is a rollup of ORDERS_ENRICHED, completed orders only. The
    # monthly batch that builds it bounds each month with an exclusive upper
    # date, so the last day of every month is never rolled up — and, being
    # downstream of the fan-out, it inherits the inflated counts and revenue.
    con.execute(
        f'CREATE TABLE "{_fq("ORDERS_DAILY")}" AS '
        "SELECT ORDER_DATE, CHANNEL, COUNT(*) AS ORDER_COUNT, ROUND(SUM(AMOUNT), 2) AS REVENUE "
        f"FROM \"{_fq('ORDERS_ENRICHED')}\" WHERE STATUS = 'completed' "
        "AND strftime('%d', date(ORDER_DATE, '+1 day')) != '01' "
        "GROUP BY ORDER_DATE, CHANNEL"
    )
    missing_days, missing_orders = con.execute(
        "SELECT COUNT(DISTINCT ORDER_DATE), COUNT(DISTINCT ORDER_ID) "
        f"FROM \"{_fq('ORDERS_ENRICHED')}\" WHERE STATUS = 'completed' "
        "AND strftime('%d', date(ORDER_DATE, '+1 day')) = '01'"
    ).fetchone()
    rows = con.execute(f'SELECT COUNT(*) FROM "{_fq("ORDERS_DAILY")}"').fetchone()[0]
    return {
        "table": _fq("ORDERS_DAILY"),
        "source": _fq("ORDERS_ENRICHED"),
        "rows": rows,
        "missing_days": missing_days,
        "missing_orders": missing_orders,
        "root_cause": (
            "the monthly batch bounds each month with ORDER_DATE < last_day instead of <=, "
            "so the last day of every month is never rolled up"
        ),
        "inherits": "fan-out inflation from ORDERS_ENRICHED (upstream, not the rollup's defect)",
    }


def _seed_payments(
    con: sqlite3.Connection, rng: random.Random, orders: list[tuple], customers: list[tuple]
) -> dict:
    con.execute(
        f'CREATE TABLE "{_fq("PAYMENTS")}" ('
        "PAYMENT_ID INTEGER, ORDER_ID INTEGER, AMOUNT REAL, CURRENCY TEXT, STATUS TEXT, "
        "PAID_AT TEXT, METHOD TEXT)"
    )
    country_of = {r[0]: r[5] for r in customers}
    rows = []
    refunded = 0
    pid = 0
    for o in orders:
        oid, cid, day, _channel, _items, _amount, _promo, status, true_amount = o
        if status not in ("completed", "refunded"):
            continue  # pending and cancelled orders were never charged
        pid += 1
        country = country_of[cid]
        if country in _EUR:
            currency, rate = "EUR", 0.92 + rng.uniform(-0.012, 0.012)
        elif country in _CURRENCY:
            currency, base = _CURRENCY[country]
            rate = base * (1 + rng.uniform(-0.015, 0.015))
        else:
            currency, rate = "USD", 1.0
        amount = round(true_amount * rate, 2)
        hour = rng.choices(range(24), weights=_HOURS)[0]
        paid = datetime.fromisoformat(day) + timedelta(
            hours=hour, minutes=rng.randint(0, 59), seconds=rng.randint(0, 59)
        )
        if status == "refunded":
            refunded += 1
        rows.append(
            (
                pid,
                oid,
                amount,
                currency,
                "settled" if status == "completed" else "refunded",
                paid.strftime("%Y-%m-%d %H:%M:%S"),
                _weighted(rng, _METHODS),
            )
        )
    con.executemany(f'INSERT INTO "{_fq("PAYMENTS")}" VALUES (?, ?, ?, ?, ?, ?, ?)', rows)

    # PAYMENTS_V2 is the migrated copy with three planted defects: the
    # backfill filter dropped refunded rows, EUR amounts were truncated to
    # whole units by a bad cast, and PAID_AT was typed as DATE.
    con.execute(
        f'CREATE TABLE "{_fq("PAYMENTS_V2")}" AS '
        "SELECT PAYMENT_ID, ORDER_ID, "
        "CASE WHEN CURRENCY = 'EUR' THEN CAST(AMOUNT AS INTEGER) ELSE AMOUNT END AS AMOUNT, "
        "CURRENCY, STATUS, date(PAID_AT) AS PAID_AT, METHOD "
        f"FROM \"{_fq('PAYMENTS')}\" WHERE STATUS = 'settled'"
    )
    eur_settled = con.execute(
        f"SELECT COUNT(*) FROM \"{_fq('PAYMENTS')}\" p WHERE CURRENCY = 'EUR' "
        f"AND STATUS = 'settled' AND AMOUNT != CAST(AMOUNT AS INTEGER)"
    ).fetchone()[0]
    return {
        "old_table": _fq("PAYMENTS"),
        "new_table": _fq("PAYMENTS_V2"),
        "old_rows": len(rows),
        "new_rows": len(rows) - refunded,
        "missing_refunded_rows": refunded,
        "eur_amount_mismatches": eur_settled,
        "paid_at_truncated_rows": len(rows) - refunded,
        "root_cause": (
            "migration filter drops refunded rows; EUR amounts truncated to integers; "
            "PAID_AT typed as DATE loses the time of day"
        ),
    }


def _write_meta(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE _grayson_meta(fqn TEXT PRIMARY KEY, row_count INTEGER, last_altered TEXT)"
    )
    tables = con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ?",
        (f"{CATALOG}.%",),
    ).fetchall()
    for (name,) in tables:
        count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        short = name.rsplit(".", 1)[-1]
        con.execute(
            "INSERT INTO _grayson_meta(fqn, row_count, last_altered) VALUES (?, ?, ?)",
            (name, count, LAST_ALTERED.get(short, LAST_ALTERED["ORDERS"])),
        )


# -- answer key -----------------------------------------------------------


def render_answer_key(truth: dict) -> str:
    c = truth["customers"]
    o = truth["orders"]
    e = truth["orders_enriched"]
    d = truth["orders_daily"]
    p = truth["payments"]
    return f"""\
# Sandbox answer key — DO NOT show this to the agent

Planted problems and exact ground truth for scoring agent runs. Each maps to a
built-in workflow. A run "succeeds" when its findings identify the problem, explain
it (root cause, or the characterisation stated here), and give numbers consistent
with the values below. `grayson sandbox score <sid>` applies exactly that rubric.

The data has the texture of a real warehouse on purpose: heavy-tailed order values
and customer activity, weekly and seasonal volume, ids that grow with signup date,
promo codes with validity windows, payments that reconcile to orders. Some of that
structure is a **decoy** (section 6): a careful run investigates it and rules it
out; a careless run reports it as a defect.

## 1. table-health — {c["table"]}  (tier 1)

Start with: `grayson session start --workflow table-health --table {c["table"]}`

- **Email NULL regression**: {c["null_email_count"]} rows have NULL EMAIL. Every one
  signed up on or after {c["null_email_cutoff"]} **through the `{c["null_email_channel"]}`
  channel** (0 NULLs before that date; 0 NULLs on other channels). A strong run
  finds both the date and the channel — the iOS signup form's release.
- **Duplicate customer ids**: ids {c["duplicate_customer_ids"]} each appear twice with
  different emails ({c["total_rows"]} rows vs {c["distinct_customers"]} distinct ids) — a
  re-import.
- **Impossible birthdates**: customer ids {c["future_birthdate_ids"]} have birthdates in 2031.

## 2. bug-hunter — {e["table"]}  (tier 1 and tier 2)

Start with: `grayson session start --workflow bug-hunter --table {e["table"]}`
(suggested anomaly description: "revenue and order counts look inflated")

Two independent causes inflate this table. A strong run separates them.

- **Join fan-out** (tier 1): ORDERS has {e["orders_rows"]} rows; ORDERS_ENRICHED has
  {e["enriched_rows"]} ({e["extra_rows"]} extra rows = duplicated ORDER_IDs).
  Root cause: {e["root_cause"]} — codes {e["duplicated_promo_codes"]} each exist twice
  in PROMOS with different VALID_FROM/VALID_TO, so the {e["affected_orders"]} orders using
  them appear twice. The fix is a join on CODE plus the validity window.
- **Amounts in minor units** (tier 2): {o["cents_affected_orders"]} `{o["cents_channel"]}`
  orders between {o["cents_window"][0]} and {o["cents_window"][1]} carry AMOUNT × 100.
  Root cause: {o["cents_root_cause"]}. Joining to PAYMENTS shows the correct amounts;
  the outliers are exactly 100× and confined to one channel and one window.

## 3. semantic-rule-qa — {o["table"]}  (tier 2)

Start with: `grayson session start --workflow semantic-rule-qa --table {o["table"]} \\
  --table {c["table"]}` (rule: "WELCOME10 applies only to a customer's first order")

- {o["welcome_orders"]} orders use WELCOME10; **{o["welcome_violations"]}** of them are not the
  customer's first order. Every violation belongs to a customer whose
  SIGNUP_CHANNEL is `{o["welcome_leak_channel"]}`. Root cause: {o["welcome_root_cause"]}.

## 4. pipeline-qa — {d["table"]}  (tier 2)

Start with: `grayson session start --workflow pipeline-qa --table {d["table"]} \\
  --table {d["source"]}`

- **Month-end days missing**: the rollup has no rows for the last day of any month —
  {d["missing_days"]} days, covering {d["missing_orders"]} completed orders. Root cause:
  {d["root_cause"]}.
- **Inherited inflation**: the rollup's counts and revenue carry the fan-out from
  ORDERS_ENRICHED (section 2). A strong run attributes that upstream rather than
  to the rollup; a run that blames the rollup for it is wrong.

## 5. migration-parity — {p["old_table"]} vs {p["new_table"]}  (tier 1 and tier 2)

Start with: `grayson session start --workflow migration-parity \\
  --table {p["old_table"]} --table {p["new_table"]}`

- **Rows dropped** (tier 1): old {p["old_rows"]}, new {p["new_rows"]} — the
  {p["missing_refunded_rows"]} missing rows are exactly the STATUS='refunded' rows (the
  backfill filter kept only settled).
- **Value drift** (tier 1): {p["eur_amount_mismatches"]} EUR rows have truncated amounts in V2
  (cast to whole units); USD, GBP, CAD, and AUD rows match exactly.
- **Precision loss** (tier 2): PAID_AT in V2 is a DATE — every one of its
  {p["paid_at_truncated_rows"]} rows lost the time of day. A schema comparison finds it;
  a values-only comparison of AMOUNT does not.

## 6. Not defects (decoys)

Real structure that a careful run checks and rules out. Reporting any of these
as a defect (at any severity above `info`) counts against a run.

- Order volume spikes on {o["black_friday"]} (Black Friday, ~4×) and the following
  Monday; December dips after the 24th; weekends are ~20% quieter.
- Order AMOUNT is heavy-tailed: a handful of orders run to several hundred. Only
  the ×100 window in section 2 is a defect.
- `pending` orders cluster in the last three days of the window — they are recent,
  not stuck. `cancelled` orders have no payment row, by design.
- SUMMER25 and FLASH5 usage is confined to their validity windows; WELCOME10 usage
  is (mostly) confined to first orders — the exceptions are section 3.
- BIRTH_DATE is optional: ~{c["null_birthdate_share"] * 100:.0f}% NULL across all
  channels and dates. Only the 2031 values are a defect.
- The `ios` channel has no signups or orders before 2025-10-06 (the app launched
  then), and PAYMENTS carries five currencies at slightly varying daily rates.
- Customer activity is heavy-tailed: a few customers have dozens of orders.

## Scoring a run

`grayson sandbox score <sid>` applies the rubric per planted problem on the
session's targets — identified (1 pt), explained: root cause or characterisation
as stated above (1 pt), quantified within ±2% (1 pt) — deterministically, says what
each missed point was looking for, and lists findings that match a decoy. `grayson
sandbox score --all` lines every session up side by side with its cost (queries,
budget, interventions), which is how two harnesses, models, or protocol files are
compared on the same problems. It is a user command: its output is this key. The
evidence trail (`grayson query log <sid>`) shows how a run got where it got.
"""
