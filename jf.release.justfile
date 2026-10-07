#!/usr/bin/env just --justfile

set shell := ["bash", "-euo", "pipefail", "-c"]
set positional-arguments

import "jf.tool.justfile"

[doc("Generate local release versions, changelogs, and a manifest")]
prepare: (tool "python3") (tool "changie")
	python3 scripts/prepare_release.py

[doc("Push a prepared image to the registry and link it in a GitHub release")]
publish-image *args: (tool "python3") (tool "docker") (tool "gh")
	python3 scripts/publish_image.py "$@"

[doc("Publish a prepared chart to OCI, GitHub Releases, and the Pages index")]
publish-chart *args: (tool "python3") (tool "helm") (tool "git") (tool "gh") (tool "cr")
	python3 scripts/publish_chart.py "$@"
