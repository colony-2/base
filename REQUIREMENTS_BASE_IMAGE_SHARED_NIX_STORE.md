# Requirements: empty image store with a shared live Nix volume

Status: proposed handoff to the `ghcr.io/colony-2/base` image team.

This proposal supersedes the binary-cache design in
[REQUIREMENTS_BASE_IMAGE_NIX_BINARY_CACHE.md](REQUIREMENTS_BASE_IMAGE_NIX_BINARY_CACHE.md)
and its implementation guide, [nix-binary-cache.md](nix-binary-cache.md), for the
intended Pulse integration. It does not describe functionality already shipped.
Do not remove support needed by existing consumers without a coordinated rollout.

## Objective

Make the base image independent of `/nix`, so Pulse can mount an initially empty
Docker local volume at `/nix`. Nix populates this shared live store on demand.
Subsequent containers execute installed packages directly from the same store.

The primary requirement is low startup cost across invocations: no image-store
seed, per-container package restoration, archive extraction on a warm hit,
whole-store scan, or export after commands. Package installation by c2j, coding
agents, and arbitrary subprocesses must use the shared store through ordinary
Nix commands. A completed installation must remain reusable after its producing
container exits or is removed.

Different image revisions must be able to reuse a volume when they advertise a
compatible store contract. Sharing must not require identical image digests.

## Base image requirements

### 1. Run independently of `/nix`

Ship Nix outside `/nix`, together with everything required to execute it. The
packaging method is up to the image team: a suitable distribution package, an
external runtime bundle, or a self-contained build are possible approaches.

The complete runtime closure must work with an empty mount hiding `/nix`:

- Nix executables, dynamic loader, shared libraries, and helper programs.
- Configuration, CA certificates, trusted keys, registries, and build-denial hook.
- Shells or interpreters used by launch scripts and wrappers.
- c2j and all other tools promised as installed in the base image.

Moving only a symlink or executable is insufficient if its interpreter, libraries,
RPATH, helper references, or configuration still require image-provided store paths.
The current image's Nix resolves into `/nix/store`; this must change.

Ship `/nix` as an empty mount point with no preinstalled store packages or store
database. Do not install packages there during image construction and then rely
on Docker copying them into a volume. The image must also work without a volume;
in that case Nix may initialize an ephemeral store in the container filesystem.

Keep ordinary image tooling outside Nix if necessary to satisfy this contract.
No image startup path may require fetching packages to make Nix or c2j runnable.

### 2. Use the mounted store directly

The integration contract is:

| Property | Required behavior |
| --- | --- |
| Mount | One Docker local volume at `/nix`, with Docker copy-up disabled (`NoCopy` / `volume-nocopy`). |
| Store path | Standard `/nix/store`; preserve absolute paths used by Nix packages. |
| Store metadata | Shared database and coordination state under `/nix/var/nix`, consistent with the mounted store. |
| Access | Direct local-store access, `NIX_REMOTE=local`; no Nix daemon required. |
| Initial user | Root, with consistent ownership across supported images. |
| Storage | Docker daemon's local Linux filesystem, including inside the Colima VM. |
| First use | Initialize an empty volume automatically through Nix; no manual setup. |

Do not keep separate per-container validity databases for the same writable store.
Do not configure a redirected logical store path such as `/nix2/store`.

The image must support direct entrypoint overrides, including launching
`c2j run with-lease` without the image's normal entrypoint or an interactive shell.
Any indispensable initialization must still occur in this mode and be safe when
multiple containers first use the volume simultaneously.

Normal Nix installation and lookup must not require a cache-specific wrapper,
export step, `nix copy`, or a `COLONY_NIX_CACHE_DIR` setting. Disable or bypass the
previous binary-cache export machinery in this mode. There must be no recursive
startup `chown`, package enumeration, or work proportional to existing store size.

### 3. Preserve the prebuilt-output-only policy

Keep existing substituters, trusted public keys, certificate configuration, and
signature verification. Fetch missing outputs from the configured substituters.
If no permitted substituter supplies an output, fail clearly rather than build.

Preserve the existing policy covering local builds, remote builders, and
`preferLocalBuild` derivations. Keep the build-denial hook outside `/nix`.
Commands named `nix build` may realize existing or substituted outputs; their name
must not be interpreted as permission to compile missing outputs.

The shared live store is authoritative storage, not an optional archive cache.
An unwritable or incompatible mounted store must produce a clear nonzero failure;
do not silently switch to a private store or weaken trust checks.

### 4. Support concurrent containers within a defined operating contract

Support overlapping reads and substitutions from multiple containers with their
normal, separate PID namespaces. Use Nix's store coordination; do not serialize
entire user commands, `nix shell` sessions, or executed applications behind a
global wrapper lock.

An empty initial store removes the seeding problem, but does not itself prove
cross-container concurrency safe. Validate the exact shipped Nix version for
first-use races, duplicate substitutions, cancellation, and colliding process IDs.
If a remaining Nix limitation prevents this contract, report it as a rollout
blocker with a reproducer rather than silently requiring host PID sharing,
privileged containers, or a daemon.

Disable automatic GC (`min-free = 0`) and any image startup or background pruning.
Do not run explicit GC or destructive store maintenance while workers use the
volume. Nix temporary GC roots assume unique process IDs; separate PID namespaces
violate that assumption. Disabling automatic GC alone is not a guarantee that all
cross-container races are resolved, which is why collision tests are required.

Pulse will retire whole volumes only when no workers or initializers use them.
Fine-grained live GC is outside this contract. Root users can override policy;
this is an operating contract for cooperating workloads, not a security boundary
against a malicious root process.

Keep mutable user profiles independent between containers. Ordinary
`nix profile install` in one container must not change another container's default
profile. Configure defaults accordingly and document their locations. Shared
immutable package paths are intended; a shared mutable root-user profile is not.

### 5. Advertise compatibility explicitly

Proposed OCI image labels, to be agreed with Pulse before implementation:

| Label | Example | Meaning |
| --- | --- | --- |
| `io.colony-2.base.nix-shared-store` | `1` | Implements this empty-store/direct-volume contract. |
| `io.colony-2.base.nix-store-compat` | `colony-nix-store-v1` | Opaque compatibility group for sharing a live store. |

Publish the exact Nix version and supported platforms in the image guide. Maintain
the same compatibility group across image changes only after verifying mixed-image
concurrent access and reuse of stores written by each supported Nix version.
Account for database migrations and older clients reopening a newer-written store.
Change the group when compatibility cannot be maintained; image digest changes
alone should not force a new group.

Pulse will additionally isolate volumes by deployment/tenant trust boundary,
platform, and cache generation. Sharing across unrelated tenants, incompatible
Nix versions, network filesystems, or macOS host-directory bind mounts is outside
the initial scope. A macOS Colima local volume is inside the Linux VM and is part
of the required test matrix.

## Acceptance tests

Provide an automated Docker acceptance suite with finite per-operation timeouts,
failure diagnostics, and cleanup. Run on supported Linux architectures and the
actual macOS CI Colima configuration. Record image digests and Nix versions.

1. **Empty mount and direct launch.** Mount a new empty volume at `/nix` with
   `volume-nocopy`, override the entrypoint, and run Nix and c2j directly. Verify
   promised base tools work without network access and without store packages.
   Separately verify ordinary Nix use without a mounted volume.
2. **Cold population.** Fetch a known, upstream-signed package and its closure into
   a fresh store. Verify execution, valid registration, and automatic initialization.
3. **Warm direct execution.** Remove the producer, start another container with
   the same volume and networking disabled, and execute the exact installed
   `/nix/store/.../bin/...` path. Verify there is no restoration or export step.
   Also verify Nix recognizes the exact output as valid. This does not require
   offline resolution of uncached flake inputs or mutable selectors.
4. **Different images.** Repeat warm reuse and concurrent use across two distinct
   image revisions advertising the same compatibility group. Include every Nix
   version combination claimed compatible, in both producer/consumer directions.
5. **Concurrent first use and installation.** Start containers against one empty
   volume simultaneously; install both identical and distinct outputs. Exercise
   matching Nix PIDs in separate PID namespaces. Check integrity, registered
   closures, successful execution, and absence of deadlocks or partial results.
6. **Interruption.** Kill a container during initialization or substitution while
   another uses the store. Verify subsequent commands can recover or retry with
   no manual lock removal, store reset, or loss of previously valid packages.
7. **Subprocess coverage and profiles.** Install through a noninteractive child
   process representative of an agent started by c2j. Verify reuse from another
   container and independent default profiles. Exercise supported modern and
   legacy Nix commands; document restrictions imposed by the no-build policy.
8. **Failure policy.** Verify read-only mounts and unsupported ownership fail
   clearly; unavailable prebuilt outputs fail without local or remote builds;
   signature checks and the `preferLocalBuild` denial remain effective.
9. **Startup cost.** Report cold first-use and warm startup/lookup timings with
   both small and populated stores. Demonstrate that container startup performs
   no copying, whole-store scan, or recursive metadata changes. Separate package
   lookup from network-dependent flake evaluation in measurements.

## Delivery and provider rollout

The image team should deliver the image, source changes, acceptance suite and
results, supported compatibility group, and a concise integration guide documenting
paths, permissions, configuration, profile behavior, and known limitations.

Pulse will separately implement capability detection, automatic volume allocation,
mounting, compatibility-based cache identity, and safe whole-volume retention.
Existing Pulse currently seeds `/nix` and keys caches by image identity; publishing
the new image alone does not complete this integration.

Use a new provider cache layout/generation for rollout. Do not repurpose existing
seeded-store or binary-cache volumes in place. uv and pnpm caching remain outside
this change. No host Nix installation, host filesystem configuration, privileged
mount, or manual user setup may be required.

## References

- [Nix local-store documentation](https://nix.dev/manual/nix/2.35/store/types/local-store.html)
- [Nix GC implementation and temporary-root PID assumptions](https://github.com/NixOS/nix/blob/2.34.8/src/libstore/gc.cc)
