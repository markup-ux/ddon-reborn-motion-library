import unittest

import numpy as np

from viewer_library import rex_motion_cues


class RexMotionCueTests(unittest.TestCase):
    def test_split_clip_uses_original_motion_clock(self):
        events = {120: [
            {"time": .25, "group": "3", "codes": [5504]},
            {"time": .75, "group": "3", "codes": [7503]},
            {"time": 1.25, "group": "3", "codes": [7503]},
            {"time": 1.50, "group": "1", "codes": [7503]},
        ]}
        times = np.arange(30) / 4
        stages = [
            {"name": "Motion[120]_start", "start": 4, "end": 7},
            {"name": "Motion[120]_loop", "start": 8, "end": 11},
        ]
        self.assertEqual(rex_motion_cues(stages, events, times, 1), ([(5, 7), (7, 7), (9, 11)], []))
        self.assertEqual(rex_motion_cues(stages[1:], events, times, 1), ([(9, 11)], []))

    def test_casting_ends_before_orb_release(self):
        events = {121: [
            {"time": .25, "group": "3", "codes": [5505]},
            {"time": .50, "group": "3", "codes": [305, 5501, 533]},
        ]}
        stages = [{"name": "Motion[121]", "start": 4, "end": 8}]
        self.assertEqual(rex_motion_cues(stages, events, np.arange(10) / 4, 1), ([(5, 5)], [6]))
