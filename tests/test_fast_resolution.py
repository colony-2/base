import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("versions", Path(__file__).parents[1] / "scripts/resolve-versions.py")
versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versions)
PIN = "2.34.3+dfsg-1"


def cached_plan(key="key"):
    return {"annotations": {versions.APT_PLAN_ANNOTATION: json.dumps({
        "key": key, "packages": {arch: {"nix-bin": PIN} for arch in ("amd64", "arm64")},
        "candidates": {arch: PIN for arch in ("amd64", "arm64")}})}}


class FastResolutionTests(unittest.TestCase):
    def test_only_sha256_package_indexes_affect_comparison(self):
        paths = [f"{c}/binary-{a}/Packages" for c in ("main", "universe", "restricted", "multiverse")
                 for a in ("amd64", "arm64")]
        release = "Date: today\nMD5Sum:\n" + "".join(f" {'b' * 32} 100 {p}\n" for p in paths)
        release += "SHA256:\n" + "".join(f" {'a' * 64} 100 {p}\n" for p in paths)
        release += " " + "c" * 64 + " 999 main/i18n/Translation-en\n"
        expected = dict.fromkeys(paths, "a" * 64)
        self.assertEqual(versions.package_index_checksums(release), expected)
        self.assertEqual(versions.package_index_checksums(release.replace("today", "tomorrow")), expected)
        for bad in (release.replace(paths[0], "missing"), release.replace("a" * 64, "bad"),
                    release + " " + "a" * 64 + " 100 " + paths[0] + "\n"):
            with self.subTest(bad=bad[-100:]), self.assertRaises(ValueError):
                versions.package_index_checksums(bad)

    @patch.object(versions, "fetch", return_value=b"signed input")
    @patch.object(versions.subprocess, "run")
    def test_invalid_signature_stops_resolution(self, run, fetch):
        run.return_value = subprocess.CompletedProcess([], 1, "untrusted data", "BAD signature")
        with self.assertRaisesRegex(RuntimeError, "Cannot authenticate"):
            versions.repository_indexes("20261022T000000Z")

    def test_plan_key_tracks_base_pin_indexes_ca_and_planning_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ("apt/packages.txt", "scripts/plan-apt.sh", "scripts/configure-apt.sh", "scripts/resolve-versions.py")
            for name in names:
                p = root / name
                p.parent.mkdir(exist_ok=True)
                p.write_text("initial")
            with patch.object(versions, "ROOT", root):
                args = ["ubuntu@digest", PIN, b"CA", {"suite": "hash"}]
                baseline = versions.apt_plan_key(*args)
                for index, replacement in enumerate(("ubuntu@new", "2.35", b"new CA", {"suite": "new"})):
                    changed = list(args)
                    changed[index] = replacement
                    self.assertNotEqual(versions.apt_plan_key(*changed), baseline)
                for name in names:
                    (root / name).write_text("changed")
                    self.assertNotEqual(versions.apt_plan_key(*args), baseline)
                    (root / name).write_text("initial")

    def test_old_or_nonmatching_metadata_requires_resolution(self):
        for manifest, key in (({}, "key"), (cached_plan("old"), "key"), (cached_plan(), None)):
            self.assertIsNone(versions.previous_apt_plans(manifest, key, PIN))
        self.assertEqual(set(versions.previous_apt_plans(cached_plan(), "key", PIN)), {"amd64", "arm64"})

    def test_matching_but_invalid_plan_fails_closed(self):
        for change in ("missing_arch", "pin", "node", "injection"):
            manifest = cached_plan()
            data = json.loads(manifest["annotations"][versions.APT_PLAN_ANNOTATION])
            if change == "missing_arch":
                del data["packages"]["arm64"]
            elif change == "pin":
                data["packages"]["arm64"]["nix-bin"] = "other"
            elif change == "node":
                data["packages"]["amd64"]["nodejs"] = "22"
            else:
                data["packages"]["amd64"]["bash"] = "$(id)"
            manifest["annotations"][versions.APT_PLAN_ANNOTATION] = json.dumps(data)
            with self.subTest(change=change), self.assertRaises(ValueError):
                versions.previous_apt_plans(manifest, "key", PIN)

    def test_matching_indexes_skip_docker_but_still_check_tool_releases(self):
        for published_key in ("key", "old"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "apt").mkdir()
                pin = root / "apt/nix-version"
                pin.write_text(PIN)
                with patch.object(versions, "ROOT", root), patch.object(versions, "APT_KEYRING", pin), \
                        patch.object(versions.shutil, "which", return_value="gpgv"), \
                        patch.object(versions, "fetch", return_value=b"CA"), \
                        patch.object(versions, "ubuntu_image", return_value=("ubuntu@index", {a: a for a in ("amd64", "arm64")})), \
                        patch.object(versions, "repository_indexes", return_value={}), \
                        patch.object(versions, "apt_plan_key", return_value="key"), \
                        patch.object(versions, "apt_plan", return_value=({"nix-bin": PIN}, PIN)) as apt, \
                        patch.object(versions, "node_tool", return_value={}) as node, \
                        patch.object(versions, "pnpm_tool", return_value={}) as pnpm, \
                        patch.object(versions, "github_tool", return_value={}) as github:
                    result = versions.resolve(cached_plan(published_key))
                self.assertEqual(apt.call_count, 0 if published_key == "key" else 2)
                node.assert_called_once()
                pnpm.assert_called_once()
                self.assertEqual(github.call_count, 4)
                self.assertEqual(result["apt"]["arm64"], {"nix-bin": PIN})
                self.assertEqual((root / ".build/apt/arm64.txt").read_text(), f"nix-bin={PIN}\n")

    def test_repository_only_change_does_not_rebuild_image(self):
        inputs = {"apt_plan_key": "one", "apt": {"arm64": {"bash": "1"}}}
        self.assertEqual(versions.fingerprint(inputs), versions.fingerprint({**inputs, "apt_plan_key": "two"}))

    def test_scheduled_check_refreshes_only_changed_plan_metadata(self):
        for scenario in ("metadata", "unchanged", "packages"):
            data = json.loads(cached_plan()["annotations"][versions.APT_PLAN_ANNOTATION])
            inputs = {"build_args": {"UBUNTU_IMAGE": "ubuntu@digest", "APT_SNAPSHOT": "20261022T000000Z"},
                      "apt": data["packages"], "nix": {"candidate": data["candidates"]},
                      "tools": {}, "apt_plan_key": "key"}
            manifest = {"annotations": {versions.ANNOTATION: versions.fingerprint(inputs),
                        versions.APT_PLAN_ANNOTATION: versions.apt_plan_annotation(inputs),
                        "org.opencontainers.image.revision": "commit"}, "manifests": [
                            {"platform": {"os": "linux", "architecture": a}} for a in ("amd64", "arm64")]}
            if scenario == "metadata":
                inputs["apt_plan_key"] = "new"
            elif scenario == "packages":
                inputs["apt"]["arm64"]["bash"] = "new"
            with tempfile.TemporaryDirectory() as directory:
                original = Path.cwd()
                try:
                    os.chdir(directory)
                    output = Path(directory) / "output"
                    with patch.object(versions, "resolve", return_value=inputs), \
                            patch.object(versions, "published_manifest", return_value=manifest), \
                            patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), \
                            patch("sys.argv", ["resolve", "--check", "--image", "test/base2", "--revision", "commit"]), \
                            contextlib.redirect_stdout(io.StringIO()):
                        versions.main()
                    self.assertIn(f"changed={str(scenario == 'packages').lower()}\n", output.read_text())
                    self.assertIn(f"refresh_metadata={str(scenario == 'metadata').lower()}\n", output.read_text())
                finally:
                    os.chdir(original)


if __name__ == "__main__":
    unittest.main()
