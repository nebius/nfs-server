#!/usr/bin/env just --justfile

set shell := ["bash", "-euo", "pipefail", "-c"]
set working-directory := ".."

import "tool.justfile"

[doc("Lint GitHub workflows with actionlint and ShellCheck")]
lint: (tool "actionlint") (tool "shellcheck")
	actionlint -shellcheck='shellcheck'
