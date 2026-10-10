#!/bin/sh
set -eu

# Realise a known cached output in an empty store. The bundled store must not
# satisfy the request, and the runtime configuration must allow substitution.
test_dir=$(mktemp -d)
trap 'rm -rf "$test_dir"' EXIT
store="local?root=$test_dir"
package=$(dirname "$(dirname "$(readlink -f "$(command -v which)")")")
nix-store --store "$store" --realise "$package"
test -x "$test_dir$package/bin/which"
nix-store --store "$store" --verify --check-contents
