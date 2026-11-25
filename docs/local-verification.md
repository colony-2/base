# Local build verification

`.c2/recipes/hooks/build/verify.yaml` runs four offline unit suites and a required
native image build and runtime check, independent of the caller's target directory:

| Task | Workflow coverage |
| --- | --- |
| `resolution` | `image.yml` resolve / Test resolution and publication: `test_resolve_versions.py` |
| `publication_planning` | Same workflow step: `test_publish_image.py` |
| `fast_resolution` | Same workflow step: `test_fast_resolution.py` |
| `smoke_versions` | Same workflow step: `test_smoke_versions.py` (mocked version output; no image needed) |
| `image` | Builds and loads the exact candidate on one Docker-capable `ubuntu*` worker and runs the full `tests/shared_nix_store/acceptance.py` suite against the resulting local image |
| `aggregate` | Checks discovery coverage, validates transported results and log hashes, and writes the report and environment record |

The aggregate compares the tests found by CI's `unittest` discovery with those
found by the named suites, without running them again. If a new test is not
covered, verification fails and `verification.md` lists the missing test ID.
Discovery diagnostics go to standard error; aggregate standard output contains
only the hook status (`passed` or `failed`).

The offline suites and aggregate declare Python using the pinned nixpkgs
selector in the recipe. The offline suites use Python's standard library. Imports read
candidate sources, bytecode writes are disabled, and working directories, temporary
files, and caches live in temporary directories. Each suite has a 180-second
subprocess timeout. An ordinary test failure is recorded and does not prevent
independent suites from running. Infrastructure errors outside the reporter fail
the recipe through the caller; they cannot produce a successful hook result.

Request text and target-directory inputs never select commands. Candidate/base
identifiers enter through environment values. No optional artifact schema is used,
so absent, null, and value-backed settings remain compatible with the caller.
Each behavior exports a log and JSON record; the aggregate exports
`verification.md` and `environment.json` as native artifact references.

The image task checks the worker, daemon and bind mount before resolving inputs
for its native architecture. A clean checkout builds with `verify-load`; the
runner neither reuses a published Base manifest nor pushes an image. Acceptance
checks the local image's platform, revision and identity and records skipped
cases with their reasons. `image.json` and five stage logs capture the available
evidence, including failures and stages not run. Placement errors are reported
by C2. The image task has a 110-minute ceiling, with up to 65 minutes for a
cold build. The runner removes named resources owned by its run, not shared Docker
resources. Release CI still gates both native architectures independently;
publication, signing, provisioning and emulation are outside this hook.

To check the reporting failure mechanics:

```sh
python3 -B .c2/tests/test_reporting.py
```

To compile/load the candidate hook through a bounded native runtime caller, run:

```sh
c2j test run --file .c2/tests/verify.test.yaml \
  --out-dir /tmp/base-verify-runtime --case-timeout 2m --artifact-mode inline
```

The fixtures use disposable candidate repositories and late `cell://` loading,
with real command execution, declared packages, artifact transport, and output
schema validation. They test absent/null/value settings, shell-like input,
candidate preservation, both smoke-version tests passing, and a fixture-only
smoke-version failure whose log is retained. The smoke fixture prints during
discovery in both cases to check that the hook status stays clean. The fixtures
use a synthetic image record and cannot satisfy the live image gate. Test
runtime artifacts include stored references plus inline evidence.
