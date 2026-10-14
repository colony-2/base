# Automatic shared Nix binary cache, contract v1

The image supports automatic post-command binary caching when
`COLONY_NIX_CACHE_DIR` names a mounted Docker local volume. Without that variable
(or with an empty value), wrappers immediately execute the real Nix command.
The image's `/nix` remains private to each container. No daemon, background
watcher, privileged mount, host Nix, or startup seed is involved.

## Provider contract and rollout

The image advertises both:

- OCI image config label `io.colony-2.base.nix-binary-cache=1`.
- `/usr/local/share/base/capabilities.json` with
  `nixBinaryCache.version = 1` and `layout = "file-binary-cache-v1"`.

Pulse should require this version before enabling the new layout, automatically
mount a **new** Docker local volume with `NoCopy` at `/var/cache/colony/nix`, and set
`COLONY_NIX_CACHE_DIR=/var/cache/colony/nix`. Keep tenant, deployment, image, and
platform isolation. Do not mount over `/nix`, reuse an old live-store cache volume,
or change an old volume while workers are attached. Keep uv/pnpm caching intact.
The wrapper never prunes the shared cache; Pulse owns whole-volume retention.

These marker names/version are the base image's proposed integration contract.
Coordinate them with Pulse before provider rollout. This repository implements
and tests the image side only. Pulse's mount migration and c2j's prepared-tool
restoration are separate work: a ready record or symlink can refer to an output
missing from this container. c2j must restore that exact output through Nix before
executing it, without re-resolving a mutable selector or removing an environment
used by another worker. A direct executable invocation cannot trigger this wrapper.
End-to-end rollout still requires those changes and tests on the actual macOS CI
Colima configuration. The Docker acceptance suite can run against that daemon.

## Settings and files

| Variable | Default | Meaning |
| --- | --- | --- |
| `COLONY_NIX_CACHE_DIR` | unset | Absolute mounted cache root; unset/empty disables caching |
| `COLONY_NIX_CACHE_LOCK_TIMEOUT_SECONDS` | `30` | Maximum wait for the shared export lock |
| `COLONY_NIX_CACHE_EXPORT_TIMEOUT_SECONDS` | `120` | Export deadline after lock acquisition, including store validation; also bounds cache initialization |
| `COLONY_NIX_CACHE_DEBUG` | unset | Set to `1` for timings and disabled-cache diagnostics |

Timeouts must be positive finite numbers. Initialization and a subsequent export
have separate deadlines. Internal local-store queries have a 10-second bound.
Termination skips export and attempts at most a one-second local snapshot.

Only root is initially supported. The root and `binary/` directories and cache
metadata must be root-owned and not group/world writable. The lock must be a
root-owned regular file with one link. Cache directory and lock symlinks are
rejected; initialized metadata must be a regular file. The wrapper creates missing directories/files, never recursively
changes ownership, and diagnoses unsupported permissions. Docker local volumes
on the daemon's Linux filesystem are supported; network filesystems and macOS
host-directory bind mounts have not been validated.

The volume contains `binary/nix-cache-info`, `binary/nar/`, NAR metadata, and a
stable `export.lock`. This is an archive cache, not an executable store. Nix's
live database, profiles, roots, and `/var/lib/colony/nix-cache/<cache-id>/pending.sqlite3`
remain container-local. The local database is per configured cache root.

## Execution, export, and concurrency

Executable wrappers for `nix`, `nix-env`, `nix-store`, `nix-build`, `nix-shell`,
`nix-channel`, `nix-copy-closure`, `nix-instantiate`, and `nix-prefetch-url`
live in `/usr/local/bin`, before the real executables in PATH. They work when the
entrypoint is overridden, including direct `c2j run with-lease`. Their Python
interpreter and internal Nix executables resolve to immutable store paths at image
build time; invoking a wrapper installs no dependencies. Internal operations use
an isolated Python interpreter, so caller `PYTHONHOME`/`PYTHONPATH` cannot shadow
the wrapper's standard library; those variables still reach the user's command.
Internal Nix operations use
`/usr/local/libexec/nix-real/`, not a process-wide bypass flag. Nested user commands
retain the normal wrappers.

Before and after the user command, the wrapper queries registered valid paths
with `nix path-info --all --json --json-format 1`. (Nix 2.34.8 does not support the
handoff's suggested `nix-store --query --all`.) It merges newly registered paths
into SQLite even if the command failed. Short database transactions coordinate
concurrent wrappers without locking their installations. A command with no new
or pending paths does no export. Failed exports stay pending for a later wrapped
invocation in the same container. Paths removed by local GC are dropped from the
pending set with a diagnostic; the wrapper never rebuilds or fetches them itself.

Initialization and export acquire an exclusive `flock` on `export.lock` before
starting the real `nix copy`. The lock inode is never replaced or removed. The
copy child inherits that descriptor, so killing the parent cannot unlock a live
writer. Unrelated user commands do not inherit it. Only writers serialize;
commands, package downloads, tools, and reads run outside the lock. Nix skips
already published outputs and handles closures, metadata, compression, hashes,
and signatures. There is no custom publisher or archive merging.

Long-running `nix run`/`nix shell` commands export when they exit. Nested wrapped
commands export when they finish. There is no guarantee of export before the
outer command exits, or after a hard container kill. Termination signals are
forwarded to the child process group; canceled exporters are killed and reaped.
Interactive commands receive the terminal's foreground process group, with job
stops and terminal ownership handed back to the caller. Stdout and stdin belong
to the original command; wrapper diagnostics and export progress use stderr.

## Substitution, trust, and failures

While enabled, the wrapper keeps `NIX_REMOTE=local` and adds a correctly escaped
`file://.../binary?priority=1&trusted=false` substituter. Existing cache URLs,
credentials, trusted keys, and other Nix configuration remain in effect. Nested
wrappers replace only their parent's generated configuration fragment.
`narinfo-cache-negative-ttl = 0` makes another writer's newly published entries
visible without deleting the reader's lookup database. This also disables negative
lookup caching for remote substituters and can increase remote requests.

Signature requirements remain enabled. Existing upstream signatures are copied
and verified on reuse. Content-addressed paths retain Nix's usual intrinsic trust.
Unsigned input-addressed outputs can be exported but are **not** automatically
trusted on reuse. No local signing key or broader trust exception is introduced.
All files and metadata must still pass Nix's hash/integrity checks. Producers
sharing a volume must be within the provider's isolation boundary.

A cache unavailable at startup is diagnosed and omitted from the command's added
substituters. Export failure, lock timeout, or export timeout never changes the
user command's exit status. Read errors that arise during a command are governed
by Nix's existing policy: this wrapper never replays the command, disables
signature checks, or hides corruption to recover. Explicit user Nix options
continue to take precedence. Diagnostics avoid argv, configuration, and secrets.

Nix's local/remote build prohibition and `preferLocalBuild` denial hook remain in
place. Cached `nix build` outputs are supported; cache misses cannot compile.
Legacy `nix-env` operations that need to construct a local profile may still fail
under that policy; outputs downloaded before the failure are nevertheless cached.
Use a separate modern profile (for example `nix profile install --profile /tmp/tools
<installable>`) instead of asking modern Nix to modify the image's legacy profile.

The modern Nix CLI automatically disables substitution when it detects no network
interfaces. For entirely network-disabled containers, use `nix-store --realise
<exact-output-path>`, or pass `--option substitute true` to modern Nix. This opts
into file substitution, not source builds. The shared cache does not supply flake
registries, mutable selector resolution, or all evaluation source files; offline
reuse tests use exact paths or self-contained fixture expressions.

Absolute calls to the underlying Nix executables, replacing PATH, direct store
writes, and executables launched without a Nix command can bypass automatic
population. This feature is best-effort convenience, not mandatory mediation.
Root can change configuration as with the existing image.

## Validation

Run unit tests with `python3 -m unittest discover -s tests -v` (Python 3.11+).
The Docker suite
creates disposable local volumes with `NoCopy`, uses upstream-signed packages,
and runs consumers with networking disabled:

```sh
python3 tests/acceptance_nix_cache.py --image colony-base:cache --platform linux/arm64
python3 tests/acceptance_nix_cache.py --image colony-base:cache --platform linux/amd64
```

Every test process and Docker call has a bounded timeout. Test fixtures obtain
signed packages from the public cache; tests exercise cold and offline reuse,
the primary install/import workflows, concurrent different/identical installs, colliding
exporter PIDs, first-use races, miss/publish/retry, interruption, pending retries,
read-only/unsupported ownership, malformed archives, signature rejection, nested
commands, stdin/JSON/quoting, terminal interaction, signals, and the build-denial
policy. Warm timings and cold setup/reuse time are printed in the test output.
Interruption and failure tests inject stalled/failed copy processes in disposable
containers; the PID collision test uses unprivileged fork padding before invoking
the real exporter. Separate tests corrupt real archives and remove signatures.
The workflow runs both architecture suites before publication.

See [validation results](nix-binary-cache-validation.md) for the tested immutable
local image digest, timing observations, and external rollout checks still open.
