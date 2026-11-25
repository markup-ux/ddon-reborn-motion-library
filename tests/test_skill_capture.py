import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from skill_capture import SkillTimeline


class SkillTimelineTests(unittest.TestCase):
    def test_hold_and_focus_loss(self):
        timeline = SkillTimeline("Rex Elementa", "Fire", "Base", "F1", "Shift")
        start = timeline.started_clock
        timeline.first_frame_at = start + .25
        for focused, down, offset in (
            (True, True, .05), (True, False, .10),
            (True, True, .75), (True, False, 1.25),
            (True, True, 1.5), (False, False, 1.75),
            (True, True, 2), (True, False, 2.25),
        ):
            timeline.observe(focused, down, start + offset)
        with TemporaryDirectory() as directory:
            clip = Path(directory) / "cast.mp4"
            row = json.loads(timeline.save(clip).read_text(encoding="utf-8"))
        self.assertEqual(row["input"], {"button": "F1", "modifier": "Shift"})
        self.assertEqual([event["event"] for event in row["events"]], ["press", "release", "press", "interrupted"])
        self.assertEqual([event["approx_frame"] for event in row["events"]], [30, 60, 75, 90])
        self.assertEqual(row["events"][1]["held_seconds"], .5)

    def test_disabled_capture_and_pre_video_press(self):
        timeline = SkillTimeline("Rex Elementa", "Thunder", "Base", "Off", "None")
        timeline.observe(True, False, timeline.started_clock)
        timeline.observe(True, True, timeline.started_clock + .05)
        self.assertEqual(timeline.events, [])
        timeline = SkillTimeline("Rex Elementa", "Thunder", "Base", "Left mouse", "None")
        start = timeline.started_clock
        timeline.first_frame_at = start + .2
        timeline.observe(True, False, start)
        timeline.observe(True, True, start + .1)
        with TemporaryDirectory() as directory:
            clip = Path(directory) / "cast.mp4"
            row = json.loads(timeline.save(clip).read_text(encoding="utf-8"))
        self.assertEqual(row["events"][0]["video_seconds"], -.1)
        self.assertIsNone(row["events"][0]["approx_frame"])
