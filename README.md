# Colony 2 base2 image

`ghcr.io/colony-2/base2:latest` is the Ubuntu 26.04 shared-store image for
**linux/amd64 and linux/arm64**. It includes Bash, Nix, Node.js **24**, Python,
pnpm, uv/uvx, c2j, and the development tools below. It runs as root, opens Bash
by default, and uses `/workspace` as its working directory.

All base tools and Nix's own runtime live outside `/nix`. The image ships an
**empty `/nix`**. Pulse can attach one shared Docker local volume there, and
containers execute installed `/nix/store/...` paths directly, including after
the producer is removed. There is no image-store seed, per-container restoration,
export hook, Nix wrapper, daemon, or application-wide lock.

This repository publishes `ghcr.io/colony-2/base2:latest`. The separate image name
distinguishes its shared-store API from the existing `ghcr.io/colony-2/base:latest`
contract.
See [the Pulse integration contract](docs/shared-nix-store.md).

| Category | Tools |
| --- | --- |
| Core utilities | Bash, coreutils, findutils, grep, sed, awk, diff, patch, which, file, less, tree |
| Search and navigation | ripgrep, fd, fzf, bat |
| Git and GitHub | Git, Git LFS, gh, OpenSSH client |
| HTTP and networking | curl, wget, CA certificates, DNS tools, ping, iproute2, OpenBSD netcat |
| JSON and YAML | jq, Mike Farah yq |
| Archives | zip, unzip, tar, gzip, xz, zstd |
| Build tools | GNU Make, GCC (C and C++), pkg-config, CMake |
| JavaScript | Node.js 24, npm/npx, pnpm/pnpx |
| Python | Python 3, venv/development support, uv/uvx, Ruff |
| Databases | SQLite |
| Shell development | ShellCheck, shfmt, entr |
| Process inspection | procps, psmisc, lsof, strace, htop |
| File synchronization | rsync |

[APT packages](apt/packages.txt) supply most tools. Node 24 uses official release
archives; pnpm uses the npm package and its matching native executable when
applicable; uv, Ruff, yq and c2j use official release artifacts. All external
artifacts are resolved and checksummed before installation. No first-use
bootstrap is needed. `fd` and `bat` expose Ubuntu's `fdfind` and `batcat` names.

Nix is pinned to **2.34.3**, Ubuntu package **2.34.3+dfsg-1**, in
[apt/nix-version](apt/nix-version). Its executable, libraries, configuration,
certificates, registry and build-denial hook all work with an empty `/nix` mount.
The final filesystem retains Ubuntu's package database and APT tooling.

## Runtime policy

Nix accesses the store directly (`NIX_REMOTE=local`) and fetches prebuilt outputs
from `https://cache.nixos.org` and `https://colony2.cachix.org`, with their signing
keys and signature verification enabled. Local and remote Nix builds are disabled,
including `preferLocalBuild`; direct GCC/Make/CMake builds still work. These are
defaults for cooperating root workloads, not a security boundary against root.

The bundled `nixpkgs` registry is pinned in [nix/registry.json](nix/registry.json).
Use ordinary `nix run`, `nix shell`, `nix build`, or `nix copy` for cached outputs.
Uncached derivations fail rather than compile. Some development shells and legacy
`nix-env` profile operations therefore cannot work under this policy. Exact paths
already in the shared store work offline; resolving flake sources/selectors can
still need network access in a fresh container.

```sh
docker run --rm ghcr.io/colony-2/base2:latest c2j version
docker run --rm ghcr.io/colony-2/base2:latest nix run nixpkgs#hello
docker run --rm -it -v "$PWD:/workspace" ghcr.io/colony-2/base2:latest
```

Without a mount, ordinary Nix use initializes an ephemeral container-local store.
For shared volumes, **Pulse must initialize once before concurrent attachment**
and check ownership, compatibility and readiness. Stock Nix does not safely handle
all simultaneous first-use/crashed-initializer cases. `base-store-preflight` is a
bounded diagnostic for Pulse, not a wrapper around Nix. See the integration guide
for initialization and failure handling.

Automatic GC and store optimization are disabled. Do not run GC, repair, deletion,
or destructive maintenance while any worker uses a shared volume. Pulse retires
whole volumes only after all users detach. Default mutable profiles are private
under `/root/.local/state/nix/profiles`; do not share `/root` between containers.

## Builds and updates

The [workflow](.github/workflows/image.yml) builds on every push to `main`, on
manual runs on `main`, and for pull requests. Hourly checks at minute 17 UTC build
only when the selected inputs or source revision change. PRs never publish.

The resolver tracks the Ubuntu image digest, the complete selected APT package
closure for **both** architectures (including base-package security updates),
and the versions/checksums of all external tools. Resolution checks the current
Ubuntu archive snapshot every time. When the base image and complete APT closure
match the published image, installation retains that image's snapshot; otherwise
it uses the newly resolved snapshot. The installed package closure must exactly
match resolution. A timestamp-only snapshot change does not trigger a rebuild.

Nix stays on its certified pin across tool updates. The available Nix candidate
is recorded, but a newer Nix package requires a reviewed pin change and store
compatibility tests. A missing pin or artifact fails the build. Node stays on
major 24. Dependency/version changes, rather than unrelated archive updates,
determine the hourly fingerprint.

Each architecture is loaded and tested in disposable containers after building.
Tests never leave a populated store in an image layer. Only after both pass does
the workflow publish the multi-platform `base2:latest` tag and immutable
`base2:shared-nix-build-<run-id>-<attempt>` tag. A failed check leaves `base2:latest`
unchanged. Intermediate architecture tags are unique to the run.

Layer reuse comes from inline BuildKit metadata in the published GHCR image;
there is no separate cache image or Actions cache. APT mounts only its target
architecture's package list. Each upstream tool has its own final-image layer
and artifact input file; the complete reporting manifest is copied afterward.
Changing a tool still invalidates later dependent layers, so frequently updated
c2j is installed last. See [image reuse and validation](docs/image-reuse.md).

CI currently runs both architectures sequentially on an amd64 runner, using
QEMU for arm64. Build, acceptance tests, and publication have separate named
steps for each architecture so their durations are visible.

The image records resolved inputs in `/usr/local/share/base/versions.json` and
its installed APT packages in `/usr/local/share/base/packages.json`. The index
records the input fingerprint and source revision; no tracking commits are needed.
A small Mozilla CA bundle is used only to bootstrap snapshot TLS before Ubuntu's
certificate package is installed; its resolved hash is also recorded.

## Local validation

Requires Python 3.11+, Docker Buildx, and emulation for the other architecture.
Run from the repository root; set `GH_TOKEN` if GitHub API rate limits require it.

```bash
python3 -m unittest discover -s tests -v
python3 scripts/resolve-versions.py --image ghcr.io/colony-2/base2
build_args=()
while IFS= read -r arg; do build_args+=(--build-arg "$arg"); done < .build/build-args.env

docker buildx build "${build_args[@]}" --platform linux/arm64 --load -t colony-base:local .
python3 tests/shared_nix_store/acceptance.py --image colony-base:local --platform linux/arm64
# Repeat with linux/amd64. A containerd-backed Docker image store can load both
# architectures together with --platform linux/amd64,linux/arm64.
```

`base-smoke-test --tools-only` checks tools offline without initializing `/nix`.
`base-smoke-test` also opens an ephemeral store and verifies Nix build denial.
The acceptance suite checks signed substitution, offline reuse after producer
removal, separate-PID concurrency, cancellation, private profiles, c2j child
processes, signature rejection and provider preflight. Historical probes retaining
the original unsupported cold-start assertions remain available separately.

See [implementation validation](docs/image-validation.md),
[version compatibility findings](NIX_STORE_COMPATIBILITY_RESULTS.md), and
[test instructions](tests/shared_nix_store/README.md).
