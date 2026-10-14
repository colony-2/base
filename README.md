# Colony 2 base image

`ghcr.io/colony-2/base:latest` is a Linux image for **amd64 and arm64**, built using
[`nixos/nix`](https://hub.docker.com/r/nixos/nix). It includes Bash, Nix (with flakes
and `nix-command` enabled), common development tools, Node.js 24, Python 3, pnpm, uv/uvx, and
[`c2j`](https://github.com/colony-2/c2j). The default command opens Bash in
`/workspace`; the image runs as root, following the upstream Nix image.

All tools are installed through Nix using [nix/packages.nix](nix/packages.nix).
Nix and the bundled tools come from one pinned `nixpkgs-unstable` revision, installed
into a fresh profile so packages inherited from the build image cannot override them.
c2j uses a local Nix derivation for its checksummed upstream release binaries.
The bundled Python supports uv virtual environments.

| Category | Tools |
| --- | --- |
| Core utilities | Bash, coreutils, findutils, grep, sed, awk, diff, patch, which, file, less, tree |
| Search and navigation | ripgrep, fd, fzf, bat |
| Git and GitHub | Git, Git LFS, gh, OpenSSH |
| HTTP and networking | curl, wget, CA certificates, dnsutils, iputils, iproute2, OpenBSD netcat |
| JSON and YAML | jq, yq-go |
| Archives | zip, unzip, tar, gzip, xz, zstd |
| Build tools | GNU Make, GCC (C and C++), pkg-config, CMake |
| JavaScript | Node.js 24, pnpm |
| Python | Python 3, uv/uvx, Ruff |
| Databases | SQLite |
| Shell development | ShellCheck, shfmt, entr |
| Process inspection | procps, psmisc, lsof, strace, htop |
| File synchronization | rsync |

The Dockerfile uses `nixos/nix` only for its build stage. The final stage starts
from `scratch` and receives the installed profile's runtime closure (the tools and
their dependencies), a registered Nix database, and minimal system configuration.
It does not inherit the upstream image layers or include its bundled Nixpkgs
channel, old profiles, unused build inputs, or download caches. The explicitly
included compilers and their dependencies increase the runtime image's size.

Runtime Nix uses prebuilt packages from `https://cache.nixos.org` and
`https://colony2.cachix.org`, with both caches' public signing keys configured.
Local and remote **Nix builds** are disabled: `max-jobs = 0`, no remote builders,
and no build fallback. Direct project builds using GCC, Make, or CMake work normally.
A pre-build hook also rejects `preferLocalBuild` derivations, which would otherwise
bypass `max-jobs`. Cache signatures remain required. These are image defaults;
root can explicitly change the configuration.

Use `nix run`, `nix shell`, or `nix copy` with cached packages. For example:

```sh
docker run --rm ghcr.io/colony-2/base:latest nix run nixpkgs#hello
```

Nix fetches package definitions on demand; no channel source tree is bundled.
If a required output is absent from the configured caches, the command fails
instead of compiling it. Commands that need local derivations, including some
development shells and legacy `nix-env` profile operations, are unsupported.

```sh
docker run --rm -it -v "$PWD:/workspace" ghcr.io/colony-2/base:latest
docker run --rm ghcr.io/colony-2/base:latest c2j version
docker run --rm ghcr.io/colony-2/base:latest base-smoke-test
```

## Automatic shared Nix binary cache

When a provider mounts a Docker local volume at `/var/cache/colony/nix` and sets
`COLONY_NIX_CACHE_DIR` to that path, executable Nix wrappers automatically export
new store paths after commands finish and use the shared cache for substitution.
The container keeps its own `/nix`; no full-store seeding or daemon is required.
Failed exports are retried on later invocations. Signature checks and the
cache-only Nix build policy remain enabled. Unset/empty disables the feature.

The image advertises capability `io.colony-2.base.nix-binary-cache=1`. See the
[contract and operating documentation](docs/nix-binary-cache.md) for timeouts,
permissions, coverage limits, and required Pulse/c2j migration work.

## Automatic builds

The [GitHub Actions workflow](.github/workflows/image.yml):

- Builds and publishes on pushes to `main` and manual workflow runs on `main`.
- Checks upstream versions hourly, at minute 17 UTC, and publishes only when the
  inputs or source revision differ from the last successful `latest` image.
- Builds and smoke tests pull requests without publishing.
- Publishes a single multi-platform manifest as `latest` and a unique
  `build-<run-id>-<attempt>` tag for each successful publication.

The check tracks the `nixos/nix:latest` manifest digest, the `nixpkgs-unstable`
revision (covering all bundled Nixpkgs tools and their dependencies), and the latest
stable c2j GitHub release, including both architectures' asset hashes.
pnpm and uv updates arrive when available in Nixpkgs. Any Nixpkgs revision change
triggers a build, including dependency updates without a tool version change.
The build pins those inputs; Nix handles dependency resolution and checksum
verification for every installed tool.

The published index stores the input fingerprint and source revision. No tracking
commits or expiring cache entries are needed. A failed build leaves `latest`
unchanged, so the next hourly check retries. Both architectures must pass the
Dockerfile's smoke checks before the multi-platform image can be published.
The checks verify all tool executables, compile and run C and C++ programs,
exercise the package managers, reject local Nix builds, and download a
signed cached package into an empty store to verify runtime substitution.
Each image contains its resolved inputs in `/usr/local/share/base/versions.json`.
The installed Nix packages and versions are recorded in
`/usr/local/share/base/packages.json`.

Push this repository to `main` with GitHub Actions enabled to activate publishing.
The workflow uses the built-in `GITHUB_TOKEN` with `packages: write`; no additional
secret is required. If a GHCR package already exists, grant this repository write
access to it. Set the package's visibility to public in GHCR if anonymous pulls
are wanted. Forks automatically publish under their own repository name.

[GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
run from the default branch, can be delayed, and are disabled after 60 days of
inactivity in public repositories. Keep `main` as the default branch and re-enable
the schedule if GitHub disables it.

## Local build

Requires Python 3.9+, Docker Buildx, and emulation for cross-architecture builds
(Docker Desktop supplies it). In Bash:

```bash
python3 scripts/resolve-versions.py
build_args=()
while IFS= read -r arg; do
  build_args+=(--build-arg "$arg")
done < .build/build-args.env

# Build and load the local architecture.
docker buildx build "${build_args[@]}" --load -t colony-base:local .
docker run --rm colony-base:local base-smoke-test

# Publish both architectures to a registry you can write to.
docker buildx build "${build_args[@]}" \
  --platform linux/amd64,linux/arm64 --push -t YOUR_REGISTRY/base:dev .
```

Set `GH_TOKEN` to use authenticated GitHub API requests if you hit anonymous rate
limits. Generated build inputs are stored under `.build/` and ignored by Git.
Run the version-check tests with `python3 -m unittest discover -s tests -v`.
