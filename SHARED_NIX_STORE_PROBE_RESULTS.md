# Shared Nix store probe results — 2026-10-15

**The proposed direct shared-store contract is blocked on Nix initialization.**
The stock Ubuntu Nix package failed an ordinary simultaneous first-use test and
two controlled initialization/recovery tests on native arm64. The proposed
provider-side ownership preflight passed its focused tests, but stock Nix does
not enforce that ownership policy itself.

Only a minimal test image was built. The production Dockerfile, workflow, and
published image are unchanged. The test code is in
[tests/shared_nix_store](tests/shared_nix_store/README.md).

| Environment/input | Value |
| --- | --- |
| Docker daemon | 29.5.2, Linux arm64 |
| Platforms exercised | Native arm64; amd64 through the daemon's emulation |
| Ubuntu base | 26.04, pinned index `sha256:f144425ff09be612d6d9ad965196e9cdc23dae1f42110a8a11a3e9a8198759f7` |
| Nix package | `nix-bin=2.34.3+dfsg-1` on both platforms |
| Local probe image | `colony-base:shared-store-probe` |
| Probe image index | `sha256:d973d7c7ee255c63dc372fe17dfc874844fed2ebbefac972c649365789426b8f` |
| Test tools | Python `3.14.3-0ubuntu2`, strace `6.19+ds-0ubuntu5` in the containers |
| Package fixture | Upstream-signed hello, figlet, and toilet closures from Nixpkgs revision `39ad350a0602fa0a58a544344e3e9187526ea45c` |
| Store configuration | Direct local access, shared `/nix`, no daemon, automatic GC disabled, signature checks and build denial enabled |

The image is loaded locally, not published. The exact index identifies this run;
APT dependencies other than Nix were resolved at build time. No host PID sharing,
privileged containers, additional capabilities, host Nix, or global command lock
was used. Each scenario used disposable Docker local volumes with copy-up
disabled.

| Check | Native arm64 | Emulated amd64 |
| --- | --- | --- |
| Empty store, executable availability, preflight and initialization | Passed | Passed |
| Signed package survives producer removal and executes directly offline | Passed | Passed |
| Simultaneous first use, identical package, both Nix processes PID 1 | **4/5 rounds passed; one failed with SQLite busy** | 5/5 passed |
| Simultaneous first use, different packages, both Nix processes PID 1 | 5/5 passed | 5/5 passed |
| Concurrent substitutions into an already populated store | 5/5 passed | 5/5 passed |
| Concurrent modern default profiles remain container-private | Passed | Passed |
| Kill a substitution with TERM/KILL; another same-PID container completes it | Both passed | Both passed |
| Another client opens during schema publication | **Failed: schema is corrupt** | Untested: syscall delay ineffective under emulation |
| Kill initializer during schema publication, then reopen without repair | **Failed: schema is corrupt** | Untested: syscall delay ineffective under emulation |
| Stock Nix rejects `/nix` owned by UID/GID 123 | **Failed: initialization exited 0** | **Failed: initialization exited 0** |
| Prototype preflight rejects unsupported ownership/modes/symlinks | Passed | Passed |
| Read-only preflight and substitution fail without a private-store fallback | Passed | Passed |
| Four concurrent preflights leave an empty store empty | Passed | Passed |
| Missing outputs cannot build, including `preferLocalBuild` | Passed | Passed |

The final native suite ran 14 tests in 62.526 seconds: 10 test methods passed and
4 failed (one failure was a repeated scenario's subtest). The amd64 suite ran 14
tests in 96.018 seconds: 11 passed, 1 failed, and 2 were skipped. The suite exits
nonzero because its assertions express the requested contract. Failures have not
been converted into expected passes.

An earlier run's ordinary concurrency cases all passed. The repeat run caught
the SQLite failure below. These small samples are evidence of a real failure,
not an estimate of its frequency or proof that other race windows do not exist.

The ordinary failure occurred in round five of two synchronized containers
installing the same signed hello output into an empty store. Each container ran
the real Nix executable as PID 1 in a different PID namespace. No syscall delay
or data mutation was used in this case. One command exited 1:

```text
error: SQLite database '/nix/var/nix/db/db.sqlite' is busy
```

The controlled tests isolate a second first-use window. Nix opens
`/nix/var/nix/db/schema` with creation/truncation flags before writing its version.
The test delays the return from that real `openat` syscall, leaving the file at
zero bytes. It changes no syscall result, Nix code, database bytes, or schema
contents. This extends a scheduling window in the actual initialization path.

A concurrent opener fails instead of waiting for initialization. Killing the
initializer in that window also leaves subsequent opens failing. The standalone
offline reproducer returned:

```text
while initializer is paused: exit 1
error: "/nix/var/nix/db/schema" is corrupt

after initializer is killed: exit 1
error: "/nix/var/nix/db/schema" is corrupt
```

This violates both simultaneous first-use and recovery-without-manual-repair
requirements. It occurs before package substitution, so it is separate from the
known PID-based temporary GC-root issue. Disabling automatic GC does not fix it.
No manual lock deletion, schema rewrite, or store reset was performed between
the failed opens. Test volumes were removed only after the scenario ended.

The emulator could execute `strace true`, but did not establish the delayed
schema-open window. The tests explicitly check the zero-byte state, detect this
limitation, and skip those cases. The standalone reproducer exits 77 there.
There is no claim that amd64 passes these two checks; native amd64 validation is
still required. Native arm64 failure is already sufficient to block the proposed
multi-platform contract for this Nix package.

Ownership results are independent of those races. Root successfully initialized
`/nix` owned by UID/GID 123 on both platforms. The prototype
[preflight.py](tests/shared_nix_store/preflight.py) rejected that case, misowned
store/database directories, group/world-write permissions, missing owner-write
permission, a symlinked store directory, and a read-only mounted store. Accepted
preflights created and removed only temporary write probes in a fixed number of
directories. They did not create a Nix database, seed packages, recursively chown,
or enumerate store objects.

The preflight demonstrates a workable **provider-enforced** ownership rule.
Pulse must actually invoke it and refuse attachment on failure. It is not
integrated into Pulse, does not check provider compatibility metadata or leases,
and does not protect against later changes by a cooperating workload that stops
following the contract. A direct Nix command still accepts unsupported ownership
unless Nix itself is changed. That proposed requirement adjustment remains
explicit.

There are two possible next steps before building the full replacement image:

1. Preserve the requirements unchanged: select or patch a Nix package with
   serialized, recovery-safe first-use initialization, then rerun these failures
   and the concurrency suite. The fix must address both database contention and
   publication/recovery of initialization metadata. Strict ownership rejection
   inside every direct invocation also needs a package-level check.
2. Change the provider contract: Pulse prepares each new volume through one
   initializer, validates readiness, and exposes it to workers only afterward.
   If initialization is interrupted, discard that never-exposed new volume and
   retry with a new one. Combine this with the tested attachment preflight.
   Ordinary worker commands can still run concurrently without wrappers. This
   changes the direct concurrent first-use/recovery requirements and is not
   implemented or fully tested by these probes.

Do not work around the findings by adding automatic retries around arbitrary
user commands, removing schema/lock files on startup, or silently changing PID
namespaces. The current evidence supports continued investigation of the Ubuntu
architecture, but not publishing it with the proposed compatibility label yet.

Reproduce with:

```sh
docker buildx build --platform linux/arm64,linux/amd64 --load \
  -t colony-base:shared-store-probe tests/shared_nix_store

# Full focused suite; known contract failures currently make it exit nonzero.
python3 tests/shared_nix_store/probe.py --platform linux/arm64
python3 tests/shared_nix_store/probe.py --platform linux/amd64

# Minimal offline initialization reproducer, best run on native Linux arm64 here.
python3 tests/shared_nix_store/reproduce_initialization.py --platform linux/arm64
```

Local logs and resolved package manifests are retained in
`.build/shared-store-probe/`. The test resources were cleaned up, while the local
probe image remains available for reproduction. Python compilation and Ruff
checks passed. This was not full c2j/base-tool, mixed-version, performance, or
actual macOS CI Colima acceptance; those gates remain open.

Subsequent mixed-version testing and the upstream compatibility review are
recorded separately in [Nix store compatibility results](NIX_STORE_COMPATIBILITY_RESULTS.md).
Those tests use read-only SQLite inspection and explicit per-path NAR hashing,
without the `nix-store --verify` command used above (which can repair database
inconsistencies). They initialize serially and do not resolve the cold-start
or ownership failures in this report.
