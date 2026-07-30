# SerenWorkbench

The tool surface an LLM reaches through — an MCP server, an operator
dashboard and a plug-and-play YAML tool loader, in one process on port
7425.

```bash
pip install seren-workbench
python -m seren_workbench
```

Builtin tools cover memory, web search, time, cluster control and the
scheduler. Your own tools are YAML files you drop in a directory and
reload — no Python, no restart.

- **[SerenWorkbench/README.md](SerenWorkbench/README.md)** — install, connect a client, endpoints
- **[docs/TOOL-MANIFESTS.md](SerenWorkbench/docs/TOOL-MANIFESTS.md)** — writing a tool, the whole format
- **[examples/tools/example-tool.yaml](SerenWorkbench/examples/tools/example-tool.yaml)** — an annotated file to copy

Part of the [Seren](https://github.com/ChadRoesler) stack. It doesn't
require the rest of it; point it at whichever services you actually run.

## Layout

```
SerenWorkbench/                 <- this repo: .slnx, LICENSE, CI
  SerenWorkbench/               <- the Python package
    seren_workbench/
    docs/  examples/  tests/
    pyproject.toml
```

Nested on purpose — same family layout as seren-memory and seren-loci, so
the repo root can hold the solution file and the workflows while the
package stays a clean `pip install -e .` target.

## Development

```bash
cd SerenWorkbench
pip install -e ".[dev]"
pytest -q
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
