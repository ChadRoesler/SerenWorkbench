# ════════════════════════════════════════════════════════════════════════
#  ParamSubst - shared {param} substitution helpers.
#
#  Three modes:
#    substitute_scalar             - plain string replace, optional URL-encode.
#    substitute_scalar(url_enc)    - same, with each value URL-encoded.
#    substitute_json_body          - type-aware: string params get JSON-escaped
#                                    (template provides the quotes), non-string
#                                    params substitute literal.
#
#  All three use the same {name} token regex.
#
#  Validation against unknown tokens: a {something} that doesn't match any
#  parameter is LEFT IN PLACE for scalar substitution. For JSON body,
#  unknown tokens raise an exception.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import re
from typing import Dict

_TOKEN_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def substitute_scalar(
    template: str,
    args: Dict[str, object],
    url_encode: bool = False,
) -> str:
    """Plain scalar substitution. Unknown tokens left intact."""
    if not template:
        return template or ""

    def _replacer(m: re.Match) -> str:
        key = m.group(1)
        raw = args.get(key)
        if raw is None:
            return m.group(0)  # leave token intact
        s = _to_invariant_string(raw)
        return __import__("urllib.parse").quote(s, safe="") if url_encode else s

    return _TOKEN_RE.sub(_replacer, template)


def substitute_json_body(
    template: str,
    args: Dict[str, object],
    param_types: Dict[str, str],
) -> str:
    """Type-aware substitution for a JSON body template.

    String params are JSON-escaped (without surrounding quotes - template
    provides those). Non-string params substitute literal.
    Unknown tokens raise ValueError.
    """
    if not template:
        return template or ""

    result = []
    last = 0
    for m in _TOKEN_RE.finditer(template):
        result.append(template[last : m.start()])
        key = m.group(1)
        if key not in args:
            raise ValueError(f"unknown parameter '{{{key}}}' in body_template")
        raw = args[key]
        ptype = param_types.get(key, "string")
        result.append(_format_json_value(raw, ptype))
        last = m.end()
    result.append(template[last:])
    return "".join(result)


def _format_json_value(raw: object, ptype: str) -> str:
    if raw is None:
        return '""' if ptype == "string" else "null"

    if ptype == "integer":
        return str(int(raw))
    elif ptype == "number":
        return repr(float(raw))
    elif ptype == "boolean":
        return "true" if bool(raw) else "false"
    else:
        # String type - JSON-escape for embedding inside quotes the template provides
        return _json_escape_inner(str(raw))


def _json_escape_inner(s: str) -> str:
    """JSON-escape a string, then trim the surrounding quotes the serializer adds."""
    encoded = json.dumps(s, ensure_ascii=False)
    # json.dumps wraps the string in quotes; the template already provides those.
    if encoded.startswith('"') and encoded.endswith('"'):
        return encoded[1:-1]
    return encoded


def _to_invariant_string(raw: object) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, (int, float)):
        return str(raw)
    return str(raw) or ""
