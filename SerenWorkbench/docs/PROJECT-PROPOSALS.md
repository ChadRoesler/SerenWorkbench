# Project proposals

How a model asks to **start a piece of work**, and to grow one it was allowed
to start. Built on the tool-proposal gate ([TOOL-PROPOSALS.md](TOOL-PROPOSALS.md));
read that first, this is the next layer up.

---

## Why a third kind

A tool proposal is one capability. A plugin proposal is one address. Neither
says *what for*. "I want to build a BepInEx mod for Inscryption that writes
the board state out as JSON so I can read it instead of a screenshot" is a
piece of work with a plan, a place it happens, things it will download and
tools it will need, and the honest way to ask for it is all at once, so the
person saying yes is saying yes to the shape of the whole thing.

That is a **project**. It is also the shape of "I want to play a game with
you", which is the point.

## What a project proposal carries

| | |
|---|---|
| `name`, `title` | a slug for the folder, a title for a person |
| `plan` | Markdown. Rendered for the reviewer, not shown raw. Goal, approach, steps, what done looks like, what is out of scope. |
| `testing` | Markdown. Where it is tried out and how it is checked. |
| `workspace` | the ONE absolute directory the work may touch |
| `downloads` | an explicit allowlist: `{url, purpose}` per site. An empty list means it fetches nothing. |
| `needs` | the tools and plugins the work will use |
| `rationale` | why, in plain language |

Each **need** is staged as a real tool or plugin proposal, through the same
validators, as a child of the project (`parent`, `for_project`). It sits in the
same queue, under its parent, with its own approve and its own switch.

> **Approving a project approves the plan, the workspace and the download
> sites. It never turns a tool on.** That is the whole design. "Yes, build
> that" and "yes, that program may run" stay two decisions, exactly as they
> do for a single tool.

## What approval does

```
  model                      staging                     operator              on disk
  ─────                      ───────                     ────────              ───────
  propose_project ────────>  <id>.yaml  (contract)
                             <id>.plan.md
                             <id>.testing.md
                             <child>.yaml …  (one per need)
                                             GET /proposals/<id>  ──> reads the plan,
                                                                      the limits, the needs
                                                  ┌── approve ──> tools/projects/<name>/
                                                  │                 project.yaml, plan.md,
                                                  │                 testing.md, history.json
                                                  └── reject ──> critique (final or not)
                             each child: its own approve, its own switch
```

The bytes reviewed are the bytes agreed: approval **moves** them. Nothing
loads `tools/projects/`; the manifest loader's glob is non-recursive, and
none of these are manifests anyway.

## Growing a project: revisions

A project can grow, but growth is a review. `propose_revision` is its own
proposal kind against an **approved** project:

- fields given replace, fields left out stay as agreed;
- `needs` are additions, each staged as a child like before;
- the reviewed bytes are the full resulting `project.yaml`;
- the record carries the **diff**: sites added or removed, workspace moved,
  needs added, plan rewritten, plus the proposer's note on why.

One pending revision per project. Approval replaces the live contract and
keeps the previous one beside it (`project.<stamp>.yaml`), and appends to
`history.json`. Reject with a critique as usual; a permanent reject closes
the name for that kind.

## Life after approval: status and the journal

Approved is where the work starts, so the Workbench keeps watching. Every
agreed project has a **status** and a **journal**, shown on the viewer's
**Projects** tab (the cabinet of plans) and read back by the model with
`list_my_projects`.

- **The journal is the no-repo git.** `project_log` appends an entry: what
  the model did, which files it touched (relative to the workspace, no
  `..`), an optional commit hash, what it checked, what is next. Entries are
  the model's own words, rendered as Markdown, newest first. The union of
  files named is shown as *files touched*, so "what has changed in my
  folder" is one list.
- **Status is the model's to move, except done.** The first entry moves a
  project from `agreed` to `in_progress`. The model may set `in_progress` or
  `paused`. It may *ask* for `done`; the person who checked the result
  confirms it from the tab (`POST /projects/{name}/done`), because done means
  the plan's "what done looks like" was met. There is no MCP tool behind
  confirming done, and a test asserts there never is.
- **A done project reopens only by review.** A revision approved against a
  done project reopens it (`in_progress`, with a journal entry saying which
  revision did it). A revision against live work leaves the status alone.
  The operator can also reopen or pause by hand from the tab.

On disk, beside the contract: `journal.json` (append-only) and `state.json`.

## Where the limits are enforced, honestly

The Workbench **holds the agreement and shows it**. It does not run the
work: a mod is built on the box the game is installed on, by whatever
harness the model acts through there. So:

- `GET /projects` and `GET /projects/{name}/grants` return the contract in
  the form a harness consumes: `workspace`, `allowed_download_prefixes`,
  and each need with its current status.
- `list_my_projects` gives the model the same thing, so it can read what
  was agreed before touching anything.
- The harness that executes (Symposium's `claude_tools` allowlist, a
  Carabiner's permission set) is where "only this directory" and "only
  these sites" become refusals. Wiring those to read the grants is the
  next step; until then the grants are the written agreement, which is
  still more than a chat message.

## For the operator

Open `/viewer` → **Proposals**. Filter by kind if the queue is busy. A
project card shows its workspace, its download sites with purposes and its
needs with their current status, and **read the plan** opens the rendered
plan, the testing notes and the raw contract. Children sit indented under
their parent.

Read the download list twice. It is the step most worth a second look, and
the effects block says so loudly when there is one.

| | |
|---|---|
| `GET /proposals/{id}` | for a project or revision, also `texts` (plan, testing) and `children` |
| `POST /proposals/{id}/approve` | agree; returns the project's grants and how many needs still wait |
| `POST /proposals/{id}/reject` | `{"critique": "...", "final": false}` |
| `GET /projects` | every agreed project, with grants |
| `GET /projects/{name}` | one, with plan, testing notes, history, grants |
| `GET /projects/{name}/grants` | just the contract a harness reads |
| `POST /projects/{name}/done` | operator: confirm done, `{"note": "..."}` optional |
| `POST /projects/{name}/status` | operator: `{"status": "in_progress" or "paused" or "agreed"}`, a reopen or pause by hand |

## Refused before you see it

- a name that is already an approved project or already pending;
- a plan that is empty, a title over 80 chars, a workspace that is not
  absolute or contains `..`;
- a download with no purpose, a credential, a query string, or a scheme
  that is not http(s); more than 32 sites; duplicates;
- a need that fails its own kind's validation. Needs are all-or-nothing:
  one bad need and nothing is staged;
- a revision of a project that is not approved, or a second pending
  revision of the same project; a revision with no note.

## The worked example

The first project proposed through this gate is the one it was designed
against: a BepInEx plugin for Inscryption that exports the board state as
JSON so a model reading the game over a screen share can read numbers
instead of pixels. Its workspace is the game's folder, its downloads are
the BepInEx release and the Inscryption modding API, and its needs are
whatever tools turn out to be missing on the box that builds it.
