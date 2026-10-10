#!/usr/bin/env python3
"""Resolve immutable build inputs using public upstream APIs (stdlib only)."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request


PLATFORMS = {("linux", "amd64"), ("linux", "arm64")}
ANNOTATION = "io.colony-2.base.inputs"


def fetch(url, headers=None):
    request_headers = {"User-Agent": "colony-2-base-version-check"}
    if url.startswith("https://api.github.com/") and os.environ.get("GH_TOKEN"):
        request_headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"
    request_headers.update(headers or {})
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)


def fetch_json(url, headers=None):
    return json.loads(fetch(url, headers))


def validated(value, pattern, name):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid {name}: {value!r}")
    return value


def version(value):
    return validated(value.removeprefix("v"), r"\d+\.\d+\.\d+", "stable version")


def nix_image():
    token = fetch_json(
        "https://auth.docker.io/token?service=registry.docker.io"
        "&scope=repository:nixos/nix:pull"
    )["token"]
    raw = fetch("https://registry-1.docker.io/v2/nixos/nix/manifests/latest", {
        "Authorization": f"Bearer {token}",
        "Accept": ", ".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
        ]),
    })
    manifest = json.loads(raw)
    platforms = {
        (item.get("platform", {}).get("os"), item.get("platform", {}).get("architecture"))
        for item in manifest.get("manifests", [])
    }
    if not PLATFORMS <= platforms:
        raise ValueError("nixos/nix:latest must support both linux/amd64 and linux/arm64")
    return "nixos/nix@sha256:" + hashlib.sha256(raw).hexdigest()


def release(repo):
    result = fetch_json(f"https://api.github.com/repos/{repo}/releases/latest")
    if result.get("draft") or result.get("prerelease"):
        raise ValueError(f"Expected a stable published release from {repo}")
    return result


def asset_sha256(release_data, name):
    assets = {asset["name"]: asset for asset in release_data["assets"]}
    if name not in assets:
        raise ValueError(f"Release is missing required asset: {name}")
    # Fail closed if the release is still uploading or does not supply a checksum.
    digest = validated(assets[name].get("digest"), r"sha256:[0-9a-f]{64}", name + " digest")
    return digest.removeprefix("sha256:")


def resolve():
    c2j = release("colony-2/c2j")
    c2j_version = version(c2j["tag_name"])
    # This tag convention is also used by the download URL in nix/packages.nix.
    if c2j["tag_name"] != f"v{c2j_version}":
        raise ValueError("Upstream release tag convention changed")
    args = {
        "NIX_IMAGE": nix_image(),
        "NIXPKGS_REV": validated(fetch_json(
            "https://api.github.com/repos/NixOS/nixpkgs/commits/nixpkgs-unstable"
        )["sha"], r"[0-9a-f]{40}", "Nixpkgs revision"),
        "C2J_VERSION": c2j_version,
    }
    for arch, c2j_arch in (
        ("AMD64", "x86_64"),
        ("ARM64", "arm64"),
    ):
        args[f"C2J_{arch}_SHA256"] = asset_sha256(c2j, f"c2j_{c2j_version}_Linux_{c2j_arch}.tar.gz")
    return args


def fingerprint(build_args):
    return hashlib.sha256(json.dumps(build_args, sort_keys=True).encode()).hexdigest()


def needs_build(manifest, expected, revision):
    annotations = manifest.get("annotations", {})
    platforms = {
        (item.get("platform", {}).get("os"), item.get("platform", {}).get("architecture"))
        for item in manifest.get("manifests", [])
    }
    return (
        annotations.get(ANNOTATION) != expected
        or annotations.get("org.opencontainers.image.revision") != revision
        or not PLATFORMS <= platforms
    )


def published_manifest(image):
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", f"{image}:latest"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        # Only a missing manifest is a first publication. Auth/network failures
        # must fail the check, rather than incorrectly recording 'unchanged'.
        error = result.stderr.lower()
        if "manifest unknown" in error or f"{image}:latest: not found" in error or "404 not found" in error:
            return {}
        raise RuntimeError(f"Cannot inspect published image: {result.stderr.strip()}")
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="GHCR image to compare on scheduled runs")
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--check", action="store_true", help="Skip unchanged published inputs")
    options = parser.parse_args()
    if options.check and (not options.image or not options.revision):
        parser.error("--check requires --image and --revision")

    args = resolve()
    digest = fingerprint(args)
    changed = not options.check or needs_build(published_manifest(options.image), digest, options.revision)
    output_dir = Path(".build")
    output_dir.mkdir(exist_ok=True)
    versions = {"fingerprint": digest, "build_args": args}
    (output_dir / "versions.json").write_text(json.dumps(versions, indent=2) + "\n")
    build_args = "\n".join(f"{key}={value}" for key, value in args.items())
    (output_dir / "build-args.env").write_text(build_args + "\n")
    print(json.dumps(versions, indent=2))
    print(f"Build required: {changed}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"changed={str(changed).lower()}\nfingerprint={digest}\n")
            output.write(f"build_args<<BUILD_ARGS\n{build_args}\nBUILD_ARGS\n")


if __name__ == "__main__":
    main()
