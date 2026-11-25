"""Encode and read back the isolated DDON playback probe using REasy v0.8.0."""

import argparse
import json
import math
from pathlib import Path
import struct
import sys


MOTION_NAME = "ddon_reborn_fighter_probe"
MOTION_ID = 1


def encode(keys, template_bytes, name=MOTION_NAME):
    from file_handlers.motion.decode import read_motlist
    from file_handlers.motion.encode import write_motlist
    from file_handlers.motion.model import JointChannels, Motion, MotionList, MotionListEntry, Track
    from utils.hash_util import murmur3_hash_utf16le

    template = read_motlist(template_bytes)
    if template.version != 1057:
        raise ValueError("expected a DD2 MOTLIST 1057 template")
    native, = (entry.item for entry in template.entries if entry.motion_id == 0)
    if native.version != 993 or native.skeleton is None:
        raise ValueError("template motion 0 must expose the DD2 MOT 993 skeleton")
    entries = {joint.name: joint for joint in native.skeleton.joints}
    frames = keys["frames"]
    if frames != list(range(len(frames))) or len(frames) < 2 or keys["fps"] != native.fps:
        raise ValueError("expected contiguous sample frames at the template frame rate")

    def track(row, field, kind, width):
        if field not in row:
            return None
        samples = row[field]
        if len(samples) != len(frames) or any(
            len(value) != width or not all(math.isfinite(c) for c in value) for value in samples
        ):
            raise ValueError(f"invalid {field} samples for {row['joint']}")
        data = b"".join(struct.pack(f"<{width}f", *value) for value in samples)
        return Track(kind, frames, 0, (), data, frames[-1])

    channels = []
    for row in keys["tracks"]:
        joint = entries[row["joint"]]
        channels.append(JointChannels(
            joint.joint_id, murmur3_hash_utf16le(joint.name),
            translation=track(row, "translation", "vector3", 3),
            rotation=track(row, "rotation", "quaternion", 4),
        ))
    motion = Motion(
        native.version, name, end_frame=frames[-1], raw_end_frame=frames[-1],
        fps=native.fps, joint_count=len(native.skeleton.joints), skeleton=native.skeleton,
        channels=channels,
    )
    output = write_motlist(MotionList(
        template.version, name, [MotionListEntry(MOTION_ID, motion)],
    ))

    probe = read_motlist(output)
    retrieved, = probe.entries
    actual = retrieved.item
    if (
        probe.version != template.version or retrieved.motion_id != MOTION_ID
        or actual.version != native.version or actual.fps != motion.fps
        or actual.end_frame != motion.end_frame or len(actual.channels) != len(channels)
        or actual.skeleton is None or len(actual.skeleton.joints) != len(native.skeleton.joints)
    ):
        raise ValueError("generated probe metadata did not round trip")
    for expected, decoded in zip(channels, actual.channels):
        if (decoded.joint_id, decoded.name_hash) != (expected.joint_id, expected.name_hash):
            raise ValueError("generated probe joint IDs did not round trip")
        for field in ("translation", "rotation"):
            original, restored = getattr(expected, field), getattr(decoded, field)
            if original is None and restored is None:
                continue
            if original is None or restored is None:
                raise ValueError("generated probe track presence did not round trip")
            if (original.frames, original.data) != (restored.frames, restored.data):
                raise ValueError("generated probe track samples did not round trip")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reasy", type=Path, required=True, help="REasy v0.8.0 source root")
    parser.add_argument("--name", default=MOTION_NAME, help="internal motion and list name")
    parser.add_argument("keys", type=Path)
    parser.add_argument("template", type=Path)
    parser.add_argument("output", type=Path, help="new MOTLIST path in ignored storage")
    args = parser.parse_args()
    sys.path.insert(0, str(args.reasy.resolve(strict=True)))
    output = encode(json.loads(args.keys.read_text(encoding="utf-8")), args.template.read_bytes(), args.name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as destination:
        destination.write(output)
    print(f"Wrote and read back {len(output)} bytes: {args.output}")


if __name__ == "__main__":
    main()
