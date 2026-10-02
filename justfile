#!/usr/bin/env just --justfile

set shell := ["bash", "-uc"]
set lazy

[doc("Docker image recipes")]
mod docker

[doc("Helm chart recipes")]
mod helm

[doc("Justfile management recipes")]
mod just "just.justfile"

[private]
@default:
	just --choose
