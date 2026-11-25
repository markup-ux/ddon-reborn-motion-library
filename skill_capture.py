"""Input markers for one explicit DDON recording session."""
from datetime import datetime, timedelta, timezone
import json
from time import perf_counter


KEYS = {
    "Off": 0,
    **{name: 0x70 + i for i, name in enumerate(f"F{n}" for n in range(1, 13))},
    **{name: ord(name) for name in "1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    "Space": 0x20,
    "Left mouse": 0x01,
    "Right mouse": 0x02,
    "Middle mouse": 0x04,
    "Mouse button 4": 0x05,
    "Mouse button 5": 0x06,
}
MODIFIERS = {"None": 0, "Shift": 0x10, "Ctrl": 0x11, "Alt": 0x12}
FPS = 60


class SkillTimeline:
    def __init__(self, skill, element, variant, key, modifier):
        self.skill, self.element, self.variant = skill, element, variant
        self.key, self.modifier = key, modifier
        self.started_at = datetime.now(timezone.utc)
        self.started_clock = perf_counter()
        self.first_frame_at = None
        self.client_pid = None
        self.input_source = "Sampled Windows input state"
        self.events = []
        self.pressed = self.primed = False

    def observe(self, focused, down, at):
        if not KEYS[self.key]:
            return
        if not focused:
            self.interrupt(at)
            return
        if not self.primed:
            self.primed = not down
            return
        if down != self.pressed:
            self.events.append(("press" if down else "release", at))
            self.pressed = down

    def interrupt(self, at):
        if self.pressed:
            self.events.append(("interrupted", at))
        self.pressed = self.primed = False

    def save(self, clip, ended_at=None):
        events = []
        pressed_at = None
        for kind, at in self.events:
            row = {"event": kind, "utc": self.utc_at(at)}
            if kind == "press":
                pressed_at = at
            elif pressed_at is not None:
                row["held_seconds"] = round(at - pressed_at, 4)
                pressed_at = None
            if self.first_frame_at is not None:
                seconds = at - self.first_frame_at
                row["video_seconds"] = round(seconds, 4)
                row["approx_frame"] = round(seconds * FPS) if seconds >= 0 else None
            events.append(row)
        report = {
            "video": clip.name,
            "skill": self.skill,
            "element": self.element,
            "variant": self.variant,
            "input": {"button": self.key, "modifier": self.modifier},
            "input_source": self.input_source,
            "video_fps": FPS,
            "client_pid": self.client_pid,
            "recording_end_utc": self.utc_at(ended_at if ended_at is not None else perf_counter()),
            "first_frame_observed_utc": self.utc_at(self.first_frame_at) if self.first_frame_at is not None else None,
            "sync_method": "Observed first FFmpeg input frame; input sampling and pipeline delay make frame indices approximate.",
            "events": events,
        }
        destination = clip.with_suffix(".skill.json")
        temp = destination.with_suffix(".tmp")
        temp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        temp.replace(destination)
        return destination

    def utc_at(self, at):
        return (self.started_at + timedelta(seconds=at - self.started_clock)).isoformat(timespec="milliseconds")
