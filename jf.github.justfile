#!/usr/bin/env just --justfile

set shell := ["bash", "-euo", "pipefail", "-c"]

[private]
mod scripts "scripts/justfile"

[doc("Lint GitHub workflows with actionlint and ShellCheck")]
@lint: (scripts::_tool "actionlint") (scripts::_tool "shellcheck")
	actionlint
