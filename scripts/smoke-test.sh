#!/bin/sh
set -eu

# All bundled tools must resolve to Nix-managed executables.
for tool in bash node python3 pnpm uv uvx c2j; do
    case "$(readlink -f "$(command -v "$tool")")" in
        /nix/store/*) ;;
        *) echo "$tool is not installed through Nix" >&2; exit 1 ;;
    esac
done

/bin/bash -c 'test "$((2 + 2))" -eq 4'
nix --version
nix eval --expr '1 + 1' | grep -qx 2
node --version
python3 --version
pnpm --version
uv --version
uvx --version
c2j version
c2j --help >/dev/null

# Confirm pnpm can actually execute Node, not just print its own version.
test_dir=$(mktemp -d)
trap 'rm -rf "$test_dir"' EXIT
printf '{"private":true}\n' > "$test_dir/package.json"
pnpm --dir "$test_dir" exec node -e 'if (process.platform !== "linux") process.exit(1)'

# uv must use a working Nix-provided Python to create virtual environments.
(
    cd "$test_dir"
    uv venv --no-python-downloads .venv
    .venv/bin/python -c 'import sys; assert sys.prefix != sys.base_prefix'
)
