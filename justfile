#!/usr/bin/env just --justfile

set shell := ["bash", "-uc"]
set lazy

import ".justfiles/tool.justfile"

[doc("Docker image recipes")]
mod docker

[doc("Helm chart recipes")]
mod helm

[doc("Justfile management recipes")]
mod just ".justfiles/just.justfile"

[doc("Changes management recipes")]
mod change ".justfiles/changie.justfile"

[doc("GitHub workflow recipes")]
mod github ".justfiles/github.justfile"

[default]
[private]
@default:
	just --choose

[doc("Format all")]
fmt: just::fmt

[doc("Lint all")]
lint: docker::lint helm::lint github::lint just::lint

[doc("Test all")]
test: docker::test helm::test

[doc("Lint and test all")]
testolint: lint test
