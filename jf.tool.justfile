#!/usr/bin/env just --justfile

set shell := ["bash", "-euo", "pipefail", "-c"]

[doc("Check that a command is installed")]
[private]
@tool name:
	if ! command -v {{ quote(name) }} >/dev/null 2>&1; then \
		printf '%s is missing. Install %s with your package manager (on macOS, run brew bundle from the repository root).\n' {{ quote(name) }} {{ quote(if name == "cr" { "chart-releaser" } else { name }) }} >&2; \
		exit 1; \
	fi
