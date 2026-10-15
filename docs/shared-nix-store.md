# Pulse integration: shared live Nix store v1

The migration image is `ghcr.io/colony-2/base:shared-nix-v1`. It ships Nix 2.34.3
(`nix-bin=2.34.3+dfsg-1`) outside an empty `/nix`. It advertises:

| Label | Value |
| --- | --- |
| `io.colony-2.base.nix-shared-store` | `1` |
| `io.colony-2.base.nix-store-compat` | `colony-nix-store-v1` |
| `io.colony-2.base.nix-store-initialization` | `provider` |

This implements the agreed adjustment to the original requirements: **Pulse owns
initialization, attachment validation and retention**. Stock Nix's simultaneous
first-use and interrupted-initializer recovery do not satisfy the original
requirements. Root Nix also accepts some misowned mounts. The image does not hide
these limitations behind a wrapper or startup script.

## Provision once, then attach concurrently

1. Allocate a new Docker **local volume** for the deployment/tenant, architecture,
   compatibility group and cache generation. Do not key it by image digest.
2. Hold a provider provisioning lease and mount it at `/nix` as root with
   `volume-nocopy`. No worker may use the new volume yet.
3. In that single initializer, run:

   ```sh
   base-store-preflight
   nix-store --init
   base-store-preflight --require-initialized
   ```

4. Publish the volume as ready only after all commands succeed. If initialization
   is killed or fails, discard that **never-exposed** volume and provision a new
   one. Do not delete or repair an existing ready volume in response to a worker
   failure.
5. Validate ownership/mount writability and compatibility before attachment;
   the ready check is available for diagnostics. Then launch ordinary workers,
   including direct `--entrypoint c2j ... run with-lease` invocations. No execution
   lock spans the worker/application lifetime.

`base-store-preflight` checks only a fixed set of directories, root:root ownership,
non-writable group/other permissions, real directories, and actual writes. The
optional readiness check reads the schema and checks required tables without a
whole-store scan. It does not validate provider leases, tenant identity, image
labels or generations; Pulse must enforce those. It does not change ownership,
initialize a database, repair anything, or run automatically on every Nix call.

For an isolated local demonstration (the shell below represents the provider's
single initializer):

```sh
image=ghcr.io/colony-2/base:shared-nix-v1
volume=colony-nix-demo
docker volume create "$volume"
docker run --rm --mount "type=volume,src=$volume,dst=/nix,volume-nocopy" \
  "$image" sh -ec 'base-store-preflight; nix-store --init; base-store-preflight --require-initialized'
docker run --rm --mount "type=volume,src=$volume,dst=/nix,volume-nocopy" \
  "$image" nix run nixpkgs#hello
```

Ordinary one-container, no-volume use initializes itself through Nix. The image
has no `VOLUME` declaration, so there is no hidden anonymous-volume behavior.

## Paths and operating constraints

| Location | Owner and purpose |
| --- | --- |
| `/usr`, `/usr/local`, `/opt` | Image tools, including Nix and its complete runtime |
| `/etc/nix/nix.conf`, `/etc/nix/registry.json` | Image policy and pinned default registry |
| `/etc/ssl/certs/ca-certificates.crt` | Distribution CA bundle |
| `/usr/local/libexec/nix-deny-build` | Image-owned build-denial hook |
| `/nix/store`, `/nix/var/nix` | One shared store, validity database and coordination state |
| `/root/.local/state/nix/profiles` | Container-private mutable profiles |
| `/root/.cache/nix` | Container-private evaluation/lookup caches |

Use root UID/GID 0 consistently. Never share only `/nix/store` with separate
validity databases. Do not replace/share `/root` without providing another private
profile location. `/root/.nix-profile` points through the private XDG profile.
If mixing legacy `nix-env` with modern `nix profile`, give the legacy command a
separate explicit profile such as `/root/.local/state/nix/profiles/legacy`;
their manifest formats are different.

Normal tools take precedence over the profile on PATH, retaining the advertised
Node 24 and Nix versions. An explicit `nix shell` can intentionally select another
tool version. Both binary caches and signature checks remain enabled; Nix builds
and remote builders remain disabled. Do not treat these root-overridable settings
as isolation from hostile root workloads.

Nix uses PID-derived temporary GC roots. With separate container PID namespaces,
**no live GC, deletion, repair or optimization** is supported. Automatic GC and
optimization are disabled. Let installed packages accumulate; retire a whole
volume only once every initializer and worker has detached. Capacity errors must
fail clearly; do not fall back to another private store.

Supported storage is Docker's local Linux filesystem, including local volumes
inside a Colima Linux VM. Network filesystems and macOS directory bind mounts
are outside this contract. Actual Colima CI and native amd64 validation must be
completed by the rollout environment; local testing here uses native arm64 and
emulated amd64.

## Compatibility and rollout

The v1 group initially admits the exact pinned Nix package with `nix-command`
and `flakes`. Other ordinary tool updates can retain the group after acceptance
tests. A Nix package/patch or store-affecting feature change requires tests against
**every version still allowed to attach**, including retained rollback images.
Matching main schema numbers alone do not establish compatibility. See the
[compatibility review](../NIX_STORE_COMPATIBILITY_RESULTS.md).

Use a new provider cache layout/generation for rollout. Do not reuse old seeded
image-store or binary-cache volumes. Prepared-record identity must include the
store generation: an exact path from an old removed volume is not ready in a new
one. Any prepared artifacts stored outside `/nix` need their own lifetime handling.
uv and pnpm caches remain separate from this change.

The image is built and committed here; Pulse capability detection, leases,
initialization, ready-state publication, attachment checks and retention are
separate work. Validate full supplied-lease execution and shared prepared records
in Pulse before promoting the migration tag. Roll back with a verified compatible
volume; rolling back an image does not roll back its store database.
