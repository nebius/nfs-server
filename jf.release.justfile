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

[doc("Publish a prepared chart to OCI, GitHub Releases, and the Pages index")]
publish-chart *args: (scripts::_tool "python3") (scripts::_tool "helm") (scripts::_tool "git") (scripts::_tool "gh") (scripts::_tool "cr")
	python3 scripts/publish_chart.py "$@"
