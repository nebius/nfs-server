#!/usr/bin/env just --justfile

set shell := ["bash", "-euo", "pipefail", "-c"]

[private]
mod scripts "scripts/justfile"

[doc("Generate local release versions, changelogs, and a manifest")]
prepare: (scripts::_tool "python3") (scripts::_tool "changie")
	python3 scripts/prepare_release.py
