#!/usr/bin/env python3
"""Opt-in integration check: import only a published image on fresh builders.

Resolve inputs and publish that exact checkout to a disposable registry first.
No separate cache export is used. Each case gets an empty BuildKit volume.
"""
import argparse
import importlib.util
import json
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("versions", ROOT / "scripts/resolve-versions.py")
versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versions)
TOOLS = ("node", "pnpm", "yq", "uv", "ruff", "c2j")


def check_log(log, arch, changed_tool):
    steps = {}
    cached = set()
    for line in log.splitlines():
        for vertex in json.loads(line).get("vertexes", []):
            step, command = vertex["digest"], vertex["name"]
            if vertex.get("cached"):
                cached.add(step)
            if " RUN " not in command:
                continue
            if f"source=.build/apt/{arch}.txt" in command:
                steps["apt"] = step
            for tool in TOOLS:
                if f"source=.build/tools/{arch}/{tool}.json" in command:
                    steps[tool] = step
    assert set(steps) == {"apt", *TOOLS}, f"Missing installation steps: {steps}"
    for tool, step in steps.items():
        assert (step in cached) == (tool != changed_tool), f"Unexpected cache result for {arch}/{tool}"


def check_case(options, arch, changed_tool):
    case = f"{arch}-" + ("changed-c2j" if changed_tool else "reporting-and-other-arch")
    builder = "base-reuse-" + uuid.uuid4().hex[:12]
    log_path = options.logs / f"{case}.jsonl"
    with tempfile.TemporaryDirectory(prefix="base-reuse-") as directory:
        context = Path(directory)
        for name in ("Dockerfile", ".dockerignore"):
            shutil.copyfile(ROOT / name, context / name)
        for name in ("scripts", "nix"):
            shutil.copytree(ROOT / name, context / name)
        build = context / ".build"
        build.mkdir()
        for name in ("apt", "tools"):
            shutil.copytree(ROOT / ".build" / name, build / name)
        shutil.copyfile(ROOT / ".build/apt-ca.pem", build / "apt-ca.pem")
        inputs = json.loads((ROOT / ".build/versions.json").read_text())
        observed = datetime.strptime(inputs["build_args"]["APT_SNAPSHOT"], "%Y%m%dT%H%M%SZ")
        # Exercise the same selection used after a later hourly resolution.
        published = {"annotations": {
            versions.APT_INPUTS_ANNOTATION: versions.apt_fingerprint(inputs),
            versions.APT_SNAPSHOT_ANNOTATION: inputs["build_args"]["APT_SNAPSHOT"],
        }}
        inputs["build_args"]["APT_SNAPSHOT"] = (observed + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")
        versions.select_apt_snapshot(inputs, published)
        if changed_tool:
            # Same verified bytes, different artifact input: must execute this
            # tool's installer again, while retaining the preceding layers.
            inputs["tools"]["c2j"]["artifacts"][arch]["url"] += "#cache-regression"
        else:
            # An unrelated architecture's input must not invalidate this one.
            other = "amd64" if arch == "arm64" else "arm64"
            with (build / f"apt/{other}.txt").open("a") as output:
                output.write("other-architecture-test=1\n")
            inputs["tools"]["c2j"]["artifacts"][other]["url"] += "#other-architecture"
            inputs["nix"]["candidate"][other] = "999.0.0"
        inputs["fingerprint"] = versions.fingerprint(inputs)
        versions.write_tool_inputs(inputs, build)
        (build / "versions.json").write_text(json.dumps(inputs, indent=2) + "\n")
        create = ["docker", "buildx", "create", "--name", builder, "--driver", "docker-container"]
        if options.network:
            create += ["--driver-opt", f"network={options.network}"]
        if options.buildkitd_config:
            create += ["--buildkitd-config", str(options.buildkitd_config)]
        start = time.monotonic()
        try:
            subprocess.run(create, check=True, capture_output=True, text=True)
            command = ["docker", "buildx", "build", "--builder", builder,
                       "--platform", f"linux/{arch}", "--progress", "rawjson",
                       "--label", f"org.opencontainers.image.revision={case}",
                       "--cache-from", f"type=registry,ref={options.image}",
                       "--output", "type=cacheonly"]
            for key, value in inputs["build_args"].items():
                command += ["--build-arg", f"{key}={value}"]
            command.append(str(context))
            with log_path.open("w") as output:
                subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=True, timeout=1200)
            check_log(log_path.read_text(), arch, changed_tool)
            elapsed = time.monotonic() - start
            print(f"PASS {case}: {elapsed:.1f}s; {log_path}", flush=True)
        finally:
            subprocess.run(["docker", "buildx", "rm", "-f", builder],
                           capture_output=True, timeout=60, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Published baseline image with inline metadata")
    parser.add_argument("--network", help="Docker network for a disposable local registry")
    parser.add_argument("--buildkitd-config", type=Path)
    parser.add_argument("--logs", type=Path, default=ROOT / ".build/cache-validation")
    options = parser.parse_args()
    options.logs.mkdir(parents=True, exist_ok=True)
    for arch in ("amd64", "arm64"):
        check_case(options, arch, None)
        check_case(options, arch, "c2j")


if __name__ == "__main__":
    main()
