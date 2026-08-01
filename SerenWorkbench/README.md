# SerenWorkbench

The tool surface an LLM reaches through.

One process on port **7425** serving an MCP endpoint, an operator dashboard and a small HTTP
API. Builtin tools cover memory, web search, time, cluster control and the
scheduler. On top of those, you can add your own tools as **YAML files** —
no Python, no restart.

Part of the [Seren](https://github.com/ChadRoesler) stack, but it doesn't
require the rest of it. Point it at whichever services you actually run.

---

## Install

```bash
pip install seren-workbench
python -m seren_workbench
```

That's a working server with the builtin tools and an empty tool
directory. Configuration is optional — copy `seren-workbench.yaml.sample`
to `seren-workbench.yaml` when you want to change ports, service URLs or
which tools start enabled.

```bash
python -m seren_workbench --config /etc/seren/workbench.yaml --port 7425
```

## Connect a client

The MCP endpoint is at `/mcp/` (trailing slash — a bare `/mcp` gets a 307).

```jsonc
{
  "mcpServers": {
    "seren-workbench": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:7425/mcp/",
               "--transport", "http-only"]
    }
  }
}
```

Set a bearer token in the config (or `SEREN_WORKBENCH_BEARER_TOKEN`) before
this leaves localhost.

---

## Adding your own tools

Drop a YAML file in `tools_dir`:

```yaml
schema_version: 1
tools:
  - name: disk_free
    description: >
      Report free space on the Seren data volume. Use when someone asks
      whether there's room for another model.
    invoke:
      kind: process
      argv: ["df", "-h", "--", "{mount_point}"]
    parameters:
      - name: mount_point
        type: string
        required: true
        pattern: "/[A-Za-z0-9._/-]*"
```

Then reload — the tool is live immediately, no restart:

```bash
curl -X POST localhost:7425/tools/manifests/reload
```

Tools can run a **process** or make a **web** call. Parameters are typed,
defaulted, range-checked and — for strings — matched against a `pattern`
or an `enum`, with the rules published into the JSON schema so the model
reads them rather than discovering them by being corrected.

**→ [docs/TOOL-MANIFESTS.md](https://github.com/ChadRoesler/SerenWorkbench/blob/main/SerenWorkbench/docs/TOOL-MANIFESTS.md)**
is the full reference.
**[examples/tools/example-tool.yaml](https://github.com/ChadRoesler/SerenWorkbench/blob/main/SerenWorkbench/examples/tools/example-tool.yaml)**
is an annotated file you can copy; the test suite loads it, so it can't
quietly stop being true.

<!-- Absolute URLs on purpose: this file is the PyPI long_description, and
     PyPI does not resolve relative links. -->


### Tools the model asks for

A model can propose a tool for itself with `propose_tool`. The manifest lands
in a staging directory that **nothing loads** — it's inert text until you read
it and approve, at which point it moves into `tools/` and goes live.

Open `/viewer` → **Proposals**; the tab shows a count when something's waiting.
Or from the shell:

```bash
curl -s localhost:7425/proposals                     # what's waiting
curl -s localhost:7425/proposals/prop_a1b2c3d4e5     # the manifest + what it would run
curl -X POST localhost:7425/proposals/prop_a1b2c3d4e5/approve
curl -X POST localhost:7425/proposals/prop_a1b2c3d4e5/reject \
     -d '{"critique":"argv shells out to a script I cannot read"}'
```

**Approving installs it switched off.** The tool appears in your list,
disabled; you turn it on separately from the Tool State tab. Two decisions —
"this isn't malicious" and "I want it live now" — kept apart on purpose.

The model can propose and read its proposals. It cannot approve — there's no
MCP tool behind any approval route, and a test asserts there never is. Reject
requires a critique, which the proposer reads and can revise against; same
loop as the memory consolidator's draft gate.

Turn it off with `dashboard.proposals_enabled: false`, which removes the tools
entirely rather than leaving them to fail.

**→ [docs/TOOL-PROPOSALS.md](https://github.com/ChadRoesler/SerenWorkbench/blob/main/SerenWorkbench/docs/TOOL-PROPOSALS.md)** — what to look at when reviewing one.

### What reload won't do

It reads files that are already on disk. It cannot author a tool, it
refuses a manifest that would shadow a builtin, it won't re-enable
something you switched off, and it treats a *missing* tools directory as
"something's wrong" rather than "delete everything." A tool exists because
a person put it somewhere.

---

## Endpoints

| | |
|---|---|
| `GET /` | service info and tool counts |
| `GET /health` | liveness |
| `GET /tools` | every tool with its schema |
| `GET`/`POST` `/tools/state` | enable and disable, per tool or per action |
| `GET /tools/manifests` | what loaded, what was skipped, and why |
| `POST /tools/manifests/reload` | re-read the directory, apply it live |
| `GET /proposals` | tools the model has asked for |
| `GET /proposals/{id}` | one, with the full manifest and what it would run |
| `POST /proposals/{id}/approve` | install it and bring it live |
| `POST /proposals/{id}/reject` | refuse it, with a critique |
| `GET /config` | resolved config, secrets masked |
| `GET /logs` | recent server logs |
| `/mcp/` | the MCP streamable-HTTP transport |
| `/viewer` | the operator dashboard |

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

## License

GPL-3.0-or-later.
