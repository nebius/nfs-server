#!/usr/bin/env just --justfile

set shell := ["bash", "-euo", "pipefail", "-c"]
set working-directory := ".."

import "tool.justfile"

[doc("Add a Changie fragment for an image or chart change")]
@new: (tool "changie")
	changie new

[doc("Merge all per-project versions into one changelog")]
@merge: (tool "changie")
	changie merge
