"""Skin locally supplied DDON GLB and DD2 MESH parts for the browser preview."""

import base64
from dataclasses import dataclass, replace
import importlib.util
from pathlib import Path
import struct
import sys

import numpy as np

from dd2_bind_pose import read_bind_pose
from retarget_motion import accessor
from viewer_materials import Material, material_for_name


@dataclass(frozen=True)
class Part:
    vertices: np.ndarray
    faces: np.ndarray
    uv: np.ndarray | None = None
    colors: np.ndarray | None = None
    material: str = ""
    appearance: Material = Material()
    visible: tuple[int, int] | None = None
    normals: np.ndarray | None = None

    def moved(self, vertices):
        return replace(self, vertices=vertices)

    def effect(self, vertices, start, end):
        return replace(self, vertices=vertices, visible=(start, end))


def skin_vertices(positions, joint_ids, weights, transforms, *, direction=False):
    totals = weights.sum(axis=1)
    if np.any(totals <= 0) or np.any(joint_ids[weights > 0] >= transforms.shape[1]):
        raise ValueError("invalid mesh skin weights or joint indices")
    joints = np.where(weights > 0, joint_ids, 0)
    points = positions if direction else np.column_stack((positions, np.ones(len(positions))))
    transforms = transforms[..., :3, :points.shape[1]]
    result = np.zeros((len(transforms), len(positions), 3))
    for influence in range(weights.shape[1]):
        indices = np.flatnonzero(weights[:, influence])
        blended = np.einsum(
            "fvij,vj->fvi", transforms[:, joints[indices, influence]], points[indices],
        )
        result[:, indices] += blended * (weights[indices, influence] / totals[indices])[None, :, None]
    return result


def source_mesh(gltf, binary, world, materials=None):
    skins = {}
    for index, skin in enumerate(gltf.get("skins", [])):
        if "inverseBindMatrices" in skin:
            inverse = accessor(gltf, binary, skin["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1)
        else:
            inverse = np.eye(4)
        skins[index] = world[:, skin["joints"]] @ inverse
    parts = []
    for node_index, node in enumerate(gltf["nodes"]):
        if "mesh" not in node:
            continue
        for part in gltf["meshes"][node["mesh"]]["primitives"]:
            fields = part["attributes"]
            if part.get("mode", 4) != 4:
                raise ValueError("expected triangle primitives for preview")
            definitions = gltf.get("materials", [])
            material = definitions[part["material"]] if "material" in part else {}
            name = material.get("name", "")
            if name.startswith(("OBJ_Shadow__", "OBJ_Alfa__shadow")):
                continue
            positions = accessor(gltf, binary, fields["POSITION"])
            smooth_face = "NORMAL" in fields and name.startswith(
                ("OBJ_Skin__face", "OBJ_Skin__head", "OBJ_Skin__ear", "OBJ_Skin__neck"))
            normals = accessor(gltf, binary, fields["NORMAL"]) if smooth_face else None
            if "skin" in node:
                pairs = [i for i in range(2) if f"JOINTS_{i}" in fields]
                joints = np.concatenate([accessor(gltf, binary, fields[f"JOINTS_{i}"]) for i in pairs], axis=1)
                weights = np.concatenate([accessor(gltf, binary, fields[f"WEIGHTS_{i}"]) for i in pairs], axis=1)
                ids = joints.astype(int)
                transforms = skins[node["skin"]]
                animated = skin_vertices(positions, ids, weights, transforms)
                if smooth_face:
                    normals = skin_vertices(normals, ids, weights, transforms, direction=True)
            else:
                animated = rigid_vertices(positions, world[:, node_index])
                if smooth_face:
                    normals = np.einsum("fij,vj->fvi", world[:, node_index, :3, :3], normals)
            faces = accessor(gltf, binary, part["indices"])[:, 0].astype(int)
            factor = material.get("pbrMetallicRoughness", {}).get("baseColorFactor", [1, 1, 1])[:3]
            uv = accessor(gltf, binary, fields["TEXCOORD_0"]) if "TEXCOORD_0" in fields else None
            color = accessor(gltf, binary, fields["COLOR_0"])[:, :3] if "COLOR_0" in fields else None
            appearance = material_for_name(name, materials or {}, Material(base=tuple(factor)))
            parts.append(Part(animated, faces, uv, color, name, appearance, normals=normals))
    return parts


def rigid_vertices(positions, transforms):
    points = np.column_stack((positions, np.ones(len(positions))))
    return np.einsum("fij,vj->fvi", transforms[..., :3, :], points)


def static_dd2(reader, path, materials=None):
    version = int(path.suffix[1:])
    data = path.read_bytes()
    header = reader._read_header(data, reader.serializer_rank(struct.unpack_from("<I", data, 4)[0]))
    stream = reader._read_buffer(data, header["mesh"], header["rank"])
    vertex_stream = next(row for row in stream["headers"] if row[0] == 0)
    uv_stream = next((row for row in stream["headers"] if row[:2] == (2, 4)), None)
    uv_table = None
    if uv_stream:
        uv_table = np.frombuffer(data, "<f2", count=stream["vert_count"] * 2,
                                 offset=stream["vert_off"] + uv_stream[2]).reshape(-1, 2).astype(float)
    for mesh in reader.read_mesh(data, version).submeshes:
        first = (mesh.pos_offset - stream["vert_off"] - vertex_stream[2]) // vertex_stream[1]
        uv = uv_table[first:first + len(mesh.positions)] if uv_table is not None else None
        appearance = (materials or {}).get(mesh.name, Material())
        yield Part(mesh.positions, mesh.indices.reshape(-1), uv, material=mesh.name, appearance=appearance)


def load_mesh_reader(path):
    spec = importlib.util.spec_from_file_location("local_dd2_mesh_reader", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def dd2_visible_parts(data, header, submesh_count, mask):
    if mask == -1:
        return [True] * submesh_count
    lod_table, = struct.unpack_from("<Q", data, header["lods"] + 56)
    lod, = struct.unpack_from("<Q", data, lod_table)
    group_table, = struct.unpack_from("<Q", data, lod + 8)
    groups = struct.unpack_from(f"<{data[lod]}Q", data, group_table)
    visible = [bool(mask & (1 << data[group])) for group in groups for _ in range(data[group + 1])]
    return visible if len(visible) == submesh_count and any(visible) else [True] * submesh_count


def read_dd2_parts(reader, paths, resolve_materials=None, masks=None):
    result = []
    for path in paths:
        data = Path(path).read_bytes()
        materials = resolve_materials(path) if resolve_materials else None
        header = reader._read_header(data, 13)
        if not header["bones"]:
            if not any(part in path.name for part in ("hair_", "beard_", "helm_")):
                raise ValueError(f"no skin data in {path.name}")
            rigid = []
            for piece in static_dd2(reader, path, materials):
                indices = np.zeros((len(piece.vertices), 1), dtype=int)
                rigid.append((piece, indices, np.ones_like(indices)))
            result.append(([{"id": 0, "name": "Head_0", "parent": None}], rigid))
            continue
        joints = read_bind_pose(data)["joints"]
        stream = reader._read_buffer(data, header["mesh"], 13)
        _, remap_count = struct.unpack_from("<II", data, header["bones"])
        palette = np.frombuffer(data, dtype="<u2", count=remap_count, offset=header["bones"] + 48)
        _, stride, offset = next(element for element in stream["headers"] if element[0] == 4)
        if stride != 16:
            raise ValueError("expected 16-byte DD2 skin records")
        count, base = stream["vert_count"], stream["vert_off"]
        records = np.frombuffer(data, dtype=np.uint8, count=count * stride, offset=base + offset).reshape(count, stride)
        indices = records[:, :8]
        weights = records[:, 8:].astype(float)
        weights[:, -1] = 255 - weights[:, :-1].sum(axis=1)
        if np.any(weights < 0) or np.any(indices[weights > 0] >= len(palette)):
            raise ValueError("mesh does not use the verified eight-influence DD2 skin layout")
        skin = palette[np.where(weights > 0, indices, 0)]
        submeshes = []
        meshes = reader.read_mesh(data, 260421070).submeshes
        _, position_stride, position_offset = next(element for element in stream["headers"] if element[0] == 0)
        visible = dd2_visible_parts(data, header, len(meshes), (masks or {}).get(path, -1))
        for mesh, part, enabled in zip(meshes, static_dd2(reader, path, materials), visible, strict=True):
            if not enabled:
                continue
            first = (mesh.pos_offset - base - position_offset) // position_stride
            vertices = slice(first, first + len(part.vertices))
            submeshes.append((part, skin[vertices], weights[vertices]))
        if not submeshes:
            raise ValueError(f"no LOD 0 geometry in {path}")
        result.append((joints, submeshes))
    return result


def dd2_mesh(parts, world, indices, joints):
    rest = np.array([joint["world_matrix"] for joint in joints]).reshape(-1, 4, 4).transpose(0, 2, 1)
    deltas = world @ np.linalg.inv(rest)
    result = []
    for part_joints, submeshes in parts:
        aliases = []
        by_id = {joint["id"]: joint for joint in part_joints}
        for joint in part_joints:
            while joint["name"] not in indices and joint["parent"] is not None:
                joint = by_id[joint["parent"]]
            aliases.append(indices.get(joint["name"], indices["root"]))
        part_transforms = deltas[:, aliases]
        for part, joint_ids, weights in submeshes:
            result.append(part.moved(skin_vertices(part.vertices, joint_ids, weights, part_transforms)))
    return result


def packed_mesh(parts):
    if not parts:
        raise ValueError("no supported character geometry")
    offsets = np.cumsum([0] + [part.vertices.shape[1] for part in parts[:-1]])
    vertices = np.concatenate([part.vertices for part in parts], axis=1)
    faces = np.concatenate([part.faces + offset for offset, part in zip(offsets, parts)])
    millimeters = np.rint(vertices * 1000)
    if not np.isfinite(millimeters).all() or np.max(np.abs(millimeters)) > 32767:
        raise ValueError("mesh position exceeds preview range")
    if faces.min() < 0 or faces.max() >= vertices.shape[1]:
        raise ValueError("mesh face references an invalid vertex")
    colors = []
    coordinates = []
    groups = []
    seen_images = {}
    first = 0
    for part in parts:
        count = part.vertices.shape[1]
        uv = part.uv if part.uv is not None else np.zeros((count, 2))
        colors.append(part.colors if part.colors is not None else np.ones((count, 3)))
        coordinates.append(uv)
        appearance = part.appearance
        maps = {name: seen_images.setdefault(uri, len(seen_images)) if uri and part.uv is not None else None
                for name, uri in (("image", appearance.image), ("normal", appearance.normal), ("surface", appearance.surface))}
        groups.append({"start": first, "count": len(part.faces), "name": part.material, "base": appearance.base,
                       "metallic": appearance.metallic, "roughness": appearance.roughness,
                       "visible": part.visible, **maps})
        first += len(part.faces)
    return {
        "vertices": vertices.shape[1],
        "poses": base64.b64encode(millimeters.astype("<i2").tobytes()).decode("ascii"),
        "faces": base64.b64encode(faces.astype("<u4").tobytes()).decode("ascii"),
        "uv": base64.b64encode(np.concatenate(coordinates).astype("<f4").tobytes()).decode("ascii"),
        "colors": base64.b64encode(np.rint(np.clip(np.concatenate(colors), 0, 1) * 255).astype("u1").tobytes()).decode("ascii"),
        "groups": groups,
        "images": list(seen_images),
        "smooth_normals": [
            {"offset": int(offset) * 3, "stride": part.vertices.shape[1] * 3, "data": base64.b64encode(
                np.rint(np.clip(part.normals / np.maximum(np.linalg.norm(part.normals, axis=2, keepdims=True), 1e-8),
                                -1, 1) * 127).astype("i1").tobytes()).decode("ascii")}
            for offset, part in zip(offsets, parts) if part.normals is not None
        ],
    }
