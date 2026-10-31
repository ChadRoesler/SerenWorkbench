"""`local_config: <name>` - a manifest reviewed in one place, deployed in another.

The manifest is the contract and carries no address and no pointer into
this box's environment; <tool_config_dir>/<name>.yaml is the deployment,
read per call, so an operator adds or rotates a tool's token by editing a
file and never restarts the service (31 Oct 2026, the design call).
"""
from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from seren_workbench.config import DashboardConfig
from seren_workbench.dynamic_tools.manifest_loader import _dict_to_manifest
from seren_workbench.dynamic_tools.web_dispatcher import invoke_web, load_local_config


def _manifest(local_config: str, **cfg):
    return _dict_to_manifest({
        "configuration": {"local_config": local_config, **cfg},
        "tools": [{
            "name": "probe",
            "invoke": {"kind": "web", "method": "GET", "path": "/thing"},
            "parameters": [],
        }],
    })


def _call(manifest, tool_config_dir):
    seen = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["req"] = request
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    result = asyncio.run(invoke_web(manifest.tools[0].invoke, manifest.configuration, "probe",
                                    {}, {}, client, tool_config_dir=str(tool_config_dir)))
    return result, seen.get("req")


def test_the_file_supplies_address_and_bearer_and_is_read_per_call(tmp_path):
    manifest = _manifest("sdb")                       # no base_url, no pointer in the manifest
    (tmp_path / "sdb.yaml").write_text(
        "base_url: http://table.local:7440\nbearer_token: first\n", encoding="utf-8")
    result, req = _call(manifest, tmp_path)
    assert not result.get("is_error"), result
    assert str(req.url) == "http://table.local:7440/thing"
    assert req.headers["Authorization"] == "Bearer first"
    # rotate: the next call sees the new token with nothing reloaded
    (tmp_path / "sdb.yaml").write_text(
        "base_url: http://table.local:7440\nbearer_token: second\n", encoding="utf-8")
    _, req = _call(manifest, tmp_path)
    assert req.headers["Authorization"] == "Bearer second"


def test_the_file_wins_over_the_manifest_and_env_pointer_still_works(tmp_path, monkeypatch):
    manifest = _manifest("sdb", base_url="http://wrong.local", bearer_token="stale")
    monkeypatch.setenv("SDB_TEST_TOKEN", "from-env")
    (tmp_path / "sdb.yaml").write_text(
        "base_url: http://right.local\nbearer_token_env: SDB_TEST_TOKEN\n", encoding="utf-8")
    cfg, err = load_local_config(manifest.configuration, str(tmp_path))
    assert err is None and cfg.base_url == "http://right.local"
    # resolve_token ranks an inline literal over an env pointer, so the file's
    # credential has to REPLACE the manifest's, not sit beside it
    assert cfg.bearer_token is None and cfg.bearer_token_env == "SDB_TEST_TOKEN"
    _, req = _call(manifest, tmp_path)
    assert str(req.url).startswith("http://right.local")
    assert req.headers["Authorization"] == "Bearer from-env"
    assert manifest.configuration.bearer_token == "stale", "the parsed manifest itself is never mutated"


def test_a_missing_file_names_the_path_and_no_call_is_made(tmp_path):
    result, req = _call(_manifest("absent"), tmp_path)
    assert result["is_error"] and req is None
    text = result["content"][0]["text"]
    assert os.path.join(str(tmp_path), "absent.yaml") in text


@pytest.mark.parametrize("bad", ["../secrets", "a/b", ".hidden", "", "x" * 80])
def test_a_name_is_a_file_stem_never_a_path(tmp_path, bad):
    manifest = _manifest(bad or "x")
    manifest.configuration.local_config = bad
    cfg, err = load_local_config(manifest.configuration, str(tmp_path))
    if bad == "":
        assert err is None and cfg is manifest.configuration, "blank means not set"
    else:
        assert err and "not a name" in err and cfg is None


def test_only_the_four_keys_may_be_set_there(tmp_path):
    (tmp_path / "sdb.yaml").write_text("base_url: http://x\ncwd: /tmp\n", encoding="utf-8")
    cfg, err = load_local_config(_manifest("sdb").configuration, str(tmp_path))
    assert cfg is None and "cwd" in err


def test_no_local_config_means_the_manifest_block_untouched(tmp_path):
    manifest = _dict_to_manifest({"configuration": {"base_url": "http://a"}, "tools": []})
    cfg, err = load_local_config(manifest.configuration, str(tmp_path))
    assert err is None and cfg is manifest.configuration


def test_reviewer_label_says_local_never_the_value():
    assert _manifest("sdb").configuration.credential_label() == "local:sdb"
    assert _manifest("sdb", bearer_token="x").configuration.credential_label().startswith("local:sdb (and")


def test_tool_config_dir_defaults_beside_tools_dir():
    d = DashboardConfig(tools_dir="/srv/wren/stores/workbench/tools")
    assert d.resolve_tool_config_dir().replace("\\", "/") == "/srv/wren/stores/workbench/customToolConfig"
    assert DashboardConfig.from_dict({"tool_config_dir": "/elsewhere"}).resolve_tool_config_dir() == "/elsewhere"
