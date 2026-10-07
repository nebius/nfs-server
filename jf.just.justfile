#!/usr/bin/env just --justfile

set shell := ["bash", "-euo", "pipefail", "-c"]

[doc("Format Just files")]
@fmt:
	for file in `find . -type f -name '*justfile'`; do \
		just --fmt --indentation $'\t' --justfile "${file}"; \
	done

[doc("Check Just file formatting without rewriting files")]
@lint:
	for file in `find . -type f -name '*justfile'`; do \
		just --fmt --check --indentation $'\t' --justfile "${file}"; \
	done
