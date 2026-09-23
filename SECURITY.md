# Security Policy

## Supported versions

| Component | Supported |
|-----------|-----------|
| Latest release tag | ✅ |
| Older tags | ❌ |

Security fixes are applied to the current release only. Pin to the latest tag.

---

## What this service is

The surface a model reaches through. Everything a model can *do* to the
machine or the cluster - remember, search, start a service, run a manifest
tool - goes through this process, so this is the place where "the model can
call it" is decided. It is self-hosted; nothing leaves the box except the
calls the tools themselves make and the optional update check against the
package index (`updates.enabled: false` or `SEREN_WORKBENCH_UPDATES_ENABLED=0`
switches that off).

Two kinds of thing run here, and they have different trust:

- **Builtin tools** are Python shipped with the package. They call other
  Seren services over HTTP with the credentials configured under
  `services:`, and SearXNG for search. `fetch_url` refuses private, loopback,
  link-local and CGNAT addresses.
- **Manifest tools** are YAML files an operator put in `tools_dir`. A
  `kind: process` tool spawns a program (argv list, never a shell); a
  `kind: web` tool makes an HTTP call. **Whoever can write to `tools_dir` can
  make the model run a program.** Treat that directory like `sudoers`.

---

## Threat model

| Surface | Default | Notes |
|---------|---------|-------|
| HTTP API + `/mcp/` | `127.0.0.1:7425` | Loopback only. A host beyond loopback with no token **refuses to start** (exit 78) and prints the three ways out; `server.allow_open_lan: true` is the written override and prints a banner every boot. |
| Bearer token (inbound) | Not set | `server.bearer_token`, `_env` or `_keyring`, or `SEREN_WORKBENCH_BEARER_TOKEN*`. With one set, every route but `/`, `/health` and `/viewer` wants `Authorization: Bearer <token>` - including `/mcp/`, the proposal routes and the toggles. Set it before this leaves localhost. |
| Bearer tokens (outbound) | Not set | What the builtins PRESENT to Memory, Lodestar and the scheduler: `services.bearer_token*` for one cluster token, `services.<name>_bearer_token*` per service. Pointers, not secrets: prefer `_env` or `_keyring`. SearXNG never gets one. |
| `tools_dir` | `~/seren-workbench/tools` | Anyone who can write here can add a `kind: process` tool. Keep it owned by the service user, mode `0755` or tighter, never on a share. A manifest that names a builtin is refused. |
| Remote imports (`from:`) | Off unless a stub names one | Fetched over plain HTTP at startup and on every reload, from a URL the operator wrote. **Only `kind: web` tools are accepted from a remote manifest**; a `kind: process` entry is skipped and reported. The credential an import presents is named on the *local* stub, never taken from the remote file. |
| Self-targeting tools | Refused | A web tool whose target is this Workbench's own listener is refused at propose time and again at dispatch. Otherwise a "helper" that POSTs `/proposals/{id}/approve` is the model approving its own next proposal. |
| Proposals | On, inert | `propose_tool` writes to `<tools_dir>/proposed`, which nothing loads. Approval is an operator HTTP route with **no MCP tool behind it**, and installs the tool *disabled*. The review payload shows the argv or URL and names any credential the tool would send. `dashboard.proposals_enabled: false` removes the tools entirely. |
| Toggle state | `<tools_dir>/.tool-state.json` | Remembers what you switched off and what you approved-but-left-off, so a reboot does not put it back. A name in `tools_enabled` / `tools_disabled` beats the file. |
| Audit log (`/logs`) | In memory | Records tool name, duration, success, argument *count*. Never argument values or results. Bearer values in service log lines are redacted by `get_recent_logs`. |
| Config file | `~/seren-workbench/seren-workbench.yaml` | May hold inline tokens. The installer sets `0600` when it writes one. Do not commit it. |
| `GET /config` | Behind the bearer | Every key ending in `bearer_token`, inbound or outbound, is masked. The `_env` and `_keyring` pointers are shown; knowing which variable holds a secret is not knowing the secret. |
| MCP host check | Off | FastMCP's DNS-rebinding protection defaults off for a trusted LAN; `SEREN_WORKBENCH_ALLOWED_HOSTS` turns it on with an allowlist. |

---

## Reviewing a proposal

Read the `effects` block, not the description. `runs` is the argv; every
`{param}` slot in it is something the model chooses at call time, so a string
parameter with `constrained: false` that lands in argv is the thing to push
back on. `calls` is the URL for a web tool - check the host. `sends_credential`
means the tool would present a token from this box's environment to that
host; ask whether the host deserves it. Full notes in
[docs/TOOL-PROPOSALS.md](SerenWorkbench/docs/TOOL-PROPOSALS.md).

---

## Deployment recommendations

- **One box, everything on it** (the intended case): the defaults. Loopback,
  no inbound token; set the outbound `services.bearer_token_env` if Memory
  or Lodestar were installed with one.
- **Cluster head reached from other nodes**: set an inbound token first,
  then widen the bind. The service will not start the other way round. Put a
  TLS-terminating proxy in front if it crosses anything you do not own.
- **Anything routable from outside the house**: don't. This process can
  start programs.

---

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Open a [GitHub Security Advisory](https://github.com/ChadRoesler/SerenWorkbench/security/advisories/new) (private disclosure). Include:

- A description of the issue and its impact
- Steps to reproduce
- Any relevant config or environment details

You will get a response within **7 days**. If a fix is needed, a patched release will be tagged and the advisory will be published after users have had time to update.
