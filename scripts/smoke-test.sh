#!/bin/sh
set -eu

# All bundled tools must resolve to Nix-managed executables.
for tool in nix bash ls find grep sed awk diff patch which file less tree \
    rg fd fzf bat git git-lfs gh ssh curl wget dig ping ip nc jq yq \
    zip unzip tar gzip xz zstd make gcc g++ pkg-config cmake \
    node pnpm python3 uv uvx ruff sqlite3 shellcheck shfmt entr \
    ps pstree lsof strace htop rsync c2j; do
    command -v "$tool" >/dev/null
    case "$(readlink -f "$(command -v "$tool")")" in
        /nix/store/*) ;;
        *) echo "$tool is not installed through Nix" >&2; exit 1 ;;
    esac
done

/bin/bash -c 'test "$((2 + 2))" -eq 4'
nix --version
nix eval --expr '1 + 1' | grep -qx 2
git --version
node --version
node -e 'if (process.versions.node.split(".")[0] !== "24") process.exit(1)'
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

# Git must support creating and reading a local commit.
git -C "$test_dir" init --quiet
git -C "$test_dir" add package.json
git -C "$test_dir" -c user.name='Smoke test' -c user.email='smoke@example.invalid' \
    commit --quiet -m 'Smoke test'
test "$(git -C "$test_dir" show HEAD:package.json)" = '{"private":true}'
git lfs version
gh --version
curl --version
test -s "$SSL_CERT_FILE"
printf '{"ok":true}\n' | jq -e .ok >/dev/null
printf 'ok: true\n' | yq -e .ok >/dev/null
test "$(sqlite3 :memory: 'select 2 + 2;')" = 4

# Exercise the compiler wrappers, headers, linker, CMake, and Make in the
# assembled image. Nix builds stay disabled; direct project builds must work.
mkdir "$test_dir/compile"
cat > "$test_dir/compile/CMakeLists.txt" <<'EOF'
cmake_minimum_required(VERSION 3.16)
project(smoke LANGUAGES C CXX)
add_executable(hello-c hello.c)
add_executable(hello-cxx hello.cc)
EOF
printf '#include <stdio.h>\nint main(void) { puts("hello-c"); return 0; }\n' > "$test_dir/compile/hello.c"
printf '#include <iostream>\nint main() { std::cout << "hello-cxx" << std::endl; }\n' > "$test_dir/compile/hello.cc"
cmake -S "$test_dir/compile" -B "$test_dir/compile/build" -G 'Unix Makefiles'
cmake --build "$test_dir/compile/build"
test "$("$test_dir/compile/build/hello-c")" = hello-c
test "$("$test_dir/compile/build/hello-cxx")" = hello-cxx

# uv must use a working Nix-provided Python to create virtual environments.
(
    cd "$test_dir"
    uv venv --no-python-downloads .venv
    .venv/bin/python -c 'import sys; assert sys.prefix != sys.base_prefix'
)

# Reject cache misses, including derivations that bypass max-jobs with
# preferLocalBuild. These builders would succeed if local builds were enabled.
for prefer_local in false true; do
    if nix-build --no-out-link --option substituters '' --expr "
        derivation {
            name = \"base-smoke-no-build-$prefer_local\";
            system = builtins.currentSystem;
            builder = \"/bin/sh\";
            args = [ \"-c\" \"echo unexpected-build > \$out\" ];
            preferLocalBuild = $prefer_local;
        }
    " > "$test_dir/build.log" 2>&1; then
        echo 'Nix unexpectedly allowed a local build' >&2
        exit 1
    fi
    if [ "$prefer_local" = true ]; then
        grep -q 'local builds are disabled' "$test_dir/build.log"
    else
        grep -q 'max-jobs' "$test_dir/build.log"
    fi
done
