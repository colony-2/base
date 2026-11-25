#!/bin/bash
# Shared options for the separately timed CI build and publication steps.
set -euo pipefail
arch=${1:?Expected amd64 or arm64}
case "$arch" in amd64|arm64) ;; *) exit 2 ;; esac
case "${2:-}" in
    load) output=(--load) ;;
    push) output=(--output 'type=image,push=true,oci-mediatypes=true') ;;
    verify-load) output=(--load) ;;
    *) echo 'Expected load, push, or verify-load' >&2; exit 2 ;;
esac
if [[ $2 == verify-load ]]; then
    tag=${3:?Expected a unique local verification tag}
    revision=${4:?Expected a candidate revision}
    [[ $tag =~ ^base-verify-[0-9a-f]{32}-candidate:local$ && $revision =~ ^[0-9a-f]{40}$ ]] || exit 2
    source=https://github.com/colony-2/base
    cache=()
else
    : "${IMAGE:?}" "${GITHUB_RUN_ID:?}" "${GITHUB_RUN_ATTEMPT:?}" "${GITHUB_SHA:?}" "${GITHUB_REPOSITORY:?}"
    tag="$IMAGE:shared-nix-build-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT-$arch"
    revision=$GITHUB_SHA
    source="https://github.com/$GITHUB_REPOSITORY"
    cache=(--cache-from "type=registry,ref=$IMAGE:latest" --cache-to type=inline)
fi
args=()
while IFS= read -r arg; do args+=(--build-arg "$arg"); done < .build/build-args.env
# Re-exporting after tests uses the same builder and options. OCI preserves the
# index annotations; a classic Docker load/push can convert images to schema2.
docker buildx build --progress plain --platform "linux/$arch" "${args[@]}" -t "$tag" \
    --label "org.opencontainers.image.revision=$revision" \
    --label "org.opencontainers.image.source=$source" \
    --metadata-file ".build/image-$arch.json" \
    "${cache[@]}" \
    "${output[@]}" .
