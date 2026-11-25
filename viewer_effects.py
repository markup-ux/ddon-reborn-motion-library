"""Decode the installed games' supported sprite sequences for the offline viewer."""

from functools import lru_cache
from pathlib import PurePosixPath
import re
import struct
import subprocess

from PIL import Image, ImageChops

from viewer_materials import ddon_texture_image, image_uri


EFFECTS = {
    "ddon-skyward": ("DDON", "Skyward Lash · sprite", "job01_cs03.arc", "t_cm01_02"),
    "ddon-healing": ("DDON", "Healing Spot · sprite", "job04_cs03.arc", "t_cm09_00"),
    "ddon-common": ("DDON", "Common sprite · t_cm02_03", "effect_common.arc", "t_cm02_03"),
    "dd2-healing-dust": ("DD2", "Healing · dust layer", "13_ch00_heal_001_00", "dust/13_dust_010_00"),
    "dd2-healing-liquid": ("DD2", "Healing · liquid layer", "13_ch00_heal_001_00", "liquid/13_liquid_010_10"),
}
DEFAULT_MOTION_EFFECTS = {
    "job01_cs03.arc": ("ddon-skyward", "hand"),
    "job04_cs03.arc": ("ddon-healing", "body"),
}
REX_ARCHIVE = r"job09_cs05(?:_ex0[12])?[.]arc"
REX_ELEMENTS = ("Fire", "Ice", "Thunder", "Holy", "Dark")
REX_ELEMENT_TINTS = ((255, 132, 39), (83, 209, 255), (163, 113, 255), (255, 214, 45), (63, 63, 254))
REX_SPIKES = "m_cm08_00_05"
REX_MESHES = ("m_cm08_00_11", "m_cm08_00_30")
REX_BURST_ATLASES = ("t_cm02_01", "t_cm05_03", "t_cm03_00", "t_cm09_00", "t_cm01_03")
SPRITE_FPS = 30
DD2_VFX = "natives/stm/vfx/"


def effect_entry(key, game, label, package, resource):
    return {"id": key, "game": game, "name": label, "source": f"{package} / {resource}"}


def rex_list(archive, index):
    return f"l_pl0009_{archive.removeprefix('job09_').removesuffix('.arc')}_{index:02}"


def effect_catalog():
    return [effect_entry(key, *info) for key, info in EFFECTS.items()]


DDON_JOB_ARCHIVE = r"job(\d{2})(_cs\d{2})?(?:_[a-z0-9]+)*[.]arc"
DDON_ATLAS = rb"effect[\\/]tex[\\/]cm[\\/](t_cm\d{2}_\d{2})_bm"
SPRITE_ERRORS = (OSError, ValueError, KeyError, IndexError, RuntimeError, struct.error, subprocess.SubprocessError)


@lru_cache(maxsize=128)
def motion_effects(library, game, archive):
    if game != "DDON" or not (job := re.fullmatch(DDON_JOB_ARCHIVE, archive)):
        return []
    preferred = DEFAULT_MOTION_EFFECTS.get(archive)
    entries = []
    if re.fullmatch(REX_ARCHIVE, archive):
        return [effect_entry(f"ddon-rex:{index}", game, f"Rex Elementa · {element}", archive,
                             rex_list(archive, index) + ".efl")
                for index, element in enumerate(REX_ELEMENTS)]
    if preferred:
        key, anchor = preferred
        entries.append({**effect_entry(key, *EFFECTS[key]), "anchor": anchor})
    try:
        # Prefer the skill's own lists over files it shares with other motions.
        lists = library.ddon_members(archive, "efl")
        prefix = f"l_pl00{job[1]}{job[2] or ''}_"
        named = [row for row in lists if PurePosixPath(row["name"]).name.casefold().startswith(prefix)]
        candidates = dict.fromkeys(
            match.decode("ascii") for row in named or lists
            for match in re.findall(DDON_ATLAS, row["path"].read_bytes().lower())
        )
        animations = {PurePosixPath(row["name"]).stem.casefold(): row["path"]
                      for row in library.ddon_members(archive, "ean")
                      if row["name"].casefold().startswith("effect/tex/cm/")}
        atlases = {PurePosixPath(row["name"]).stem.casefold() for row in library.ddon_members("ddo_common.arc", "tex")
                   if row["name"].casefold().startswith("effect/tex/cm/")}
        for resource in candidates:
            if resource not in animations or resource + "_bm" not in atlases:
                continue
            if preferred and resource == EFFECTS[preferred[0]][3]:
                continue
            try:
                if len(ddon_cells(animations[resource].read_bytes())) < 3:
                    continue
            except (ValueError, OSError, struct.error):
                continue
            entry = effect_entry(f"ddon-archive:{archive}:{resource}", game, f"{resource} · archive sprite", archive, resource)
            entries.append({**entry, "anchor": "hand"})
    except SPRITE_ERRORS:
        # Many motion archives contain no supported effect resources.
        pass
    return entries


def rex_mesh_tracks(data, offset):
    # These two mesh types use the same bounded fixed-size optional record layout.
    end, = struct.unpack_from("<H", data, offset + 228)
    if end not in (320, 368, 416):
        raise ValueError("Unknown Rex Elementa mesh animation layout.")
    tracks = {}
    for at in range(offset + 240, offset + 240 + end - 320, 48):
        if data[at:at + 4] not in (b"\x03\x02\x00H", b"\x03\x02\x00@"):
            continue
        keys = [struct.unpack_from("<I4s4s", data, at + 4 + i * 12) for i in range(3)]
        ticks = [tick for tick, _, _ in keys]
        if ticks[0] != 0 or not 0 < ticks[1] < ticks[2] <= 240:
            raise ValueError("Invalid Rex Elementa mesh key times.")
        if all(a == b and a[3] > 0 for _, a, b in keys):
            tracks["color"] = [[tick, *rgba] for tick, rgba, _ in keys]
        elif all(tangent == b"\0" * 4 for _, _, tangent in keys):
            values = [struct.unpack("<f", value)[0] for _, value, _ in keys]
            if all(0.01 <= value <= 8 for value in values):
                tracks["scale"] = list(zip(ticks, values))
    tracks["spin"], = struct.unpack_from("<f", data, offset + 160)
    return tracks


def rex_cast_sprite(library, archive, tint):
    resource = "t_cm01_02"
    for row in library.ddon_members(archive, "efl"):
        if PurePosixPath(row["name"]).stem == "l_pl0009_45":
            references = re.findall(DDON_ATLAS, row["path"].read_bytes().lower())
            if resource.encode("ascii") in references:
                return ddon_atlas(library, archive, resource, tint)
    raise ValueError("The Rex Elementa casting sprite is missing.")


def rex_effect_assets(library, archive, key):
    if not re.fullmatch(REX_ARCHIVE, archive) or not re.fullmatch(r"ddon-rex:[0-4]", key):
        raise ValueError("Select a Rex Elementa animation to use this orb.")
    element = int(key[-1])
    lists = {PurePosixPath(row["name"]).stem: row["path"] for row in library.ddon_members(archive, "efl")}
    parent = rex_list(archive, element)
    child = f"l_pl0009_cs05_{element + 5:02}"
    parent_data = lists[parent].read_bytes()
    child_data = lists[child].read_bytes()
    if child.encode("ascii") not in parent_data or child_data[:8] != b"EFL\0\x06\x03\x12\x20":
        raise ValueError("Rex Elementa linked effect is unavailable.")
    models = {PurePosixPath(row["name"]).stem: row["path"] for row in library.ddon_members(archive, "mod")}
    result = []
    for name in REX_MESHES:
        reference = f"effect\\mod\\cm\\m_cm08_00\\{name}".encode("ascii")
        offset = child_data.index(reference)
        rgba = child_data[offset - 4:offset]
        if len(rgba) != 4 or child_data[offset - 8:offset - 4] != rgba:
            raise ValueError("Invalid Rex Elementa mesh color record.")
        result.append((models[name], tuple(rgba), rex_mesh_tracks(child_data, offset)))
    if element < 3:  # Fire, Ice and Thunder are confirmed in the gameplay reference.
        reference = f"effect\\mod\\cm\\m_cm08_00\\{REX_SPIKES}".encode("ascii")
        offset = parent_data.index(reference)
        rgba = parent_data[offset - 4:offset]
        if len(rgba) != 4 or parent_data[offset - 8:offset - 4] != rgba:
            raise ValueError("Invalid Rex Elementa spike color record.")
        result.append((models[REX_SPIKES], tuple(rgba), {"spin": 0.004}))
    resource = REX_BURST_ATLASES[element]
    referenced = {path.decode("ascii") for path in re.findall(DDON_ATLAS, parent_data.lower())}
    burst = {}
    if resource in referenced:
        try:
            burst = ddon_atlas(library, archive, resource)
        except SPRITE_ERRORS:
            pass
    return f"Rex Elementa · {REX_ELEMENTS[element]}", result, burst


def ddon_cells(data):
    magic, version, size, tracks = struct.unpack_from("<4sIII", data)
    if (magic, version, size) != (b"EAN\0", 0x20120224, len(data) - 16) or tracks < 1:
        raise ValueError("Unsupported DDON effect animation.")
    if 16 + 32 * tracks > len(data):
        raise ValueError("Invalid DDON effect track table.")
    descriptors = (struct.unpack_from("<IH", data, 16 + 32 * index) for index in range(tracks))
    offset, count = max(descriptors, key=lambda descriptor: descriptor[1])
    if not count or offset < 32 * tracks or 16 + offset + count * 24 > len(data):
        raise ValueError("Invalid DDON effect frame table.")
    return [struct.unpack_from("<4f", data, 24 + offset + i * 24) for i in range(count)]


def dd2_cells(data):
    magic, version, tracks, count = struct.unpack_from("<4sIII", data)
    table, end = struct.unpack_from("<QQ", data, 40)
    if (magic, version, tracks) != (b".SVU", 1, 1) or not count or not 56 <= table < end <= len(data):
        raise ValueError("Unsupported DD2 effect UV sequence.")
    if end - table != count * 96:
        raise ValueError("Invalid DD2 effect frame table.")
    return [struct.unpack_from("<4f", data, table + i * 96 + 8) for i in range(count)]


def sprite_atlas(image, cells):
    if not all(0 <= x < right <= 1 and 0 <= y < bottom <= 1 for x, y, right, bottom in cells):
        raise ValueError("Effect sprite cell is outside its atlas.")
    return {"image": image_uri(image, preserve_alpha=True), "cells": cells,
            "width": image.width, "height": image.height, "fps": SPRITE_FPS}


@lru_cache(maxsize=64)
def ddon_atlas(library, archive, resource, tint=None):
    def member(package, extension, name):
        row = next((row for row in library.ddon_members(package, extension)
                    if row["name"].casefold() == f"effect/tex/cm/{name}.{extension}"), None)
        if row is None:
            raise ValueError(f"Effect resource {name} is missing from {package}.")
        return row["path"].read_bytes()

    cells = ddon_cells(member(archive, "ean", resource))
    texture = member("ddo_common.arc", "tex", resource + "_bm")
    image = ddon_texture_image(texture, texture[13])
    if tint is not None:
        image = ImageChops.multiply(image.convert("RGBA"), Image.new("RGBA", image.size, (*tint, 255)))
    return sprite_atlas(image, cells)


@lru_cache(maxsize=64)
def load_effect(library, key):
    if key in EFFECTS:
        game, label, package, resource = EFFECTS[key]
    else:
        category, _, ref = key.partition(":")
        package, _, resource = ref.partition(":")
        if category != "ddon-archive" or not any(item["id"] == key for item in motion_effects(library, "DDON", package)):
            raise ValueError("Unknown effect.")
        game, label = "DDON", f"{resource} · archive sprite"
    if game == "DDON":
        return {**ddon_atlas(library, package, resource), "name": f"{game} · {label}",
                "note": "Native sprite frames with preview timing and placement."}
    else:
        atlas = resource.split("/")[-1]
        uvs = f"vfx/texture/uvsequence/{atlas}.uvs"
        efx = library.dd2_file(f"{DD2_VFX}effects/character/ch00/cm/{package}.efx.5965310").read_bytes()
        if efx[:4] != b"efxr" or uvs.encode("utf-16le") not in efx.lower():
            raise ValueError("DD2 effect does not reference the expected UV sequence.")
        cells = dd2_cells(library.dd2_file(f"natives/stm/{uvs}.8").read_bytes())
        is_liquid = resource.startswith("liquid/")
        suffix = "_msk4" if is_liquid else ""
        texture = library.materials.dd2_texture(f"vfx/texture/{resource}{suffix}.tex")
        if texture is None:
            raise ValueError("Cannot decode the DD2 effect atlas.")
        image = Image.new("RGBA", texture.size, (161, 235, 210))
        image.putalpha(texture.getchannel("R" if is_liquid else "A"))
    return {**sprite_atlas(image, cells), "name": f"{game} · {label}",
            "note": "Native sprite frames with preview tint, timing and placement."}
