# Purpose

This repository owns the Colony 2 development container image and its build,
validation, and publication tooling. Its current artifact is
`ghcr.io/colony-2/base2:latest`, an Ubuntu image for `linux/amd64` and
`linux/arm64` that supplies ready-to-use development tools and supports a
provider-managed shared Nix store. The `base2` name distinguishes this interface
from the separate `ghcr.io/colony-2/base:latest` contract.

## Responsibilities

- Define the image filesystem and runtime defaults in [Dockerfile](Dockerfile),
  including Bash, Node.js, Python, Nix, c2j, and the tools described in
  [README.md](README.md).
- Resolve and verify build inputs, including the Ubuntu image digest, complete
  APT package closure for both architectures, and external tool artifacts.
  Maintain installation and publication tooling in [scripts](scripts), the
  package selection in [apt/packages.txt](apt/packages.txt), and the certified
  Nix pin in [apt/nix-version](apt/nix-version).
- Maintain cache-based Nix policy and the pinned registry in
  [nix/runtime.conf](nix/runtime.conf) and [nix/registry.json](nix/registry.json).
  Keep Nix and all base tools usable outside an initially empty `/nix`.
- Validate tools, input resolution, publication, and shared-store behavior through
  [tests](tests) and image smoke checks. Build and test both architectures before
  publishing the multi-platform image through the
  [image workflow](.github/workflows/image.yml). Publish immutable build tags
  alongside `latest`, and track resolved inputs and installed packages in image
  metadata.

## Interfaces

Containers run as root, default to Bash, and use `/workspace` as the working
directory. The image bundles upstream c2j as an executable; it does not implement
c2j itself. Tool inventory, build/update behavior, and local validation commands
are documented in [README.md](README.md).

The shared-store interface is an empty `/nix` mount point, direct local Nix
access, and container-private mutable profiles. Signed binary substitution is
enabled; local and remote Nix builds are disabled by default. These settings
support cooperating root workloads and are not an isolation boundary against root.

Image labels advertise shared-store support, the `colony-nix-store-v1`
compatibility group, and provider-owned initialization. The image supplies
`base-smoke-test` for runtime checks and `base-store-preflight` for bounded mount
and readiness diagnostics. Exact labels, paths, supported storage, compatibility
rules, and operating constraints are defined in the
[Pulse integration contract](docs/shared-nix-store.md).

## Ownership boundaries

Pulse owns volume provisioning and leases, one-time initialization, readiness
publication, attachment and compatibility checks, tenant/generation identity,
and retention. This repository supplies the image, policy, diagnostics, and
compatibility evidence for that integration. The preflight diagnostic does not
initialize or repair stores and does not replace provider coordination.

Live shared-store garbage collection, deletion, repair, and optimization are
outside the supported contract. Pulse retires whole volumes only after all users
detach. Provider rollout and end-to-end lease execution validation belong to
Pulse; image acceptance tests do not establish that rollout is complete.

Upstream tools and binary caches remain external dependencies. This repository
owns their selection, installation, and image integration, rather than their
implementations or application workloads. Changes to Nix or store-affecting
features require compatibility validation against every admitted version,
including rollback images, as described in the integration contract.
