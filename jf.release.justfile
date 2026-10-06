#!/usr/bin/env just --justfile

set shell := ["bash", "-euo", "pipefail", "-c"]
set positional-arguments

[private]
mod scripts "scripts/justfile"

[doc("Generate local release versions, changelogs, and a manifest")]
prepare: (scripts::_tool "python3") (scripts::_tool "changie")
	python3 scripts/prepare_release.py

[doc("Push a prepared image to the registry and link it in a GitHub release")]
publish-image *args: (scripts::_tool "python3") (scripts::_tool "docker") (scripts::_tool "gh")
	python3 scripts/publish_image.py "$@"
