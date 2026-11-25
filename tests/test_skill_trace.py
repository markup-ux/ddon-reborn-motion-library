from datetime import datetime, timedelta
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from skill_assets import effect_graph
from skill_trace import file_access


class SkillTraceTests(unittest.TestCase):
    def test_only_matching_game_file_access_in_clip_window(self):
        start = datetime(2026, 11, 25, 23, 59, 59).astimezone()
        end = start + timedelta(seconds=3)
        report = {"first_frame_observed_utc": start.isoformat(), "recording_end_utc": end.isoformat(), "client_pid": 100}
        row = {"Time of Day": "12:00:00.2500000 AM", "Process Name": "DDO.EXE", "PID": "100",
               "Path": r"D:\DDON\nativePC\rom\jobs\job09_cs05.arc", "Result": "SUCCESS", "Operation": "ReadFile"}
        rows = [
            row,
            {**row, "Time of Day": "11:59:59.1000000 PM", "Operation": "CreateFile"},
            {**row, "PID": "101"},
            {**row, "Process Name": "launcher.exe"},
            {**row, "Time of Day": "12:00:07.1000000 AM"},
            {**row, "Result": "NAME NOT FOUND"},
            {**row, "Path": r"C:\Users\Someone\settings.json"},
        ]
        self.assertEqual(file_access(report, rows), [
            {"file": "jobs/job09_cs05.arc", "first_video_seconds": .1, "last_video_seconds": 1.25, "opens": 1, "reads": 1}
        ])

    def test_extracts_linked_efl_and_original_asset_names(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            header = struct.pack("<4sIIfHH", b"EFL\0", 0x20120306, 0, 30.0, 3, 2).ljust(48, b"\0")
            parent = root / "parent.efl"
            parent.write_bytes(header + b"effect\\efl\\pl\\child\0effect\\tex\\cm\\t_cm00_00_bm\0")
            child = root / "child.efl"
            child.write_bytes(header)
            library = SimpleNamespace(ddon_members=lambda *_: [{"name": p.name, "path": p} for p in (parent, child)])
            graph = effect_graph(library, "archive.arc", ["parent"], restrict=True)
        self.assertEqual([record["member"] for record in graph["records"]], ["parent.efl", "child.efl"])
        self.assertEqual(graph["records"][0]["units"], 3)
        self.assertEqual(graph["records"][0]["references"], ["effect/efl/pl/child.efl", "effect/tex/cm/t_cm00_00_bm.tex"])
