# Nix binary cache validation — 2026-10-14

The image-side contract was validated locally with Docker 29.5.2 on an Ubuntu
24.04.4 Linux arm64 daemon. Arm64 ran natively; amd64 ran under the daemon's
configured emulation. This is not the actual macOS CI Colima environment.

## Immutable image

Local multi-platform image: `colony-base:cache`.

OCI index digest: `sha256:fb255ad6e6b9eb6c316b65916a3891fe29c4a926ad817c0da09c5f5c6d2fea2a`.

This image is built and loaded in the local Docker daemon, **not published to
GHCR**. The local reference is `colony-base@sha256:fb255ad6e6b9eb6c316b65916a3891fe29c4a926ad817c0da09c5f5c6d2fea2a`; it is not a
registry pull reference. A registry publication must use the validated workflow
and record its resulting registry digest for Pulse's rollout.

The image advertises `io.colony-2.base.nix-binary-cache=1` and the matching v1
capability JSON. Both architectures retain three filesystem layers. Nix is
2.34.8; runtime packages and source-build policy are unchanged by this feature.
Build inputs fingerprint: `8fbc4caddc196be4dc3a229e0d64552ca9feff4fa30921ffc6c9e0d0d2270880`.

## Results

- 30 unit tests passed (version resolution plus wrapper bookkeeping/process tests).
- Hadolint, ShellCheck, actionlint, Ruff, and `git diff --check` passed.
- Both platform builds passed the existing image smoke checks and signed-cache test.
- 24 Docker-volume acceptance tests passed per architecture on this image.
- An additional disabled-cache probe confirmed no shared binary directory or local
  pending database was created when the feature was unset.

The Docker suite used upstream-signed `hello`, `figlet`, and `toilet` outputs.
The `toilet` test asserts multiple paths in its closure are absent from the base
image, then verifies restoration and execution in a fresh network-disabled
container. Fixed-output content-addressed fixtures additionally exercise legacy
commands; they are not the sole evidence of cache trust/reuse.

The suite also covers cold/no-seed startup, nested/child command execution,
concurrent writers in one and separate containers, first-use races, real exporters
with forced matching PID 2048, prior misses becoming hits, termination, failed
and timed-out exports with later retries, CLI/stdin/terminal behavior, caller
Python environment isolation, signature rejection, corrupted archives, and
source-build denial. Fault injection for stalls and failures is confined to
disposable containers.

## Timing observations

Timings are local observations, not performance guarantees. The first export
copies the sample package's dependency closure; subsequent exports let Nix skip
already published paths. Warm figures average ten `nix eval` invocations with no
new/pending paths. The benchmark prints and checks that no copy occurred.
Emulated amd64 timing is not representative of native amd64 production.

| Platform | Acceptance suite | Empty-cache initialization | First export | Real Nix warm mean | Wrapped warm mean | Added mean overhead |
| --- | --- | --- | --- | --- | --- | --- |
| arm64 | 24 passed (199.817s) | 0.036s | 6.404s | 20.1 ms | 100.1 ms | 80.0 ms |
| amd64 | 24 passed (301.348s) | 0.125s | 8.106s | 130.7 ms | 633.1 ms | 502.4 ms |

The 30-second lock and 120-second export defaults remain unchanged. Exports have
a single deadline after acquiring the lock, including the valid-path query.

## External rollout work still open

1. Agree the v1 capability marker with Pulse, create/mount a separate `NoCopy`
   volume, and stop mounting/seeding `/nix` for this contract. Preserve isolation
   and uv/pnpm behavior; do not migrate in-use old volumes in place.
2. Verify/fix c2j restoration of exact Nix outputs referenced by shared prepared
   environment records, including concurrent consumers.
3. Run this acceptance suite on the actual macOS CI Colima setup, and run the
   provider/c2j end-to-end scenarios there.
4. Publish through CI and supply the resulting immutable registry digest.

These were not claimed as completed by local image acceptance. See
[the operating contract](nix-binary-cache.md) for configuration and coverage limits.
