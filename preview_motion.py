"""Build a local HTML preview of a DDON clip and one or more DD2 retargets."""

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from retarget_motion import JOINTS, accessor, read_glb, rotation_array, sample
from preview_mesh import dd2_mesh, load_mesh_reader, packed_mesh, read_dd2_parts, source_mesh
from preview_legs import LIMBS, correct_ddon_limbs, fit_limb


SOURCE_BY_ROLE = {role: node for node, role in JOINTS.items()} | {"R_PropA": "100:150"}
CHAINS = (
    ("Body", ("Hip", "Spine_0", "Spine_2", "Neck_0", "Head_0")),
    ("Right arm", ("Spine_2", "R_Arm_Clavicle", "R_Arm_Upper", "R_Arm_Lower", "R_Arm_Hand", "R_PropA")),
    ("Left arm", ("Spine_2", "L_Arm_Clavicle", "L_Arm_Upper", "L_Arm_Lower", "L_Arm_Hand")),
    ("Right leg", ("Hip", "R_Leg_Upper", "R_Leg_Lower", "R_Leg_Ankle")),
    ("Left leg", ("Hip", "L_Leg_Upper", "L_Leg_Lower", "L_Leg_Ankle")),
)
ROLES = list(dict.fromkeys(role for _, chain in CHAINS for role in chain))


def world_matrices(locals_, parents):
    result = {}

    def visit(index):
        if index not in result:
            parent = parents.get(index)
            local = locals_[:, index]
            result[index] = local if parent is None else visit(parent) @ local
        return result[index]

    return np.stack([visit(index) for index in range(locals_.shape[1])], axis=1)


def with_idle(action, idle, parents, root, blend):
    def blend_pose(start, end, weight):
        def local(frame):
            result = frame.copy()
            children = list(parents)
            result[children] = np.linalg.solve(frame[list(parents.values())], frame[children])
            return result

        a, b = local(start), local(end)
        a_scale, b_scale = (np.linalg.norm(matrix[:, :3, :3], axis=1) for matrix in (a, b))
        qa = Rotation.from_matrix(a[:, :3, :3] / a_scale[:, None]).as_quat()
        qb = Rotation.from_matrix(b[:, :3, :3] / b_scale[:, None]).as_quat()
        qb[np.sum(qa * qb, axis=1) < 0] *= -1
        weight = weight * weight * (3 - 2 * weight)
        scales = (1 - weight) * a_scale + weight * b_scale
        a[:, :3, :3] = Rotation.from_quat((1 - weight) * qa + weight * qb).as_matrix() * scales[:, None]
        a[:, :3, 3] = (1 - weight) * a[:, :3, 3] + weight * b[:, :3, 3]
        return world_matrices(a[None], parents)[0]

    origin = idle[0, root, :3, 3]
    pre, post = idle.copy(), idle.copy()
    pre[:, :, :3, 3] += action[0, root, :3, 3] - origin
    post[:, :, :3, 3] += action[-1, root, :3, 3] - origin
    for frame in range(blend):
        weight = (frame + 1) / (blend + 1)
        end = len(pre) - blend + frame
        pre[end] = blend_pose(pre[end], action[0], weight)
        post[frame] = blend_pose(action[-1], post[frame], weight)
    return np.concatenate((pre, action, post))


def source_pose(gltf, binary, clip, times, foot_targets=False, *, fit_hands=False, limb_targets=None):
    nodes = gltf["nodes"]
    names = {node.get("name"): index for index, node in enumerate(nodes)}
    parents = {child: index for index, node in enumerate(nodes) for child in node.get("children", [])}
    pos = np.tile(np.array([node.get("translation", [0, 0, 0]) for node in nodes], dtype=float), (len(times), 1, 1))
    rot = np.tile(np.array([node.get("rotation", [0, 0, 0, 1]) for node in nodes], dtype=float), (len(times), 1, 1))
    scale = np.tile(np.array([node.get("scale", [1, 1, 1]) for node in nodes], dtype=float), (len(times), 1, 1))
    fields = {"translation": pos, "rotation": rot, "scale": scale}
    keyed = set()
    animation = next((item for item in gltf.get("animations", []) if item["name"] == clip), None)
    for channel in animation["channels"] if animation else []:
        binding = channel["target"]
        field = binding["path"]
        if field not in fields:
            continue
        sampler = animation["samplers"][channel["sampler"]]
        keys = accessor(gltf, binary, sampler["input"])[:, 0]
        values = accessor(gltf, binary, sampler["output"])
        fields[field][:, binding["node"]] = sample(keys, values, times, field, sampler.get("interpolation", "LINEAR"))
        keyed.add((binding["node"], field))
    locals_ = np.tile(np.eye(4), (len(times), len(nodes), 1, 1))
    locals_[:, :, :3, :3] = rotation_array(rot) * scale[..., None, :]
    locals_[:, :, :3, 3] = pos
    world = world_matrices(locals_, parents)
    if foot_targets or fit_hands:
        rest = np.tile(np.eye(4), (len(nodes), 1, 1))
        rest[:, :3, 3] = [node.get("translation", [0, 0, 0]) for node in nodes]
        fitted = correct_ddon_limbs(world, rest, parents, names, pos, rotation_array(rot), keyed, foot_targets)
        if limb_targets is not None:
            limb_targets.update(fitted)
    return world, {role: names[node] for role, node in SOURCE_BY_ROLE.items() if node in names}


def target_pose(joints, motion, source_feet=None, *, limb_targets=None):
    indices = {joint["name"]: index for index, joint in enumerate(joints)}
    ids = {joint["id"]: index for index, joint in enumerate(joints)}
    parents = {
        index: ids[joint["parent"]]
        for index, joint in enumerate(joints) if joint["parent"] is not None
    }
    rest = np.array([joint["local_matrix"] for joint in joints]).reshape(-1, 4, 4).transpose(0, 2, 1)
    locals_ = np.tile(rest, (len(motion["frames"]), 1, 1, 1))
    for track in motion["tracks"]:
        index = indices.get(track["joint"])
        if index is None:
            continue
        if "rotation" in track:
            scale = np.linalg.norm(rest[index, :3, :3], axis=0)
            locals_[:, index, :3, :3] = rotation_array(np.array(track["rotation"])) * scale
        if "translation" in track:
            locals_[:, index, :3, 3] = track["translation"]
        if "scale" in track:
            axis = locals_[:, index, :3, :3]
            axes = axis / np.maximum(np.linalg.norm(axis, axis=1, keepdims=True), 1e-9)
            locals_[:, index, :3, :3] = axes * np.asarray(track["scale"])[:, None, :]
    world = world_matrices(locals_, parents)
    if source_feet:
        for side, goals in source_feet.items():
            upper, lower, ankle = (indices[f"{side}_Leg_{node}"] for node in ("Upper", "Lower", "Ankle"))
            fit_limb(world, rest, parents, upper, lower, ankle, goals + world[:, indices["root"], :3, 3])
    for role, (goals, bends) in (limb_targets or {}).items():
        upper, lower, end = (indices[name] for name in LIMBS[role][1])
        fit_limb(world, rest, parents, upper, lower, end, goals + world[:, indices["root"], :3, 3], bends=bends)
    return world, indices


def render_data(label, pose, indices):
    positions = pose[..., :3, 3][:, [indices[role] for role in ROLES]]
    grip = pose[:, indices["R_PropA"], :3, :]
    root = pose[:, indices["root"], :3, 3]
    return {
        "label": label,
        "positions": np.round(positions, 5).tolist(),
        "grip": np.round(grip, 5).tolist(),
        "root": np.round(root, 5).tolist(),
    }


def scene_html(label, fps, frames, views, skeletal=True, *, heading="", note="", transition=None, stages=None, times=None, effect=None):
    payload = {
        "clip": label, "heading": heading or label, "fps": fps, "frames": frames, "views": views, "note": note,
        "transition": transition, "stages": stages, "times": times, "effect": effect,
        "chains": [[label, [ROLES.index(role) for role in chain]] for label, chain in CHAINS] if skeletal else [],
    }
    template = Path(__file__).with_name("motion_preview.html").read_text(encoding="utf-8")
    data = json.dumps(payload, allow_nan=False, ensure_ascii=True).replace("<", "\\u003c")
    return template.replace("__MOTION_DATA__", data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_glb", type=Path)
    parser.add_argument("target_bind", type=Path)
    parser.add_argument("output", type=Path, help="new HTML path in ignored local storage")
    parser.add_argument("retargets", type=Path, nargs="+", help="synchronized retarget JSON files")
    parser.add_argument("--dd2-mesh-reader", type=Path, help="path to the local viewer's mesh_re.py")
    parser.add_argument("--dd2-mesh", type=Path, action="append", default=[], help="body, head or equipment mesh")
    parser.add_argument("--foot-targets", action="store_true", help="preview the fitted DDON foot-marker hypothesis")
    args = parser.parse_args()
    motions = [json.loads(path.read_text(encoding="utf-8")) for path in args.retargets]
    first = motions[0]
    timeline = ("source_clip", "frames", "fps")
    if any(any(motion[key] != first[key] for key in timeline) for motion in motions[1:]):
        raise ValueError("comparison requires the same source clip and sample times")
    gltf, binary = read_glb(args.source_glb)
    joints = json.loads(args.target_bind.read_text(encoding="utf-8"))["joints"]
    times = np.array(first["frames"]) / first["fps"]
    source_world, source_indices = source_pose(gltf, binary, first["source_clip"], times, args.foot_targets)
    suffix = " · fitted feet" if args.foot_targets else ""
    views = [render_data("DDON source" + suffix, source_world, source_indices)]
    views[0]["mesh"] = packed_mesh(source_mesh(gltf, binary, source_world))
    if args.dd2_mesh and not args.dd2_mesh_reader:
        parser.error("--dd2-mesh requires --dd2-mesh-reader")
    parts = read_dd2_parts(load_mesh_reader(args.dd2_mesh_reader), args.dd2_mesh) if args.dd2_mesh else []
    feet = None
    if args.foot_targets:
        root = source_world[:, source_indices["root"], :3, 3]
        feet = {side: source_world[:, source_indices[f"{side}_Leg_Ankle"], :3, 3] - root for side in "RL"}
    for path, motion in zip(args.retargets, motions):
        world, indices = target_pose(joints, motion, feet)
        views.append(render_data(path.stem + suffix, world, indices))
        if parts:
            views[-1]["mesh"] = packed_mesh(dd2_mesh(parts, world, indices, joints))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as destination:
        destination.write(scene_html(first["source_clip"], first["fps"], first["frames"], views))
    print(f"Wrote {len(views)} synchronized rigs to {args.output}")


if __name__ == "__main__":
    main()
