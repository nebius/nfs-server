#!/usr/bin/env just --justfile

set shell := ["bash", "-uc"]
set lazy

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

[private]
@default:
	just --choose
