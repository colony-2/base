# Shared Nix store tests

## Production acceptance

After resolving and building the main Dockerfile as described in the repository
README, run:

```sh
python3 tests/shared_nix_store/acceptance.py --image colony-base:local --platform linux/arm64
python3 tests/shared_nix_store/acceptance.py --image colony-base:local --platform linux/amd64
```

This is the suite used by CI before publishing `ghcr.io/colony-2/base2:latest`. It implements
the provider-initialized contract: one initializer prepares a volume before
concurrent use, and the image's `base-store-preflight` validates attachments.
It tests actual c2j recipe subprocesses, offline base tools with an empty mount,
signed cold/warm reuse, modern exact-path Nix commands, private profiles, matching
PIDs, interrupted imports, unsigned rejection, read-only/bad-ownership failures,
and distinct image revisions with the same Nix pin. It also records startup and
lookup timings before/after population with 1,000 extra store objects.

The peer image is a locally derived revision with a changed label and the same
certified Nix package; it is removed after the test. This does not certify future
Nix versions. Helpers are mounted into disposable containers, not shipped as
part of the production image. No initialized store is copied into the image.

## Historical feasibility probes

These tests exercise the two open questions in the Ubuntu proposal: shared local
store concurrency and ownership enforcement. The adjacent Dockerfile is a minimal
test fixture, separate from the production image. It contains Ubuntu, pinned
`nix-bin=2.34.3+dfsg-1`, certificates, Python, and strace. It does not contain c2j or
the full base tool set and ships an empty `/nix`.

```sh
docker buildx build --platform linux/arm64,linux/amd64 --load \
  -t colony-base:shared-store-probe tests/shared_nix_store
python3 tests/shared_nix_store/probe.py --platform linux/arm64
python3 tests/shared_nix_store/probe.py --platform linux/amd64
```

The host needs Python 3.11+ and Docker with support for the requested architecture.
The fixture setup downloads upstream-signed hello, figlet, and toilet closures
from a pinned Nixpkgs revision. Test stores start empty, use Docker local volumes
with `volume-nocopy`, and retain signature checks. Most consumers have networking
disabled; interruption tests use an isolated Docker network and a test HTTP server
serving the unchanged signed cache files. These cache files are fixture inputs,
not the proposed runtime storage design. All reuse is through the shared live
store. Containers, volumes, and networks are named uniquely and cleaned up.

The three repeated concurrency cases default to five rounds each. `worker.py`
waits at a test barrier, records the PID and PID namespace, then execs the real
Nix executable. Both Nix processes therefore have PID 1 in different namespaces.
There is no wrapper locking around Nix commands. `--rounds N` changes the count.

Tests 8 and 9 deliberately extend the real schema-file creation window with
strace's syscall delay facility. They change neither syscall results nor file
contents. A second process opens the store while its schema is still empty; a
separate case kills the initializer and tests recovery without repair. The
emulator in the validation environment could run strace but did not apply this
delay, so those cases are skipped there. Native testing is required for them.

For the small initialization reproducer alone, with no downloads or fixture
population:

```sh
python3 tests/shared_nix_store/reproduce_initialization.py --platform linux/arm64
```

Exit 1 means a store-open failure was reproduced. Exit 77 means the syscall pause
was not established and the runtime cannot test the race using this mechanism.
The reproducer runs two store opens, one while the initializer is paused and one
after it is killed, and removes only its own test resources afterward.

`preflight.py` is a prototype provider-side attachment check, not a Nix wrapper.
It checks a fixed set of directories for root ownership, permissions, symlinks,
and actual writability. It creates and removes temporary probe files, without
initializing Nix, scanning packages, or changing ownership. Tests cover empty and
populated stores, misowned root/store/database directories, unsafe permissions,
symlinks, read-only mounts, and simultaneous preflights. Compatibility metadata,
provider leases, and protection against later mutations by root are outside this
prototype. It has not been integrated into Pulse.

The assertions reflect the requested contract: known stock-Nix failures remain
test failures, not tests marked as expected successes. See
[the recorded findings](../../SHARED_NIX_STORE_PROBE_RESULTS.md) for the outcome
and limitations. APT dependencies other than Nix are resolved at probe build
time; record the resulting image digest when comparing runs.

## Mixed Nix versions

Build two additional minimal probe images; the existing 26.04 probe supplies
Nix 2.34.3. These do not build or change the production image.

```sh
docker buildx build --platform linux/arm64,linux/amd64 \
  --build-arg UBUNTU_IMAGE=ubuntu:22.04@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401 \
  --build-arg NIX_PACKAGE_VERSION=2.6.0+dfsg-3 \
  -t colony-base:nix-compat-2.6 --load tests/shared_nix_store
docker buildx build --platform linux/arm64,linux/amd64 \
  --build-arg UBUNTU_IMAGE=ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 \
  --build-arg NIX_PACKAGE_VERSION=2.18.1+dfsg-1ubuntu5 \
  -t colony-base:nix-compat-2.18 --load tests/shared_nix_store
python3 tests/shared_nix_store/compatibility.py --platform linux/arm64
python3 tests/shared_nix_store/compatibility.py --platform linux/amd64
```

The compatibility matrix checks every pair, sequentially as A → B → A and
B → A → B, with a new signed cached package installed at every step. Concurrent
tests initialize with each member of the pair, then run two Nix processes as
PID 1 in separate namespaces against the same or different missing outputs.
Each concurrent scenario repeats three times (`--rounds N` overrides this).
All imports use an unchanged signed fixture from cache.nixos.org; consumers
have networking disabled. The fixture is only a test data source; both clients
operate directly on the same live `/nix` volume and execute its paths in place.

Database integrity and foreign keys are inspected through read-only SQLite
connections before cross-version validation. Each client checks registration
and recomputes the NAR hash of every path in the tested closures, then executes
the installed programs offline. No database repair command is used. Initialization
is serial to isolate compatibility from the previously identified cold-start
race. GC, builds, profiles, flake evaluation, and arbitrary experimental features
are outside this matrix. Nix 2.6 warns about the newer profile configuration
setting; profile behavior is not part of this test.

A separate case enables `ca-derivations`, lets Nix 2.18 upgrade the actual CA
schema written by 2.6, and asserts that 2.6 subsequently rejects it. This expected
rejection demonstrates a compatibility boundary even when the main schema
number remains unchanged. It does not imply a failure of the default config.
See [compatibility findings](../../NIX_STORE_COMPATIBILITY_RESULTS.md).
