# Deployment recipes

grayson is local-first: the default install needs no server, no daemon, and no
credentials handed to grayson (Snowflake auth stays entirely inside the `snow`
CLI; git auth stays entirely inside git). These recipes cover the three ways
to run it beyond a laptop. The trust model behind each is in
[SECURITY.md](SECURITY.md#bypass-and-containment-where-the-guards-authority-ends).

## Modes at a glance

Two independent choices: the **surface** (full harness vs knowledge-only) and
the **transport** (local stdio vs served HTTP). All four combinations work.

| Mode | Command | Runs | Snowflake credentials | Git auth | Best for |
|---|---|---|---|---|---|
| Full, local (default) | `grayson mcp serve` — the CLI is the same surface in shell form | On the analyst's machine, spawned by the harness | The analyst's own `snow` connection | The analyst's own, when a library is linked | Running investigations end to end |
| Knowledge-only, local | `grayson mcp serve --knowledge-only --library <url-or-path>` | On a collaborator's machine, spawned by their harness | None | The collaborator's own, read access to the library repo | Briefing an agent from the team library without running the harness |
| Knowledge-only, served (appliance) | As above plus `--http`, or the `docker/` image | On an internal VM or container | None | One read-only deploy credential, held by the server | A whole team's agents; each client needs only a URL and token |
| Full, served (single identity) | `grayson mcp serve --http` | Under a service account holding the credentials | A key-pair connection (read-only role recommended) | A service credential, if a library is linked | Environments where warehouse credentials must not exist beside agents |

Auth beyond the bearer token (network placement, TLS termination, identity
proxies) is deliberately left to the hosting environment — the served modes
are plain HTTP services and compose with whatever the platform provides.

## 1. Solo / same-machine (the default)

Nothing to deploy. The CLI and the stdio MCP server are the same trust domain
as the agent — the guard, a read-only Snowflake role, and (optionally)
`grayson harness guard apply` are the rails.

```bash
uv tool install git+https://github.com/Kcarreras/grayson
cd your-data-repo && grayson init . && grayson doctor
grayson user set <your-id>
grayson harness init claude-code        # offers guard permissions interactively
```

## 2. Knowledge appliance (read-only, containerized, for the whole team)

Serves the team library — knowledge, workflows, views, check results, and
published records — read-only over HTTP. No warehouse credentials exist in
this container at all; the only secret is a **read-only deploy key** for the
qa-library repo and the bearer token clients present.

```bash
docker build -f docker/Dockerfile -t grayson .
docker run -d --name grayson-knowledge \
  -e GRAYSON_LIBRARY_URL=git@github.com:your-org/qa-library.git \
  -e GRAYSON_MCP_TOKEN=<long-random-token> \
  -v /path/to/deploy_key:/home/grayson/.ssh/id_ed25519:ro \
  -p 8850:8850 grayson
```

Or with compose: `docker compose -f docker/compose.yaml up -d` (edit the env
values first). Each teammate points their agent at it:

```bash
claude mcp add --transport http grayson-knowledge \
  http://your-host:8850/mcp --header "Authorization: Bearer <token>"
```

Notes:
- The container clones the library at startup and fast-forwards on each
  restart; if git auth breaks later it keeps serving the existing clone and
  `library_info` reports the staleness.
- The token gates the tool surface; there is nothing warehouse-shaped behind
  it to escalate to. For access beyond a trusted network, front the port with
  TLS (any reverse proxy) — the server itself speaks plain HTTP.
- Provision the deploy key read-only. The appliance never needs push.

## 3. Credential-isolated full server (single identity)

The full tool surface — sessions, guarded queries, checkpoints, findings —
served from wherever the Snowflake credentials live, so the agent's
environment holds none. **One token, one `grayson user` identity**: right for
one analyst's isolated setup or an agent fleet operating as a service
identity, wrong for multiple analysts sharing it (their work would be
indistinguishable).

On the credentialed side (a service account on the same machine, or a
separate host):

```bash
# as the service user, one-time:
uv tool install git+https://github.com/Kcarreras/grayson
snow connection add ...            # key-pair auth; SSO is interactive and
                                   # does not suit a headless service
cd /srv/grayson-workspace && grayson init . && grayson doctor
grayson user set qa-bot            # the identity all callers appear as
grayson library link git@github.com:your-org/qa-library.git --auto-push
GRAYSON_MCP_TOKEN=<token> grayson mcp serve --http --host 127.0.0.1
```

A systemd user service keeps it running:

```ini
# ~/.config/systemd/user/grayson-mcp.service
[Unit]
Description=grayson MCP server
[Service]
Environment=GRAYSON_MCP_TOKEN=<token>
WorkingDirectory=/srv/grayson-workspace
ExecStart=%h/.local/bin/grayson mcp serve --http --host 127.0.0.1
Restart=on-failure
[Install]
WantedBy=default.target
```

The agent (different OS user, container, or machine — anywhere without the
snow config) connects to the URL with the token. Same-machine setups need
nothing else; cross-machine, tunnel or TLS-proxy the port.

Strongly recommended regardless of recipe: a **dedicated read-only Snowflake
role** on whatever connection agents can reach — it is the only control that
holds even if everything else is bypassed.

## Recipe chooser

| Situation | Recipe |
|---|---|
| One analyst, one machine | 1 — solo |
| Teammates' agents need the team's knowledge/records, not the harness | 2 — knowledge appliance |
| Policy: warehouse credentials must not exist where agents run | 3 — isolated server |
| Multiple analysts sharing one live server | Not supported yet — records/knowledge already compound through the library instead |
