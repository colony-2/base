# Ubuntu shared-store image validation

Validated 2026-10-15 on Docker 29.5.2, native Linux arm64 with amd64 emulation.
The final local image is `colony-base:ubuntu-shared`, OCI index:

`sha256:af69014cf4cbcb4dcc1c3b8cf7ec7762d018c76b26f61f987a4841636a9b3dbb`

This is implementation evidence, not a claim that the image has been pushed or
that Pulse integration has shipped. CI is configured to publish `shared-nix-v1`
after both architecture checks pass; `latest` remains the old image during rollout.

## Inputs

- Ubuntu 26.04 index: `sha256:f144425ff09be612d6d9ad965196e9cdc23dae1f42110a8a11a3e9a8198759f7`.
- Ubuntu snapshot: `20261015T020000Z`, archive signatures and TLS verification enabled.
- Nix: `2.34.3+dfsg-1`, compatibility group `colony-nix-store-v1`.
- Node: 24.22.0; pnpm: 12.11.2; uv: 0.13.1; Ruff: 0.17.0;
  yq: 4.54.1; c2j: 0.0.75.
- Python: 3.14.4. The resolved APT closures contain 277 arm64 and 280 amd64 packages.
- External artifacts were checked against resolved SHA256 hashes, or npm SHA512
  integrity for pnpm and its native dependencies. pnpm's native executable is
  installed during construction and tested offline; it does not bootstrap on launch.
- Resolved input fingerprint:
  `484a5b739ba67bca040a13153012a83d6dfbd874fd942e785e42c0407a0f3a38`.

The runtime manifest also records platform and registry configuration. Exact
package lists, tool artifact URLs and digests are embedded in the image.

## Results

| Check | Native arm64 | Emulated amd64 |
| --- | --- | --- |
| Production acceptance suite | 13/13 pass (64.082 s) | 13/13 pass (122.957 s) |
| Empty `/nix`, network disabled, all base tools | Pass | Pass |
| Direct Nix/c2j entrypoint overrides and login shell | Pass | Pass |
| C/C++ compile/link/run, Git commit, pnpm executing Node, uv venv | Pass | Pass |
| Signed cold substitution; producer removed; offline reuse | Pass | Pass |
| Same/different missing outputs, PID 1 in separate namespaces | 6 rounds pass | 6 rounds pass |
| Private profiles and fresh-container empty profile | Pass | Pass |
| TERM/KILL interrupted substitutions and retry | Pass | Pass |
| Image preflight rejects wrong ownership, modes, symlinks and read-only mounts | Pass | Pass |
| One-time initialization/readiness and no-volume ephemeral use | Pass | Pass |
| Local builds/cache misses and `preferLocalBuild` rejected | Pass | Pass |
| Unsigned cache outputs rejected | Pass | Pass |
| Actual c2j recipe subprocess invokes Nix and executes a cached package | Pass | Pass |
| Two distinct image revisions: both producer directions and concurrent use | Pass | Pass |

The second revision is a derived image with a changed label and the identical
certified Nix package. This validates reuse across image identities, not admission
of another Nix version. The separate [compatibility matrix](../NIX_STORE_COMPATIBILITY_RESULTS.md)
records the older/newer version investigation.

Validation uses read-only SQLite integrity/foreign-key checks, recomputed NAR
hashes, path-registration checks and offline execution. No repair command masks
store errors. The test fixture preserves cache.nixos.org signatures; consumers
share the live store directly. Helpers are mounted only for the tests. All test
containers, volumes and derived peer images were removed afterward.

19 resolver/change-detection unit tests pass, including APT dependency changes,
timestamp-only skips, Node major pinning, both architectures, native pnpm inputs,
missing checksums and registry failures. Ruff, ShellCheck, Python compilation,
Actionlint and Git whitespace checks pass. The registry publication itself was
not exercised against GHCR; CI explicitly exports OCI media types before creating
the annotated index so the hourly fingerprint is retained.

## Startup measurements

These are observations on this development environment, not performance guarantees.
The populated case has 1,000 extra locally added store objects plus the signed
hello/figlet/toilet closures (17 paths). Five Docker samples include create,
start, wait and log retrieval; ten in-container samples measure direct execution
and exact-path validity lookup separately. Cold initialization excludes downloads.

| Measurement | arm64 | amd64 under emulation |
| --- | --- | --- |
| Nix initialization | 31.78 ms | 83.13 ms |
| Empty-store Docker invocation, median | 105.05 ms | 116.32 ms |
| Populated-store Docker invocation, median | 113.94 ms | 125.99 ms |
| Direct hello execution, median | 0.72 ms | 14.17 ms |
| Exact-path validity lookup, median | 5.33 ms | 74.99 ms |

The image has no entrypoint or Docker `VOLUME` declaration. Ordinary startup adds
no store traversal, copying, restoration, GC or ownership repair. These timings
do not establish asymptotic behavior of Nix's internal database operations.

## Image contents and limits

Both images have 12 filesystem layers. Docker reports about 395 MiB (arm64) and
413 MiB (amd64) of image content; allocated `/usr` + `/opt` + `/var` measure about
1.14 GiB and 1.17 GiB respectively. These are different measures. The full compiler,
Python development, Nix and other requested tool dependencies remain substantial.
Most small layers are configuration/metadata; `/nix` itself is empty (one directory).
Nix's dynamic dependencies resolve outside `/nix`, and its real executable is
`/usr/bin/nix`.

The known stock-Nix cold-start race and root ownership behavior were not fixed
inside Nix. This release assigns those responsibilities to Pulse's single
initializer and attachment checks. Original reproducers remain available and
are not silently marked successful. See [the provider contract](shared-nix-store.md).

Actual Colima CI, native amd64 hardware, supplied-lease execution against Pulse,
prepared-record generation/lifetime handling, live GC and future Nix-version
certification are outside these local results. Live GC is prohibited by v1.

Local build/test logs, image inspection, exact manifests and timing data are in
`.build/ubuntu-migration/`. Reproduce with the commands in the repository README
and `tests/shared_nix_store/acceptance.py`.

## Resolver CI failure reproduced on 2026-10-19

The original validation used Docker 29.5.2's containerd image store. It did not
cover the classic Docker image store used by the usual GitHub Actions runner
setup ([Docker documentation](https://docs.docker.com/build/ci/github-actions/multi-platform/)).
The failed GitHub run's traceback hid Docker's stderr; its archived logs were
not accessible without authentication from this environment.

An isolated Docker 28.5.2 daemon with the classic `vfs` store reproduced a matching
exit 125. Concurrent `docker run --platform linux/amd64` and
`docker run --platform linux/arm64` against the reported Ubuntu index
`sha256:c098726dd9b9733f3e885f8de0ef375206a311b0a317eadc40d347b1115fb944`
ran arm64 successfully but failed amd64 with:

```text
docker: cannot overwrite digest sha256:c098726dd9b9733f3e885f8de0ef375206a311b0a317eadc40d347b1115fb944
```

The resolver now selects each architecture's child manifest digest from the
same verified index for APT containers. BuildKit still receives the index digest.
Failures include architecture, image, snapshot, exit status, stdout and stderr;
cleanup timeouts cannot mask the original diagnostic.

Validation of this resolver change:

- Complete live version resolution, including concurrent APT plans for both
  architectures, passes on Docker 28.5.2/classic and Docker 29.5.2/containerd.
- Both runs use the reported Ubuntu index and snapshot `20261019T200000Z`.
- All 22 unit tests pass, including child-manifest selection, Docker/APT error
  diagnostics, and a timeout with a second timeout during cleanup.
- Ruff checks pass with the Python 3.10 target. Logs are in `.build/ci-fix/`.

This validation covers the resolver fix. It does not claim a rerun of GitHub CI
or new image builds; the runtime filesystem is unchanged by this patch.
