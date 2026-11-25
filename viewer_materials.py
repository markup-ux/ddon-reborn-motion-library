"""Resolve native material textures for the offline viewer."""

import base64
from dataclasses import dataclass, replace
from functools import cached_property, lru_cache
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import zlib

import numpy as np
from PIL import Image, ImageOps


def load_module(name, path):
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def image_uri(image, preserve_alpha=False, max_size=512):
    mode = "RGBA" if preserve_alpha else "RGB"
    image = image.convert(mode)
    image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    output = BytesIO()
    if preserve_alpha and image.getchannel("A").getextrema()[0] != 255:
        image.save(output, format="PNG")
        return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    image = image.convert("RGB")
    image.save(output, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


@dataclass(frozen=True)
class Material:
    base: tuple = (1, 1, 1)
    image: str = ""
    normal: str = ""
    surface: str = ""
    metallic: float = 1
    roughness: float = 1
    source: str = ""
    color_texture: str = ""


def data_uri(image):
    output = BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def ddon_texture_image(data, kind):
    if len(data) < 24 or data[:4] != b"TEX\0" or data[4] != 0x9D or data[13] != kind:
        raise ValueError("unsupported DDON texture")
    bcn, block_size = {0x13: (1, 8), 0x14: (1, 8), 0x18: (3, 16), 0x1F: (3, 16)}[kind]
    dims, = struct.unpack_from("<I", data, 8)
    width, height = (dims >> 6) & 0x1FFF, dims >> 19
    start, end = struct.unpack_from("<II", data, 16)
    size = ((width + 3) // 4) * ((height + 3) // 4) * block_size
    if not (width and height and 24 <= start < end <= len(data) and end - start == size):
        raise ValueError("invalid DDON texture dimensions")
    return Image.frombytes("RGBA", (width, height), data[start:end], "bcn", bcn)


def dd2_alpha_cutoffs(data, reader, rows):
    offset = 16
    for row in rows:
        entry, next_offset = reader._read_header(data, offset, 51)
        if entry["name"] != row.name:
            raise ValueError("MDF material order differs from header order")
        # MDF 51 can have an extra word before the texture count.
        flags_offset = offset + 40 + (4 if struct.unpack_from("<I", data, offset + 56)[0] > 1 else 0)
        flags, = struct.unpack_from("<I", data, flags_offset)
        params = {param.name: param.values[0] for param in row.params if len(param.values) == 1}
        alpha_used = flags & ((1 << 1) | (1 << 7) | (1 << 24) | (1 << 25))
        yield params.get("AlphaTestRef") if alpha_used and not params.get("Ignore_AlphaMap") else None
        offset = next_offset


def ddon_palette(data, color_index):
    if len(data) < 80 or struct.unpack_from("<4sI", data) != (b"DMT\0", 17):
        raise ValueError("unsupported DDON montage")
    kind, = struct.unpack_from("<I", data, 8)
    variants, per_variant = struct.unpack_from("<II", data, 24)
    offset, = struct.unpack_from("<I", data, 48)
    if kind == 9 or not variants or not per_variant:
        return {}
    if offset < 80 or offset + 96 * variants * per_variant > len(data):
        raise ValueError("invalid DDON montage color table")
    start = offset + color_index % variants * per_variant * 96
    result = {}
    for entry in range(start, start + per_variant * 96, 96):
        material, = struct.unpack_from("<I", data, entry + 80)
        rows = struct.unpack_from("<20f", data, entry)
        if all(np.isfinite(rows)) and any(rows):
            result[material] = (rows[8:11], rows[12:15], rows[16:19])
    return result


def ddon_apply_palette(albedo, mask, rate, threshold, colors):
    mask = mask.convert("RGB").resize(albedo.size, Image.Resampling.BILINEAR)
    weights = (1 - np.asarray(mask, dtype=np.float32) / 255) * rate
    red, green, blue = np.moveaxis(weights, -1, 0)
    green_weight = np.where(red <= threshold[0], green, 0)
    blue_weight = np.where((red <= threshold[1]) & (green <= threshold[2]), blue, 0)
    factors = [1 + weight[..., None] * (np.asarray(color) - 1)
               for weight, color in zip((red, green_weight, blue_weight), colors)]
    pixels = np.array(albedo.convert("RGBA"))
    # DDON squares its base color map before multiplying the color-mask terms.
    tint = np.sqrt(np.maximum(factors[0] * factors[1] * factors[2], 0))
    pixels[..., :3] = np.rint(np.clip(pixels[..., :3] * tint, 0, 255)).astype(np.uint8)
    return Image.fromarray(pixels)


def mrl_bindings(data):
    magic, version, materials, textures, _, texture_at, material_at = struct.unpack_from("<4s6I", data)
    if (magic, version) != (b"MRL\0", 34) or texture_at + textures * 76 > material_at:
        raise ValueError("unsupported MRL material layout")
    table = [data[texture_at + i * 76 + 12:texture_at + (i + 1) * 76].split(b"\0", 1)[0].decode("ascii")
             for i in range(textures)]
    bindings = {}
    for i in range(materials):
        row = struct.unpack_from("<15I", data, material_at + 60 * i)
        start, length = row[13], (row[6] & 0xFFF) * 12
        if start < material_at + materials * 60 or start + length > len(data):
            continue
        slots = {}
        mask, constants = None, None
        for command, value, object_id in struct.iter_unpack("<3I", data[start:start + length]):
            kind, slot = command & 0xF, object_id >> 12
            if kind == 3 and 1 <= value <= len(table):
                path = table[value - 1].replace("\\", "/").casefold() + ".tex"
                if command in (0x36DDCDC3, 0x36EDCDC3, 0x370DCDC3):
                    slots[command] = path
                if slot == 0x383E5 and path.endswith("_cmm.tex"):
                    mask = path
            elif kind == 1 and slot == 0x6F016:
                offset = start + value
                if start + length <= offset and offset + 80 <= len(data):
                    constants = struct.unpack_from("<20f", data, offset)
        if slots:
            diffuse = tuple(dict.fromkeys(slots[k] for k in (0x36DDCDC3, 0x36EDCDC3)
                                          if k in slots and slots[k].endswith(("_bm.tex", "_nuki.tex"))))
            bindings[row[1]] = (i, diffuse, slots.get(0x370DCDC3), mask, constants)
    return bindings


class Materials:
    def __init__(self, library):
        self.library = library

    @cached_property
    def dd2_decoders(self):
        local = Path(__file__).with_name("viewer_codecs")
        return load_module("viewer_mdf", local / "mdf_re.py"), load_module("viewer_tex", local / "tex.py")

    @cached_property
    def dd2_paths(self):
        with (self.library.args.work / "DD2_STM.list").open(encoding="utf-8-sig") as source:
            return {line.strip().casefold(): line.strip() for line in source if ".mdf2." in line or ".tex." in line}

    @lru_cache(maxsize=16)
    def dd2_texture(self, path):
        name = path.replace("\\", "/").casefold()
        if not name.startswith("natives/"):
            name = "natives/stm/" + name
        if "/null" in name:
            return None
        candidates = [name]
        prefix = "natives/stm/"
        if name.startswith(prefix + "character/"):
            candidates.insert(0, prefix + "streaming/" + name[len(prefix):])
        for candidate in candidates:
            actual = self.dd2_paths.get(f"{candidate}.251211553")
            if not actual:
                continue
            try:
                decoder = self.dd2_decoders[1]
                data = self.library.dd2_file(actual).read_bytes()
                dxgi, = struct.unpack_from("<I", data, 0x10)
                # The DDS reader requires the non-sRGB tags for these block formats.
                if dxgi in (72, 75, 78):
                    data = data[:0x10] + struct.pack("<I", dxgi - 1) + data[0x14:]
                image = decoder.to_image(data)
                image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
                return image
            except (OSError, ValueError, NotImplementedError, struct.error):
                continue
        return None

    @lru_cache(maxsize=192)
    def dd2_maps(self, path, normal_path, mask_path, alpha_path, cutoff, channel):
        diffuse = self.dd2_texture(path)
        if diffuse is None:
            return "", "", ""
        nr = self.dd2_texture(normal_path)
        mask = self.dd2_texture(mask_path)
        opacity = None
        if cutoff is not None:
            alpha = self.dd2_texture(alpha_path)
            if alpha is not None:
                opacity = alpha.getchannel("A")
            elif mask is not None and not alpha_path and "_mtos." not in mask_path.casefold():
                opacity = mask.getchannel("R")
        color = diffuse.convert("RGB")
        if opacity is not None:
            opacity = opacity.resize(color.size, Image.Resampling.BILINEAR)
            color.putalpha(opacity.point([255 if i / 255 >= cutoff else 0 for i in range(256)]))
        size = (256, 256)
        if channel == "BaseDielectricMap" or path.casefold().endswith("_albd.tex"):
            metal = ImageOps.invert(diffuse.getchannel("A"))
        elif channel == "BaseMetallicMap" or path.casefold().endswith("_albm.tex"):
            metal = diffuse.getchannel("A")
        else:
            metal = Image.new("L", (1, 1), 0)
        roughness = nr.getchannel("A") if nr is not None else Image.new("L", (1, 1), 255)
        occlusion = mask.getchannel("B") if mask is not None else Image.new("L", (1, 1), 255)
        channels = [channel.resize(size, Image.Resampling.BILINEAR) for channel in (metal, roughness, occlusion)]
        surface = data_uri(Image.merge("RGB", channels))
        normal = ""
        if nr is not None:
            normal_image = nr.convert("RGB")
            normal_image.thumbnail((512, 512), Image.Resampling.BILINEAR)
            normal = data_uri(normal_image)
        return image_uri(color, preserve_alpha=opacity is not None, max_size=1024), normal, surface

    @lru_cache(maxsize=32)
    def dd2_stitch(self, path, cutoff):
        packed = self.dd2_texture(path)
        if packed is None:
            return ""
        image = Image.new("RGB", packed.size, "white")
        alpha = packed.getchannel("A").point([255 if i / 255 >= cutoff else 0 for i in range(256)])
        image.putalpha(alpha)
        return image_uri(image, preserve_alpha=True)

    @lru_cache(maxsize=128)
    def dd2(self, path, skin=None):
        logical = self.library.dd2_origins.get(path)
        if logical is None:
            return {}
        root = re.sub(r"\.mesh\.\d+$", "", logical.casefold())
        candidates = [f"{root}.mdf2.51", f"{root}_mannequin.mdf2.51"]
        if "/_equipment/" in root:
            paired = root.removesuffix("_beast")
            if paired != root:
                candidates.append(f"{paired}.mdf2.51")
            if paired.endswith("_f"):
                candidates.append(f"{paired[:-2]}_m.mdf2.51")
        if skin:
            candidates.insert(0, skin.casefold())
        materials = {}
        untextured_variant = set()
        for name in candidates:
            if name not in self.dd2_paths:
                continue
            try:
                mdf = self.library.dd2_file(self.dd2_paths[name])
                reader = self.dd2_decoders[0]
                data = mdf.read_bytes()
                rows = reader.read_mdf(data)
                cutoffs = list(dd2_alpha_cutoffs(data, reader, rows))
            except (ImportError, OSError, ValueError, struct.error):
                continue
            for row, cutoff in zip(rows, cutoffs):
                if row.name in materials and row.name not in untextured_variant:
                    continue
                params = {param.name: param.values for param in row.params if param.values}
                color = params.get("BaseColor", (1, 1, 1))[:3]
                textures = row.textures
                slot = next((key for key in ("BaseDielectricMap", "BaseMetallicMap", "AlbedoMap", "BaseMap", "BaseColorMap")
                             if textures.get(key)), "")
                image, normal, surface = self.dd2_maps(
                    textures.get(slot, ""), textures.get("NormalRoughnessMap", ""),
                    textures.get("AlphaTranslucentOcclusionCavityMap", ""), textures.get("BaseAlphaMap", ""),
                    cutoff, slot,
                )
                color_texture = textures.get(slot, "")
                if "Stitch_NAM" in textures and "Stitch_Color" in params:
                    color = params["Stitch_Color"][:3]
                    color_texture = textures["Stitch_NAM"]
                    image = self.dd2_stitch(color_texture, cutoff if cutoff is not None else 0.5)
                if row.name in untextured_variant and not image:
                    continue
                materials[row.name] = Material(
                    tuple(color), image, normal, surface, params.get("Metallic", (1,))[0],
                    params.get("Roughness", (1,))[0], name, color_texture if image else "",
                )
                if image:
                    untextured_variant.discard(row.name)
                elif skin and name == skin.casefold() and textures.get(slot, "").casefold().endswith("/nullwhite.tex"):
                    # The swap has no albedo for this panel on the shared mesh.
                    untextured_variant.add(row.name)
            if materials and not untextured_variant:
                break
        return materials

    @lru_cache(maxsize=16)
    def dd2_player_body(self, path, head, skin_color=None):
        materials = self.dd2(path).copy()
        face = self.dd2(head).get("head_mat")
        atlas = self.dd2_texture(face.color_texture) if face and face.color_texture else None
        # The mannequin sets these parameters to white. Estimate the viewer
        # tint from the corresponding head when the runtime colors are absent.
        rgb = np.array([0.49, 0.32, 0.24])
        if atlas is not None:
            pixels = np.asarray(atlas.convert("RGB").resize((96, 96)))
            r, g, b = pixels.transpose(2, 0, 1)
            skin = pixels[(r > g) & (g > b) & (r >= 70)]
            if len(skin):
                rgb = np.median(skin, axis=0) / 255
        for name in ("body_mat", "hand_mat", "nail_mat"):
            material = materials.get(name)
            if material and not material.image:
                color = rgb if name != "nail_mat" else rgb * np.array([1.08, 1.09, 1.10])
                if skin_color is not None:
                    color = color * np.asarray(skin_color) / 255
                materials[name] = replace(material, base=tuple(np.clip(color, 0, 1) ** 2.2),
                                          metallic=0.0, roughness=0.8)
        return materials

    @lru_cache(maxsize=128)
    def ddon_texture(self, path, cutout=False):
        if cutout or path.name.casefold().endswith("_nuki.tex"):
            data = path.read_bytes()
            if len(data) > 13 and data[13] in (0x14, 0x18):
                return image_uri(ddon_texture_image(data, data[13]), preserve_alpha=True)
        target = self.library.cache / (sha256(str(path).encode()).hexdigest()[:20] + ".tex")
        dds = target.with_suffix(".tex.dds")
        if not dds.exists():
            shutil.copyfile(path, target)
            self.library.revil("mtf_tex_to_dds", target, dds)
        with Image.open(dds) as image:
            return image_uri(image, preserve_alpha=cutout)

    @lru_cache(maxsize=96)
    def ddon_normal(self, path):
        try:
            packed = ddon_texture_image(path.read_bytes(), 0x1F)
        except (OSError, ValueError, struct.error):
            return ""
        # DDON's normal map stores X in BC3 alpha and Y in green.
        xy = Image.merge("RGB", (packed.getchannel("A"), packed.getchannel("G"), Image.new("L", packed.size, 255)))
        xy.thumbnail((512, 512), Image.Resampling.BILINEAR)
        return data_uri(xy)

    @lru_cache(maxsize=32)
    def ddon(self, path, color=None):
        origin = self.library.ddon_origins.get(path) if path else (
            "Human.arc", "obj/pl/pl000000/model/pl000000_00.mod",
        )
        if not origin:
            return {}
        archive, model = origin
        try:
            materials = self.library.ddon_members(archive, "mrl")
            mrl = next(item["path"] for item in materials if item["name"].casefold() == model[:-4].casefold() + ".mrl")
            bindings = mrl_bindings(mrl.read_bytes())
        except (OSError, ValueError, StopIteration, struct.error, subprocess.SubprocessError):
            return {}
        textures = {}
        archives = (archive, "ddo_common.arc") if model.casefold().startswith("effect/") else (archive,)
        for package in archives:
            try:
                for item in self.library.ddon_members(package, "tex"):
                    textures.setdefault(item["name"].casefold(), item["path"])
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
        palette = {}
        if any(mask and constants for _, _, _, mask, constants in bindings.values()):
            try:
                montage_name = model[:-4].casefold() + ".dmt"
                montage = next(item["path"] for item in self.library.ddon_members(archive, "dmt")
                               if item["name"].casefold() == montage_name)
                variant = self.library.ddon_default_color(archive) if color is None else color
                palette = ddon_palette(montage.read_bytes(), variant)
            except (OSError, ValueError, StopIteration, struct.error, subprocess.SubprocessError):
                pass
        result = {}
        for key, (index, candidates, normal_path, mask, constants) in bindings.items():
            for name in candidates:
                if name not in textures:
                    continue
                try:
                    cutout = name.startswith("obj/hr/") or any(part in name for part in ("_eyebrow_", "_eyelashes_"))
                    image = self.ddon_texture(textures[name], cutout)
                    if name.endswith("_nuki.tex") and mask in textures and constants:
                        data = textures[name].read_bytes()
                        if len(data) > 13 and data[13] in (0x14, 0x18):
                            try:
                                albedo = ddon_texture_image(data, data[13])
                                mask_map = ddon_texture_image(textures[mask].read_bytes(), 0x13)
                            except (OSError, ValueError, struct.error):
                                pass
                            else:
                                original = (constants[8:11], constants[12:15], constants[16:19])
                                dyed = ddon_apply_palette(albedo, mask_map, constants[:3], constants[4:7],
                                                           palette.get(index, original))
                                image = image_uri(dyed, preserve_alpha=True)
                except (OSError, ValueError, struct.error, subprocess.SubprocessError):
                    continue
                normal = self.ddon_normal(textures[normal_path]) if normal_path in textures else ""
                result[key] = Material(image=image, normal=normal, source=model[:-4] + ".mrl", color_texture=name)
                break
        return result


def material_for_name(name, table, default):
    if name in table:
        return table[name]
    key = (~zlib.crc32(name.encode("ascii", errors="replace"))) & 0xFFFFFFFF
    return table.get(key, default)
