# ════════════════════════════════════════════════════════════════════════
#  ProposalTools - how a model asks for a capability it doesn't have.
#
#  propose_tool / list_my_proposals.
#
#  These descriptions are written FOR THE INSTANCE THAT WILL READ THEM,
#  not for a docs page. They say plainly what a proposal is, what it costs
#  a human to review one, and that a refusal is a normal outcome rather
#  than a failure - because a tool surface is a thing you ask for, and
#  asking well is most of the work.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
from typing import Optional

from ...proposals import ProposalStore, ProposalError

# Which toolbox these land in on the dashboard. Derivation would put
# this module in a box of its own; this says otherwise. Per-tool
# "toolbox" keys in a TOOL_DEF override even this.
TOOLBOX = "Proposals"


PROPOSE_TOOL_DEF = {
    "name": "propose_tool",
    "description": (
        "Propose a new tool for yourself, as a YAML manifest, for a human to "
        "review. This does NOT create a working tool: the manifest is written "
        "to a staging directory that nothing loads, and it only becomes "
        "callable if the operator approves it. Nothing you write here runs.\n"
        "\n"
        "Use it when you keep hitting the same wall — some capability you "
        "needed more than once and had to work around. Don't use it to "
        "speculatively broaden what you can do; every proposal costs a person "
        "their attention, and the reviewer is reading argv lines to decide "
        "whether to trust them.\n"
        "\n"
        "Two kinds. `process` spawns a program (argv is a list, no shell). "
        "`web` makes an HTTP call. Give every string parameter a `pattern` or "
        "`enum` where you can — a constraint you propose yourself is the "
        "clearest evidence you've thought about what could be passed in.\n"
        "\n"
        "The rationale matters as much as the manifest. Say what you were "
        "trying to do and what you tried first. If it comes back rejected you "
        "get the critique, and you can revise and propose again citing "
        "supersedes — that's the expected loop, not a failure state."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "manifest": {
                "type": "string",
                "description": (
                    "The full tool manifest as YAML text. Same format as any "
                    "file in the tools directory: a top-level `tools:` list, "
                    "each with name, description, invoke and parameters. "
                    "Inline only — remote `from:` imports are refused."
                ),
            },
            "rationale": {
                "type": "string",
                "description": (
                    "Why you want this, in plain language. What you were doing "
                    "when you needed it, and what you did instead."
                ),
            },
            "supersedes": {
                "type": "string",
                "description": (
                    "Optional. The id of a rejected proposal this one revises, "
                    "so the reviewer can see the chain and your response to "
                    "their critique."
                ),
            },
        },
        "required": ["manifest", "rationale"],
    },
}


LIST_PROPOSALS_TOOL_DEF = {
    "name": "list_my_proposals",
    "description": (
        "List tools you have proposed and where each one stands: pending, "
        "approved, or rejected with the reviewer's critique. Check here before "
        "proposing something — you may already have asked, and a rejected "
        "proposal carries the reason you should be revising against."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Optional filter.",
                "enum": ["pending", "approved", "rejected", "superseded"],
            },
        },
        "required": [],
    },
}


async def propose_tool(
    manifest: str,
    rationale: str,
    supersedes: Optional[str] = None,
    proposals: ProposalStore = None,
    **kwargs,
) -> str:
    if proposals is None:
        return _err(
            "Tool proposals are not enabled on this Workbench.",
            "The operator sets dashboard.proposals_enabled to turn this on.",
        )

    try:
        p = proposals.propose(manifest, rationale, supersedes)
    except ProposalError as ex:
        return _err(str(ex), "Fix the manifest and propose again.")
    except Exception as ex:  # noqa: BLE001
        return _err(f"could not stage the proposal: {ex}", "")

    return json.dumps({
        "ok": True,
        "proposal_id": p.id,
        "status": p.status,
        "tools": p.tool_names,
        "attempt": p.attempt,
        "staged_at": proposals.directory,
        "what_happens_next": (
            "The manifest is staged where nothing loads it. An operator has to "
            "read it and approve before any of these tools exist. Check back "
            "with list_my_proposals; if it's rejected you'll get a critique to "
            "revise against."
        ),
    }, indent=2)


async def list_my_proposals(
    status: Optional[str] = None,
    proposals: ProposalStore = None,
    **kwargs,
) -> str:
    if proposals is None:
        return _err("Tool proposals are not enabled on this Workbench.", "")

    items = proposals.list(status)
    return json.dumps({
        "count": len(items),
        "proposals": [
            {
                "id": p.id,
                "status": p.status,
                "tools": p.tool_names,
                "rationale": p.rationale,
                "attempt": p.attempt,
                "supersedes": p.supersedes,
                "critique": p.critique,
                "age": _age(p.created_at),
            }
            for p in items
        ],
    }, indent=2)


def _age(ts: float) -> str:
    import time
    secs = max(0, int(time.time() - (ts or 0)))
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60}m ago"
    if secs < 172800:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
