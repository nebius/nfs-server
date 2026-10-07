#!/usr/bin/env just --justfile

set shell := ["bash", "-uc"]
set lazy

import "jf.tool.justfile"

[doc("Docker image recipes")]
mod docker

[doc("Helm chart recipes")]
mod helm

[doc("Local release preparation scripts")]
mod scripts "scripts/justfile"

[doc("Justfile management recipes")]
mod just "jf.just.justfile"

[doc("Changes management recipes")]
mod change "jf.change.justfile"

[doc("Release management recipes")]
mod release "jf.release.justfile"

[doc("GitHub workflow recipes")]
mod github "jf.github.justfile"

[default]
[private]
@default:
	just --choose

[doc("Format all")]
fmt: scripts::fmt just::fmt

[doc("Lint all")]
lint: docker::lint helm::lint scripts::lint github::lint just::lint

[doc("Test all")]
test: docker::test helm::test scripts::test

[doc("Lint and test all")]
testolint: lint test
