"""
Parameter-constraint tests for dynamic (YAML manifest) tools.

Every test here names the hole it exists for.

The background: ToolParameter shipped with `min`/`max` and nothing else.
Those are numeric bounds — so the ONLY parameter type that can reach a
subprocess argv slot or a URL path (string) was the only type with no
constraint surface at all. `pattern`, `enum` and the leading-dash refusal
close that; these tests are the net under them.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from seren_workbench.dynamic_tools.manifest_loader import _dict_to_manifest
from seren_workbench.dynamic_tools.tool_audit_log import ToolAuditLog
from seren_workbench.dynamic_tools.yaml_dispatched_tool import YamlDispatchedTool


def _tool(param_yaml: dict, kind: str = "process") -> YamlDispatchedTool:
    """Build a single dispatched tool around one parameter definition."""
    invoke = (
        {"kind": "process", "argv": [sys.executable, "-c", "print('{v}')"]}
        if kind == "process"
        else {"kind": "web", "base_url": "http://localhost", "path": "/x/{v}"}
    )
    manifest = _dict_to_manifest({
        "schema_version": 1,
        "tools": [{
            "name": "probe",
            "description": "constraint probe",
            "invoke": invoke,
            "parameters": [param_yaml],
        }],
    })
    return YamlDispatchedTool(
        entry=manifest.tools[0], owner=manifest,
        source_path="probe.yaml", http_client=None, audit_log=ToolAuditLog(),
    )


def _resolve(tool: YamlDispatchedTool, value):
    return tool._resolve_arguments({"v": value})


# ── The loader has to actually carry the new fields ────────────────────

def test_loader_parses_the_string_constraints():
    """A constraint that the YAML parser drops on the floor is not a
    constraint. pattern/enum/allow_leading_dash must survive _dict_to_manifest."""
    mf = _dict_to_manifest({
        "tools": [{
            "name": "t", "invoke": {"kind": "process", "argv": ["x"]},
            "parameters": [{
                "name": "v", "type": "string",
                "pattern": r"[a-z]+", "enum": ["a", "b"],
                "allow_leading_dash": True,
            }],
        }],
    })
    p = mf.tools[0].parameters[0]
    assert p.pattern == r"[a-z]+"
    assert p.enum == ["a", "b"]
    assert p.allow_leading_dash is True


# ── CWE-88: a value that becomes a flag ────────────────────────────────

def test_leading_dash_refused_in_process_argv():
    """THE ONE THAT MATTERS. argv is a list and there is no shell, so
    CWE-78 is structurally closed — but `argv: [curl, "{v}"]` with
    v="-o/home/you/.ssh/authorized_keys" is a file write in which every
    component behaved exactly as designed."""
    _args, err = _resolve(_tool({"name": "v", "type": "string"}),
                          "-o/home/caesar/.ssh/authorized_keys")
    assert err is not None
    assert "flag" in err
    assert "allow_leading_dash" in err, "the error must say how to opt in"


def test_leading_dash_allowed_when_the_manifest_opts_in():
    """Default-deny, not can't-ever. A tool genuinely meant to carry flags
    says so in the manifest."""
    args, err = _resolve(
        _tool({"name": "v", "type": "string", "allow_leading_dash": True}), "-v")
    assert err is None and args["v"] == "-v"


def test_leading_dash_is_fine_on_a_web_tool():
    """A query value starting with '-' is inert over HTTP. Refusing it there
    would break working tools to buy nothing."""
    args, err = _resolve(_tool({"name": "v", "type": "string"}, kind="web"), "-v")
    assert err is None and args["v"] == "-v"


def test_negative_numbers_still_work():
    """The dash guard is string-only on purpose — an integer parameter
    coerces to int and cannot smuggle a flag, so -5 must stay legal."""
    args, err = _resolve(_tool({"name": "v", "type": "integer"}), -5)
    assert err is None and args["v"] == -5


# ── pattern ────────────────────────────────────────────────────────────

def test_pattern_must_match_the_WHOLE_value():
    """re.search would let 'rm -rf /; abc' through a '[a-z]+' pattern by
    matching the 'abc'. fullmatch is the only safe reading of a constraint."""
    tool = _tool({"name": "v", "type": "string", "pattern": r"[a-z]+"})
    assert _resolve(tool, "abc")[1] is None
    err = _resolve(tool, "abc; rm -rf /")[1]
    assert err is not None and "pattern" in err


def test_uncompilable_pattern_refuses_rather_than_evaporates():
    """FAIL-CLOSED. A regex that doesn't compile must not be quietly dropped
    — that hands the manifest author a constraint they believe in and do
    not have."""
    tool = _tool({"name": "v", "type": "string", "pattern": "[unclosed"})
    _args, err = _resolve(tool, "anything")
    assert err is not None and "invalid pattern" in err


def test_pattern_is_published_in_the_schema():
    """A rule the caller can't see is a rule it breaks blind."""
    schema = _tool({"name": "v", "type": "string",
                    "pattern": r"\d{4}", "enum": None}).input_schema()
    assert schema["properties"]["v"]["pattern"] == r"\d{4}"


# ── enum ───────────────────────────────────────────────────────────────

def test_enum_is_a_closed_set():
    tool = _tool({"name": "v", "type": "string", "enum": ["day", "week"]})
    assert _resolve(tool, "day")[1] is None
    err = _resolve(tool, "month")[1]
    assert err is not None and "permitted values" in err


def test_enum_compares_after_coercion():
    """The value arrives as a string over JSON but the enum is declared with
    ints; comparing pre-coercion would reject every legal value."""
    tool = _tool({"name": "v", "type": "integer", "enum": [1, 2, 3]})
    assert _resolve(tool, "2")[1] is None
    assert _resolve(tool, "9")[1] is not None


def test_enum_is_published_in_the_schema():
    schema = _tool({"name": "v", "type": "string",
                    "enum": ["a", "b"]}).input_schema()
    assert schema["properties"]["v"]["enum"] == ["a", "b"]


# ── the old range logic, after collapsing two functions into one ───────

def test_numeric_bounds_still_bite():
    """_check_range/_range_error computed the same predicate twice and were
    merged. Merging is where an off-by-one gets to hide, so: regression."""
    tool = _tool({"name": "v", "type": "integer", "min": 1, "max": 10})
    assert _resolve(tool, 5)[1] is None
    assert "below min" in _resolve(tool, 0)[1]
    assert "above max" in _resolve(tool, 11)[1]
    assert _resolve(tool, 1)[1] is None, "min is inclusive"
    assert _resolve(tool, 10)[1] is None, "max is inclusive"


def test_required_and_default_semantics_survived_the_rewrite():
    """_resolve_arguments was restructured; its four-way branch (supplied /
    explicit null / absent-with-default / absent-required) must be intact."""
    req = _tool({"name": "v", "type": "string", "required": True})
    assert "required" in req._resolve_arguments({})[1]
    assert "required" in req._resolve_arguments({"v": None})[1]

    opt = _tool({"name": "v", "type": "string", "default": "dflt"})
    assert opt._resolve_arguments({})[0]["v"] == "dflt"
    assert opt._resolve_arguments({"v": "given"})[0]["v"] == "given"

    bare = _tool({"name": "v", "type": "string"})
    assert bare._resolve_arguments({})[0] == {}, "optional + no default => omitted"


def test_a_default_that_violates_its_own_constraint_is_reported_as_a_default():
    """Otherwise the author hunts for a caller sending bad input that is in
    fact their own manifest."""
    tool = _tool({"name": "v", "type": "string",
                  "enum": ["a"], "default": "z"})
    _args, err = tool._resolve_arguments({})
    assert err is not None and "default" in err


# ── kind=web: header templating ────────────────────────────────────────

def _capture_web_call(headers_yaml: dict, args: dict) -> "httpx.Request":
    """Run one kind=web dispatch against a mock transport, return the request."""
    import httpx
    from seren_workbench.dynamic_tools.web_dispatcher import invoke_web

    seen = {}

    def _handler(request: "httpx.Request") -> "httpx.Response":
        seen["req"] = request
        return httpx.Response(200, text="ok")

    manifest = _dict_to_manifest({
        "tools": [{
            "name": "probe", "invoke": {
                "kind": "web", "method": "GET",
                "base_url": "http://svc.local", "path": "/thing/{v}",
                "headers": headers_yaml,
            },
            "parameters": [{"name": "v", "type": "string"}],
        }],
    })
    entry = manifest.tools[0]
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    asyncio.run(invoke_web(entry.invoke, manifest.configuration, "probe",
                           args, {"v": "string"}, client))
    return seen["req"]


def test_header_values_substitute_their_parameters():
    """They used to be the one templated field that didn't. So
    `Authorization: "Bearer {token}"` sent the literal characters "{token}"
    — no error, no warning, just a 401 from the far end and nothing
    anywhere to explain it."""
    req = _capture_web_call({"Authorization": "Bearer {v}"}, {"v": "s3cr3t"})
    assert req.headers["authorization"] == "Bearer s3cr3t"


def test_header_NAMES_do_not_substitute():
    """Deliberate asymmetry: a templated header name isn't a real use case,
    and allowing it would let a parameter value inject a whole header."""
    req = _capture_web_call({"X-{v}": "static"}, {"v": "Injected"})
    assert "X-{v}" in req.headers
    assert "X-Injected" not in req.headers


def test_path_substitution_still_url_encodes():
    """Regression on the traversal guard next door to the header change."""
    req = _capture_web_call({}, {"v": "../../admin"})
    assert "/thing/..%2F..%2Fadmin" in str(req.url)


# ── the shipped example must actually work ─────────────────────────────

def test_the_example_manifest_loads_clean():
    """examples/tools/example-tool.yaml is what a new operator copies and
    what docs/TOOL-MANIFESTS.md describes. Documentation that doesn't parse
    is worse than no documentation — it costs someone an evening before
    they stop believing it. So the docs are executable."""
    from pathlib import Path
    from seren_workbench.dynamic_tools.manifest_loader import ManifestLoader

    examples = Path(__file__).resolve().parents[1] / "examples" / "tools"
    assert examples.is_dir(), f"example manifests missing at {examples}"

    result = ManifestLoader().load_directory(str(examples))
    assert result.failed_files == [], f"example failed to parse: {result.failed_files}"
    assert result.skipped_tools == [], f"example tool skipped: {result.skipped_tools}"

    names = {e.name for e, _, _ in result.resolved_inline_tools}
    assert names == {
        "example_disk_free", "example_recent_notes", "example_append_note"
    }


def test_the_examples_own_constraints_behave_as_documented():
    """The example advertises a pattern and an enum. Prove the file teaches
    the truth, not just that it parses."""
    from pathlib import Path
    from seren_workbench.dynamic_tools.manifest_loader import ManifestLoader
    from seren_workbench.dynamic_tools.tool_audit_log import ToolAuditLog

    examples = Path(__file__).resolve().parents[1] / "examples" / "tools"
    result = ManifestLoader().load_directory(str(examples))
    tools = {}
    for entry, owner, source in result.resolved_inline_tools:
        tools[entry.name] = YamlDispatchedTool(
            entry=entry, owner=owner, source_path=source,
            http_client=None, audit_log=ToolAuditLog())

    df = tools["example_disk_free"]
    assert df._resolve_arguments({"mount_point": "/mnt/nvme"})[1] is None
    assert df._resolve_arguments({"mount_point": "; rm -rf /"})[1] is not None

    notes = tools["example_recent_notes"]
    assert notes._resolve_arguments({"token": "t", "window": "week"})[1] is None
    assert notes._resolve_arguments({"token": "t", "window": "decade"})[1] is not None
