import importlib.util
import json
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("publish", Path(__file__).parents[1] / "scripts/publish-image.py")
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)
v = publish.versions


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.inputs = {"build_args": {"UBUNTU_IMAGE": "ubuntu@digest", "APT_SNAPSHOT": "20261021T220000Z"},
                       "apt": {a: {"nix-bin": "2.34.3+dfsg-1"} for a in ("amd64", "arm64")},
                       "nix": {"candidate": {a: "2.34.3+dfsg-1" for a in ("amd64", "arm64")}},
                       "apt_plan_key": "key"}
        self.metadata = {a: {"containerimage.digest": "sha256:" + c * 64}
                         for a, c in (("amd64", "a"), ("arm64", "b"))}
        self.previous = {"digest": "sha256:" + "c" * 64, "annotations": {
            v.ANNOTATION: v.fingerprint(self.inputs), "org.opencontainers.image.revision": "commit",
            "custom": "preserve-me"}, "manifests": [
                {"platform": {"os": "linux", "architecture": a}} for a in ("amd64", "arm64")]}

    def command(self, refresh=False):
        return publish.publication_command("registry/base2", self.inputs, self.previous, self.metadata,
                                           "commit", "https://github.com/colony-2/base", "build-2", refresh)

    def test_combines_exact_digests_including_partial_reruns(self):
        command = self.command()
        self.assertEqual(command[-2:], ["registry/base2@sha256:" + c * 64 for c in ("a", "b")])
        self.assertIn("registry/base2:build-2", command)
        self.assertIn("registry/base2:latest", command)
        annotation = next(x for x in command if x.startswith(f"index:{v.APT_PLAN_ANNOTATION}="))
        self.assertEqual(json.loads(annotation.split("=", 1)[1])["packages"], self.inputs["apt"])

    def test_missing_or_invalid_digest_prevents_publication(self):
        for bad in ({"amd64": self.metadata["amd64"]},
                    {**self.metadata, "arm64": {"containerimage.digest": "invalid"}},
                    {a: self.metadata["amd64"] for a in self.metadata}):
            with self.subTest(metadata=bad):
                saved, self.metadata = self.metadata, bad
                with self.assertRaises(ValueError):
                    self.command()
                self.metadata = saved

    def test_metadata_refresh_pins_existing_index_and_preserves_other_annotations(self):
        self.inputs["apt_plan_key"] = "new repository state"
        command = self.command(refresh=True)
        self.assertEqual(command[-1], "registry/base2@" + self.previous["digest"])
        self.assertNotIn("registry/base2:build-2", command)
        self.assertIn("index:custom=preserve-me", command)

    def test_refresh_rejects_changed_packages_revision_or_missing_architecture(self):
        for kind in ("packages", "revision", "platform"):
            self.setUp()
            if kind == "packages":
                self.inputs["apt"]["arm64"]["new-package"] = "1"
            elif kind == "revision":
                self.previous["annotations"]["org.opencontainers.image.revision"] = "old"
            else:
                self.previous["manifests"].pop()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.command(refresh=True)


if __name__ == "__main__":
    unittest.main()
