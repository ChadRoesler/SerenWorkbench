"""
Tool proposals — the gate between "the model asked" and "the thing runs".

THE LOAD-BEARING CLAIM of this feature is a negative: a proposal is inert.
It is a file in a directory nothing loads, and the only path to callable is
an operator approving it. Most of this file exists to attack that claim.

The rest covers the review loop — reject-with-critique, revise, supersede —
which mirrors the consolidator's draft gate on purpose.
"""
from __future__ import annotations

import json
import sys
import textwrap

import pytest

try:
    import mcp  # noqa: F401
    _mcp_available = True
except ImportError:
    _mcp_available = False

from seren_workbench.config import WorkbenchConfig, DashboardConfig, ServicesConfig
from seren_meninges import ServerConfig, TlsConfig


def _manifest(name: str = "count_widgets", text: str = "42") -> str:
    argv = [sys.executable, "-c", f"print('{text}')"]
    return textwrap.dedent("""\
        schema_version: 1
        tools:
          - name: %s
            description: Counts the widgets. Use when someone asks how many.
            invoke:
              kind: process
              argv: %s
            parameters: []
    """) % (name, json.dumps(argv))


@pytest.fixture
def dirs(tmp_path):
    tools = tmp_path / "tools"; tools.mkdir()
    proposed = tools / "proposed"; proposed.mkdir()
    return tools, proposed


@pytest.fixture
def client(dirs, make_client):
    tools, _ = dirs
    return make_client(WorkbenchConfig(
        server=ServerConfig(), tls=TlsConfig(),
        dashboard=DashboardConfig(tools_dir=str(tools)),
        services=ServicesConfig(),
    ))


def _names(client) -> set[str]:
    return {t["name"] for t in client.get("/tools").json()["tools"]}


def _propose(client, **kw):
    """Call propose_tool through its real implementation + store."""
    from seren_workbench.models.tools.proposal_tools import propose_tool
    import asyncio
    kw.setdefault("manifest", _manifest())
    kw.setdefault("rationale", "Asked three times this week and had no way.")
    return json.loads(asyncio.get_event_loop().run_until_complete(
        propose_tool(proposals=client.app.state.proposals, **kw)
    )) if False else json.loads(asyncio.run(
        propose_tool(proposals=client.app.state.proposals, **kw)
    ))


# ══ THE NEGATIVE CLAIM ═════════════════════════════════════════════════

def test_a_staged_proposal_is_NEVER_loaded(client, dirs):
    """THE ONE THAT MATTERS. The staging dir sits INSIDE tools_dir, which is
    only safe because the loader globs '*.yaml' non-recursively. That is an
    implementation detail holding up a security property, so it gets a test:
    if someone ever makes that glob recursive, this goes red."""
    _tools, proposed = dirs
    (proposed / "prop_deadbeef00.yaml").write_text(_manifest("smuggled_tool"))

    client.post("/tools/manifests/reload")

    assert "smuggled_tool" not in _names(client)
    body = client.get("/tools/manifests").json()
    assert "smuggled_tool" not in [t["name"] for t in body["live"]]


def test_proposing_does_not_create_a_callable_tool(client):
    r = _propose(client)
    assert r["ok"] is True and r["status"] == "pending"
    assert "count_widgets" not in _names(client)

    client.post("/tools/manifests/reload")
    assert "count_widgets" not in _names(client), "reload must not activate it either"


def test_approval_is_not_reachable_from_the_tool_surface(client):
    """The model may propose and may read its proposals. It may not approve.
    If approval ever gains an MCP tool, the gate is decorative."""
    tool_names = _names(client)
    assert "propose_tool" in tool_names
    assert "list_my_proposals" in tool_names
    for forbidden in ("approve_proposal", "reject_proposal", "reload_tools",
                      "install_tool", "approve_tool"):
        assert forbidden not in tool_names


def test_propose_tool_schema_does_not_leak_the_injected_store(client):
    """DI params must not surface as phantom arguments — a param that isn't
    in _DI_TYPES lands in the schema and the model tries to fill it."""
    t = {x["name"]: x for x in client.get("/tools").json()["tools"]}["propose_tool"]
    params = {p["name"] for p in t["parameters"]}
    assert params == {"manifest", "rationale", "supersedes"}
    assert "proposals" not in params


# ══ Refusals at propose time ═══════════════════════════════════════════

def test_cannot_propose_over_a_builtin(client):
    builtins = {t["name"] for t in client.get("/tools").json()["tools"]
                if t["type"] == "builtin"}
    victim = "remember" if "remember" in builtins else sorted(builtins)[0]
    r = _propose(client, manifest=_manifest(victim))
    assert "error" in r and "already the name of a live tool" in r["error"]


def test_cannot_propose_a_remote_import(client):
    """A `from:` import means the reviewed content lives elsewhere and can
    change after approval — the review would be of a pointer."""
    r = _propose(client, manifest=textwrap.dedent("""\
        schema_version: 1
        tools:
          - from: http://192.168.0.200:9999/mcp-manifest
    """))
    assert "error" in r and "remote imports" in r["error"]


def test_cannot_propose_the_same_name_twice_while_pending(client):
    assert _propose(client)["ok"] is True
    r = _propose(client)
    assert "error" in r and "awaiting review" in r["error"]


def test_rationale_is_required(client):
    r = _propose(client, rationale="   ")
    assert "error" in r and "rationale is required" in r["error"]


def test_manifest_must_parse_and_be_complete(client):
    assert "not valid YAML" in _propose(client, manifest="tools: [oh: no: bad")["error"]
    assert "no tools" in _propose(client, manifest="schema_version: 1")["error"]
    assert "invoke.kind" in _propose(client, manifest=textwrap.dedent("""\
        tools:
          - name: half_a_tool
            description: Incomplete on purpose.
    """))["error"]


def test_tool_names_are_constrained(client):
    r = _propose(client, manifest=_manifest("Bad-Name!"))
    assert "error" in r and "lower_snake_case" in r["error"]


def test_a_description_is_mandatory(client):
    r = _propose(client, manifest=textwrap.dedent("""\
        tools:
          - name: silent_tool
            invoke:
              kind: process
              argv: ["/bin/true"]
    """))
    assert "error" in r and "needs a description" in r["error"]


# ══ The review loop ════════════════════════════════════════════════════

def test_approve_installs_it_DISABLED_not_live(client):
    """TWO GATES. Approving says 'I read this and it isn't malicious'.
    Enabling says 'and I want it callable now'. Collapsing them means the
    last chance to change your mind is before you've seen the thing in
    context — so approve registers it switched OFF."""
    pid = _propose(client)["proposal_id"]
    assert "count_widgets" not in _names(client)

    body = client.post(f"/proposals/{pid}/approve").json()
    assert body["ok"] is True
    assert body["installed"] is True
    assert body["enabled"] is False
    assert body["reload"]["added"] == ["count_widgets"]

    by_name = {t["name"]: t for t in client.get("/tools").json()["tools"]}
    assert "count_widgets" in by_name, "it must be registered and visible"
    assert by_name["count_widgets"]["enabled"] is False, "but NOT enabled"


@pytest.mark.skipif(not _mcp_available, reason="mcp not installed")
def test_an_approved_but_disabled_tool_refuses_to_run(client):
    """Visible in the list is not the same as callable. The enable gate has
    to bite in the CALL path or it's decorative."""
    pid = _propose(client)["proposal_id"]
    client.post(f"/proposals/{pid}/approve")

    h = _session(client)
    r = _rpc(client, h, "tools/call", {"name": "count_widgets", "arguments": {}}, rid=9)
    blob = json.dumps(r).lower()
    assert "disabled" in blob, f"a disabled tool must refuse: {r}"


def test_enabling_is_a_separate_deliberate_act(client):
    pid = _propose(client)["proposal_id"]
    client.post(f"/proposals/{pid}/approve")

    r = client.post("/tools/state", json={"tool": "count_widgets", "enabled": True})
    assert r.json()["ok"] is True

    by_name = {t["name"]: t for t in client.get("/tools").json()["tools"]}
    assert by_name["count_widgets"]["enabled"] is True


def test_enabling_survives_a_later_reload(client, dirs):
    """seed_disabled must only bite on FIRST appearance. If it re-applied on
    every reload, the operator's enable would silently revert."""
    tools, _ = dirs
    pid = _propose(client)["proposal_id"]
    client.post(f"/proposals/{pid}/approve")
    client.post("/tools/state", json={"tool": "count_widgets", "enabled": True})

    (tools / "unrelated.yaml").write_text(_manifest("something_else"))
    client.post("/tools/manifests/reload")

    by_name = {t["name"]: t for t in client.get("/tools").json()["tools"]}
    assert by_name["count_widgets"]["enabled"] is True, "enable was undone by a reload"


def test_pending_count_rides_along_on_the_summary(client):
    assert client.get("/").json()["pending_proposals"] == 0
    _propose(client)
    assert client.get("/").json()["pending_proposals"] == 1
    pid = client.get("/proposals").json()["proposals"][0]["id"]
    client.post(f"/proposals/{pid}/reject", json={"critique": "nope"})
    assert client.get("/").json()["pending_proposals"] == 0


def test_approval_installs_the_reviewed_bytes_verbatim(client, dirs):
    """Approval MOVES the file. If it regenerated one, the thing approved and
    the thing installed could differ and the review would be advisory."""
    tools, _ = dirs
    original = _manifest("byte_check")
    pid = _propose(client, manifest=original)["proposal_id"]
    installed = client.post(f"/proposals/{pid}/approve").json()["installed_as"]
    assert (tools / installed).read_text() == original


def test_reject_requires_a_critique_and_returns_it_to_the_proposer(client):
    pid = _propose(client)["proposal_id"]

    bare = client.post(f"/proposals/{pid}/reject", json={"critique": ""})
    assert bare.status_code == 409 and "critique is required" in bare.json()["error"]

    ok = client.post(f"/proposals/{pid}/reject",
                     json={"critique": "argv shells out to a script I can't read."})
    assert ok.json()["ok"] is True

    import asyncio
    from seren_workbench.models.tools.proposal_tools import list_my_proposals
    mine = json.loads(asyncio.run(
        list_my_proposals(proposals=client.app.state.proposals)))
    got = mine["proposals"][0]
    assert got["status"] == "rejected"
    assert "can't read" in got["critique"]


def test_a_revision_supersedes_the_rejected_one(client):
    first = _propose(client)["proposal_id"]
    client.post(f"/proposals/{first}/reject", json={"critique": "too broad"})

    second = _propose(client, supersedes=first,
                      rationale="Narrowed it per the critique.")
    assert second["ok"] is True and second["attempt"] == 2

    by_id = {p["id"]: p for p in client.get("/proposals").json()["proposals"]}
    assert by_id[first]["status"] == "superseded"
    assert by_id[second["proposal_id"]]["supersedes"] == first


def test_approved_and_rejected_cannot_be_re_reviewed(client):
    pid = _propose(client)["proposal_id"]
    client.post(f"/proposals/{pid}/approve")
    again = client.post(f"/proposals/{pid}/approve")
    assert again.status_code == 409 and "not 'pending'" in again.json()["error"]


def test_approve_rechecks_the_collision_it_could_not_have_known_about(client, dirs):
    """A proposal can sit in review while the live surface moves. If a reload
    adds the proposed name in the meantime, approving would install a shadow."""
    tools, _ = dirs
    pid = _propose(client)["proposal_id"]
    (tools / "race.yaml").write_text(_manifest("count_widgets", text="i got here first"))
    client.post("/tools/manifests/reload")

    r = client.post(f"/proposals/{pid}/approve")
    assert r.status_code == 409
    assert "became a live tool" in r.json()["error"]


# ══ Reviewer ergonomics + hardening ════════════════════════════════════

def test_the_reviewer_is_shown_what_would_actually_run(client):
    """A name and a friendly description are not what you should approve on."""
    pid = _propose(client)["proposal_id"]
    p = client.get(f"/proposals/{pid}").json()

    assert p["manifest"].strip(), "the manifest must come back verbatim"
    eff = p["effects"][0]
    assert eff["kind"] == "process"
    assert eff["executes_a_binary"] is True
    assert eff["runs"][0] == sys.executable, "the argv must be spelled out"
    assert "Read the argv" in eff["review_note"]


def test_proposal_ids_cannot_traverse(client, dirs, tmp_path):
    """Tested against the STORE, not the URL. `GET /proposals/..` never
    reaches the handler — the HTTP layer normalises it away — so asserting
    on the route would be measuring the client library, not the guard.
    The id is what gets pasted into a filename, so that's what to attack."""
    store = client.app.state.proposals
    secret = tmp_path / "secret.json"
    secret.write_text('{"id": "prop_0000000000", "status": "approved"}')

    for evil in ("../secret", "../../etc/passwd", "prop_../../x",
                 "..", "", "prop_ZZZZZZZZZZ", "prop_short",
                 "prop_0000000000/../x"):
        assert store.get(evil) is None, f"store accepted id {evil!r}"

    # And the route inherits that refusal for anything that does reach it.
    for evil in ("prop_zzzzzzzzzz", "prop_short", "not-an-id"):
        assert client.get(f"/proposals/{evil}").status_code == 404


def test_proposals_can_be_switched_off_entirely(dirs, make_client):
    """Off means ABSENT, not present-and-erroring. A tool that lists and then
    refuses every call spends the model's attention teaching it a lesson the
    operator already knew."""
    tools, _ = dirs
    c = make_client(WorkbenchConfig(
        server=ServerConfig(), tls=TlsConfig(),
        dashboard=DashboardConfig(tools_dir=str(tools), proposals_enabled=False),
        services=ServicesConfig(),
    ))
    names = {t["name"] for t in c.get("/tools").json()["tools"]}
    assert "propose_tool" not in names
    assert "list_my_proposals" not in names
    assert c.get("/proposals").status_code == 503


# ══ The dashboard queue ════════════════════════════════════════════════

def test_the_viewer_has_an_approvals_queue(client):
    """Chad's mental model is 'it shows up in the workbench approvals queue'.
    Before this it was curl-only, which is not a queue anybody checks."""
    html = client.get("/viewer").text
    assert 'data-tab="proposals"' in html, "no Proposals tab"
    assert 'id="proposals-body"' in html, "no queue pane"
    assert 'id="proposals-pip"' in html, "no waiting-count pip"


def test_the_count_pip_can_actually_hide_itself(client):
    """SHIPPED BROKEN, caught in a screenshot. The JS sets pip.hidden at
    zero, but `[hidden] { display: none }` comes from the UA stylesheet at
    the same specificity as `.pip { display: inline-block }` — and a later
    rule of equal specificity wins. So the tab showed a '0' badge forever.

    Any class rule that sets `display` on an element JS hides needs its own
    [hidden] guard. This asserts the guard exists, because the symptom is
    purely visual and nothing else here would ever go red for it."""
    html = client.get("/viewer").text
    assert ".pip[hidden]" in html, (
        "the .pip display rule will override the hidden attribute"
    )
    # And the guard has to be able to beat the base rule: either it comes
    # first with !important, or the attribute selector out-specifies it.
    assert "display: none" in html.split(".pip[hidden]")[1][:60]


def test_the_viewer_wires_the_queue_to_the_real_endpoints(client):
    """A tab that renders and calls nothing is a screenshot."""
    html = client.get("/viewer").text
    assert "/proposals" in html
    assert "refreshProposals" in html
    for action in ("approve", "reject"):
        assert f'/${{action}}' in html or f"/{action}" in html
    # And it must say out loud that approval does not mean enabled.
    assert "disabled" in html.lower()


def test_the_queue_degrades_honestly_when_proposals_are_off(dirs, make_client):
    """With proposals disabled the endpoint 503s. The tab must read that as
    a STATE ('disabled by config'), not render a red failure box."""
    tools, _ = dirs
    c = make_client(WorkbenchConfig(
        server=ServerConfig(), tls=TlsConfig(),
        dashboard=DashboardConfig(tools_dir=str(tools), proposals_enabled=False),
        services=ServicesConfig(),
    ))
    html = c.get("/viewer").text
    assert "proposals_enabled" in html, "the disabled case must be handled in the JS"
    assert c.get("/").json()["pending_proposals"] == 0


# ══ End to end over the real MCP transport ═════════════════════════════

_MCP_HEADERS = {"Accept": "application/json, text/event-stream",
                "Content-Type": "application/json"}


def _session(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}}, headers=_MCP_HEADERS)
    h = dict(_MCP_HEADERS)
    if r.headers.get("mcp-session-id"):
        h["mcp-session-id"] = r.headers["mcp-session-id"]
    client.post("/mcp", json={"jsonrpc": "2.0",
                              "method": "notifications/initialized"}, headers=h)
    return h


def _rpc(client, h, method, params, rid=2):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": rid,
                                  "method": method, "params": params}, headers=h)
    for line in r.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    return json.loads(r.text)


@pytest.mark.skipif(not _mcp_available, reason="mcp not installed")
def test_the_whole_loop_over_mcp(client):
    """propose over MCP -> not callable -> operator approves -> callable.
    The full arc, on the live surface, exactly as it would happen."""
    h = _session(client)

    call = _rpc(client, h, "tools/call", {
        "name": "propose_tool",
        "arguments": {"manifest": _manifest("count_widgets", text="1337"),
                      "rationale": "Needed this twice already."}})
    payload = json.loads(json.dumps(call["result"]))
    pid = json.loads(
        [c["text"] for c in payload["content"] if c["type"] == "text"][0]
    )["proposal_id"]

    listed = _rpc(client, h, "tools/list", {}, rid=3)
    assert "count_widgets" not in {t["name"] for t in listed["result"]["tools"]}

    assert client.post(f"/proposals/{pid}/approve").json()["ok"] is True

    listed = _rpc(client, h, "tools/list", {}, rid=4)
    assert "count_widgets" in {t["name"] for t in listed["result"]["tools"]}

    # Gate two: installed but off, so it still refuses.
    blocked = _rpc(client, h, "tools/call",
                   {"name": "count_widgets", "arguments": {}}, rid=5)
    assert "disabled" in json.dumps(blocked).lower()

    # The operator flips it on — and only now does it run.
    client.post("/tools/state", json={"tool": "count_widgets", "enabled": True})
    ran = _rpc(client, h, "tools/call",
               {"name": "count_widgets", "arguments": {}}, rid=6)
    assert ran["result"].get("isError") is not True, ran
    assert "1337" in json.dumps(ran["result"])
