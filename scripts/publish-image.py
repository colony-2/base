#!/usr/bin/env python3
"""Publish tested architecture digests, or refresh only comparison metadata."""
import argparse
import importlib.util
import json
import os
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location("versions", Path(__file__).with_name("resolve-versions.py"))
versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versions)


def publication_command(image, inputs, previous, metadata, revision, source, build_tag, refresh=False):
    fingerprint = versions.fingerprint(inputs)
    if refresh:
        if versions.needs_build(previous, fingerprint, revision):
            raise ValueError("Cannot refresh metadata for different inputs or source revision")
        # Pin the previously inspected index so descriptors/attestations and
        # all existing annotations survive without rebuilding or pulling layers.
        digest = versions.validated(previous.get("digest"), r"sha256:[0-9a-f]{64}", "published digest")
        sources = [f"{image}@{digest}"]
        annotations = dict(previous.get("annotations", {}))
    else:
        if set(metadata) != {"amd64", "arm64"}:
            raise ValueError("Both tested architecture digests are required")
        sources = [f"{image}@" + versions.validated(metadata[arch].get("containerimage.digest"),
                   r"sha256:[0-9a-f]{64}", f"{arch} digest") for arch in ("amd64", "arm64")]
        if sources[0] == sources[1]:
            raise ValueError("Architecture digests must be distinct")
        annotations = {"org.opencontainers.image.revision": revision,
                       "org.opencontainers.image.source": source}
    annotations.update({versions.ANNOTATION: fingerprint,
                        versions.APT_INPUTS_ANNOTATION: versions.apt_fingerprint(inputs),
                        versions.APT_SNAPSHOT_ANNOTATION: inputs["build_args"]["APT_SNAPSHOT"],
                        versions.APT_PLAN_ANNOTATION: versions.apt_plan_annotation(inputs)})
    command = ["docker", "buildx", "imagetools", "create", "--tag", f"{image}:latest"]
    if not refresh:
        command += ["--tag", f"{image}:{build_tag}"]
    for key, value in annotations.items():
        command += ["--annotation", f"index:{key}={value}"]
    return [*command, *sources]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    options = parser.parse_args()
    inputs = json.loads(Path(".build/versions.json").read_text())
    previous = json.loads(Path(".build/published.json").read_text())
    metadata = {} if options.refresh else {
        arch: json.loads(Path(f".build/images/image-{arch}.json").read_text()) for arch in ("amd64", "arm64")}
    command = publication_command(os.environ["IMAGE"], inputs, previous, metadata,
                                  os.environ["GITHUB_SHA"], "https://github.com/" + os.environ["GITHUB_REPOSITORY"],
                                  f"shared-nix-build-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}",
                                  options.refresh)
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
