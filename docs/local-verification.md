# Local build verification

`.c2/recipes/hooks/build/verify.yaml` runs three independent, offline unit suites
from the candidate repository, irrespective of the build target directory:

| Task | Workflow coverage |
| --- | --- |
| `resolution` | `image.yml` resolve / Test resolution and publication: `test_resolve_versions.py` |
| `publication_planning` | Same workflow step: `test_publish_image.py` |
| `fast_resolution` | Same workflow step: `test_fast_resolution.py` |
| `aggregate` | Validates transported results and log hashes; writes the aggregate report and environment record |

Each const operation independently declares Python using the pinned nixpkgs
selector in the recipe. The suites use Python's standard library. Imports read
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

The real resolver (registry/upstream access and Docker APT planning), Docker,
buildx, QEMU, image reuse probes, both architecture image builds, shared-store
container acceptance, GHCR authentication, image/metadata publication, signing,
and hosted Actions artifact transfer remain held. The hook never probes for
these capabilities to expand its scope. Existing CI remains unchanged.

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
candidate preservation, baseline success, and an appended failing test in a
fixture-only copy of the publication suite. They do not submit a merge or publish
build. Test runtime artifacts include stored references plus inline evidence.
