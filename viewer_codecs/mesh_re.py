"""Dragon's Dogma 2 mesh reader for the current (DD2 v3) file layout.

The field order matches RE-Engine-Lib's MeshFile for serializer version DD2_V3
(internal version 251205828, path suffix .mesh.260421070). Older DD2 meshes
(.mesh.231011879 and .mesh.240423143) use the earlier header and are read too.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

MAGIC = 0x4853454D  # MESH
DD2_V3 = 251205828
DD2_V1 = 230517984

# Serializer ranks. DD2 v3 is past the Wilds/Pragmata header additions.
_RE4 = 5
_DD2_V2 = 8
_ONI2 = 9
_WILDS = 10
_PRAGMATA = 11


def serializer_rank(internal: int) -> int:
    if internal == DD2_V3:
        return 13
    if internal == DD2_V1:
        # File suffix distinguishes v1 (231011879) from v2 (240423143). The
        # caller passes the refined rank when it knows the suffix.
        return 7
    if internal >= 250000000:
        return 13
    if internal >= 240704828:
        return _WILDS
    return _RE4


@dataclass
class Submesh:
    name: str
    material_index: int
    positions: np.ndarray
    normals: np.ndarray
    indices: np.ndarray
    pos_offset: int
    normal_offset: int


@dataclass
class Mesh:
    version: int
    rank: int
    submeshes: list[Submesh] = field(default_factory=list)
    material_names: list[str] = field(default_factory=list)
    bone_names: list[str] = field(default_factory=list)


def _ascii_at(data: bytes, off: int) -> str:
    if off <= 0 or off >= len(data):
        return ""
    end = data.find(b"\0", off)
    if end < 0:
        end = min(off + 128, len(data))
    return data[off:end].decode("ascii", "replace")


def _read_header(data: bytes, rank: int) -> dict:
    if struct.unpack_from("<I", data, 0)[0] != MAGIC:
        raise ValueError("not an RE Engine mesh")
    version, file_size, lod_hash = struct.unpack_from("<III", data, 4)
    o = 16
    h = {"version": version, "file_size": file_size, "lod_hash": lod_hash, "rank": rank}
    if rank < _ONI2:
        flags, ukn_count, name_count, ukn1 = struct.unpack_from("<HhhH", data, o)
        o += 8
        keys = [
            "ukn", "lods", "shadow", "occluder", "normal_recalc", "blend", "mesh",
            "sf6_1", "floats", "bounds", "bones", "mat_index", "bone_index", "blend_index",
        ]
        vals = struct.unpack_from("<" + "Q" * len(keys), data, o)
        o += 8 * len(keys)
        h.update(dict(zip(keys, vals)))
        h["flags"] = flags
        h["name_count"] = name_count
        if rank < 7:  # before DD2 v1
            h["streaming"], h["names"] = struct.unpack_from("<QQ", data, o)
            o += 16
            h["hash"] = 0
        else:
            h["names"], h["hash"], h["streaming"] = struct.unpack_from("<QQQ", data, o)
            o += 24
        h["vertices"], h["sdf"] = struct.unpack_from("<QQ", data, o)
    else:
        wild1, name_count, flags, ukn_count = struct.unpack_from("<IhHH", data, o)
        o += 10
        if rank >= 13:
            h["buffer_count"] = struct.unpack_from("<H", data, o)[0]
            o += 2
        else:
            o += 2
        o += 12  # three ints
        keys = [
            "vertices", "lods", "shadow", "occluder", "normal_recalc", "blend", "mesh",
            "sf6_1", "floats", "bounds", "bones", "mat_index", "bone_index", "blend_index",
            "names", "streaming", "sf6_4",
        ]
        vals = struct.unpack_from("<" + "Q" * len(keys), data, o)
        h.update(dict(zip(keys, vals)))
        h["flags"] = flags
        h["name_count"] = name_count
        h["hash"] = 0
        h["sdf"] = 0
    return h


def _buffer_header_size(rank: int) -> int:
    # longs: element off, vertex off, shapekey off; ints: total, vertex size
    size = 8 + 8 + 8 + 4 + 4 + 2 + 2
    if rank >= _PRAGMATA:
        size += 16
    size += 4 + 4 + 4  # shadow, occ, blend
    if rank >= _WILDS:
        size += 16  # blend2, blend3, shapekey size, buffer index
    else:
        size += 4 + 4 + 8
    return size


def _read_buffer(data: bytes, at: int, rank: int):
    o = at
    elem_off, vert_off, shape_off = struct.unpack_from("<QQQ", data, o)
    o += 24
    total_size, vert_size, elem_count, total_elems = struct.unpack_from("<IIhh", data, o)
    o += 12
    if rank >= _PRAGMATA:
        o += 16
    shadow_off, occ_off, blend_off = struct.unpack_from("<III", data, o)
    headers = []
    e = elem_off
    for _ in range(elem_count):
        typ, size, off = struct.unpack_from("<hhI", data, e)
        headers.append((typ, size, off))
        e += 8
    extra = []
    for _ in range(max(0, total_elems - elem_count)):
        typ, size, off = struct.unpack_from("<hhI", data, e)
        extra.append((typ, size, off))
        e += 8
    if len(headers) >= 2:
        vert_count = (headers[1][2] - headers[0][2]) // headers[0][1]
    elif headers:
        vert_count = (vert_size - headers[0][2]) // headers[0][1]
    else:
        vert_count = 0
    return {
        "elem_off": elem_off,
        "vert_off": vert_off,
        "vert_size": vert_size,
        "total_size": total_size,
        "shadow_off": shadow_off,
        "headers": headers,
        "extra": extra,
        "vert_count": vert_count,
    }


def _positions(data: bytes, buf: dict):
    for typ, size, off in buf["headers"]:
        if typ == 0 and size >= 12:
            start = buf["vert_off"] + off
            count = buf["vert_count"]
            arr = np.frombuffer(data, dtype="<f4", count=count * 3, offset=start).reshape(count, 3).copy()
            return arr, start, size
    return None, 0, 12


def _normals(data: bytes, buf: dict):
    for typ, size, off in buf["headers"]:
        if typ == 1 and size >= 4:
            start = buf["vert_off"] + off
            count = buf["vert_count"]
            raw = np.frombuffer(data, dtype=np.int8, count=count * size, offset=start).reshape(count, size)
            n = ((raw[:, :3].astype(np.float32) * 2.0) + 1.0) / 255.0
            length = np.linalg.norm(n, axis=1, keepdims=True)
            length[length < 1e-8] = 1.0
            return (n / length).astype(np.float32), start, size
    return None, 0, 8


def _submesh_fmt(rank: int) -> str:
    # material u16, buffer u8, ukn u8, [ukn1 u32 if oni+], indices, face off, vert off,
    # streaming u32, streaming2 u32, [ukn2 u32 if dd2v2+]
    if rank >= 13 or rank >= _ONI2:
        return "<HBBIIIII II" if False else "<HBBIIIIIII"
    if rank >= _DD2_V2:
        return "<HBBIIIIII"
    return "<HBBIIIII"


def _parse_groups(data: bytes, lod_off: int, rank: int, positions, normals, pos_base, pos_stride, nrm_base, nrm_stride, faces, integer_faces):
    mesh_count, _vf, _u, lod_factor = struct.unpack_from("<BBBx f", data, lod_off)
    # layout: u8 count, u8 format, u8 ukn, u8 pad, f32 factor, u64 header
    mesh_count = data[lod_off]
    header_off = struct.unpack_from("<Q", data, lod_off + 8)[0]
    group_offs = struct.unpack_from("<" + "Q" * mesh_count, data, header_off)
    submeshes = []
    fmt = _submesh_fmt(rank)
    step = struct.calcsize(fmt)
    for goff in group_offs:
        _gid = data[goff]
        sub_count = data[goff + 1]
        vertex_count, index_count = struct.unpack_from("<II", data, goff + 8)
        so = goff + 16
        raw_subs = []
        for _ in range(sub_count):
            fields = struct.unpack_from(fmt, data, so)
            so += step
            # fields: mat, buffer, uknB, ukn1, indices, face_off, vert_off, stream, stream2, ukn2
            mat = fields[0]
            indices_count = fields[4]
            face_off = fields[5]
            vert_off = fields[6]
            raw_subs.append((mat, indices_count, face_off, vert_off))
        for i, (mat, indices_count, face_off, vert_off) in enumerate(raw_subs):
            if i + 1 < len(raw_subs):
                vcount = raw_subs[i + 1][3] - vert_off
            else:
                vcount = vertex_count - (vert_off - raw_subs[0][3])
            if vcount <= 0 or vert_off < 0 or vert_off + vcount > len(positions):
                continue
            if integer_faces:
                idx = faces[face_off:face_off + indices_count]
            else:
                idx = faces[face_off:face_off + indices_count]
            if len(idx) < 3:
                continue
            idx = idx[:len(idx) - (len(idx) % 3)].reshape(-1, 3).astype(np.uint32)
            # Indices are into the whole vertex buffer.
            if idx.max(initial=0) >= vert_off and idx.min(initial=0) >= vert_off - 1:
                idx = idx - np.uint32(vert_off)
            idx = np.clip(idx, 0, vcount - 1)
            pos = positions[vert_off:vert_off + vcount]
            if normals is not None and vert_off + vcount <= len(normals):
                nrm = normals[vert_off:vert_off + vcount]
                n_off = nrm_base + vert_off * nrm_stride
            else:
                nrm = np.tile(np.array([0, 1, 0], np.float32), (vcount, 1))
                n_off = 0
            submeshes.append(Submesh(
                name="",
                material_index=mat,
                positions=np.ascontiguousarray(pos, dtype=np.float32),
                normals=np.ascontiguousarray(nrm, dtype=np.float32),
                indices=np.ascontiguousarray(idx, dtype=np.uint32),
                pos_offset=pos_base + vert_off * pos_stride,
                normal_offset=n_off,
            ))
    return submeshes


def read_mesh(data: bytes, file_version: int = 0) -> Mesh:
    internal = struct.unpack_from("<I", data, 4)[0]
    if file_version == 240423143:
        rank = _DD2_V2
    elif file_version == 231011879:
        rank = 7
    else:
        rank = serializer_rank(internal)
        if internal == DD2_V1 and file_version == 0:
            rank = _DD2_V2
    header = _read_header(data, rank)
    if header["mesh"] <= 0 or header["lods"] <= 0:
        raise ValueError("mesh is missing its geometry directory")
    buf = _read_buffer(data, header["mesh"], rank)
    positions, pos_base, pos_stride = _positions(data, buf)
    if positions is None:
        raise ValueError("mesh has no position buffer")
    normals, nrm_base, nrm_stride = _normals(data, buf)

    lod_at = header["lods"]
    lod_count = data[lod_at]
    material_count = data[lod_at + 1]
    total_mesh = struct.unpack_from("<H", data, lod_at + 4)[0]
    integer_faces = data[lod_at + 6] != 0
    # sphere (4 floats) + aabb (8 floats) then the lod-offset pointer
    lod_table = struct.unpack_from("<Q", data, lod_at + 8 + 16 + 32)[0]
    lod0 = struct.unpack_from("<Q", data, lod_table)[0]

    face_at = buf["vert_off"] + buf["vert_size"]
    if buf["shadow_off"] > buf["vert_size"]:
        face_bytes = buf["shadow_off"] - buf["vert_size"]
    else:
        face_bytes = buf["total_size"] - buf["vert_size"]
    face_bytes = max(0, min(face_bytes, len(data) - face_at))
    if integer_faces:
        faces = np.frombuffer(data, dtype="<i4", count=face_bytes // 4, offset=face_at)
    else:
        faces = np.frombuffer(data, dtype="<u2", count=face_bytes // 2, offset=face_at)

    subs = _parse_groups(
        data, lod0, rank, positions, normals, pos_base, pos_stride, nrm_base, nrm_stride, faces, integer_faces)

    names = []
    if header["names"] and header["name_count"]:
        offs = struct.unpack_from("<" + "Q" * header["name_count"], data, header["names"])
        names = [_ascii_at(data, off) for off in offs]
    mat_names = []
    if header["mat_index"] and material_count:
        idxs = struct.unpack_from("<" + "H" * material_count, data, header["mat_index"])
        for i in idxs:
            mat_names.append(names[i] if 0 <= i < len(names) else f"material_{i}")
    for sub in subs:
        if 0 <= sub.material_index < len(mat_names):
            sub.name = mat_names[sub.material_index]
        else:
            sub.name = f"material_{sub.material_index}"
    bones = []
    if header.get("bones"):
        b_at = header["bones"]
        bone_count, bone_map = struct.unpack_from("<II", data, b_at)
        hierarchy = struct.unpack_from("<Q", data, b_at + 16)[0]
        if header.get("bone_index") and names:
            bi = header["bone_index"]
            for i in range(min(bone_count, 1024)):
                idx = struct.unpack_from("<H", data, bi + i * 2)[0]
                bones.append(names[idx] if idx < len(names) else str(idx))
        else:
            bones = [f"bone_{i}" for i in range(bone_count)]
        _ = hierarchy
    return Mesh(version=header["version"], rank=rank, submeshes=subs, material_names=mat_names, bone_names=bones)


def bake_transform(data: bytearray, sub: Submesh, matrix: np.ndarray) -> None:
    """Write a 4x4 transform back into this submesh's position (and normal) bytes."""
    count = len(sub.positions)
    pos = np.concatenate([sub.positions, np.ones((count, 1), np.float32)], axis=1)
    baked = (matrix @ pos.T).T[:, :3].astype(np.float32)
    raw = baked.astype("<f4").tobytes()
    start = sub.pos_offset
    data[start:start + len(raw)] = raw
    if sub.normal_offset <= 0:
        return
    linear = matrix[:3, :3].astype(np.float32)
    try:
        normal_m = np.linalg.inv(linear).T
    except np.linalg.LinAlgError:
        normal_m = linear
    nrm = (normal_m @ sub.normals.T).T
    length = np.linalg.norm(nrm, axis=1, keepdims=True)
    length[length < 1e-8] = 1.0
    nrm = nrm / length
    quant = np.clip(np.rint((nrm * 255.0 - 1.0) * 0.5), -128, 127).astype(np.int8)
    stride = 8
    for i in range(count):
        at = sub.normal_offset + i * stride
        data[at:at + 3] = quant[i].tobytes()
