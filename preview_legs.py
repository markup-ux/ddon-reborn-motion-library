"""Reconstruct limb poses from DDON player-rig endpoint markers."""

import numpy as np
from scipy.spatial.transform import Rotation


LEGS = (("2:14", "3:15", "5:16"), ("6:18", "7:19", "9:20"))
ARMS = (("80:6", "81:7", "83:8"), ("103:10", "104:11", "106:12"))
LIMBS = {
    f"{side}_{end}": (chain, tuple(f"{side}_{prefix}_{part}" for part in ("Upper", "Lower", end)))
    for prefix, end, chains in (("Arm", "Hand", ARMS), ("Leg", "Ankle", LEGS))
    for side, chain in zip("RL", chains)
}


def is_endpoint_marker(values, rest):
    # These tracks carry root-space positions where a local bone offset would
    # normally stay near the rest vector.
    length = np.linalg.norm(rest)
    return length > 1e-5 and np.median(np.linalg.norm(values - rest, axis=1)) > 1.15 * length


def unit(value):
    length = np.linalg.norm(value)
    if length <= 1e-9:
        raise ValueError("cannot reconstruct a degenerate leg segment")
    return value / length


def swing(old, new):
    return Rotation.align_vectors([unit(new)], [unit(old)])[0].as_matrix()


def fit_limb(world, rest, parents, upper, lower, end, goals, *, rotations=None, bends=None):
    original = world.copy()
    segments = [np.linalg.norm(rest[joint, :3, 3]) for joint in (lower, end)]
    a, b = segments
    if min(a, b) <= 1e-5:
        raise ValueError("cannot fit a limb with a zero-length bind segment")
    # Inherit the nearest corrected ancestor for twist, finger and toe bones.
    corrected = {upper, lower, end}
    affected = {}
    for child in range(world.shape[1]):
        ancestor = parents.get(child)
        while ancestor is not None and ancestor not in corrected:
            ancestor = parents.get(ancestor)
        if ancestor is not None and child not in corrected:
            affected[child] = ancestor
    for frame, goal in enumerate(goals):
        base = original[frame, upper, :3, 3]
        raw_bend = original[frame, lower, :3, 3] - base
        to_goal = goal - base
        if np.linalg.norm(to_goal) <= 1e-9:
            to_goal = raw_bend
        axis = unit(to_goal)
        distance = np.clip(np.linalg.norm(to_goal), abs(a - b) + 1e-5, a + b - 1e-5)
        # Use the source bend for the target rather than choosing a new branch.
        hint = raw_bend if bends is None else bends[frame]
        pole = hint - axis * (axis @ hint)
        if np.linalg.norm(pole) < 1e-5:
            basis = np.eye(3)[np.argmin(np.abs(axis))]
            pole = basis - axis * (axis @ basis)
        along = (a * a + distance * distance - b * b) / (2 * distance)
        middle = base + axis * along + unit(pole) * np.sqrt(max(a * a - along * along, 0))
        tip = base + axis * distance
        upper_rot = original[frame, upper, :3, :3]
        lower_rot = original[frame, lower, :3, :3]
        downstream = lower_rot @ rest[end, :3, 3]
        world[frame, upper, :3, :3] = swing(raw_bend, middle - base) @ upper_rot
        world[frame, lower, :3, :3] = swing(downstream, tip - middle) @ lower_rot
        world[frame, lower, :3, 3] = middle
        world[frame, end, :3, 3] = tip
        if rotations is not None:
            world[frame, end, :3, :3] = rotations[frame]
        for child, ancestor in affected.items():
            delta = world[frame, ancestor] @ np.linalg.inv(original[frame, ancestor])
            world[frame, child] = delta @ original[frame, child]


def correct_ddon_limbs(world, rest, parents, names, positions, local_rotations, keyed, fit_feet):
    root = world[:, names["reference"]]
    fitted = {}
    for role, (chain, _) in LIMBS.items():
        if (role.endswith("Ankle") and not fit_feet) or not all(name in names for name in chain):
            continue
        upper, lower, end = (names[name] for name in chain)
        markers = positions[:, end]
        if (end, "translation") not in keyed or not is_endpoint_marker(markers, rest[end, :3, 3]):
            continue
        rotation = root[:, :3, :3]
        goals = (rotation @ markers[..., None])[..., 0] + root[:, :3, 3]
        # The translation and rotation of the sampled IK endpoints are paired
        # in character space. Ordinary FK endpoints keep their local rotations.
        aim = rotation @ local_rotations[:, end] if (end, "rotation") in keyed else None
        fit_limb(world, rest, parents, upper, lower, end, goals, rotations=aim)
        fitted[role] = (goals - root[:, :3, 3], world[:, lower, :3, 3] - world[:, upper, :3, 3])
    return fitted
