# Writing a plug-and-play tool

A **tool manifest** is a YAML file that gives the model a new capability
without anyone writing Python. Drop it in `tools_dir`, reload, and it's
callable.

This is the whole format. It's short on purpose.

---

## The smallest thing that works

```yaml
schema_version: 1

tools:
  - name: disk_free
    description: >
      Report free space on the Seren data volume. Use when someone asks
      whether there's room for another model.
    invoke:
      kind: process
      argv: ["df", "-h", "/mnt/nvme"]
    parameters: []
```

Save it as `~/seren-workbench/tools/disk.yaml` (or wherever `dashboard.tools_dir` points; `~` works), then:

```bash
curl -X POST localhost:7425/tools/manifests/reload
```

The tool is live. No restart.

> **Write the description for the model, not for yourself.** It is the only
> thing standing between "the right tool at the right moment" and "never
> gets picked." Say what it does *and when to reach for it*.

---

## `invoke.kind: process`

Runs a program. `argv` is a **list**, and it is spawned directly — there is
never a shell involved, so nothing you can put in a parameter will be
re-parsed as a command. Semicolons, backticks and `$(...)` are just
characters.

```yaml
    invoke:
      kind: process
      argv: ["git", "-C", "/srv/repo", "log", "--oneline", "-n", "{count}"]
      cwd: /srv                 # optional
      timeout_seconds: 30       # optional, default 30
```

Each `{name}` substitutes that parameter's value **as one argv element**,
whatever it contains.

**stdout** becomes the tool's answer (truncated at 16k). **stderr** is
logged for you and never shown to the model — a program that chatters on
stderr won't pollute the conversation. A non-zero exit becomes a tool
error with the stderr tail attached as a hint.

## `invoke.kind: web`

Makes an HTTP call.

```yaml
    invoke:
      kind: web
      base_url: http://127.0.0.1:7421
      method: POST
      path: /notes/{note_id}/amend
      headers:
        X-Request-Source: "workbench/{note_id}"
      body_template: '{"text": "{text}", "pinned": {pinned}}'
```

- **`path`** substitutes URL-encoded, so a parameter can't escape its
  segment or traverse upward.
- **`headers`** substitute in their *values*. Header **names** don't, on
  purpose — a templated name would let a parameter value inject a whole
  header.
- **`body_template`** is type-aware. String parameters are JSON-escaped and
  land *inside* the quotes the template provides; numbers and booleans land
  bare. So `"{text}"` and `{pinned}` are both correct as written above.

A `configuration:` block at the top of the file sets `cwd` / `base_url`
defaults for every tool in it.

### Sending a token

If the service behind a web tool wants a bearer, **don't make it a
parameter**. A `{token}` slot means the model has to hold the secret and
hand it over on every call, and it shows up in the schema it reads. Name it
once for the file instead:

```yaml
configuration:
  base_url: http://127.0.0.1:7421
  bearer_token_env: SEREN_MARGIN_TOKEN     # the NAME of an env var
  # bearer_token_keyring: seren/margin     # or a keyring ref
  # bearer_token: "..."                    # or, as a last resort, inline
```

Every `kind: web` tool in the file that doesn't set its own `Authorization`
header then sends `Bearer <value>`, resolved *at call time* - rotate the
variable and the next call uses the new one. Same three pointers, same
precedence, as every `server:` block in the Seren family.

### One place a web tool can't point

A tool whose target is the Workbench's own listener is refused - at propose
time and again when called. The builtin tools are how the model reaches the
Workbench; a manifest tool that POSTs to `/proposals/{id}/approve` is the
model approving its own next proposal, however friendly its description.

---

## How it looks on the dashboard

Tools are shown as **Toolbox → tool**, with custom tools one level deeper
under a single *Custom Toolboxes* group. Everything here is presentational
and changes nothing about how a tool runs.

You don't have to do anything. Drop in `hotdog-math.yaml` and you get a
**Hotdog Math** toolbox, with `dogs_between_cities` shown as *Dogs Between
Cities*. The callable name and the source file are still there, inside the
expanded card, for when you're auditing rather than browsing.

Override either when the derived version reads badly:

```yaml
metadata:
  toolbox: Costco Science          # every tool in this file

tools:
  - name: dogs_between_cities
    display_name: "Hot Dog Distance"    # this tool's label
    toolbox: Serious Business           # ...and its box, overriding the file
```

`name` is unaffected — it stays the identifier the model calls. A display
name is for the human scanning the list.

---

## Parameters

```yaml
    parameters:
      - name: count
        type: integer          # string | integer | number | boolean
        required: true
        description: How many commits to list.
        default: 10
        min: 1
        max: 100
```

`min` / `max` are numeric bounds. For strings you have two more:

```yaml
      - name: window
        type: string
        enum: ["day", "week", "month"]     # a closed set

      - name: ref
        type: string
        pattern: "[A-Za-z0-9._/-]+"        # must match ENTIRELY
```

Both are published into the tool's JSON schema, so the model sees the rule
rather than discovering it by getting told off. `pattern` is
`re.fullmatch` — a pattern of `[a-z]+` rejects `abc; rm -rf /` instead of
matching the `abc` and shrugging.

> If a `pattern` doesn't compile, the parameter is **refused** rather than
> quietly ignored. A constraint that evaporates is worse than no constraint,
> because you think you have one.

### The leading-dash rule

For `kind: process`, a string value starting with `-` is rejected.

Not because of shell injection — that's structurally impossible here. The
problem is subtler and survives having no shell at all:

```yaml
    argv: ["curl", "-sS", "{url}"]
```

Called with `url = "-o/home/you/.ssh/authorized_keys"`, that's a file
write, and every single component behaved exactly as designed. The value
became a *flag*.

Two ways through it, best first:

```yaml
    argv: ["curl", "-sS", "--", "{url}"]     # "--" ends option parsing
```

```yaml
      - name: extra_flags
        type: string
        allow_leading_dash: true             # genuinely meant to carry flags
```

---

## Importing someone else's tools

A service can host its own manifest and you point at it:

```yaml
tools:
  - from: http://127.0.0.1:7421/mcp-manifest
    bearer_token_env: SEREN_MARGIN_TOKEN     # only if that service wants one
    overrides:
      - name: note_to_self
        description: Custom wording that fits this deployment better.
```

Fetched at startup and on reload, three attempts, two seconds apart. If it
fails the rest of your tools still load. Imports don't chain — a remote
manifest containing its own `from:` is skipped.

Two things a remote manifest can't do:

- **Hand you a program.** Only `kind: web` tools are accepted from an
  import. A `kind: process` entry is skipped and named in `skipped` with the
  reason - it arrived over plain HTTP from a server that could change it
  before your next reload, which is not what "reviewed" means. Want it?
  Copy it inline, where you can read it.
- **Name a credential.** The token the imported tools present is the one
  on *your* stub (`bearer_token_env` above), never one the remote file
  declares. Which secret on this box a tool may read is your call.

---

## Reloading

| | |
|---|---|
| `GET /tools/manifests` | what loaded, what was skipped and why, which files failed to parse |
| `POST /tools/manifests/reload` | re-read the directory and apply the difference **live** |

The reload response tells you exactly what moved: `added`, `removed`,
`replaced`. Edited tools count as `replaced` — the diff is on the tool's
full definition, not just its name, so changing an `argv` re-registers it
rather than leaving the old one running.

Things reload deliberately will **not** do:

- **Touch a builtin.** A manifest named after a builtin tool is refused and
  reported. Builtins were protected by registration order before reload
  existed; now the guard is explicit.
- **Re-enable what you switched off.** A reload is a statement about what's
  on disk, not about what's allowed to run. Neither does a restart: toggles
  are remembered in `<tools_dir>/.tool-state.json` (`dashboard.state_file`
  to move it). A name you list in `tools_enabled` / `tools_disabled` is your
  written word and beats the file.
- **Treat a missing directory as "delete everything."** An unmounted volume
  leaves your tools alone. An *empty* directory does mean remove them all —
  that distinction is the whole point.
- **Author anything.** It reads files that are already there. A tool exists
  because a person put it somewhere.

---

## When something doesn't show up

Ask the server before you guess:

```bash
curl -s localhost:7425/tools/manifests | python3 -m json.tool
```

`skipped` carries the reason for each one — no `invoke.kind`, no name,
duplicate name across two files, or shadowing a builtin. `failed_files`
carries the parse error. Nothing fails silently; the answer is in there.
