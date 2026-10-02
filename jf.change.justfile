#!/usr/bin/env just --justfile

set shell := ["bash", "-euo", "pipefail", "-c"]

# region Tools

[doc('Check if Changie is installed')]
[private]
@tool-changie:
	if ! command -v changie >/dev/null 2>&1; then \
		echo 'Changie is missing'; \
		echo 'Go to https://changie.dev/guide/installation/ for installation instructions'; \
		exit 1; \
	fi;

# endregion Tools

[doc("Add a Changie fragment for an image or chart change")]
@new: tool-changie
	changie new

[doc("Merge all per-project versions into one changelog")]
@merge: tool-changie
	changie merge
