# Tool proposals

How a model asks for a capability it doesn't have — and why that's safe to
allow.

---

## The shape

A model calls `propose_tool` with a manifest and a reason. The manifest is
written into a staging directory. **Nothing loads that directory.** It is
inert text until a human reads it and approves, at which point the file
moves into `tools/` and the reload makes it callable.

```
  model                    staging                operator              live
  ─────                    ───────                ────────              ────
  propose_tool  ────────>  <id>.yaml
                           <id>.json
                                      GET /proposals/<id>  ──> reads argv
                                                               │
                                            ┌── approve ──> installed, DISABLED
                                            │                    │
                                            │              you enable it ──> live
                                            └── reject ──> critique
  list_my_proposals <──────────────────────────────────────────┘
        │
        └── revise, propose again with supersedes: <id>
```

The model can propose and can read its own proposals. It cannot approve.
There is no MCP tool behind any approval route, and there's a test asserting
there never is — the moment approval is reachable from the tool surface, the
gate is decoration.

This is deliberately the same shape as the memory consolidator's draft gate:
synthesise, review, reject-with-critique, revise, supersede. The gate that
protects what gets remembered also protects what gets to run.

## Turning it off

```yaml
dashboard:
  proposals_enabled: false
```

Off means **absent** — `propose_tool` and `list_my_proposals` don't appear in
the tool list at all, rather than appearing and refusing. A tool that lists
and then errors on every call spends the model's attention teaching it
something you already knew.

---

## For the operator

Open `/viewer` → **Proposals**. The tab carries a count pip when something
is waiting, so you find out because it's there, not because you went looking.

| | |
|---|---|
| `GET /proposals` | everything, newest first, with a pending count |
| `GET /proposals/{id}` | one proposal **including the full manifest text** |
| `POST /proposals/{id}/approve` | install it, **switched off** |
| `POST /proposals/{id}/reject` | `{"critique": "..."}` — required |
| `POST /tools/state` | the second gate: `{"tool": "...", "enabled": true}` |

### Two gates, not one

Approving **installs** a tool. It does not turn it on.

After approve, the tool is registered, visible in the tool list, and
**disabled** — a call to it is refused with "disabled by the operator". You
enable it separately, from the Tool State tab or `POST /tools/state`.

That's deliberate. *"I read this and it isn't malicious"* and *"I want this
callable right now"* are two different judgements, and merging them means the
last moment you can change your mind is before you've ever seen the thing
sitting in your tool list. It also gives you somewhere to put a tool you're
willing to keep but not willing to leave live.

Enabling sticks — a later reload won't switch it back off.

### What to actually look at

`GET /proposals/{id}` returns `manifest` verbatim and an `effects` block
that spells out what each tool would *do*:

```jsonc
{
  "tool": "count_widgets",
  "kind": "process",
  "runs": ["/usr/bin/python3", "-c", "print('42')"],
  "executes_a_binary": true,
  "review_note": "This spawns a program. Read the argv. Check every {param} slot for what a caller could put there.",
  "parameters": [{"name": "path", "type": "string", "constrained": true}]
}
```

Read the `runs` line, not the description. The description is what the model
wrote to explain itself; the argv is what happens.

For every `{param}` slot in an argv, ask what the worst legal value does.
`constrained: false` on a string parameter that lands in argv is the thing
to push back on — a `pattern` or `enum` should be there. (The leading-dash
rule already blocks the flag-injection case; see
[TOOL-MANIFESTS.md](TOOL-MANIFESTS.md).)

`kind: web` is the lower-stakes case — it can only reach the `base_url` in
the manifest. Still check which host that is.

### Rejecting well

The critique is mandatory because it's the only thing the proposer has to
revise against. "No" produces a wall; "the argv shells out to a script I
can't read from here — inline the logic or point at something in
/opt/seren" produces a better second attempt.

A rejected proposal isn't dead. The model can revise and re-propose citing
`supersedes`, and you'll see the chain and the attempt number.

---

## Refused before you ever see it

Proposals are validated through the **same parser the loader uses**, so
"it validated" and "it will load" can't drift apart. These never reach your
queue:

- **A name that collides** with a builtin, a live tool, or another pending
  proposal. Approval must never be a way to *replace* something. This is
  re-checked at approve time too, because the live surface can move while a
  proposal sits in review.
- **A remote `from:` import.** The reviewed content would live on another
  host and could change after approval — you'd be reviewing a pointer.
- **A missing description**, a malformed name, an unknown `invoke.kind`, a
  `process` with no argv, a `web` with no path, or YAML that doesn't parse.
- **A missing rationale.** You need to know what it was *for*.

## The safety property, stated plainly

The staging directory sits inside `tools_dir` (at `tools/proposed/` by
default) purely because that's where you'd look for it. That's only safe
because the loader globs `*.yaml` **non-recursively**.

That's an implementation detail holding up a security property, which is
exactly the kind of thing that quietly stops being true. So there's a test —
`test_a_staged_proposal_is_NEVER_loaded` — that writes a manifest into the
staging dir, reloads, and asserts the tool doesn't exist. Make that glob
recursive and the suite goes red before anyone gets hurt.

Set `dashboard.proposals_dir` if you'd rather keep staging somewhere else
entirely.
