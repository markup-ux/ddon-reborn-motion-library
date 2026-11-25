"""Dragon's Dogma 2 .mdf2 reader and in-place color writer.

The on-disk version lives in the file name (.mdf2.51 on the current game).
The header short after the magic is a separate field and is usually 1.
Color parameters are patched where they already sit, so the rest of the
material file is left untouched.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field


def _wstr(data: bytes, off: int) -> str:
    if off <= 0 or off >= len(data) - 1:
        return ""
    chars = []
    while off + 1 < len(data):
        c = struct.unpack_from("<H", data, off)[0]
        off += 2
        if c == 0:
            break
        chars.append(chr(c))
    return "".join(chars)


def _offset_wstr(data: bytes, pos: int) -> tuple[str, int]:
    off = struct.unpack_from("<Q", data, pos)[0]
    return _wstr(data, off), pos + 8


@dataclass
class Param:
    name: str
    values: list[float]
    offset: int  # absolute file offset of the first float

    @property
    def is_color(self) -> bool:
        n = self.name.lower()
        if len(self.values) < 3:
            return False
        keys = ("color", "colour", "albedo", "emissive", "tint", "basecolor")
        return any(k in n for k in keys)


@dataclass
class Material:
    name: str
    shader: str
    params: list[Param] = field(default_factory=list)
    textures: dict[str, str] = field(default_factory=dict)

    def color_param(self) -> Param | None:
        for want in ("BaseColor", "EmissiveColor1", "ExtraPatternMap_Color"):
            for p in self.params:
                if p.name == want and len(p.values) >= 3:
                    return p
        for p in self.params:
            if p.is_color:
                return p
        return None


_SHADERS = {
    0: "Standard", 1: "Decal", 4: "Transparent", 5: "Distortion",
    8: "Water", 9: "SpeedTree", 14: "Forward",
}


def _read_header(data: bytes, pos: int, version: int) -> tuple[dict, int]:
    name, pos = _offset_wstr(data, pos)
    name_hash, params_size, param_count = struct.unpack_from("<III", data, pos)
    pos += 12
    onimusha = False
    if version == 51:
        peek = struct.unpack_from("<I", data, pos + 36)[0]
        onimusha = peek > 1
        if onimusha:
            pos += 4
    tex_count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    gpbf_n = gpbf_d = 0
    if version >= 19:
        gpbf_n, gpbf_d = struct.unpack_from("<II", data, pos)
        pos += 8
    shader = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    if version >= 31:
        pos += 4  # ukn
    pos += 4  # alpha flags
    if version >= 31:
        pos += 8  # ukn1
    if version >= 51 and not onimusha:
        pos += 8
    param_off, tex_off = struct.unpack_from("<QQ", data, pos)
    pos += 16
    if version >= 19:
        pos += 8  # gpbf offset
    params_off = struct.unpack_from("<Q", data, pos)[0]
    pos += 8
    mmtr, pos = _offset_wstr(data, pos)
    if version >= 31:
        pos += 8
    return {
        "name": name,
        "param_count": param_count,
        "tex_count": tex_count,
        "shader": _SHADERS.get(shader, str(shader)),
        "mmtr": mmtr,
        "param_header": param_off,
        "tex_header": tex_off,
        "params": params_off,
    }, pos


def read_mdf(data: bytes, file_version: int = 51) -> list[Material]:
    magic, internal, count = struct.unpack_from("<IHH", data, 0)
    if magic != 0x46444D:
        raise ValueError("not an mdf2 file")
    pos = 16
    headers = []
    for _ in range(count):
        header, pos = _read_header(data, pos, file_version)
        headers.append(header)
    materials = []
    for header in headers:
        textures = {}
        tpos = header["tex_header"]
        for _ in range(header["tex_count"]):
            kind, tpos = _offset_wstr(data, tpos)
            tpos += 8  # two hashes
            path, tpos = _offset_wstr(data, tpos)
            if file_version >= 13:
                tpos += 8
            textures[kind] = path
        params = []
        ppos = header["param_header"]
        for _ in range(header["param_count"]):
            name_off = struct.unpack_from("<Q", data, ppos)[0]
            ppos += 8 + 8  # name offset + two hashes
            if file_version >= 13:
                rel, comp, _extra = struct.unpack_from("<Ihh", data, ppos)
                ppos += 8
            else:
                comp = struct.unpack_from("<h", data, ppos)[0]
                rel = struct.unpack_from("<I", data, ppos + 4)[0]
                ppos += 8
            comp = max(0, min(comp, 16))
            abs_off = header["params"] + rel
            values = list(struct.unpack_from("<" + "f" * comp, data, abs_off)) if comp else []
            params.append(Param(_wstr(data, name_off), values, abs_off))
        materials.append(Material(header["name"], header["shader"], params, textures))
    return materials


def patch_params(data: bytearray, materials: list[Material]) -> bytearray:
    for mat in materials:
        for param in mat.params:
            for i, value in enumerate(param.values):
                struct.pack_into("<f", data, param.offset + i * 4, float(value))
    return data
