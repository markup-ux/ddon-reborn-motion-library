"""Retarget a DDON player-rig motion as a DD2 skeletal playback candidate."""

import argparse
import json
from pathlib import Path
import struct

import numpy as np
from scipy.spatial.transform import Rotation, Slerp


# Provisional anatomical correspondences for the DDON pl000000 source rig.
JOINTS = {
    "reference": "root",
    "0:0": "Hip",
    "10:1": "Spine_0",
    "11:2": "Spine_2",
    "12:3": "Neck_0",
    "13:4": "Head_0",
    "79:5": "R_Arm_Clavicle",
    "80:6": "R_Arm_Upper",
    "81:7": "R_Arm_Lower",
    "83:8": "R_Arm_Hand",
    "102:9": "L_Arm_Clavicle",
    "103:10": "L_Arm_Upper",
    "104:11": "L_Arm_Lower",
    "106:12": "L_Arm_Hand",
    "2:14": "R_Leg_Upper",
    "3:15": "R_Leg_Lower",
    "5:16": "R_Leg_Ankle",
    "6:18": "L_Leg_Upper",
    "7:19": "L_Leg_Lower",
    "9:20": "L_Leg_Ankle",
}

FPS = 60

# Match shoulder/elbow directions and palm orientation before applying motion.
ARM_AIMS = (
    ("R_Arm_Upper", "81:7", "R_Arm_Lower"),
    ("R_Arm_Lower", "83:8", "R_Arm_Hand"),
    ("L_Arm_Upper", "104:11", "L_Arm_Lower"),
    ("L_Arm_Lower", "106:12", "L_Arm_Hand"),
)
PALM_AIMS = (
    ("R_Arm_Hand", ("91:28", "97:25"), "94:22", "R_Hand_MiddleF_1", "R_Hand_ThumbF_1"),
    ("L_Arm_Hand", ("114:41", "120:44"), "117:38", "L_Hand_MiddleF_1", "L_Hand_ThumbF_1"),
)


def read_glb(path):
    data = path.read_bytes()
    if struct.unpack_from("<4sII", data) != (b"glTF", 2, len(data)):
        raise ValueError("expected a complete glTF 2.0 binary")
    length, kind = struct.unpack_from("<I4s", data, 12)
    end = 20 + length
    bin_length, bin_kind = struct.unpack_from("<I4s", data, end)
    if kind != b"JSON" or bin_kind != b"BIN\0" or end + 8 + bin_length != len(data):
        raise ValueError("expected JSON and binary chunks")
    return json.loads(data[20:end]), memoryview(data)[end + 8:]


def accessor(gltf, binary, index):
    entry = gltf["accessors"][index]
    view = gltf["bufferViews"][entry["bufferView"]]
    dtype = np.dtype({5120: "i1", 5121: "u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}[
        entry["componentType"]
    ])
    components = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}[entry["type"]]
    stride = view.get("byteStride", components * dtype.itemsize)
    offset = view.get("byteOffset", 0) + entry.get("byteOffset", 0)
    if view.get("buffer", 0) != 0 or "sparse" in entry:
        raise ValueError("expected a dense accessor in the GLB buffer")
    values = np.ndarray(
        (entry["count"], components), dtype=dtype, buffer=binary, offset=offset,
        strides=(stride, dtype.itemsize),
    ).astype(float)
    if entry.get("normalized") and dtype.kind in "iu":
        values = np.maximum(values / np.iinfo(dtype).max, -1.0)
    if not np.all(np.isfinite(values)):
        raise ValueError("invalid non-finite animation samples")
    return values


def sample(times, values, key_times, field, interpolation):
    if len(times) != len(values) or not len(times) or np.any(np.diff(times) <= 0):
        raise ValueError("invalid animation key times")
    if len(times) == 1:
        return np.repeat(values, len(key_times), axis=0)
    if interpolation == "STEP":
        return values[np.maximum(0, np.searchsorted(times, key_times, side="right") - 1)]
    if interpolation != "LINEAR":
        raise ValueError(f"unsupported interpolation {interpolation}")
    if field == "rotation":
        return Slerp(times, Rotation.from_quat(values))(np.clip(key_times, times[0], times[-1])).as_quat()
    return np.column_stack([np.interp(key_times, times, column) for column in values.T])


def rotation_array(quaternions):
    shape = (*quaternions.shape[:-1], 3, 3)
    return Rotation.from_quat(quaternions.reshape(-1, 4)).as_matrix().reshape(shape)


def rig_world_rotations(local, parents):
    world = np.empty_like(local)
    visited = set()

    def visit(index, active):
        if index in visited:
            return world[..., index, :, :]
        if index in active:
            raise ValueError("cyclic source node hierarchy")
        parent = parents.get(index)
        rotation = local[..., index, :, :]
        world[..., index, :, :] = rotation if parent is None else visit(parent, active | {index}) @ rotation
        visited.add(index)
        return world[..., index, :, :]

    for index in range(local.shape[-3]):
        visit(index, set())
    return world


def arm_alignment(names, target_names, rest_pos, source_rest, target_world, joint_map):
    corrections = {}
    for owner, source_child, target_child in ARM_AIMS:
        index = target_names[owner]
        source_aim = source_rest[joint_map[index]] @ rest_pos[names[source_child]]
        target_aim = target_world[target_names[target_child], :3, 3] - target_world[index, :3, 3]
        corrections[index] = Rotation.align_vectors([source_aim], [target_aim])[0].as_matrix()
    for owner, fingers, thumb, target_finger, target_thumb in PALM_AIMS:
        index = target_names[owner]
        source_owner = source_rest[joint_map[index]]
        source_forward = source_owner @ np.mean([rest_pos[names[finger]] for finger in fingers], axis=0)
        source_side = source_owner @ rest_pos[names[thumb]]
        origin = target_world[index, :3, 3]
        target_forward = target_world[target_names[target_finger], :3, 3] - origin
        target_side = target_world[target_names[target_thumb], :3, 3] - origin
        corrections[index] = Rotation.align_vectors(
            [source_forward, source_side], [target_forward, target_side], weights=[np.inf, 1.0],
        )[0].as_matrix()
    return corrections


def retarget(gltf, binary, target, clip="Motion[0]", sword_grip=False, align_arms=False, *,
             sample_times=None, solved_world=None, marker_joints=()):
    nodes = gltf["nodes"]
    animation, = (item for item in gltf["animations"] if item["name"] == clip)
    names = {node.get("name"): index for index, node in enumerate(nodes)}
    target_names = {joint["name"]: joint["id"] for joint in target}
    joint_map = {target_names[dst]: names[src] for src, dst in JOINTS.items()}
    grip = target_names["R_PropA"] if sword_grip else None
    if sword_grip:
        joint_map[grip] = names["100:150"]
    source_parents = {
        child: index for index, node in enumerate(nodes) for child in node.get("children", [])
    }
    rest_quat = np.array([node.get("rotation", [0, 0, 0, 1]) for node in nodes])
    rest_pos = np.array([node.get("translation", [0, 0, 0]) for node in nodes])
    if any("matrix" in node for node in nodes):
        raise ValueError("source rig must use separate TRS properties")
    channels = {}
    for channel in animation["channels"]:
        sampler = animation["samplers"][channel["sampler"]]
        times = accessor(gltf, binary, sampler["input"])[:, 0]
        values = accessor(gltf, binary, sampler["output"])
        key = (channel["target"]["node"], channel["target"]["path"])
        if key in channels:
            raise ValueError("duplicate source channel")
        channels[key] = (times, values, sampler.get("interpolation", "LINEAR"))

    if sample_times is None:
        end = max(times[-1] for times, _, _ in channels.values())
        end_frame = round(end * FPS)
        if end_frame < 1 or abs(end * FPS - end_frame) > 1e-4:
            raise ValueError("source clip does not end on a 60 Hz frame")
        times = np.arange(end_frame + 1) / FPS
        fps = FPS
    else:
        times = np.asarray(sample_times)
        fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] else FPS
    frames = list(range(len(times)))
    source_quat = np.broadcast_to(rest_quat, (len(frames), *rest_quat.shape)).copy()
    positions = {}
    for (index, field), (keys, values, interpolation) in channels.items():
        if field == "rotation":
            source_quat[:, index] = sample(keys, values, times, field, interpolation)
        elif field == "translation":
            positions[index] = sample(keys, values, times, field, interpolation)

    source_rest = rig_world_rotations(rotation_array(rest_quat), source_parents)
    source_pose = (rig_world_rotations(rotation_array(source_quat), source_parents)
                   if solved_world is None else solved_world[:, :, :3, :3])
    used = set(joint_map.values())
    ignored = [
        {"node": nodes[index]["name"], "path": field}
        for index, field in channels if index not in used or field not in {"translation", "rotation"}
    ]
    target_locals = np.array([joint["local_matrix"] for joint in target]).reshape(-1, 4, 4).transpose(0, 2, 1)
    target_world = np.array([joint["world_matrix"] for joint in target]).reshape(-1, 4, 4).transpose(0, 2, 1)
    target_local_rot = Rotation.from_matrix(target_locals[:, :3, :3]).as_matrix()
    target_rest_rot = Rotation.from_matrix(target_world[:, :3, :3]).as_matrix()
    corrections = arm_alignment(
        names, target_names, rest_pos, source_rest, target_world, joint_map,
    ) if align_arms else {}
    target_pose = {}
    tracks = []
    for joint in target:
        index, parent = joint["id"], joint["parent"]
        parent_pose = np.eye(3) if parent is None else target_pose[parent]
        if index not in joint_map:
            target_pose[index] = parent_pose @ target_local_rot[index]
            continue

        source = joint_map[index]
        source_parent = source_parents.get(source)
        basis = np.eye(3) if source_parent is None else source_rest[source_parent]
        target_basis = np.eye(3) if parent is None else target_rest_rot[parent]
        if index == grip:
            # Preserve DD2's initial grip and carry over the source's hand-relative changes.
            grip_rotation = rotation_array(source_quat[:, source])
            conversion = target_basis.T @ basis
            local = conversion @ (grip_rotation @ grip_rotation[0].T) @ conversion.T @ target_local_rot[index]
            target_pose[index] = parent_pose @ local
        else:
            rest_rotation = target_rest_rot[index]
            if index in corrections:
                rest_rotation = corrections[index] @ rest_rotation
            desired = source_pose[:, source] @ source_rest[source].T @ rest_rotation
            target_pose[index] = desired
            local = np.swapaxes(parent_pose, -2, -1) @ desired
        rotations = Rotation.from_matrix(local).as_quat()
        for frame in range(1, len(rotations)):
            if rotations[frame - 1] @ rotations[frame] < 0:
                rotations[frame] *= -1
        track = {"joint": joint["name"], "rotation": rotations.tolist()}
        if source in positions and joint["name"] not in marker_joints:
            origin = positions[source][0] if index == grip else rest_pos[source]
            delta = positions[source] - origin
            track["translation"] = (delta @ basis.T @ target_basis + target_locals[index, :3, 3]).tolist()
        tracks.append(track)

    return {"source_clip": animation["name"], "fps": fps, "frames": frames,
            "unmapped_source_channels": ignored, "tracks": tracks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clip", default="Motion[0]", help="name of the source GLB animation")
    parser.add_argument("--sword-grip", action="store_true", help="test the R_PropA weapon attachment mapping")
    parser.add_argument("--align-arms", action="store_true", help="align arm and palm rest-pose directions")
    parser.add_argument("source_glb", type=Path)
    parser.add_argument("target_bind", type=Path)
    parser.add_argument("output", type=Path, help="new JSON path under ignored local storage")
    args = parser.parse_args()
    gltf, binary = read_glb(args.source_glb)
    target = json.loads(args.target_bind.read_text(encoding="utf-8"))["joints"]
    result = retarget(gltf, binary, target, args.clip, args.sword_grip, args.align_arms)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, allow_nan=False)
        output.write("\n")
    print(f"Wrote {len(result['tracks'])} joints over {len(result['frames'])} frames; "
          f"{len(result['unmapped_source_channels'])} source channels need follow-up.")


if __name__ == "__main__":
    main()
