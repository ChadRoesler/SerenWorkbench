# ════════════════════════════════════════════════════════════════════════
#  ProcessDispatcher - runs a kind=process tool by spawning a subprocess.
#
#  THE SHELL-SAFETY MOVE
#
#  argv is a LIST. Each {paramName} token inside ANY element substitutes
#  to that parameter's stringified value, in place. The substituted string
#  lands as ONE argv element regardless of what it contains. Because we
#  spawn via subprocess.run with a list (not shell=True), classic CWE-78
#  is structurally closed.
#
#  TIMEOUTS - default 30s, override per-tool via invoke.timeout_seconds.
#
#  STDOUT/STDERR - stdout becomes the tool's result text (truncated if huge).
#  stderr is logged to stderr but NOT returned to the LLM.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import sys
from typing import Dict, Optional

from .manifest_models import ManifestConfiguration, ToolInvoke
from .param_subst import substitute_scalar

DEFAULT_TIMEOUT_SECONDS = 30
MAX_STDOUT_CHARS = 16_000


def invoke_process(
    invoke: ToolInvoke,
    file_config: ManifestConfiguration | None,
    tool_name: str,
    args: Dict[str, object],
) -> dict:
    """Spawn the configured argv with parameter substitution.

    Returns a dict suitable as an MCP CallToolResult:
      {"content": [{"type": "text", "text": ...}], "is_error": bool}
    """
    if not invoke.argv:
        return _error("tool has no invoke.argv list - can't run.")

    # Resolve cwd
    cwd = invoke.cwd or (file_config.cwd if file_config else None)
    timeout_sec = invoke.timeout_seconds if invoke.timeout_seconds and invoke.timeout_seconds > 0 else DEFAULT_TIMEOUT_SECONDS

    # Substitute params into each argv element individually
    substituted = [substitute_scalar(slot, args) for slot in invoke.argv]
    if not substituted:
        return _error("argv resolved to empty.")

    import subprocess
    import shlex

    proc = None
    try:
        proc = subprocess.Popen(
            substituted,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except Exception as ex:
        return _error(
            f"failed to start '{substituted[0]}': {ex}",
            hint="Check that the binary exists and is executable, and that cwd is correct.",
        )

    try:
        outs, errs = proc.communicate(timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        proc.wait()
        return _error(
            f"tool '{tool_name}' timed out after {timeout_sec}s.",
            hint="Bump invoke.timeout_seconds in the manifest if the tool genuinely needs more.",
        )

    out_text = outs.decode("utf-8", errors="replace") if outs else ""
    err_text = errs.decode("utf-8", errors="replace") if errs else ""

    truncated = False
    if len(out_text) > MAX_STDOUT_CHARS:
        out_text = out_text[:MAX_STDOUT_CHARS] + "\n…[stdout truncated]"
        truncated = True

    if err_text:
        print(
            f"[mcp-audit] {tool_name} stderr: {_truncate(err_text, 500)}",
            file=sys.stderr,
        )

    if proc.returncode != 0:
        tail = _truncate(err_text if err_text else out_text, 400)
        return _error(
            f"tool '{tool_name}' exited {proc.returncode}.",
            hint=f"stderr tail: {tail}",
        )

    return {
        "content": [{"type": "text", "text": out_text if out_text else "(no output)"}],
    }


def _truncate(s: str, max_len: int) -> str:
    return s if len(s) <= max_len else s[:max_len] + "…"


def _error(msg: str, hint: str | None = None) -> dict:
    text = f"{msg}\nhint: {hint}" if hint else msg
    return {"is_error": True, "content": [{"type": "text", "text": text}]}
