#!/usr/bin/env just --justfile

set shell := ["bash", "-uc"]
set lazy

[doc("Docker image recipes")]
mod docker

[doc("Helm chart recipes")]
mod helm

[doc("Justfile management recipes")]
mod just "jf.just.justfile"

[doc("Changes management recipes")]
mod change "jf.change.justfile"

[private]
@default:
	just --choose
