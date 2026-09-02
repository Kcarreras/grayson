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
Behind a gateway that already authenticates every caller (and typically owns
the `Authorization` header itself), pass `--no-token` to disable the built-in
bearer wall — only ever on a port reachable solely through that gateway.

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

The image builds from a digest-pinned Red Hat UBI 9 base with Python
dependencies installed from `uv.lock` — two builds of one commit are the same
appliance. It refuses to start with `GRAYSON_LIBRARY_URL` or
`GRAYSON_MCP_TOKEN` missing, naming the missing setting, rather than guessing.

```bash
docker build -f docker/Dockerfile -t grayson .
docker run -d --name grayson-knowledge \
  -e GRAYSON_LIBRARY_URL=git@your-git-host:your-org/qa-library.git \
  -e GRAYSON_MCP_TOKEN=<long-random-token> \
  -v /path/to/deploy_key:/run/secrets/grayson_deploy_key:ro \
  -p 8850:8850 grayson
```

Or with compose: `docker compose -f docker/compose.yaml up -d` (edit the env
values first). Each teammate points their agent at it:

```bash
claude mcp add --transport http grayson-knowledge \
  http://your-host:8850/mcp --header "Authorization: Bearer <token>"
```

Notes:
- **Liveness**: `GET /healthz` answers `200` without a token (process
  liveness only, no library content) — point your platform's probe at it.
  The image also carries a Docker `HEALTHCHECK` using the same endpoint.
- The deploy key is mounted read-only anywhere (`GRAYSON_DEPLOY_KEY` points
  at it; `/run/secrets/grayson_deploy_key` is the default) — the entrypoint
  copies it into place with the ownership and permissions ssh requires, so
  the image works under an arbitrary platform-assigned UID (e.g. OpenShift).
- Host keys: mount your git host's `known_hosts` (`GRAYSON_KNOWN_HOSTS`, or
  `/run/secrets/grayson_known_hosts`) for strict checking; without one the
  appliance trusts the host key on first use per container.
- The container clones the library at startup and fast-forwards on each
  restart; if git auth breaks later it keeps serving the existing clone and
  `library_info` reports the staleness.
- The token gates the tool surface; there is nothing warehouse-shaped behind
  it to escalate to. For access beyond a trusted network, front the port with
  TLS (any reverse proxy) — the server itself speaks plain HTTP. Behind a
  gateway that authenticates every caller, `GRAYSON_MCP_NO_TOKEN=1` disables
  the built-in wall (same rules as `--no-token` above).
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

## Pinning a version for a team install

Install from a **tag**, never from a moving branch, so every analyst on the
pilot runs the same code and an upgrade is a deliberate step:

```bash
uv tool install "git+ssh://git@github.com/<org>/<install-repo>@v0.1.0-pilot.3"
```

The practice behind the tags:

- **Tags are cut where the install comes from.** The person who forwards a
  release to the install repo creates an annotated tag on the exact upstream
  commit being forwarded (`v0.1.0-pilot.N` during the pilot; bump `N` for
  every forward that changes behaviour) and pushes it to the install repo.
  The tag names a commit that exists in both repos, so it identifies the same
  code everywhere even though only the install repo carries the tag. The
  development repo's `main` and the merged pull requests are the changelog.
- **A downstream mirror only forwards.** An install repo that lives under a
  different account than the development repo (one machine usually holds
  credentials for one account) is kept in sync by fetching upstream and
  pushing the same commits on, then tagging:

  ```bash
  git fetch origin --tags                 # origin = upstream development repo
  git push dax origin/main:main           # dax    = downstream install repo
  git tag -a v0.1.0-pilot.4 origin/main -m "Pilot 4: <what changed>"
  git push dax v0.1.0-pilot.4
  ```

  `main` on the mirror should always be an ancestor of upstream `main`
  (`git merge-base --is-ancestor dax/main origin/main`); if it is not, someone
  committed to the mirror directly and that change needs to come upstream first.
- **Upgrading a tag pin is a reinstall.** `grayson upgrade` (and
  `uv tool upgrade`) re-resolve the pinned ref, which for a tag is a no-op.
  Move to a new tag explicitly:

  ```bash
  uv tool install --reinstall "git+ssh://git@github.com/<org>/<install-repo>@v0.1.0-pilot.4"
  ```

  The install output names the commit the tool was built from; it should match
  the tagged commit.

## Recipe chooser

| Situation | Recipe |
|---|---|
| One analyst, one machine | 1 — solo |
| Teammates' agents need the team's knowledge/records, not the harness | 2 — knowledge appliance |
| Policy: warehouse credentials must not exist where agents run | 3 — isolated server |
| Multiple analysts sharing one live server | Not supported yet — records/knowledge already compound through the library instead |
