"""Read and sample local DD2 MOT data with the external REasy motion codec."""

import json
import math
from pathlib import Path
import sys


def main():
    reasy, source, action, selected = sys.argv[1:]
    sys.path.insert(0, reasy)
    import numpy as np
    from file_handlers.motion.decode import read_file
    from file_handlers.motion.keys import Curve
    from file_handlers.motion.model import Motion, MotionList
    from file_handlers.motion.rules import encoding, mot_profile

    file = read_file(Path(source).read_bytes())
    if isinstance(file, MotionList):
        entries = [(str(i), f"{entry.motion_id} · {getattr(entry.item, 'name', type(entry.item).__name__)}", entry.item)
                   for i, entry in enumerate(file.entries)]
    elif isinstance(file, Motion):
        entries = [("0", file.name, file)]
    else:
        raise ValueError("The codec did not return a standalone motion or motion list.")
    if action == "list":
        clips = [{"key": key if isinstance(item, Motion) else "", "name": name} for key, name, item in entries]
        return {"clips": clips, "slots": len(clips), "unavailable": sum(not row["key"] for row in clips)}
    _, name, motion = next(entry for entry in entries if entry[0] == selected)
    if not isinstance(motion, Motion):
        raise ValueError("This entry references another resource or a motion tree.")
    fallback = next((item.skeleton for _, _, item in entries if isinstance(item, Motion) and item.skeleton), None)
    skeleton = motion.skeleton or fallback
    if not skeleton:
        raise ValueError("The motion has no embedded skeleton.")
    names = {entry.joint_id: entry.name for entry in skeleton.joints}
    end = float(motion.end_frame)
    count = max(1, min(81, 1 + math.ceil(end)))
    fps = (count - 1) * motion.fps / end if count > 1 and end > 0 else motion.fps
    times = np.linspace(0, end, count)
    family = mot_profile(motion.version).keys
    tracks = []
    for channel in motion.channels:
        name_by_id = names.get(channel.joint_id)
        if not name_by_id:
            continue
        track = {"joint": name_by_id}
        for field in ("translation", "rotation", "scale"):
            value = getattr(channel, field)
            if value is None:
                continue
            curve = Curve(value, encoding(family, value.kind, value.encoding))
            samples = np.asarray([curve.sample(frame) for frame in times])
            if np.isfinite(samples).all() and (field != "rotation" or np.all(np.linalg.norm(samples, axis=1) > 1e-10)):
                track[field] = samples.tolist()
        tracks.append(track)
    if not tracks:
        raise ValueError("This motion has no CPU joint tracks supported by the preview.")
    return {"source_clip": name, "frames": list(range(count)), "fps": fps, "tracks": tracks}


if __name__ == "__main__":
    # The subprocess protocol is always UTF-8, including localized clip names.
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(main(), allow_nan=False))
