# Reusing the published image

Fresh CI runners import inline BuildKit metadata and layers from
`ghcr.io/colony-2/base2:latest`. There is no separate registry cache image or
GitHub Actions cache. A preliminary `docker pull` is not required by BuildKit.

The resolver checks the current Ubuntu snapshot on every invocation. The
published index records `io.colony-2.base.apt-inputs` (a hash of the Ubuntu index
and both complete package closures) and `io.colony-2.base.apt-snapshot` (the
installation snapshot). Matching inputs retain the published installation
snapshot, including on pushes and manual runs. Changed packages or a changed
Ubuntu base select the current snapshot. Missing metadata, including the first
build after this change, also selects the current snapshot. Malformed matching
metadata and registry/authentication errors fail instead of silently using stale
or untrusted inputs.

Before invoking Docker, it fetches the three Ubuntu `InRelease` files and verifies
their signatures with `gpgv` and the Ubuntu archive keyring. It compares SHA256
checksums of the uncompressed package indexes for all four components and both
architectures, ignoring timestamp/signature/translation-only changes. The plan
key also includes the Ubuntu base, Nix pin, bootstrap CA, package list, and
planning scripts. A matching `io.colony-2.base.apt-plan` annotation supplies the
previously resolved package sets and Nix candidates. Tool-release checks always
run. Missing/nonmatching metadata triggers full resolution; bad signatures or
invalid matching plans fail closed. Hosts without gpgv/the Ubuntu keyring use
the original Docker resolution path.

If indexes changed but the selected packages/tools did not, CI refreshes only
the comparison annotations on `latest`, referencing the previously inspected
index by digest. This preserves its architecture descriptors and attestations;
it does not change filesystem layers or the immutable build tag. The index
digest itself changes. Without this refresh, an unrelated Ubuntu package update
would cause the same expensive resolution on every subsequent hourly check.
Workflow concurrency serializes these metadata updates with image publications.

`versions.json` distinguishes `resolution_snapshot` from the installation
`build_args.APT_SNAPSHOT`. These timestamps do not affect hourly change detection.
The installer still verifies the entire installed APT closure, including base
packages, against the freshly selected versions. Security updates change that
closure and therefore cannot be hidden by snapshot reuse.

APT binds only the target architecture's package list. Each upstream tool's
installation step binds a small JSON file containing only its target artifact
URL and checksum (and native dependency for pnpm). Tools install directly into
final-image layers, so their installation results are available through inline
metadata. Temporary downloads are removed in the same step. The full reporting
manifest is copied near the end, after the expensive steps.

This is ordinary dependent-layer reuse: changing APT invalidates the following
tool layers, and changing one tool invalidates later layers. Node and pnpm come
first, with c2j last. Reporting-only changes do not reinstall packages or tools.
Changing only the other architecture's tool artifact does not invalidate the
target architecture's installation. Layers still have to be downloaded on a
fresh runner, and export and tests still take time.

## Native CI jobs

One resolver job produces a small tar archive of the exact build inputs. Parallel
matrix jobs build/test on `ubuntu-24.04` (amd64) and `ubuntu-24.04-arm` (arm64),
with a host-architecture assertion and no QEMU setup. These are
[GitHub-hosted native runner labels](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
QEMU remains only in the resolver for fallback arm64 APT simulation.

The jobs transfer inputs and exported digest metadata through workflow artifacts,
not image layers or build caches. Each architecture imports the published GHCR
image itself. A final job waits for both successful tests and combines their
exact digests. Per-architecture metadata artifacts support rerunning one failed
job even when its sibling completed in an earlier run attempt. The resolver's
artifact name is passed as an output so partial reruns retain the same inputs.
PRs test both platforms without publishing. Unchanged checks skip both builders;
metadata-only refreshes do not require a build job.

## Verification

`python3 -m unittest discover -s tests -v` checks snapshot retention, updates to
the base and either architecture's packages, first publication, malformed/future
metadata, push behavior, and isolation of tool inputs.

The opt-in `tests/check_image_reuse.py` integration test requires the current
checkout's resolved inputs and an image built from exactly those inputs with
`--cache-to type=inline`. It creates a fresh, disposable BuildKit builder for
each of four cases: reporting/other-architecture changes and a c2j artifact-input
change, for both amd64 and arm64. It asserts that every expected installation
vertex reports `cached: true` in BuildKit's structured progress, and that c2j
executes again when its artifact input changes. Text progress alone can label
materialization of a reused layer as `DONE` while downloading/extracting it.
The c2j fixture changes the URL fragment while retaining the verified bytes.

For an already published matching image:

```sh
python3 tests/check_image_reuse.py --image ghcr.io/colony-2/base2:latest
```

For local validation before publication, use a disposable registry instead:

```bash
mkdir -p .build/cache-validation
cat > .build/cache-validation/buildkitd.toml <<'EOF'
[registry."base-cache-registry:5000"]
  http = true
EOF
docker network create base-cache-validation
docker run -d --name base-cache-registry --network base-cache-validation \
  -p 127.0.0.1:5099:5000 registry:2
docker buildx create --name base-cache-cold --driver docker-container \
  --driver-opt network=base-cache-validation \
  --buildkitd-config .build/cache-validation/buildkitd.toml

args=()
while IFS= read -r arg; do args+=(--build-arg "$arg"); done < .build/build-args.env
docker buildx build --builder base-cache-cold --platform linux/amd64,linux/arm64 \
  "${args[@]}" --cache-to type=inline --output type=image,push=true,oci-mediatypes=true \
  -t base-cache-registry:5000/base2:baseline .
python3 tests/check_image_reuse.py --image base-cache-registry:5000/base2:baseline \
  --network base-cache-validation --buildkitd-config .build/cache-validation/buildkitd.toml

# Pull and run the production acceptance suite on both platforms as well.
for arch in amd64 arm64; do
  docker pull --platform "linux/$arch" localhost:5099/base2:baseline
  python3 tests/shared_nix_store/acceptance.py \
    --image localhost:5099/base2:baseline --platform "linux/$arch"
done

docker buildx rm base-cache-cold
docker rm -fv base-cache-registry
docker network rm base-cache-validation
```

This local registry is only a test fixture. CI continues to use the published
GHCR image and separates each architecture's build, acceptance, and publication
into named steps on its native runner.
