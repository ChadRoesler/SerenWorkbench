"""
Tests for ToolRegistry — the enable/disable state manager.

Directly tests the registry without going through HTTP. Follows the same
pattern as SerenMemory/tests/test_mcp_tools.py (direct impl testing).
"""
from __future__ import annotations

from seren_workbench.tool_registry import ToolInfo, ToolRegistry, build_registry


def _sample_tools() -> list[ToolInfo]:
    """Return a small list of ToolInfo for isolated registry tests."""
    return [
        ToolInfo(name="remember", description="Save to memory", type="builtin",
                 source="models/tools/memory_tools.py",
                 actions=[{"name": "remember", "description": "write short-term"}]),
        ToolInfo(name="recall", description="Search memory", type="builtin",
                 source="models/tools/memory_tools.py",
                 actions=[{"name": "recall", "description": "search short-term"}]),
        ToolInfo(name="forget", description="Delete memory", type="builtin",
                 source="models/tools/memory_tools.py",
                 actions=[{"name": "forget", "description": "delete entry"}]),
    ]


# ── Construction ────────────────────────────────────────────────────────

def test_registry_initialises_all_enabled():
    tools = _sample_tools()
    reg = ToolRegistry(tools, [])
    for t in tools:
        assert reg.is_enabled(t.name) is True


def test_build_registry_returns_24_builtins():
    """The FULL builtin surface — 16 suffixed defs + the 8 recovered from
    the bare-TOOL_DEFINITION discovery bug."""
    reg = build_registry()
    assert len(reg._builtin) >= 24
    assert len(reg._dynamic) == 0


def test_build_registry_seeds_disabled_list():
    """DashboardConfig.tools_disabled must actually bite at startup — it was
    a dead config field before this pass."""
    reg = build_registry(tools_disabled=["fetch_url", "search_the_web"])
    assert reg.is_enabled("fetch_url") is False
    assert reg.is_enabled("search_the_web") is False
    assert reg.is_enabled("remember") is True


def test_build_registry_enabled_list_is_allowlist():
    """Non-empty tools_enabled = allowlist: everything else starts disabled."""
    reg = build_registry(tools_enabled=["remember", "recall"])
    assert reg.is_enabled("remember") is True
    assert reg.is_enabled("recall") is True
    assert reg.is_enabled("forget") is False
    assert reg.is_enabled("get_current_time") is False


# ── Enable/Disable Tools ────────────────────────────────────────────────

def test_disable_tool():
    tools = _sample_tools()
    reg = ToolRegistry(tools, [])
    assert reg.disable_tool("remember") is True
    assert reg.is_enabled("remember") is False
    assert reg.is_enabled("recall") is True  # other tools unaffected


def test_disable_nonexistent_tool():
    tools = _sample_tools()
    reg = ToolRegistry(tools, [])
    assert reg.disable_tool("nonexistent") is False


def test_enable_tool():
    tools = _sample_tools()
    reg = ToolRegistry(tools, [])
    reg.disable_tool("remember")
    assert reg.enable_tool("remember") is True
    assert reg.is_enabled("remember") is True


def test_enable_nonexistent_tool():
    tools = _sample_tools()
    reg = ToolRegistry(tools, [])
    assert reg.enable_tool("nonexistent") is False


# ── Enable/Disable Actions ──────────────────────────────────────────────

def test_disable_action():
    tools = _sample_tools()
    reg = ToolRegistry(tools, [])
    assert reg.disable_action("remember", "remember") is True
    assert reg.is_action_enabled("remember", "remember") is False


def test_disable_nonexistent_action():
    tools = _sample_tools()
    reg = ToolRegistry(tools, [])
    assert reg.disable_action("remember", "nonexistent") is False


def test_enable_action():
    tools = _sample_tools()
    reg = ToolRegistry(tools, [])
    reg.disable_action("remember", "remember")
    assert reg.enable_action("remember", "remember") is True
    assert reg.is_action_enabled("remember", "remember") is True


def test_action_state_independent_of_tool():
    """Disabling an action should not affect the tool-level state."""
    tools = _sample_tools()
    reg = ToolRegistry(tools, [])
    reg.disable_action("remember", "remember")
    assert reg.is_enabled("remember") is True  # tool still enabled


# ── Snapshot ────────────────────────────────────────────────────────────

def test_snapshot_reflects_tool_state():
    tools = _sample_tools()
    reg = ToolRegistry(tools, [])
    reg.disable_tool("remember")
    snap = reg.snapshot()
    for t in snap["tools"]:
        if t["name"] == "remember":
            assert t["enabled"] is False
        else:
            assert t["enabled"] is True


def test_snapshot_reflects_action_state():
    tools = _sample_tools()
    reg = ToolRegistry(tools, [])
    reg.disable_action("remember", "remember")
    snap = reg.snapshot()
    for t in snap["tools"]:
        if t["name"] == "remember":
            for a in t["actions"]:
                if a["name"] == "remember":
                    assert a["enabled"] is False


# ── All-tools list ──────────────────────────────────────────────────────

def test_all_tools_applies_current_enabled():
    tools = _sample_tools()
    reg = ToolRegistry(tools, [])
    reg.disable_tool("remember")
    all_t = reg.all_tools()
    for t in all_t:
        if t.name == "remember":
            assert t.enabled is False
        else:
            assert t.enabled is True


def test_get_tool_returns_none_for_missing():
    tools = _sample_tools()
    reg = ToolRegistry(tools, [])
    assert reg.get_tool("nonexistent") is None


def test_get_tool_finds_by_name():
    tools = _sample_tools()
    reg = ToolRegistry(tools, [])
    t = reg.get_tool("remember")
    assert t is not None
    assert t.name == "remember"
