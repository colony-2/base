#!/bin/bash
# Shared options for the separately timed CI build and publication steps.
set -euo pipefail
arch=${1:?Expected amd64 or arm64}
case "$arch" in amd64|arm64) ;; *) exit 2 ;; esac
case "${2:-}" in
    load) output=(--load) ;;
    push) output=(--output 'type=image,push=true,oci-mediatypes=true') ;;
    *) echo 'Expected load or push' >&2; exit 2 ;;
esac
: "${IMAGE:?}" "${GITHUB_RUN_ID:?}" "${GITHUB_RUN_ATTEMPT:?}" "${GITHUB_SHA:?}" "${GITHUB_REPOSITORY:?}"
args=()
while IFS= read -r arg; do args+=(--build-arg "$arg"); done < .build/build-args.env
tag="$IMAGE:shared-nix-build-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT-$arch"
# Re-exporting after tests uses the same builder and options. OCI preserves the
# index annotations; a classic Docker load/push can convert images to schema2.
docker buildx build --progress plain --platform "linux/$arch" "${args[@]}" -t "$tag" \
    --label "org.opencontainers.image.revision=$GITHUB_SHA" \
    --label "org.opencontainers.image.source=https://github.com/$GITHUB_REPOSITORY" \
    --cache-from "type=registry,ref=$IMAGE:latest" --cache-to type=inline \
    "${output[@]}" .
