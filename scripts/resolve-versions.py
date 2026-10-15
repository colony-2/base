#!/usr/bin/env python3
"""Resolve immutable build inputs using public upstream APIs (stdlib only)."""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

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


def ubuntu_image():
    token = fetch_json(
        "https://auth.docker.io/token?service=registry.docker.io"
        "&scope=repository:library/ubuntu:pull"
    )["token"]
    raw = fetch("https://registry-1.docker.io/v2/library/ubuntu/manifests/26.04", {
        "Authorization": f"Bearer {token}",
        "Accept": ("application/vnd.oci.image.index.v1+json, "
                   "application/vnd.docker.distribution.manifest.list.v2+json"),
    })
    manifest = json.loads(raw)
    platforms = {
        (item.get("platform", {}).get("os"), item.get("platform", {}).get("architecture"))
        for item in manifest.get("manifests", [])
    }
    if not PLATFORMS <= platforms:
        raise ValueError("ubuntu:26.04 must support both linux/amd64 and linux/arm64")
    return "ubuntu@sha256:" + hashlib.sha256(raw).hexdigest()


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


ROOT = Path(__file__).resolve().parents[1]
CA_URL = "https://raw.githubusercontent.com/certifi/python-certifi/master/certifi/cacert.pem"


def github_tool(repo, names):
    data = release(repo)
    tool_version = version(data["tag_name"])
    artifacts = {}
    for arch, template in names.items():
        name = template.format(version=tool_version)
        digest = asset_sha256(data, name)
        url = next(a["browser_download_url"] for a in data["assets"] if a["name"] == name)
        if not url.startswith(f"https://github.com/{repo}/releases/download/"):
            raise ValueError(f"Unexpected artifact URL: {url}")
        artifacts[arch] = {"url": url, "sha256": digest}
    return {"version": tool_version, "artifacts": artifacts}


def node_tool():
    releases = fetch_json("https://nodejs.org/dist/index.json")
    choices = [r for r in releases if re.fullmatch(r"v24\.\d+\.\d+", r["version"])
               and {"linux-x64", "linux-arm64"} <= set(r["files"])]
    selected = max(choices, key=lambda r: tuple(map(int, r["version"][1:].split("."))))
    tag = selected["version"]
    checksums = dict(line.split()[::-1] for line in fetch(
        f"https://nodejs.org/dist/{tag}/SHASUMS256.txt").decode().splitlines() if line.strip())
    artifacts = {}
    for arch, node_arch in (("amd64", "x64"), ("arm64", "arm64")):
        name = f"node-{tag}-linux-{node_arch}.tar.xz"
        artifacts[arch] = {"url": f"https://nodejs.org/dist/{tag}/{name}",
                           "sha256": validated(checksums[name], r"[0-9a-f]{64}", "Node checksum")}
    return {"version": version(tag), "artifacts": artifacts}


def npm_artifact(data):
    url = data["dist"]["tarball"]
    if not url.startswith("https://registry.npmjs.org/"):
        raise ValueError("Unexpected npm tarball URL")
    integrity = validated(data["dist"]["integrity"], r"sha512-[A-Za-z0-9+/]+={0,2}", "npm integrity")
    digest = base64.b64decode(integrity.removeprefix("sha512-"), validate=True)
    if len(digest) != 64:
        raise ValueError("Invalid npm SHA512 length")
    return {"url": url, "sha512": digest.hex()}


def pnpm_tool():
    data = fetch_json("https://registry.npmjs.org/pnpm/latest")
    pnpm_version = version(data["version"])
    artifact = npm_artifact(data)
    result = {"version": pnpm_version, "artifacts": {arch: artifact for arch in ("amd64", "arm64")}}
    # pnpm >=11 ships its native executable as an optional npm dependency.
    # Resolve it now; the shipped executable must never bootstrap on first use.
    if int(pnpm_version.split(".")[0]) >= 11:
        result["native_artifacts"] = {}
        for arch, npm_arch in (("amd64", "x64"), ("arm64", "arm64")):
            name = f"@pnpm/exe.linux-{npm_arch}"
            native_version = version(data["optionalDependencies"][name])
            native = fetch_json(f"https://registry.npmjs.org/{name}/{native_version}")
            if native["version"] != native_version:
                raise ValueError("pnpm native dependency version mismatch")
            result["native_artifacts"][arch] = npm_artifact(native)
    return result


def parse_apt_plan(output):
    initial, rest = output.split("# PLAN\n", 1)
    plan, policy = rest.split("# NIX-CANDIDATE\n", 1)
    packages = dict(line.split("=", 1) for line in initial.splitlines())
    for line in plan.splitlines():
        if line.startswith("Remv "):
            raise ValueError(f"Unexpected package removal: {line}")
        if line.startswith("Inst "):
            match = re.match(r"Inst ([a-z0-9+.-]+)(?::[a-z0-9]+)? (?:\[[^]]+\] )?\((\S+) ", line)
            if not match:
                raise ValueError(f"Unrecognized APT plan: {line}")
            packages[match[1]] = match[2]
    for name, value in packages.items():
        validated(name, r"[a-z0-9+.-]+", "APT package name")
        validated(value, r"[a-zA-Z0-9.+:~_-]+", "APT package version")
    candidate = re.search(r"Candidate: (\S+)", policy)
    if not candidate:
        raise ValueError("APT did not report a Nix candidate")
    if {"nodejs", "nix-setup-systemd"} & packages.keys():
        raise ValueError("APT unexpectedly selected distribution Node or daemon setup")
    return dict(sorted(packages.items())), candidate[1]


def apt_plan(image, snapshot, nix_version, arch):
    name = "base-apt-resolve-" + uuid.uuid4().hex
    try:
        result = subprocess.run([
            "docker", "run", "--rm", "--name", name, "--platform", f"linux/{arch}",
            "--mount", f"type=bind,src={ROOT},dst=/inputs,readonly", image,
            "sh", "/inputs/scripts/plan-apt.sh", snapshot, nix_version,
        ], text=True, capture_output=True, timeout=300, check=True)
        packages, candidate = parse_apt_plan(result.stdout)
        if packages.get("nix-bin") != nix_version:
            raise ValueError("APT changed the certified Nix pin")
        return packages, candidate
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30, check=False)


def resolve():
    output_dir = ROOT / ".build"
    output_dir.mkdir(exist_ok=True)
    ca = fetch(CA_URL)
    (output_dir / "apt-ca.pem").write_bytes(ca)
    image = ubuntu_image()
    snapshot = datetime.now(timezone.utc).strftime("%Y%m%dT%H0000Z")
    nix_version = validated((ROOT / "apt/nix-version").read_text().strip(),
                            r"[0-9][a-zA-Z0-9.+:~_-]+", "Nix package pin")
    rust_names = {"amd64": "{tool}-x86_64-unknown-linux-gnu.tar.gz",
                  "arm64": "{tool}-aarch64-unknown-linux-gnu.tar.gz"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        apt_jobs = {arch: pool.submit(apt_plan, image, snapshot, nix_version, arch)
                    for arch in ("amd64", "arm64")}
        jobs = {
            "node": pool.submit(node_tool), "pnpm": pool.submit(pnpm_tool),
            "c2j": pool.submit(github_tool, "colony-2/c2j", {
                "amd64": "c2j_{version}_Linux_x86_64.tar.gz",
                "arm64": "c2j_{version}_Linux_arm64.tar.gz"}),
            "yq": pool.submit(github_tool, "mikefarah/yq", {
                arch: f"yq_linux_{arch}" for arch in ("amd64", "arm64")}),
        }
        for tool in ("uv", "ruff"):
            jobs[tool] = pool.submit(github_tool, f"astral-sh/{tool}",
                                    {a: n.format(tool=tool) for a, n in rust_names.items()})
        tools = {name: job.result() for name, job in jobs.items()}
        plans = {arch: job.result() for arch, job in apt_jobs.items()}
    apt_dir = output_dir / "apt"
    apt_dir.mkdir(exist_ok=True)
    for arch, (packages, _) in plans.items():
        (apt_dir / f"{arch}.txt").write_text("".join(f"{p}={v}\n" for p, v in packages.items()))
    return {
        "schema": 2,
        "build_args": {"UBUNTU_IMAGE": image, "APT_SNAPSHOT": snapshot},
        "bootstrap_ca": {"url": CA_URL, "sha256": hashlib.sha256(ca).hexdigest()},
        "nix": {"package_version": nix_version, "version": version(nix_version.split("+")[0]),
                "compatibility_group": "colony-nix-store-v1",
                "candidate": {a: candidate for a, (_, candidate) in plans.items()}},
        "apt": {a: packages for a, (packages, _) in plans.items()},
        "tools": tools,
    }


def fingerprint(inputs):
    # A timestamp-only change must not trigger an hourly publication.
    value = json.loads(json.dumps(inputs))
    value.pop("fingerprint", None)
    value.get("build_args", {}).pop("APT_SNAPSHOT", None)
    value.get("nix", {}).pop("candidate", None)
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


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


def published_manifest(image, tag="shared-nix-v1"):
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", f"{image}:{tag}"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        # Only a missing manifest is a first publication. Auth/network failures
        # must fail the check, rather than incorrectly recording 'unchanged'.
        error = result.stderr.lower()
        if "manifest unknown" in error or f"{image}:{tag}: not found" in error or "404 not found" in error:
            return {}
        raise RuntimeError(f"Cannot inspect published image: {result.stderr.strip()}")
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="GHCR image to compare on scheduled runs")
    parser.add_argument("--tag", default="shared-nix-v1")
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--check", action="store_true", help="Skip unchanged published inputs")
    options = parser.parse_args()
    if options.check and (not options.image or not options.revision):
        parser.error("--check requires --image and --revision")

    versions = resolve()
    args = versions["build_args"]
    digest = fingerprint(versions)
    changed = not options.check or needs_build(published_manifest(options.image, options.tag), digest, options.revision)
    output_dir = Path(".build")
    output_dir.mkdir(exist_ok=True)
    versions["fingerprint"] = digest
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
