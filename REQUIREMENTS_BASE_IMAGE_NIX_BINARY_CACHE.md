# Requirements: automatic shared Nix binary caching in the base image

Status: proposed implementation handoff to the `ghcr.io/colony-2/base` team.
The export/reuse mechanism has been prototyped; the wrapper and provider migration
described here have not been implemented.

## Outcome

Nix installations performed by c2j, coding agents, and their subprocesses should
automatically populate a shared binary cache. Later containers should reuse those
outputs through ordinary Nix substitution. Users should not configure caches or
run export commands themselves.

Keep each container's image-provided `/nix` intact. Mount a separate Docker volume
for the binary cache. A real executable wrapper around Nix runs the requested
command, then uses `nix copy` to export newly available store paths under a shared
file lock. No Nix daemon, background cache service, privileged container, custom
filesystem mount, or host Nix installation is required.

This replaces the proposed shared, seeded `/nix` volume for Nix caching. It does
not replace uv or pnpm caching. Pulse's existing implementation still mounts a
shared `/nix`; a coordinated provider change is required before this design is
active. See [the existing Docker cache proposal](PROPOSAL_DOCKER_DEPENDENCY_CACHES.md).

## Storage and configuration contract

The following names are proposed as the contract between the image and Pulse.
Agree any changes before implementing the provider integration.

| Setting/path | Contract |
| --- | --- |
| `COLONY_NIX_CACHE_DIR` | Absolute path to a mounted cache root; unset or empty disables the feature. Pulse will supply `/var/cache/colony/nix`. |
| `$COLONY_NIX_CACHE_DIR/binary` | Nix file binary cache, used for both substitution and export. |
| `$COLONY_NIX_CACHE_DIR/export.lock` | Stable shared advisory lock file for every exporter into this cache. |
| `COLONY_NIX_CACHE_LOCK_TIMEOUT_SECONDS` | Maximum export-lock wait; proposed default 30 seconds. |
| `COLONY_NIX_CACHE_EXPORT_TIMEOUT_SECONDS` | Maximum export duration after acquiring the lock; proposed default 120 seconds. |

The image must work with caching disabled and without a mounted cache. Enabling
caching must create the required cache directories automatically. The initial
supported runtime user is the base image's root user; document permissions and
reject unsupported ownership without recursively changing unrelated files.

The binary cache holds NAR archives and Nix metadata, not a second live Nix store:

```text
/nix/                                  # per-container image store and runtime additions
/var/cache/colony/nix/                  # shared Docker volume
  export.lock
  binary/
    nix-cache-info
    nar/<archive-hash>.nar.<compression>
    <store-path-hash>.narinfo
```

Keep the Nix database, profiles, roots, and bookkeeping for pending exports local
to each container. Do not seed the cache by copying or exporting the entire base
store at startup. Exporting a newly installed output may also export base-image
paths in its dependency closure; this is expected with `nix copy`.

## Executable wrapper requirements

Install real executable wrappers ahead of the real Nix commands in the image's
default `PATH`. Shell aliases and shell startup files are insufficient. The
feature must work when Pulse overrides the image entrypoint and directly starts
`c2j run with-lease`.

Cover `nix` and the image's legacy installation/import commands, including
`nix-env`, `nix-store`, `nix-build`, and `nix-shell`. Support calls from noninteractive
subprocesses and common `nix build`, `nix profile install`, `nix run`, `nix shell`,
and `nix copy --from ...` workflows. A command named `build` must still obey the
image's prebuilt-output-only policy.

The implementation language is up to the image team. Required interpreters,
locking utilities, and real Nix executables must already be in the image; running
the wrapper must not install its own dependencies. Use known absolute paths to
real Nix commands for internal queries and exports to prevent recursion. An
internal bypass must not disable caching for ordinary nested user commands.

Preserve argv boundaries, working directory, stdin, terminal behavior, stdout,
stderr, and the original command's exit status. In particular, JSON output and
store-path output must remain parseable. Send wrapper diagnostics and export
progress to stderr. Handle termination signals, reap children, and avoid leaving
export processes running after their wrapper is canceled.

Absolute calls to the underlying Nix binary, applications that replace `PATH`,
and direct store writes can bypass the wrapper. Document these coverage limits;
this is automatic caching for normal command execution, not mandatory mediation
of every possible store mutation.

## Detecting and exporting installed paths

Use Nix's registered valid paths to find new outputs. Do not parse human-readable
command output, infer paths from package selectors, or scan partially written
store directories. A before/after snapshot using the real
`nix-store --query --all` is an acceptable starting point.

Capture newly registered paths even when the requested command returns nonzero:
it may have successfully downloaded some outputs before failing. Maintain a
container-local pending set so paths whose export failed or timed out can be
retried on a later wrapped invocation. A simple before/after difference alone
would lose that retry opportunity. Coordinate bookkeeping between concurrent
wrappers in the same container without serializing their installation commands.

Do not export the complete store on every invocation. Commands that produce no
new paths should avoid export work unless there are pending paths to retry.
Handle paths removed by local GC between discovery and export as a recoverable
cache failure, not permission to rebuild them or change the user's result.

Pass exact valid store paths to the real `nix copy`; let Nix handle dependency
closures, archives, compression, hashes, signatures, and metadata. There is no
custom publisher, staging-cache merger, or rsync step in this design.
[Nix documents closure copying here](https://nix.dev/manual/nix/2.34/command-ref/new-cli/nix3-copy.html).

For long-running `nix run` or `nix shell`, post-command export occurs when the
command exits. Wrapped Nix commands started inside that process can export when
they finish. No guarantee is made that outputs are exported before an outer
command exits or after the container is forcibly killed. A background watcher
is outside this initial scope.

## Shared export locking and duplicate handling

Acquire an exclusive `flock` on the shared lock file **before starting the real
exporting Nix process**, including its cache initialization. Hold it until that
process has exited. All automatic writers must follow this protocol. Coordinate
first-use initialization of `nix-cache-info` under the same lock.

Illustrative locking only; timeout, signal, and status handling are omitted:

```bash
flock "$COLONY_NIX_CACHE_DIR/export.lock" \
  /path/to/real/nix copy \
  --to "file://$COLONY_NIX_CACHE_DIR/binary" "${store_paths[@]}"
```

Construct the actual file URI correctly for the configured path. Never delete
or replace the lock file during use: its shared inode is the coordination point.
Keep the lock alive for the export process, but do not leak its descriptor to
unrelated long-lived children. Use finite lock and export timeouts. A terminated
exporter must release the lock without a user removing a stale lock file.

Do not hold this lock during the requested Nix command, package downloads,
execution of installed tools, or cache reads. Concurrent installations remain
possible. A second exporter that starts after the first completes should let
Nix detect and skip already cached outputs and dependencies. This does not
deduplicate downloads that happened concurrently before either export completed.

The lock works around a demonstrated cross-container race: Nix 2.34.8's file
cache writer names temporary files with PID and a process-local counter. Separate
containers can use the same pair. The wrapper must not assume this operation is
safe without coordination. See the
[local binary cache writer](https://github.com/NixOS/nix/blob/2.34.8/src/libstore/local-binary-cache-store.cc).

The supported backing storage is a Docker local volume on the Docker daemon's
Linux filesystem, including inside Colima's VM. Network filesystems and macOS
host-directory bind mounts are outside the initial tested storage contract.

## Substitution, trust, and build policy

When enabled, configure the shared file cache as an additional substituter with
preference over remote caches. Preserve existing public/private substituters,
credentials, trusted keys, and other image configuration. Keep `NIX_REMOTE=local`;
the real store remains the container's `/nix/store`.

A miss should allow normal remote substitution. A hit should restore the exact
output and required dependencies into the local store, where executables can use
their normal absolute `/nix/store` references. The archive cache is not directly
executable. It does not by itself guarantee offline flake evaluation or mutable
selector resolution; use exact output paths in offline reuse tests.

New cache entries must become visible to later commands even when this container
previously cached a miss. Initially use `narinfo-cache-negative-ttl = 0` while the
shared cache is enabled, and test miss-then-publish-then-retry behavior. This
setting also affects other substituters; document that tradeoff. See
[Nix's lookup-cache settings](https://nix.dev/manual/nix/2.34/command-ref/conf-file.html#conf-narinfo-cache-negative-ttl).

Preserve signature verification and existing trust policy. Do not globally turn
off signature checks or silently mark arbitrary file caches trusted. Test reuse
of an output signed by an existing trusted upstream key, including its closure.
If local trust is needed for another supported workflow, specify that separately
and confine it to the provider-owned cache and its trust boundary.

Preserve the image's prohibition on source builds: no local builds, no remote
builders, and the existing build-denial hook covering `preferLocalBuild` paths.
If no substituter contains an output, installation must fail rather than build.
Do not use a post-build hook for population: Nix explicitly does not run that
hook for substituted outputs. See
[Nix configuration](https://nix.dev/manual/nix/2.34/command-ref/conf-file.html#conf-post-build-hook).

## Failure behavior and diagnostics

Cache population is best effort. Lock timeout, export timeout, unwritable cache,
or export failure must not turn a successful user command into a failed command,
or replace the exit status of a failed command. Emit a concise diagnostic and
retain pending paths for a later attempt where possible. Do not retry indefinitely
or perform a long export while handling termination.

Diagnose an unavailable cache before the main command where possible and run
without that additional substituter. Define and test read-error behavior; never
bypass integrity or signature checks to recover a corrupted cache entry.

Diagnostics should distinguish cache-disabled, initialization failure, lock
timeout, export timeout, and export failure. Debug output should include lock
wait and export duration. Do not log credentials. Ordinary stdout must contain
only the original command's output.

The image must not prune the shared cache while other containers may use it.
Pulse owns whole-volume retention and removal when no workers are attached.

## Pulse integration and c2j compatibility

These are coordinated follow-up requirements for the Pulse/c2j owners, not work
that can be completed solely in the base image:

- Automatically create and mount the cache volume with Docker `NoCopy`, set the
  agreed environment variables, and retain tenant/deployment/image/platform
  isolation. No user setup or privileged host configuration is required.
- Stop mounting a volume over `/nix` and remove full-store seeding for the new
  image contract. Use a new cache layout version; do not interpret old live-store
  volumes as binary caches. Leave old in-use volumes intact during migration.
- Gate attachment on an explicit image capability/version marker agreed with the
  base team. A changed `latest` tag alone is insufficient to distinguish contracts.
- Keep uv/pnpm behavior intact. Validate shared c2j prepared-tool entries: a ready
  record or result symlink may reference a Nix path absent from a fresh container.
  Before invoking it, c2j must restore the recorded exact output from the binary
  cache or run its existing preparation path safely. The wrapper cannot intercept
  a direct executable invocation when no Nix command is run.
- Test concurrent consumers of those shared readiness records. Do not re-resolve
  a mutable reference or delete a prepared environment used by another worker
  merely because the local Nix output is missing.

End-to-end rollout is blocked until that prepared-environment behavior is
verified or fixed. The image wrapper should work independently of c2j for all
ordinary covered Nix commands.

## Acceptance tests

Use disposable Docker volumes and bounded tests. Run on supported Linux
architectures and on the actual macOS CI Colima configuration before rollout.

| Scenario | Required result |
| --- | --- |
| Disabled/no volume | Ordinary Nix behavior; no shared-cache initialization or export. |
| Cold empty volume | Base tools still work; no full `/nix` seed; first install initializes and populates the cache automatically. |
| Fresh offline container | Realize a known exact output and its closure from the cache, verify integrity, and execute it with networking disabled. |
| Trusted signed package | Reuse a real prebuilt upstream-signed output under the existing signature policy, not just a synthetic content-addressed object. |
| Child-process coverage | A process standing in for a coding agent installs through PATH; a later container reuses the result without c2j-specific export logic. |
| Legacy commands | Installation/import through each supported legacy wrapper populates the cache. |
| Concurrent identical installs | Two separate containers export safely; the later exporter skips paths already published. Include a test with colliding exporter PIDs. |
| Concurrent different installs | Both closures become usable; installs overlap while only exports serialize. |
| First-use race | Two containers start with an empty shared volume; initialization is valid and neither replaces the lock inode. |
| Prior cache miss | Container B misses an output, A publishes it, and a later B command finds it without deleting B's lookup database. |
| Interrupted export | Kill a writer while holding the lock; another writer proceeds, retries successfully, and readers never accept an incomplete archive. |
| Failure/timeout | Unwritable cache, held lock, and stalled export give bounded diagnostics and preserve the requested command's status. Pending paths retry later. |
| CLI transparency | JSON/stdout, stdin-consuming commands, argument quoting, interactive use, signals, and nonzero status behave correctly. |
| Nested/long-running commands | Nested Nix invocations are still wrapped; post-command export timing is as documented; no recursion or leaked locks. |
| Missing substitute | Output unavailable locally or remotely fails without invoking local or remote builders. |
| c2j warm preparation | A fresh container safely restores an exact output referenced by a shared ready entry, including with another worker using that entry. |
| Repeated warm invocation | No full-store export; already cached closures are skipped; record wrapper overhead on the actual base image. |

Mechanism-level evidence already obtained on Linux Docker with Nix 2.34.8:
two uncoordinated exports reproduced a temporary-file collision; serialized
exports both succeeded, with the second reporting zero paths copied; a fresh
network-disabled container restored and verified the synthetic prebuilt object.
This does not constitute wrapper, signed-package, c2j, or Colima acceptance.

## Deliverables

Deliver the image wrappers and configuration, the agreed capability marker and
environment contract, automated acceptance tests, and operating documentation
covering bypasses, timeouts, trust, and export timing. Provide an immutable image
digest and test results for Pulse integration. Measure cold setup and warm export
costs before changing timeout defaults or introducing a more complex publisher.
