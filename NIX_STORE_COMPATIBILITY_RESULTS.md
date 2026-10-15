# Nix shared-store version compatibility

Reviewed and tested on 2026-10-15. This supplements the
[initialization and ownership probes](SHARED_NIX_STORE_PROBE_RESULTS.md), not a
production-image implementation. The question is whether different Nix versions
can directly use the same live `/nix`, sequentially and concurrently.
The subsequent image implementation is recorded in
[the production validation report](docs/image-validation.md).

## What upstream promises, and what it does not

There is no basis for promising arbitrary newer → older store access. Nix's
official troubleshooting guide explicitly documents older Nix rejecting a store
whose database a newer Nix has upgraded. Package/profile rollback is not a
promise to downgrade Nix's database automatically.
[Official troubleshooting](https://nix.dev/guides/troubleshooting#how-to-fix-error-current-nix-store-schema-is-version-10-but-i-only-support-7).

The local-store implementation has explicit upgrade paths and rejects schemas
newer than it understands. Upgrades themselves have limits: very old formats
require an intermediate Nix version. The main-schema upgrade path takes an
exclusive store lock; ordinary store opens hold a shared lock. Therefore a
migration can also wait for existing clients. This is separate from the earlier
fresh-store initialization bug. Nix 2.34 additionally records named migrations
in `SchemaMigrations`, so checking only the integer in `db/schema` is insufficient.
[Nix 2.34.3 local-store implementation](https://github.com/NixOS/nix/blob/2.34.3/src/libstore/local-store.cc).

Experimental features explicitly may change or disappear without compatibility.
In particular, the manual says that `git-hashing` can create objects older Nix
versions cannot understand. Compatibility must include the feature set, not just
the Nix version or main database schema. Enabling `nix-command` and `flakes`
does not enable every other experimental feature.
[Experimental feature policy and inventory](https://nix.dev/manual/nix/2.34/development/experimental-features.html).

Historical source illustrates why exact versions matter. Nix 1.11 originally
rejected schemas above its supported version 7. Nix 1.11.16 instead accepts
schema numbers below 256 and has accommodations for later schema changes. Thus
even “1.x versus 2.x” is too coarse a compatibility rule. These historical 1.x
versions were reviewed in source, not executed in this matrix.
[Nix 1.11](https://github.com/NixOS/nix/blob/1.11/src/libstore/local-store.cc),
[Nix 1.11 schema constant](https://github.com/NixOS/nix/blob/1.11/src/libstore/local-store.hh),
[Nix 1.11.16](https://github.com/NixOS/nix/blob/1.11.16/src/libstore/local-store.cc).

Daemon protocol compatibility is a different interface: this design uses
`NIX_REMOTE=local`, so clients themselves open the database. A protocol handshake
with a single daemon does not mediate these clients. I found no documented
blanket guarantee for arbitrary mixed-version direct access across separate PID
namespaces in the reviewed material.
[Local store](https://nix.dev/manual/nix/2.34/store/types/local-store.html),
[Local daemon store](https://nix.dev/manual/nix/2.34/store/types/local-daemon-store.html).

## Test configuration

| Nix | Ubuntu | Pinned APT package |
| --- | --- | --- |
| 2.6.0 | 22.04 | `2.6.0+dfsg-3` |
| 2.18.1 | 24.04 | `2.18.1+dfsg-1ubuntu5` |
| 2.34.3 | 26.04 | `2.34.3+dfsg-1` |

Minimal probe images install Nix and its libraries outside `/nix`. All clients
are root, share a Docker local volume mounted at `/nix` with `volume-nocopy`,
and use the same configuration as the earlier probe. Builds, automatic GC,
and store optimization are disabled. The main matrix enables `nix-command`
and `flakes`; the separate boundary case additionally enables `ca-derivations`.
Nix 2.6 warns about the unknown `use-xdg-base-directories` setting; profile
selection is outside this matrix.

The fixture contains unchanged, signed cache.nixos.org artifacts for `hello`,
`figlet`, and `toilet`, resolved using Nixpkgs commit
`39ad350a0602fa0a58a544344e3e9187526ea45c`. Consumer containers have no network
access and substitute from that fixture into the same live store. Packages
already present execute directly from the shared paths. The fixture is test
input, not a proposed per-container cache-restoration architecture.

For every pair A/B:

- Run A → B → A and B → A → B on separate fresh volumes. Each step imports a
  different missing package and checks all packages written so far. Both clients
  then validate the complete result.
- Initialize a populated store with A or B, then launch A/B together at a
  barrier. Test both the same missing output and different missing outputs,
  three rounds each. Both processes are PID 1 in different PID namespaces.
- Before cross-version validation, inspect SQLite using a read-only connection:
  `integrity_check`, `foreign_key_check`, schema marker, table names and named
  migrations. Each client checks registration, recomputes NAR hashes across the
  tested closures and executes the packages offline. No database repair command
  is used.

The matrix has six sequential sequences and 36 concurrent rounds per
architecture, plus one experimental migration/downgrade boundary case.
Initialization is deliberately serial to isolate compatibility from the known
cold-start races. Concurrent tests use already populated stores.

## Results

Both runs completed successfully: native arm64 in 125.518 seconds and emulated
amd64 in 376.725 seconds. Across both architectures, all 12 sequential sequences
and 72 concurrent rounds passed, and both experimental boundary cases reproduced
the expected downgrade rejection. The runner reports three top-level unittest
tests per architecture, with these scenarios inside them.

| Pair | arm64: both sequential orders | arm64: concurrent rounds | amd64: both sequential orders | amd64: concurrent rounds |
| --- | --- | --- | --- | --- |
| 2.6.0 / 2.18.1 | Pass | 12/12 pass | Pass | 12/12 pass |
| 2.6.0 / 2.34.3 | Pass | 12/12 pass | Pass | 12/12 pass |
| 2.18.1 / 2.34.3 | Pass | 12/12 pass | Pass | 12/12 pass |

The ordinary store retained main schema **10** throughout. Opening through
2.34.3 added the `SchemaMigrations` table to older stores; the tested older
clients continued reading and writing them successfully. All checked database
integrity, foreign-key, content-hash and program-execution assertions passed.
The complete tested closure contains 17 store paths.

The separate experimental test reproduced an actual incompatible downgrade:

1. Nix 2.6.0, with `ca-derivations`, created CA schema **3**.
2. Nix 2.18.1, with that feature, upgraded it to CA schema **4**.
3. Nix 2.6.0, with that feature, then failed with:

   ```text
   error: current Nix store ca-schema is version 4, but I only support 3
   ```

The main schema stayed at 10, and SQLite integrity remained valid. Turning off
that extra feature allowed ordinary imports through 2.6.0 again. This is a
feature compatibility boundary, not corrupted data and not a failure of our
proposed default configuration. It agrees with the explicit guards in
[Nix 2.6.0](https://github.com/NixOS/nix/blob/2.6.0/src/libstore/local-store.cc)
and [Nix 2.18.1](https://github.com/NixOS/nix/blob/2.18.1/src/libstore/local-store.cc).

These are focused substitution/read/write tests, not a certification of every
operation in these releases. They do not cover concurrent main-schema upgrades,
GC, source builds, mutable profiles, flake/Nixpkgs evaluation across versions,
arbitrary new object formats, native amd64 hardware, or actual Colima CI. They
also do not resolve the earlier concurrent initialization failure. A passing
matrix establishes evidence for this workload and configuration, not a promise
for future versions.

## Release policy implied by these findings

Keep one certified Nix package and feature set across ordinary base-tool image
updates. Those images can keep sharing the existing volume; an image digest
change alone does not require another store or copying packages.

Treat a Nix/patch/feature update as a candidate for the same compatibility group.
Run the upgrade/downgrade and concurrency checks against every Nix version still
allowed to attach, including the oldest retained rollback image. Adjacent-pair
success does not establish compatibility with every earlier release. Review
schema migrations and object-format changes as well as running the tests.

If an incompatible migration is required, do not attach an older image to that
upgraded volume. Use a new compatibility group/volume, or an explicitly managed
offline transition that retires incompatible clients. Rolling back only the
container image does not roll back the volume's database. Do not enable
additional store-affecting experimental features casually in a shared group.

This policy is a project release contract supported by tests, not an upstream
promise that all future Nix versions will interoperate. The earlier initialization
and ownership findings still need resolution before publishing the full design.

## Reproduction and provenance

See [test instructions](tests/shared_nix_store/README.md#mixed-nix-versions) and
[the executable matrix](tests/shared_nix_store/compatibility.py).
Local OCI index identities used:

| Image | Index digest |
| --- | --- |
| `colony-base:nix-compat-2.6` | `sha256:bce56ae65604fe0f92dd1665386ddfb9f96b7cbe3b4f9092e079b7712d74cf7d` |
| `colony-base:nix-compat-2.18` | `sha256:1c07702af7aaf2a04398c23800c236c1e13785a34ee6a2867354d490cf56b361` |
| `colony-base:shared-store-probe` | `sha256:d973d7c7ee255c63dc372fe17dfc874844fed2ebbefac972c649365789426b8f` |

Build logs, resolved dependency manifests for each version/architecture, runtime
logs and reviewed source files are retained locally under `.build/nix-compat/`.
APT dependencies other than Nix were resolved at build time. Rebuilding later
may change them; compare image identities and manifests.

All test containers and volumes were removed after the runs; the local probe
images remain. Python compilation and Ruff checks passed. Production image
sources were not changed, and no commit was made.
