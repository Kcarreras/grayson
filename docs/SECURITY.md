# Security posture & review log

seekql runs agent-authored SQL against a production warehouse under the user's own
Snowflake role. The query guard is designed to be the airtight wall even when no
read-only role is available. This document records the threat model and the adversarial
reviews the code has passed.

## Threat model

- **Adversary**: an agent (or prompt-injected content steering one) that submits SQL
  through seekql, trying to write, run DDL, cause side effects, read outside the declared
  scope, exfiltrate data, or exhaust host resources.
- **Trust boundary**: only the user, via chat, issues authoritative instructions. All SQL
  and all tool-observed content is data, never commands.
- **seekql never handles credentials** — authentication is entirely Snowflake CLI's
  responsibility (SSO/external-browser, key-pair, etc.).

## Layered defenses

1. **Statement guard** (`guard/rules.py`) — parses every statement with sqlglot
   (Snowflake dialect); default-deny outside `SELECT`/`SHOW`/`DESCRIBE`/`EXPLAIN`;
   single-statement only; forbidden-node walk blocks DML/DDL nested anywhere; function
   denylist blocks `SYSTEM$*` and `RESULT_SCAN`; UDTF row sources blocked in strict mode;
   unqualified names blocked in strict mode; per-statement cost caps (auto-LIMIT,
   timeout, budget).
2. **Snowflake role** (defense in depth) — a read-only role is preferred when available;
   the guard is built to hold under the user's normal role regardless.
3. **Local analysis** (`cache/local.py`) — cached-result queries run on a read-only
   SQLite connection (`mode=ro`), single SELECT only, artifact tables only, a function
   denylist, `trusted_schema=OFF`, and a wall-clock interrupt watchdog.
4. **Append-only audit** — every statement (accepted or rejected) is recorded with a
   guard verdict; executor exceptions and cache failures still record a terminal status,
   never a stranded `pending` row.

## Review log

### 2026-08-21 — Phase 1 adversarial review (18-agent workflow, find → verify)

A multi-agent review red-teamed the guard, executor, cache, and session state; each
finding was independently verified against the source. Ten issues were confirmed and all
were fixed in the same phase (regression tests in `tests/test_guard_hardening.py` and
`tests/test_hardening_misc.py`):

| # | Severity | Issue | Fix |
|---|---|---|---|
| 1 | high | `SYSTEM$` side-effect functions callable inside SELECT | Function denylist by `SYSTEM$` prefix |
| 2 | high | Unqualified table name bypassed strict-scope | Strict mode blocks unverifiable names |
| 3 | medium | UDTF/`TABLE(fn())` row source bypassed scope | Blocked in strict mode, warned otherwise |
| 4 | medium | `RESULT_SCAN` read arbitrary prior results | Added to function denylist (both forms) |
| 5 | medium | Local-analysis query had no compute/time cap | Interrupt watchdog + function denylist |
| 6 | medium | Executor/cache raise stranded a `pending` audit row | Wrapped; terminal status always recorded |
| 7 | low | Identifier regex `$` accepted a trailing newline | Anchored with `\Z` |
| 8 | low | Auth-error markers too broad (`sso`) | Tightened to specific phrases/codes |
| 9 | low | `load_extension` not blocked in guard (only SQLite default) | Explicit denylist + `trusted_schema=OFF` |
| 10 | low | Budget cap racy under concurrent workers (TOCTOU) | In-flight `pending` rows count toward budget |

Two candidate findings were verified as **false positives** (the `SEEKQL_SNOW_CMD` env
override is a documented test hook reading only from the environment; `ensure_within`
path containment is sound) and one as a non-issue (guard depends on sqlglot/SQLite
parsing, but the read-only connection and artifact allowlist make divergence
non-exploitable).

## Residual risks (accepted)

- A pre-existing malicious UDF with an external-access integration could exfiltrate
  *in-scope* data when invoked from a SELECT. Creating such a UDF requires privileged
  account setup that the guard already blocks; scope-limiting reduces blast radius. Use a
  read-only role for the strongest guarantee.
- The guard's correctness depends on sqlglot parsing SQL the way Snowflake executes it.
  sqlglot is version-pinned; dialect updates are reviewed before bumping.
