# Mirrors the two CI matrix legs locally.
# Requires: make (Git for Windows ships it; or: choco install make / scoop install make)
#
# Targets:
#   make test         - Python tests in a fresh venv (.[dev])
#   make clean        - remove any leftover venvs
#
# NOTE vs the SCC/memory Makefiles this descends from: SerenWorkbench has NO
# extras matrix. mcp is a CORE dep (an MCP server without its protocol SDK is
# a dashboard with no purpose), there's no [corp] or [vector] extra, and no
# VS Code extension lives in this repo — so the single `test` target IS the
# whole local surface, matching CI's single extras=dev leg.
#
# The target creates a fresh isolated venv, runs tests, then removes it.
# Venvs are also gitignored as a belt-and-suspenders safety net.

SHELL        := pwsh.exe
.SHELLFLAGS  := -NoProfile -NonInteractive -Command

PKG_DIR    := SerenWorkbench
VENV_BASE  := .venv-base

.PHONY: test clean

test:
	Remove-Item -Recurse -Force $(VENV_BASE) -ErrorAction SilentlyContinue; \
	python -m venv $(VENV_BASE); \
	$$env:SETUPTOOLS_SCM_PRETEND_VERSION='0.0.0'; \
	.\.venv-base\Scripts\pip.exe install -e "$(PKG_DIR)/.[dev]"; \
	.\.venv-base\Scripts\python.exe -m pytest $(PKG_DIR)/tests/ -v; \
	$$status=$$LASTEXITCODE; \
	Remove-Item -Recurse -Force $(VENV_BASE) -ErrorAction SilentlyContinue; \
	exit $$status

clean:
	Remove-Item -Recurse -Force $(VENV_BASE) -ErrorAction SilentlyContinue
