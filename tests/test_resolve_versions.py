import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
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
    @patch.object(versions, "nix_image", return_value="nixos/nix@sha256:" + "f" * 64)
    @patch.object(versions, "fetch_json")
    def test_resolves_nixpkgs_and_both_c2j_architectures(self, fetch_json, nix_image):
        fetch_json.side_effect = {
            "https://api.github.com/repos/NixOS/nixpkgs/commits/nixpkgs-unstable": {"sha": "a" * 40},
            "https://api.github.com/repos/colony-2/c2j/releases/latest": {
                "tag_name": "v1.2.3",
                "assets": [
                    {"name": "c2j_1.2.3_Linux_x86_64.tar.gz", "digest": "sha256:" + "b" * 64},
                    {"name": "c2j_1.2.3_Linux_arm64.tar.gz", "digest": "sha256:" + "c" * 64},
                ],
            },
        }.__getitem__
        self.assertEqual({
            "NIX_IMAGE": nix_image.return_value,
            "NIXPKGS_REV": "a" * 40,
            "C2J_VERSION": "1.2.3",
            "C2J_AMD64_SHA256": "b" * 64,
            "C2J_ARM64_SHA256": "c" * 64,
        }, versions.resolve())

    @patch.object(versions, "resolve", return_value={"NIXPKGS_REV": "a" * 40})
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
                    self.assertIn("build_args<<BUILD_ARGS\nNIXPKGS_REV=" + "a" * 40 + "\nBUILD_ARGS", output_path.read_text())
                    self.assertEqual(resolve.return_value, json.loads(Path(".build/versions.json").read_text())["build_args"])
            finally:
                os.chdir(original_dir)

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
    def test_nix_index_digest_and_platforms(self, fetch_json, fetch):
        raw = json.dumps(published()).encode()
        fetch.return_value = raw
        self.assertEqual("nixos/nix@sha256:" + versions.hashlib.sha256(raw).hexdigest(), versions.nix_image())
        manifest = published()
        manifest["manifests"].pop()
        fetch.return_value = json.dumps(manifest).encode()
        with self.assertRaises(ValueError):
            versions.nix_image()


if __name__ == "__main__":
    unittest.main()
