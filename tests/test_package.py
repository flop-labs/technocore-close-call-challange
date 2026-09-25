import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.build import outputs
from scripts.verify import verify_manifest

ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_generated_files_and_manifest_match_sources(self):
        for path, expected in outputs(ROOT).items():
            self.assertEqual((ROOT / path).read_bytes(), expected, path)
        manifest = json.loads((ROOT / "manifest.json").read_text())
        self.assertEqual(verify_manifest(ROOT, manifest), [])

    def test_missing_corrupt_and_escaping_artifacts_fail_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "example.txt"
            path.write_bytes(b"approved\n")
            record = {"url": path.name, "bytes": 9,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            manifest = {"schema_version": 1, "package": "technocore-close-call",
                        "files": {path.name: record}}
            self.assertEqual(verify_manifest(root, manifest), [])
            path.write_bytes(b"tampered\n")
            self.assertIn("mismatch", verify_manifest(root, manifest)[0])
            path.unlink()
            self.assertIn("missing", verify_manifest(root, manifest)[0])
            manifest["files"] = {"../outside": {**record, "url": "../outside"}}
            with self.assertRaisesRegex(ValueError, "unsafe"):
                verify_manifest(root, manifest)
            manifest["files"] = {}
            with self.assertRaisesRegex(ValueError, "nonempty"):
                verify_manifest(root, manifest)

    def test_commands_succeed(self):
        commands = [
            ["scripts/build.py", "--check"],
            ["scripts/verify.py"],
            ["close_call_fold.py", "examples/sample-season.jsonl", "--config", "contest.json"],
        ]
        for args in commands:
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, "close_call_fold.py", "contest.json"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
