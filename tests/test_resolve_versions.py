import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "versions", Path(__file__).parents[1] / "scripts" / "resolve-versions.py"
)
versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versions)


def published(digest="inputs", revision="commit"):
    return {
        "annotations": {
            versions.ANNOTATION: digest,
            "org.opencontainers.image.revision": revision,
        },
        "manifests": [
            {"platform": {"os": "linux", "architecture": arch}}
            for arch in ("amd64", "arm64")
        ],
    }


class ChangeDetectionTests(unittest.TestCase):
    def test_unchanged_skips(self):
        self.assertFalse(versions.needs_build(published(), "inputs", "commit"))

    def test_new_upstream_version_builds(self):
        self.assertTrue(versions.needs_build(published(), "new-inputs", "commit"))

    def test_upstream_rollback_builds(self):
        self.assertTrue(versions.needs_build(published("new-inputs"), "inputs", "commit"))

    def test_changed_source_builds(self):
        self.assertTrue(versions.needs_build(published(), "inputs", "new-commit"))

    def test_first_publication_builds(self):
        self.assertTrue(versions.needs_build({}, "inputs", "commit"))

    def test_missing_architecture_builds(self):
        manifest = published()
        manifest["manifests"].pop()
        self.assertTrue(versions.needs_build(manifest, "inputs", "commit"))

    def test_fingerprint_is_order_independent_and_tracks_every_input(self):
        args = {"NIX_IMAGE": "digest", "NIXPKGS_REV": "rev", "C2J_VERSION": "1.2.3",
                "C2J_AMD64_SHA256": "amd64-hash", "C2J_ARM64_SHA256": "arm64-hash"}
        digest = versions.fingerprint(args)
        self.assertEqual(digest, versions.fingerprint(dict(reversed(list(args.items())))))
        for key in args:
            with self.subTest(key=key):
                self.assertNotEqual(digest, versions.fingerprint({**args, key: "changed"}))

    @patch.object(versions.subprocess, "run")
    def test_missing_registry_manifest_is_first_build(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "", "ERROR: manifest unknown")
        self.assertEqual({}, versions.published_manifest("ghcr.io/example/base"))

    @patch.object(versions.subprocess, "run")
    def test_registry_outage_and_auth_failure_are_errors(self, run):
        for message in ("500 Internal Server Error", "401 Unauthorized", "connection timed out", "host not found"):
            with self.subTest(message=message):
                run.return_value = subprocess.CompletedProcess([], 1, "", message)
                with self.assertRaises(RuntimeError):
                    versions.published_manifest("ghcr.io/example/base")


class ResolutionTests(unittest.TestCase):
    @patch.object(versions, "resolve", return_value={"build_args": {"UBUNTU_IMAGE": "ubuntu@sha256:" + "a" * 64, "APT_SNAPSHOT": "20261015T020000Z"}, "apt": {"arm64": {"bash": "1"}}})
    @patch.object(versions, "published_manifest")
    def test_push_forces_build_and_schedule_skips_identical_inputs(self, inspect, resolve):
        import contextlib
        import io
        import os

        digest = versions.fingerprint(resolve.return_value)
        inspect.return_value = published(digest, "commit")
        original_dir = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                output_path = Path(directory) / "github-output"
                with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_path)}):
                    with patch("sys.argv", ["resolve-versions.py"]), contextlib.redirect_stdout(io.StringIO()):
                        versions.main()
                    inspect.assert_not_called()
                    self.assertIn("changed=true\n", output_path.read_text())
                    output_path.write_text("")
                    argv = ["resolve-versions.py", "--check", "--image", "ghcr.io/example/base", "--revision", "commit"]
                    with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
                        versions.main()
                    self.assertIn("changed=false\n", output_path.read_text())
                    self.assertIn("build_args<<BUILD_ARGS\nUBUNTU_IMAGE=", output_path.read_text())
                    self.assertEqual(resolve.return_value["apt"], json.loads(Path(".build/versions.json").read_text())["apt"])
            finally:
                os.chdir(original_dir)

    def test_snapshot_time_and_uncertified_nix_candidate_do_not_trigger_build(self):
        first = {"build_args": {"APT_SNAPSHOT": "20261015T010000Z"},
                 "nix": {"candidate": {"arm64": "2.34.3"}, "package_version": "2.34.3"},
                 "apt": {"arm64": {"libc6": "1"}, "amd64": {"libc6": "1"}}}
        second = json.loads(json.dumps(first))
        second["build_args"]["APT_SNAPSHOT"] = "20261015T020000Z"
        second["nix"]["candidate"]["arm64"] = "2.35.0"
        self.assertEqual(versions.fingerprint(first), versions.fingerprint(second))
        second["apt"]["amd64"]["libc6"] = "2"
        self.assertNotEqual(versions.fingerprint(first), versions.fingerprint(second))

    def test_apt_plan_captures_base_upgrades_and_new_dependencies(self):
        plan = "bash=1\nlibc6=2\n# PLAN\nInst bash [1] (3 Ubuntu:26.04 [arm64])\nInst nix-bin (2.34.3+dfsg-1 Ubuntu:26.04 [arm64])\nInst libfoo:arm64 (1:2.0-1 Ubuntu:26.04 [arm64])\n# NIX-CANDIDATE\n  Candidate: 2.35.0\n"
        packages, candidate = versions.parse_apt_plan(plan)
        self.assertEqual(packages, {"bash": "3", "libc6": "2", "libfoo": "1:2.0-1", "nix-bin": "2.34.3+dfsg-1"})
        self.assertEqual(candidate, "2.35.0")
        for line in ("Remv bash [1]", "Inst invalid", "Inst nodejs (22.1.0 Ubuntu [arm64])", "Inst nix-setup-systemd (1 Ubuntu [arm64])"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                versions.parse_apt_plan(plan.replace("# NIX-CANDIDATE", line + "\n# NIX-CANDIDATE"))

    @patch.object(versions.subprocess, "run")
    def test_apt_failure_reports_platform_exit_code_and_docker_or_apt_output(self, run):
        for status, diagnostic in ((125, "cannot overwrite digest sha256:abc"),
                                   (100, "E: Version '2.34.3+dfsg-1' was not found")):
            with self.subTest(status=status):
                run.side_effect = [subprocess.CalledProcessError(
                    status, ["docker", "run"], output="APT progress", stderr=diagnostic),
                    subprocess.CompletedProcess([], 0)]
                with self.assertRaises(RuntimeError) as error:
                    versions.apt_plan("ubuntu@sha256:abc", "20261019T200000Z", "2.34.3+dfsg-1", "amd64")
                message = str(error.exception)
                for expected in ("linux/amd64", f"status {status}", "APT progress", diagnostic):
                    self.assertIn(expected, message)
                self.assertEqual(run.call_args.args[0][:3], ["docker", "rm", "-f"])

    @patch.object(versions.subprocess, "run")
    def test_apt_timeout_keeps_partial_output_when_cleanup_also_times_out(self, run):
        run.side_effect = [subprocess.TimeoutExpired(["docker", "run"], 300,
                           output=b"partial plan", stderr=b"network stalled"),
                           subprocess.TimeoutExpired(["docker", "rm"], 30)]
        with patch("sys.stderr"), self.assertRaises(RuntimeError) as error:
            versions.apt_plan("ubuntu@sha256:abc", "20261019T200000Z", "2.34.3+dfsg-1", "arm64")
        for expected in ("linux/arm64", "timed out after 300s", "partial plan", "network stalled"):
            self.assertIn(expected, str(error.exception))

    def test_resolve_runs_apt_with_child_digests_and_builds_with_index(self):
        images = {arch: "ubuntu@sha256:" + char * 64 for arch, char in (("amd64", "a"), ("arm64", "b"))}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "apt").mkdir()
            (root / "apt/nix-version").write_text("2.34.3+dfsg-1")
            with patch.object(versions, "ROOT", root), \
                    patch.object(versions, "fetch", return_value=b"test CA"), \
                    patch.object(versions, "ubuntu_image", return_value=("ubuntu@index", images)), \
                    patch.object(versions, "apt_plan", return_value=({"nix-bin": "2.34.3+dfsg-1"}, "2.34.3+dfsg-1")) as plan, \
                    patch.object(versions, "node_tool", return_value={}), \
                    patch.object(versions, "pnpm_tool", return_value={}), \
                    patch.object(versions, "github_tool", return_value={}):
                result = versions.resolve()
            self.assertEqual(result["build_args"]["UBUNTU_IMAGE"], "ubuntu@index")
            self.assertEqual({call.args[3]: call.args[0] for call in plan.call_args_list}, images)
            for arch in images:
                self.assertEqual((root / f".build/apt/{arch}.txt").read_text(), "nix-bin=2.34.3+dfsg-1\n")

    @patch.object(versions, "fetch")
    @patch.object(versions, "fetch_json")
    def test_node_stays_on_24_and_requires_both_architectures(self, fetch_json, fetch):
        fetch_json.return_value = [
            {"version": "v26.1.0", "files": ["linux-x64", "linux-arm64"]},
            {"version": "v24.9.0", "files": ["linux-x64", "linux-arm64"]},
            {"version": "v24.10.0", "files": ["linux-x64", "linux-arm64"]},
            {"version": "v24.11.0", "files": ["linux-x64"]},
        ]
        fetch.return_value = ("a" * 64 + "  node-v24.10.0-linux-x64.tar.xz\n" + "b" * 64 + "  node-v24.10.0-linux-arm64.tar.xz\n").encode()
        result = versions.node_tool()
        self.assertEqual(result["version"], "24.10.0")
        self.assertEqual(set(result["artifacts"]), {"amd64", "arm64"})

    @patch.object(versions, "fetch_json")
    def test_pnpm_resolves_native_binaries_before_build(self, fetch_json):
        import base64
        integrity = "sha512-" + base64.b64encode(b"x" * 64).decode()
        data = {"version": "12.11.2", "dist": {"tarball": "https://registry.npmjs.org/pnpm/-/pnpm-12.11.2.tgz", "integrity": integrity},
                "optionalDependencies": {"@pnpm/exe.linux-x64": "12.11.2", "@pnpm/exe.linux-arm64": "12.11.2"}}
        native = {"version": "12.11.2", "dist": {"tarball": "https://registry.npmjs.org/native.tgz", "integrity": integrity}}
        fetch_json.side_effect = [data, native, native]
        result = versions.pnpm_tool()
        self.assertEqual(set(result["native_artifacts"]), {"amd64", "arm64"})
        self.assertEqual(result["native_artifacts"]["arm64"]["sha512"], (b"x" * 64).hex())
        fetch_json.side_effect = [data, {**native, "version": "12.11.1"}]
        with self.assertRaises(ValueError):
            versions.pnpm_tool()

    def test_npm_integrity_and_origin_are_required(self):
        for distribution in ({"tarball": "https://example.com/pnpm.tgz", "integrity": "sha512-aaa"},
                             {"tarball": "https://registry.npmjs.org/pnpm.tgz", "integrity": "sha512-eA=="},
                             {"tarball": "https://registry.npmjs.org/pnpm.tgz", "integrity": "sha1-abc"}):
            with self.subTest(distribution=distribution), self.assertRaises(ValueError):
                versions.npm_artifact({"dist": distribution})

    def test_release_missing_architecture_or_checksum_fails(self):
        with self.assertRaises(ValueError):
            versions.asset_sha256({"assets": []}, "arm64.tar.gz")
        with self.assertRaises(ValueError):
            versions.asset_sha256({"assets": [{"name": "arm64.tar.gz", "digest": None}]}, "arm64.tar.gz")

    def test_valid_asset_checksum(self):
        data = {"assets": [{"name": "arm64.tar.gz", "digest": "sha256:" + "a" * 64}]}
        self.assertEqual("a" * 64, versions.asset_sha256(data, "arm64.tar.gz"))

    def test_reject_prerelease_or_injected_version(self):
        for value in ("1.2.3-rc.1", "latest", "1.2.3\nBAD=value", "$(id)"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                versions.version(value)

    @patch.object(versions, "fetch")
    @patch.object(versions, "fetch_json", return_value={"token": "test-token"})
    def test_ubuntu_index_digest_and_platforms(self, fetch_json, fetch):
        manifest = published()
        for item, char in zip(manifest["manifests"], ("a", "b")):
            item["digest"] = "sha256:" + char * 64
        raw = json.dumps(manifest).encode()
        fetch.return_value = raw
        index, images = versions.ubuntu_image()
        self.assertEqual("ubuntu@sha256:" + versions.hashlib.sha256(raw).hexdigest(), index)
        self.assertEqual(images, {"amd64": "ubuntu@sha256:" + "a" * 64, "arm64": "ubuntu@sha256:" + "b" * 64})
        manifest["manifests"][0]["digest"] = "invalid"
        fetch.return_value = json.dumps(manifest).encode()
        with self.assertRaises(ValueError):
            versions.ubuntu_image()
        manifest["manifests"][0]["digest"] = "sha256:" + "a" * 64
        manifest["manifests"].append(manifest["manifests"][0])
        fetch.return_value = json.dumps(manifest).encode()
        with self.assertRaises(ValueError):
            versions.ubuntu_image()
        manifest["manifests"].pop()
        manifest["manifests"].pop()
        fetch.return_value = json.dumps(manifest).encode()
        with self.assertRaises(ValueError):
            versions.ubuntu_image()


if __name__ == "__main__":
    unittest.main()
