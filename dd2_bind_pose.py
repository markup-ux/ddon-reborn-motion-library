"""Export the bind pose from a DD2 MESH 260421070 body mesh."""

import argparse
import json
import math
from pathlib import Path
import struct


MESH_HEADER_VERSION = 251205828


def read_bind_pose(data):
    magic, version, size = struct.unpack_from("<4sII", data)
    if (magic, version, size) != (b"MESH", MESH_HEADER_VERSION, len(data)):
        raise ValueError("expected a complete DD2 MESH with header version 251205828")

    # DD2 updated MESH layout, as documented by REasy's mesh parser.
    name_count, = struct.unpack_from("<H", data, 20)
    bones_at, _, name_indices_at, _, names_at = struct.unpack_from("<5Q", data, 120)
    if bones_at <= 0 or bones_at + 48 > len(data):
        raise ValueError("skeleton header lies outside the mesh")
    count, remap_count, hierarchy_at, local_at, world_at, inverse_at = struct.unpack_from(
        "<II8x4Q", data, bones_at,
    )
    if not count or count > name_count or remap_count > count:
        raise ValueError("invalid skeleton counts")
    for offset, span in (
        (name_indices_at, count * 2), (names_at, name_count * 8),
        (hierarchy_at, count * 16), (local_at, count * 64),
        (world_at, count * 64), (inverse_at, count * 64),
    ):
        if offset <= 0 or offset + span > len(data):
            raise ValueError("skeleton table lies outside the mesh")

    joints = []
    for index in range(count):
        joint_id, parent = struct.unpack_from("<2h", data, hierarchy_at + index * 16)
        name_index, = struct.unpack_from("<H", data, name_indices_at + index * 2)
        if joint_id != index or parent < -1 or parent >= index or name_index >= name_count:
            raise ValueError(f"invalid hierarchy or name index for joint {index}")
        name_at, = struct.unpack_from("<Q", data, names_at + name_index * 8)
        name_end = data.find(b"\0", name_at)
        if name_at <= 0 or name_end <= name_at:
            raise ValueError(f"missing name for joint {index}")
        matrices = {
            label: struct.unpack_from("<16f", data, offset + index * 64)
            for label, offset in (
                ("local_matrix", local_at), ("world_matrix", world_at),
                ("inverse_bind_matrix", inverse_at),
            )
        }
        if any(not math.isfinite(value) for matrix in matrices.values() for value in matrix):
            raise ValueError(f"non-finite bind transform for joint {index}")
        joints.append({
            "id": index,
            "parent": None if parent == -1 else parent,
            "name": data[name_at:name_end].decode("utf-8"),
            **matrices,
        })
    return {"mesh_header_version": version, "joints": joints}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mesh", type=Path)
    parser.add_argument("output", type=Path, help="new JSON path in ignored local storage")
    args = parser.parse_args()
    try:
        pose = read_bind_pose(args.mesh.read_bytes())
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(pose, output, indent=2, allow_nan=False)
            output.write("\n")
    except (OSError, ValueError, struct.error) as error:
        parser.exit(2, f"error: {error}\n")
    print(f"Exported {len(pose['joints'])} joints to {args.output}")


if __name__ == "__main__":
    main()
