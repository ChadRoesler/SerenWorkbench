# ════════════════════════════════════════════════════════════════════════
#  WebTools - MCP tools for letting the LLM look stuff up on the web.
#
#  SearchTheWeb wraps SearXNG (port 8080 on the NUC).
#  FetchUrl pulls page content for a specific URL.
#
#  WHY SearXNG (not a public search API): no API keys, no rate limits,
#  aggregates ~10 engines, runs in-cluster.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import sys
from typing import Optional

import httpx
from ...tool_config.mcp_config import McpConfig


SEARCH_TOOL_DEFINITION = {
    "name": "search_the_web",
    "description": (
        "Searches the web via SearXNG (a self-hosted metasearch aggregating "
        "DuckDuckGo, Brave, Bing, and others). Use this when the user asks "
        "about current events, recent information, or anything you don't "
        "know from training. Returns JSON with a 'results' array - each "
        "result has 'title', 'url', and 'snippet' (a short excerpt). "
        "Default max_results is 5; raise it (up to 20) for broader sweeps."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query - plain English works fine, no special syntax needed.",
            },
            "max_results": {
                "type": "integer",
                "description": "Max results to return. Default 5, capped at 20.",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}

FETCH_TOOL_DEFINITION = {
    "name": "fetch_url",
    "description": (
        "Fetches the text content of a URL. Use this after search_the_web to "
        "get the full content of a promising result, or when the user "
        "provides a specific URL to summarize. Returns JSON with 'url', "
        "'status_code', 'text' (the page body, truncated to ~8k chars), "
        "'truncated' (bool), and 'original_bytes'. "
        "Private network addresses are refused to prevent SSRF."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full URL to fetch, including http:// or https:// scheme.",
            },
        },
        "required": ["url"],
    },
}


async def search_the_web(
    query: str,
    max_results: int = 5,
    searxng: httpx.AsyncClient = None,
    config: Optional[McpConfig] = None,
    **kwargs,
) -> str:
    if not query:
        return _err("Empty query.", "Provide a search query string.")

    section = config.for_tool("search_the_web") if config else None
    default_results = section.get_int("default_results", 5) if section else 5
    max_results_cap = section.get_int("max_results", 20) if section else 20
    n = max(1, min(max_results if max_results > 0 else default_results, max_results_cap))

    try:
        path = f"/search?q={__import__('urllib.parse').quote(query)}&format=json"
        resp = await searxng.get(path)

        if not resp.is_success:
            body = resp.text
            return _err(
                f"SearXNG returned HTTP {resp.status_code}.",
                body[:500] + "…" if len(body) > 500 else body,
            )

        data = resp.json()
        results = []
        for r in data.get("results", []):
            if len(results) >= n:
                break
            results.append({
                "title": r.get("title"),
                "url": r.get("url"),
                "snippet": r.get("content"),
            })

        return json.dumps({
            "query": query,
            "results": results,
            "result_count": len(results),
        }, indent=2)

    except httpx.RequestError as ex:
        return _err(f"SearXNG unreachable: {ex}", "Check that searxng containers are running.")
    except httpx.TimeoutException:
        return _err("SearXNG timed out after 15 seconds.", "Slow upstream engine.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"SearXNG returned malformed JSON: {ex}", "Check format=json is enabled.")


async def fetch_url(
    url: str,
    searxng: httpx.AsyncClient = None,
    config: Optional[McpConfig] = None,
    **kwargs,
) -> str:
    if not url:
        return _err("Empty URL.", "Provide a full URL including the scheme.")

    from urllib.parse import urlparse
    parsed = urlparse(url)
    if not parsed.scheme or parsed.scheme not in ("http", "https"):
        return _err(f"Invalid URL: {url}", "Must be an absolute http:// or https:// URL.")

    # -- SSRF guard --
    ssrf_err = await _check_ssrf(parsed)
    if ssrf_err:
        return ssrf_err

    try:
        resp = await searxng.get(url)

        content_type = resp.headers.get("content-type", "unknown")
        body = resp.text

        section = config.for_tool("fetch_url") if config else None
        max_chars = section.get_int("max_chars", 8_000) if section else 8_000
        truncated = len(body) > max_chars
        text = body[:max_chars] if truncated else body
        original_bytes = len(body)

        print(
            f"[mcp-audit] FetchUrl: url={url} status={resp.status_code} bytes={original_bytes}",
            file=sys.stderr,
        )

        return json.dumps({
            "url": url,
            "status_code": resp.status_code,
            "content_type": content_type,
            "truncated": truncated,
            "original_bytes": original_bytes,
            "text": text,
        }, indent=2)

    except httpx.RequestError as ex:
        return _err(f"Could not fetch {url}: {ex}", "URL may be down or blocking us.")
    except httpx.TimeoutException:
        return _err(f"Fetching {url} timed out after 15 seconds.", "Target site is slow.")


# -- SSRF helpers --

async def _check_ssrf(parsed) -> Optional[str]:
    import socket
    try:
        addrs = await _resolve_hostname(parsed.hostname)
    except Exception as ex:
        return _err(f"DNS lookup failed for {parsed.hostname}: {ex}", "Host could not be resolved.")

    if not addrs:
        return _err(f"DNS returned no addresses for {parsed.hostname}.", "Cannot fetch a host with no IPs.")

    for addr in addrs:
        if _is_blocked_address(addr):
            return _err(
                f"Refused: {parsed.hostname} resolves to a blocked address ({addr}).",
                "FetchUrl is restricted to public internet targets.",
            )
    return None


async def _resolve_hostname(hostname: str) -> list:
    import socket
    loop = asyncio.get_event_loop()
    addrs = await loop.getnameinfo((hostname, 0), socket.AI_CANONNAME)
    # Use socket's getaddrinfo for resolution
    infos = await loop.run_in_executor(None, socket.getaddrinfo, hostname, None)
    return [info[4][0] for info in infos]


def _is_blocked_address(addr: str) -> bool:
    import ipaddress
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True

    if ip.is_loopback:
        return True
    if ip.is_private:
        return True
    if ip.is_link_local:
        return True
    if ip.is_multicast:
        return True
    if ip.is_unspecified:
        return True
    # CGNAT range 100.64.0.0/10
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in ipaddress.IPv4Network("100.64.0.0/10", strict=False):
            return True
    return False


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
