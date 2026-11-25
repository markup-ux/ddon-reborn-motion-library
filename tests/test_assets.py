import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ddon_reborn as assets


def archive_header(magic=b"ARCC", version=7):
    return struct.pack("<4sHH", magic, version, 0)


class AssetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.client = self.root / "DDON"
        self.rom = self.client / "nativePC" / "rom"
        self.source = self.rom / "motion" / "sample.ARC"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(archive_header())
        version = self.client / "dlinfo" / "versionlist"
        version.parent.mkdir()
        version.write_text("03040008", encoding="ascii")
        self.extractor = self.root / "skilltool.exe"
        self.extractor.touch()
        self.output = self.root / "run"

    def extract(self, archive=None, output=None):
        return assets.extract(
            self.client, archive or self.source.relative_to(self.rom),
            ".lmt", self.extractor, output or self.output,
        )

    def test_inventory_identifies_known_and_short_headers(self):
        (self.rom / "refs.arc").write_bytes(archive_header(b"ARCS"))
        (self.rom / "truncated.arc").write_bytes(b"ARCC")
        (self.rom / "notes.txt").touch()
        entries = {entry["source_path"]: entry for entry in assets.inventory(self.rom)["archives"]}
        self.assertEqual(set(entries), {"motion/sample.ARC", "refs.arc", "truncated.arc"})
        self.assertEqual(entries["motion/sample.ARC"]["version"], 7)
        self.assertEqual(entries["refs.arc"]["format"], "ARCS")
        self.assertEqual(entries["truncated.arc"]["status"], "unrecognized")

    def test_staging_and_output_manifest(self):
        def fake_run(command, *, cwd, stdout, stderr, check):
            archive = Path(command[2])
            self.assertEqual(command[1], "dump")
            self.assertEqual(command[3], "lmt")
            self.assertEqual(cwd, self.output)
            self.assertTrue(archive.is_relative_to(self.output / "input"))
            self.assertEqual(archive.read_bytes(), archive_header())
            archive.write_bytes(b"changed by stand-in")
            (cwd / "dump").mkdir()
            (cwd / "dump" / "motion_clip.lmt").write_bytes(b"synthetic motion")
            stdout.write(b"  motion/clip.lmt  size=16  -> dump/motion_clip.lmt\n")

        with patch.object(assets.subprocess, "run", side_effect=fake_run):
            result = self.extract()
        self.assertEqual(result["archive"]["source_path"], "nativePC/rom/motion/sample.ARC")
        self.assertEqual(result["archive"]["sha256"], assets.sha256(self.source))
        self.assertEqual(self.source.read_bytes(), archive_header())
        self.assertEqual(len(result["outputs"]), 1)
        self.assertEqual(result["outputs"][0]["kind"], "animation")
        self.assertEqual(result["outputs"][0]["member_path"], "motion/clip.lmt")
        self.assertEqual(result, json.loads((self.output / "manifest.json").read_text(encoding="utf-8")))

    def test_rejects_output_inside_client(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            self.extract(output=self.client / "run")
        self.assertFalse((self.client / "run").exists())

    def test_rejects_archive_traversal(self):
        with self.assertRaisesRegex(ValueError, "relative path"):
            self.extract(archive=Path("../elsewhere.arc"))

    def test_rejects_reference_archive(self):
        self.source.write_bytes(archive_header(b"ARCS"))
        with self.assertRaisesRegex(ValueError, "ARCC version 7"):
            self.extract()
        self.assertFalse(self.output.exists())

    def test_extractor_failure_records_no_manifest(self):
        error = subprocess.CalledProcessError(1, ["extractor"])
        with patch.object(assets.subprocess, "run", side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError):
                self.extract()
        self.assertTrue((self.output / "extractor.log").is_file())
        self.assertFalse((self.output / "manifest.json").exists())

    def test_empty_extractor_output_records_no_manifest(self):
        with patch.object(assets.subprocess, "run"):
            with self.assertRaisesRegex(ValueError, "no matching files"):
                self.extract()
        self.assertFalse((self.output / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
