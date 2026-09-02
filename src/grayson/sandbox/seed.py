"""Sandbox warehouse seeding: mock retail data with planted, verifiable problems.

Generation is deterministic (fixed RNG seed) and returns the exact ground
truth, which `grayson sandbox init` renders into an answer key for the human.
The planted problems map one-to-one onto built-in workflows:

- table-health   → SANDBOX.SHOP.CUSTOMERS: an email-NULL regression after a
                   cutoff signup date, duplicated customer ids, and
                   future-dated birthdates.
- bug-hunter     → SANDBOX.SHOP.ORDERS_ENRICHED: join fan-out duplicates
                   caused by non-unique promo codes in SANDBOX.SHOP.PROMOS.
- migration-parity → SANDBOX.SHOP.PAYMENTS vs PAYMENTS_V2: the migration
                   silently drops refunded rows and truncates EUR amounts.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

CATALOG = "SANDBOX"
SCHEMA = "SHOP"
LAST_ALTERED = "2026-08-18 06:12:00"

_NULL_EMAIL_CUTOFF = "2026-07-15"
_DUP_CUSTOMER_IDS = [101, 202, 303]
_FUTURE_BIRTHDATE_IDS = [77, 411, 902]
_DUP_PROMO_CODES = ["SUMMER25", "FLASH5"]


def _fq(name: str) -> str:
    return f"{CATALOG}.{SCHEMA}.{name}"


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
    truth = {
        "customers": _seed_customers(con, rng),
        "orders_enriched": _seed_orders(con, rng),
        "payments": _seed_payments(con, rng),
    }
    _write_meta(con)
    con.commit()
    return truth


# -- tables ---------------------------------------------------------------


def _seed_customers(con: sqlite3.Connection, rng: random.Random) -> dict:
    con.execute(
        f'CREATE TABLE "{_fq("CUSTOMERS")}" ('
        "CUSTOMER_ID INTEGER, EMAIL TEXT, SIGNUP_DATE TEXT, COUNTRY TEXT, BIRTH_DATE TEXT)"
    )
    countries = ["US", "GB", "DE", "FR", "ES", "NL"]
    rows = []
    null_email_count = 0
    for cid in range(1, 1001):
        month = rng.randint(1, 8)
        day = rng.randint(1, 28)
        signup = f"2026-{month:02d}-{day:02d}"
        email: str | None = f"user{cid}@example.com"
        # Planted: the signup-form regression — a slice of post-cutoff signups
        # lost their email. Pre-cutoff rows are never NULL.
        if signup >= _NULL_EMAIL_CUTOFF and rng.random() < 0.55:
            email = None
            null_email_count += 1
        birth_year = rng.randint(1955, 2007)
        birth = f"{birth_year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        if cid in _FUTURE_BIRTHDATE_IDS:
            birth = f"2031-0{rng.randint(1, 9)}-15"  # planted: impossible birthdate
        rows.append((cid, email, signup, rng.choice(countries), birth))
    # Planted: duplicated primary keys with conflicting emails.
    for cid in _DUP_CUSTOMER_IDS:
        rows.append((cid, f"dup{cid}@example.net", "2026-06-01", "US", "1990-01-01"))
    con.executemany(f'INSERT INTO "{_fq("CUSTOMERS")}" VALUES (?, ?, ?, ?, ?)', rows)
    return {
        "table": _fq("CUSTOMERS"),
        "total_rows": len(rows),
        "null_email_count": null_email_count,
        "null_email_cutoff": _NULL_EMAIL_CUTOFF,
        "duplicate_customer_ids": _DUP_CUSTOMER_IDS,
        "future_birthdate_ids": _FUTURE_BIRTHDATE_IDS,
    }


def _seed_orders(con: sqlite3.Connection, rng: random.Random) -> dict:
    con.execute(f'CREATE TABLE "{_fq("PROMOS")}" (CODE TEXT, DESCRIPTION TEXT, DISCOUNT_PCT REAL)')
    promos = [
        ("WELCOME10", "New customer welcome", 10.0),
        ("SUMMER25", "Summer sale", 25.0),
        ("VIP15", "VIP tier discount", 15.0),
        ("FLASH5", "Flash weekend", 5.0),
        ("LOYAL20", "Loyalty reward", 20.0),
        # Planted: re-issued codes create non-unique join keys.
        ("SUMMER25", "Summer sale (extended)", 20.0),
        ("FLASH5", "Flash weekend (relaunch)", 7.5),
    ]
    con.executemany(f'INSERT INTO "{_fq("PROMOS")}" VALUES (?, ?, ?)', promos)

    con.execute(
        f'CREATE TABLE "{_fq("ORDERS")}" ('
        "ORDER_ID INTEGER, CUSTOMER_ID INTEGER, ORDER_DATE TEXT, AMOUNT REAL, "
        "PROMO_CODE TEXT, STATUS TEXT)"
    )
    codes = ["WELCOME10", "SUMMER25", "VIP15", "FLASH5", "LOYAL20"]
    orders = []
    affected = 0
    for oid in range(1, 3001):
        promo = rng.choice(codes) if rng.random() < 0.30 else None
        if promo in _DUP_PROMO_CODES:
            affected += 1
        orders.append(
            (
                oid,
                rng.randint(1, 1000),
                f"2026-{rng.randint(5, 8):02d}-{rng.randint(1, 28):02d}",
                round(rng.uniform(5, 500), 2),
                promo,
                rng.choice(["completed"] * 8 + ["pending", "refunded"]),
            )
        )
    con.executemany(f'INSERT INTO "{_fq("ORDERS")}" VALUES (?, ?, ?, ?, ?, ?)', orders)

    # ORDERS_ENRICHED is the *result* of the buggy LEFT JOIN — orders using a
    # re-issued code appear twice, silently inflating row counts and revenue.
    con.execute(
        f'CREATE TABLE "{_fq("ORDERS_ENRICHED")}" AS '
        f"SELECT o.ORDER_ID, o.CUSTOMER_ID, o.ORDER_DATE, o.AMOUNT, o.PROMO_CODE, "
        f"o.STATUS, p.DESCRIPTION AS PROMO_DESCRIPTION, p.DISCOUNT_PCT "
        f'FROM "{_fq("ORDERS")}" o LEFT JOIN "{_fq("PROMOS")}" p ON o.PROMO_CODE = p.CODE'
    )
    enriched = con.execute(f'SELECT COUNT(*) FROM "{_fq("ORDERS_ENRICHED")}"').fetchone()[0]
    return {
        "table": _fq("ORDERS_ENRICHED"),
        "orders_rows": len(orders),
        "enriched_rows": enriched,
        "extra_rows": enriched - len(orders),
        "duplicated_promo_codes": _DUP_PROMO_CODES,
        "affected_orders": affected,
        "root_cause": "non-unique promo codes in PROMOS fan out the LEFT JOIN",
    }


def _seed_payments(con: sqlite3.Connection, rng: random.Random) -> dict:
    con.execute(
        f'CREATE TABLE "{_fq("PAYMENTS")}" ('
        "PAYMENT_ID INTEGER, ORDER_ID INTEGER, AMOUNT REAL, CURRENCY TEXT, STATUS TEXT)"
    )
    rows = []
    refunded = 0
    eur = 0
    for pid in range(1, 2501):
        currency = "EUR" if rng.random() < 0.15 else "USD"
        status = "refunded" if rng.random() < 0.05 else "settled"
        if status == "refunded":
            refunded += 1
        if currency == "EUR":
            eur += 1
        rows.append((pid, rng.randint(1, 3000), round(rng.uniform(5, 500), 2), currency, status))
    con.executemany(f'INSERT INTO "{_fq("PAYMENTS")}" VALUES (?, ?, ?, ?, ?)', rows)

    # PAYMENTS_V2 is the migrated copy with two planted defects: the backfill
    # filter dropped refunded rows entirely, and EUR amounts were truncated to
    # whole units by a bad cast.
    con.execute(
        f'CREATE TABLE "{_fq("PAYMENTS_V2")}" AS '
        f"SELECT PAYMENT_ID, ORDER_ID, "
        f"CASE WHEN CURRENCY = 'EUR' THEN CAST(AMOUNT AS INTEGER) ELSE AMOUNT END AS AMOUNT, "
        f"CURRENCY, STATUS "
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
        "root_cause": "migration filter drops refunded rows; EUR amounts truncated to integers",
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
        con.execute(
            "INSERT INTO _grayson_meta(fqn, row_count, last_altered) VALUES (?, ?, ?)",
            (name, count, LAST_ALTERED),
        )


# -- answer key -----------------------------------------------------------


def render_answer_key(truth: dict) -> str:
    c = truth["customers"]
    e = truth["orders_enriched"]
    p = truth["payments"]
    return f"""\
# Sandbox answer key — DO NOT show this to the agent

Planted problems and exact ground truth for scoring agent runs. Each maps to a
built-in workflow. A run "succeeds" when its findings identify the problem, the
root cause, and numbers consistent with the values below.

## 1. table-health — {c["table"]}

Start with: `grayson session start --workflow table-health --table {c["table"]}`

- **Email NULL regression**: {c["null_email_count"]} rows have NULL EMAIL, and every
  one of them signed up on or after {c["null_email_cutoff"]} (0 NULLs before that date).
  A strong run notices the date correlation, not just the NULL count.
- **Duplicate customer ids**: ids {c["duplicate_customer_ids"]} each appear twice with
  conflicting emails ({c["total_rows"]} rows total vs 1000 distinct-intent customers).
- **Impossible birthdates**: customer ids {c["future_birthdate_ids"]} have birthdates in 2031.

## 2. bug-hunter — {e["table"]}

Start with: `grayson session start --workflow bug-hunter --table {e["table"]}`

- ORDERS has {e["orders_rows"]} rows; ORDERS_ENRICHED has {e["enriched_rows"]}
  ({e["extra_rows"]} extra rows = duplicated ORDER_IDs).
- **Root cause**: {e["root_cause"]} — codes {e["duplicated_promo_codes"]} each exist
  twice in SANDBOX.SHOP.PROMOS, so the {e["affected_orders"]} orders using them fan
  out to two rows each.
- A strong run replicates the duplication, isolates it to those two codes, quantifies
  the blast radius ({e["extra_rows"]} inflated rows), and rules out source duplication
  in ORDERS itself.

## 3. migration-parity — {p["old_table"]} vs {p["new_table"]}

Start with: `grayson session start --workflow migration-parity \\
  --table {p["old_table"]} --table {p["new_table"]}`

- Row counts: old {p["old_rows"]}, new {p["new_rows"]} — the {p["missing_refunded_rows"]}
  missing rows are exactly the STATUS='refunded' rows (the backfill filter dropped them).
- **Value drift**: {p["eur_amount_mismatches"]} EUR rows have truncated amounts in V2
  (cast to whole units); USD rows match exactly.
- A strong run reports both defects separately with those counts.

## Scoring a run

`grayson sandbox score <sid>` applies the rubric per planted problem — identified
(1 pt), explained: root cause or characterisation as stated above (1 pt),
quantified within ±2% (1 pt) — to the session's findings, deterministically, and
says what each missed point was looking for. `grayson sandbox score --all` lines
every session up side by side with its cost (queries, budget, interventions), which
is how two harnesses, models, or protocol files are compared on the same problems.
It is a user command: its output is this key. The evidence trail
(`grayson query log <sid>`) shows how a run got where it got.
"""
