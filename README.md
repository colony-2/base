# Colony 2 base image

`ghcr.io/colony-2/base:latest` is a Linux image for **amd64 and arm64**, based on
[`nixos/nix`](https://hub.docker.com/r/nixos/nix). It includes Bash, Nix (with flakes
and `nix-command` enabled), Node.js 24, Python 3, pnpm, uv/uvx, and
[`c2j`](https://github.com/colony-2/c2j). The default command opens Bash in
`/workspace`; the image runs as root, following the upstream Nix image.

All tools are installed through Nix using [nix/packages.nix](nix/packages.nix).
Bash, Node.js, Python, pnpm, and uv come from one pinned `nixpkgs-unstable` revision.
c2j uses a local Nix derivation for its checksummed upstream release binaries.
The bundled Python supports uv virtual environments.

```sh
docker run --rm -it -v "$PWD:/workspace" ghcr.io/colony-2/base:latest
docker run --rm ghcr.io/colony-2/base:latest c2j version
docker run --rm ghcr.io/colony-2/base:latest base-smoke-test
```

## Automatic builds

The [GitHub Actions workflow](.github/workflows/image.yml):

- Builds and publishes on pushes to `main` and manual workflow runs on `main`.
- Checks upstream versions hourly, at minute 17 UTC, and publishes only when the
  inputs or source revision differ from the last successful `latest` image.
- Builds and smoke tests pull requests without publishing.
- Publishes a single multi-platform manifest as `latest` and a unique
  `build-<run-id>-<attempt>` tag for each successful publication.

The check tracks the `nixos/nix:latest` manifest digest, the `nixpkgs-unstable`
revision (covering Bash, Node.js, Python, pnpm, uv, and their dependencies), and the latest
stable c2j GitHub release, including both architectures' asset hashes.
pnpm and uv updates arrive when available in Nixpkgs. Any Nixpkgs revision change
triggers a build, including dependency updates without a tool version change.
The build pins those inputs; Nix handles dependency resolution and checksum
verification for every installed tool.

The published index stores the input fingerprint and source revision. No tracking
commits or expiring cache entries are needed. A failed build leaves `latest`
unchanged, so the next hourly check retries. Both architectures must pass the
Dockerfile's smoke checks before the multi-platform image can be published.
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
