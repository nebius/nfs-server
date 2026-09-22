#!/usr/bin/env just --justfile

[doc("Format Just files")]
fmt:
	#!/usr/bin/env sh
	for file in `find . -type f -name '*justfile'`; do
		just --fmt --indentation '	' --justfile ${file}
	done
