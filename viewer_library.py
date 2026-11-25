"""Browse and preview local DDON and DD2 resources without starting either game."""

import argparse
from dataclasses import dataclass, replace
from functools import cached_property
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import numpy as np

from catalog_names import JOBS, ddon_equipment_looks, ddon_label, ddon_vocation, enemy_names, equipment_names, preset_model_tags, skill_names
from ddon_reborn import extract
from preview_legs import LIMBS
from preview_mesh import dd2_mesh, load_mesh_reader, packed_mesh, read_dd2_parts, rigid_vertices, source_mesh, static_dd2
from preview_motion import render_data, scene_html, source_pose, target_pose, with_idle
from retarget_motion import accessor, read_glb, retarget
from skill_assets import skill_assets
from viewer_dd2_export import build_export
from viewer_effects import (REX_ARCHIVE, REX_BURST_ATLASES, REX_ELEMENTS, REX_ELEMENT_TINTS, SPRITE_ERRORS,
                            ddon_atlas, effect_catalog, load_effect, motion_effects,
                            rex_cast_sprite, rex_effect_assets)
from viewer_materials import Materials


DDON_TEST_ARMOR = ("ab210000_00", "aa200000_00", "al230000_00")
DDON_PLAYER_MODELS = {
    "male": "obj/pl/pl000000/model/pl000000_00.mod",
    "female": "obj/pl/pl000000/model/pl000000_01.mod",
}


def is_ddon_player_motion(asset, title):
    return asset.game == "DDON" and (asset.path == "Fighter attack study" or
                                    title.casefold().startswith("obj/pl/pl000000/"))
DD2_TEST_ARMOR = ("tops/001/tops_001_bd", "tops/001/tops_001_wb", "tops/001/tops_001_am",
                  "pants/001/pants_001_lg", "pants/001/pants_001_wl")
ARMOR_FILTERS = (
    ("head", "Head", ("ah", "helm")),
    ("chest", "Chest armor", ("ab", "tops_bd")),
    ("body", "Body clothing", ("wb", "tops_wb", "underwear")),
    ("arms", "Hands and arms", ("aa", "tops_am")),
    ("legwear", "Legwear", ("wl", "pants_wl")),
    ("feet", "Feet and boots", ("al", "pants_lg")),
    ("waist", "Waist", ("tops_bt",)),
    ("cloak", "Cloaks", ("ac", "mantle")),
    ("facewear", "Facewear", ("facewear",)),
    ("backpack", "Backpacks", ("backpack",)),
    ("lantern", "Lanterns", ("lt", "lantern")),
    ("other", "Other armor", ()),
)
ARMOR_REGION_BY_SLOT = {slot: region for region, _, slots in ARMOR_FILTERS for slot in slots}
WEAPON_FILTERS = (
    ("sword", "Swords"), ("shield", "Shields"), ("greatsword", "Greatswords and hammers"),
    ("dagger", "Daggers · Seeker / Thief"), ("bow", "Bows"), ("staff", "Staves · Priest / Mage"),
    ("archistaff", "Archistaves · Sorcerer"), ("magick_bow", "Magick bows"),
    ("polearm", "Lances and polearms"), ("rod", "Rods · Shield Sage"),
    ("gauntlet", "Magick gauntlets · Alchemist"), ("magick_sword", "Magick blades"),
    ("censer", "Censers · Trickster"), ("other", "Other weapons"),
)
WEAPON_FAMILIES = {
    "DDON": {
        "wp300": "sword", "wp301": "shield", "wp302": "greatsword", "wp303": "shield",
        "wp304": "rod", "wp305": "dagger", "wp306": "bow", "wp307": "gauntlet",
        "wp308": "magick_bow", "wp310": "staff", "wp311": "archistaff",
        "wp312": "polearm", "wp314": "magick_sword",
    },
    "DD2": {
        "wp00": "sword", "wp01": "shield", "wp02": "greatsword", "wp03": "dagger",
        "wp04": "bow", "wp05": "magick_bow", "wp07": "staff",
        "wp08": "archistaff", "wp09": "polearm", "wp10": "censer",
    },
}


@dataclass(frozen=True)
class Asset:
    game: str
    kind: str
    path: str
    count: int = 1
    examples: str = ""
    label: str = ""
    vocation: str = ""
    material: str | None = None
    aliases: tuple[str, ...] = ()
    shared: bool = False
    color: int | None = None


def armor_slot(path):
    name = Path(path).name.casefold()
    ddon = re.match(r"(aa|ab|ac|ah|al|lt|wb|wl)\d", name)
    if ddon:
        return ddon[1]
    layer = re.match(r"(tops|pants)_\d+_(\w{2})_", name)
    if layer:
        return f"{layer[1]}_{layer[2]}"
    part = re.match(r"(helm|mantle|facewear|backpack|underwear|lantern)_\d", name)
    return part[1] if part else name


def armor_region(path):
    return ARMOR_REGION_BY_SLOT.get(armor_slot(path), "other")


def weapon_family(asset):
    stem = Path(asset.path).name.casefold()
    family = stem[:5] if asset.game == "DDON" else stem.split("_", 1)[0]
    return WEAPON_FAMILIES.get(asset.game, {}).get(family, "other")


def read_catalog(ddon_index, dd2_index, rom, metadata, ddon_looks):
    assets = [Asset("DDON", "animation", "Fighter attack study", vocation="Fighter")]
    enemies, skills = enemy_names(metadata), skill_names(metadata)
    equipment = equipment_names(ddon_index.with_name("ddon-itemlist.ipa.json"),
                                ddon_index.with_name("ddon-model-tags.json"), metadata)
    current = None
    with ddon_index.open(encoding="utf-8-sig") as source:
        for line in source:
            match = re.match(r"^(lmt|mod)\t(\d+)\t(.+?)\s*$", line)
            if match:
                extension, count, path = match.groups()
                relative = Path(path).relative_to(rom).as_posix()
                current = [extension, relative, int(count), []]
                assets.append(current)
            elif current and line.lstrip().startswith("-> "):
                current[-1].append(line.strip()[3:].replace("\\", "/"))
    ddon_variants = []
    for index in range(1, len(assets)):
        extension, path, count, members = assets[index]
        root = path.split("/")[0].casefold()
        kind = "animation" if extension == "lmt" else {
            "wp": "weapon", "armor": "armor", "edit": "appearance",
        }.get(root, "model")
        label, aliases = ddon_label(path, kind, enemies, skills, equipment)
        vocation = ddon_vocation(path) if kind == "animation" else ""
        base = Asset("DDON", kind, path, count, " ".join([label, *aliases, *members]), label, vocation,
                     aliases=tuple(aliases))
        looks = ddon_looks.get(path.casefold()) if kind in {"armor", "weapon"} else None
        if looks:
            colors = {}
            for name, color in looks:
                colors.setdefault(color, []).append(name)
            variants = []
            for color, names in colors.items():
                title = names[0] + (f" (+{len(names) - 1} items)" if len(names) > 1 else "")
                variants.append(replace(base, examples=" ".join([*names, *members]), label=title,
                                        aliases=tuple(names), color=color))
            base = variants[0]
            ddon_variants.extend(variants[1:])
        assets[index] = base
    with dd2_index.open(encoding="utf-8-sig") as source:
        for line in source:
            path = line.strip()
            if re.search(r"\.(?:motlist|mot)\.\d+$", path):
                kind = "animation"
            elif re.search(r"\.mesh\.\d+$", path):
                if "/character/wp/" in path:
                    kind = "weapon"
                elif "/character/_kit/_equipment/" in path:
                    kind = "armor"
                elif any(f"/character/_kit/{part}/" in path for part in ("body", "head", "hair", "beard")):
                    kind = "appearance"
                elif "/character/ch/" in path:
                    kind = "target"
                else:
                    continue
            else:
                continue
            assets.append(Asset("DD2", kind, path))
    label_file = dd2_index.with_name("dd2-equipment-labels.json")
    if label_file.is_file():
        indexed = {asset.path: i for i, asset in enumerate(assets) if asset.game == "DD2"}
        shared = {}
        for row in json.loads(label_file.read_text(encoding="utf-8-sig")):
            index = indexed.get(row["mesh"])
            if index is None or not row["names"]:
                continue
            asset = assets[index]
            names = tuple(row["names"])
            label = names[0] + (f" (+{len(names) - 1} items)" if len(names) > 1 else "")
            variant = replace(asset, examples=" ".join(names), label=label, material=row.get("material"), aliases=names)
            if variant.material:
                assets.append(variant)
                shared.setdefault(index, set()).update(names)
            else:
                assets[index] = variant
        for index, names in shared.items():
            if not assets[index].label:
                aliases = tuple(sorted(names, key=str.casefold))
                assets[index] = replace(assets[index], examples=" ".join(aliases), aliases=aliases, shared=True)
        for mesh, index in indexed.items():
            if "_beast.mesh." not in mesh or assets[index].label:
                continue
            human = indexed.get(mesh.replace("_beast.mesh.", ".mesh."))
            if human is None or not assets[human].aliases:
                continue
            counterpart = assets[human]
            label = counterpart.label + " · Beastren" if counterpart.label else ""
            assets[index] = replace(assets[index], label=label, aliases=counterpart.aliases,
                                    examples=" ".join((*counterpart.aliases, "Beastren")),
                                    shared=counterpart.shared)
    return [*assets, *ddon_variants]


def lmt_slots(path):
    data = path.read_bytes()
    magic, version, count = struct.unpack_from("<4sHH", data)
    if magic != b"LMT\0" or version != 67:
        return []
    return [slot for slot, off in enumerate(struct.unpack_from(f"<{count}I", data, 8)) if 8 + 4 * count <= off < len(data)]


def animation_duration(gltf, binary, animation):
    return max(accessor(gltf, binary, sampler["input"])[-1, 0] for sampler in animation["samplers"])


def sequence_samples(gltf, binary, animations):
    durations = np.array([animation_duration(gltf, binary, clip) for clip in animations])
    if len(animations) == 1:
        steps = min(80, round(durations[0] * 60))
        fps = steps / durations[0] if steps else 60
        return fps, [np.linspace(0, durations[0], steps + 1)]
    minimum = np.where(durations > 0, 2, 1)
    steps = max(int(minimum.sum()), min(100, round(durations.sum() * 60)))
    fps = steps / durations.sum() if durations.sum() > 0 else 60
    remaining = np.cumsum(minimum[::-1])[::-1]
    boundaries = [0]
    for i, end in enumerate(np.rint(np.cumsum(durations) * fps).astype(int)[:-1], start=1):
        boundaries.append(max(boundaries[-1] + minimum[i - 1], min(end, steps - remaining[i])))
    boundaries.append(steps)
    times = []
    for i, (duration, start, end) in enumerate(zip(durations, boundaries[:-1], boundaries[1:])):
        final = i == len(animations) - 1
        times.append(np.linspace(0, duration, end - start + final, endpoint=final))
    return fps, times


def rex_motion_cues(stages, events, times, loop_offset):
    cast, release = [], []
    for stage in stages:
        name = stage["name"]
        phase = {
            "Motion[120]_start": (120, 0, {5504, 7503}),
            "Motion[120]_loop": (120, loop_offset, {7503}),
            "Motion[121]": (121, 0, {5505, 5501}),
        }.get(name)
        if phase is None:
            continue
        slot, offset, codes = phase
        start, end = stage["start"], stage["end"]
        for event in events.get(slot, []):
            matched = codes.intersection(event["codes"])
            if event["group"] != "3" or not matched or event["time"] < offset:
                continue
            time = times[start] + event["time"] - offset
            frame = start + int(np.searchsorted(times[start:end + 1], time - 1e-6, side="left"))
            if frame > end:
                continue
            if 5501 in matched:
                release.append(frame)
                if cast and cast[-1][1] == end:
                    cast[-1] = (cast[-1][0], frame - 1)
            else:
                cast.append((frame, end))
    return cast, release


class Library:
    def __init__(self, args):
        self.args = args
        self.cache = args.work / "library-cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.rom = args.ddon / "nativePC" / "rom"
        metadata = getattr(args, "metadata", None) or args.ddon.parent.parent / "Server" / "Arrowgene.Ddon.Shared"
        self.dd2_origins = {}
        self.ddon_origins = {}
        self.catalog = read_catalog(args.work / "ddon-index.txt", args.work / "DD2_STM.list", self.rom, metadata, self.ddon_looks)
        self.ddon_models = {asset.path: asset for asset in self.catalog if asset.game == "DDON" and asset.kind != "animation"}
        self.reader = load_mesh_reader(Path(__file__).with_name("viewer_codecs") / "mesh_re.py")
        self.materials = Materials(self)

    @cached_property
    def armor_sets(self):
        named = {(asset.path, asset.label): index for index, asset in enumerate(self.catalog)
                 if asset.game == "DDON" and asset.kind == "armor"}
        specs = {
            "ddon-solemn": ("Solemn armor", (("ab210000_00", "Solemn Armor"), ("aa200000_00", "Solemn Arms"),
                                               ("al230000_00", "Solemn Legs"))),
            "ddon-supply": ("Supply outfit", (("ab210033_00", "Supply Vest"), ("al230033_00", "Supply Bottoms"))),
            "ddon-seeker": ("Seeker equipment preset", (("ab210046_00", "Faith Jacket"), ("wb240011_00", "Special Bracers"),
                ("aa200002_00", "Two Hands"), ("al230002_00", "Ninja Bottoms"), ("wl250026_00", "Limber Metal Hose"),
                ("ac260001_00", "Brown Cape"))),
        }
        result = {}
        for key, (name, pieces) in specs.items():
            refs = [named.get((f"armor/{path}.arc", label)) for path, label in pieces]
            if all(ref is not None for ref in refs):
                result[key] = {"id": key, "name": name, "game": "DDON", "count": len(refs), "refs": refs}
        result["dd2-training"] = {"id": "dd2-training", "name": "Training armor", "game": "DD2",
                                   "count": len(DD2_TEST_ARMOR)}
        if self.saved_dd2 and self.saved_dd2.get("armor"):
            result["dd2-saved"] = {"id": "dd2-saved", "name": f"{self.saved_dd2['name']}'s saved armor",
                                   "game": "DD2", "body": self.saved_dd2["body"], "count": len(self.saved_dd2["armor"])}
        return result

    @cached_property
    def armor_regions(self):
        return {index: armor_region(asset.path) for index, asset in enumerate(self.catalog) if asset.kind == "armor"}

    def armor_filters(self, game):
        available = {region for index, region in self.armor_regions.items()
                     if not game or self.catalog[index].game == game}
        return [{"value": value, "label": label} for value, label, _ in ARMOR_FILTERS if value in available]

    @cached_property
    def weapon_families(self):
        return {index: weapon_family(asset) for index, asset in enumerate(self.catalog) if asset.kind == "weapon"}

    def weapon_filters(self, game):
        available = {family for index, family in self.weapon_families.items()
                     if not game or self.catalog[index].game == game}
        return [{"value": value, "label": label} for value, label in WEAPON_FILTERS if value in available]

    @cached_property
    def saved_ddon(self):
        if not getattr(self.args, "saved_characters", False):
            return None
        path = self.args.work / "saved-ddon-character.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8-sig"))
        return None

    @cached_property
    def saved_dd2(self):
        if not getattr(self.args, "saved_characters", False):
            return None
        path = self.args.work / "saved-dd2-character.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8-sig"))
        return None

    def index_preset_equipment(self):
        rom = self.args.ddon / "nativePC" / "rom"
        item_data = json.loads((self.args.work / "ddon-itemlist.ipa.json").read_text(encoding="utf-8-sig"))
        presets = []
        for file in sorted(rom.glob("equip_preset_*.arc")):
            try:
                entries = self.ddon_members(file.name, "equip_preset")
                meshes = self.ddon_members(file.name, "mod")
            except (ValueError, OSError, subprocess.SubprocessError):
                continue
            presets.extend((file.name, row["path"].read_bytes(), [mesh["name"] for mesh in meshes]) for row in entries)
        tags = preset_model_tags(item_data, presets)
        output = self.args.work / "ddon-model-tags.json"
        if output.is_file():
            existing = json.loads(output.read_text(encoding="utf-8-sig"))
            tags = {**existing, **tags}
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(tags, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output)
        return len(tags)

    @cached_property
    def ddon_looks(self):
        try:
            entries = self.ddon_members("wep_res_table.arc", "wrt")
            resource = next(row["path"] for row in entries if row["name"].casefold() == "etc/weprestable.wrt")
            items = json.loads((self.args.work / "ddon-itemlist.ipa.json").read_text(encoding="utf-8-sig"))
            return ddon_equipment_looks(items, resource.read_bytes())
        except (OSError, ValueError, StopIteration, struct.error, subprocess.SubprocessError):
            return {}

    def ddon_default_color(self, archive):
        looks = self.ddon_looks.get(archive.casefold())
        return looks[0][1] if looks else 0

    def ddon_model_path(self, archive):
        return self.members(self.ddon_models[archive])[0]["path"]

    def test_armor(self, game, body):
        if game == "DDON":
            return [self.ddon_model_path(f"armor/{piece}.arc") for piece in DDON_TEST_ARMOR]
        gender = {"male": "m", "female": "f"}[body]
        return [self.dd2_file(f"natives/stm/character/_kit/_equipment/{piece}_{gender}.mesh.260421070")
                for piece in DD2_TEST_ARMOR]

    @cached_property
    def pak_tools(self):
        from viewer_codecs import pak
        return pak

    @cached_property
    def paks(self):
        pak = self.pak_tools
        return [pak.Pak(path) for path in pak.game_pak_paths(self.args.dd2)]

    @cached_property
    def outfit_keys(self):
        return {self.outfit_key(asset): index for index, asset in enumerate(self.catalog)
                if asset.kind in {"armor", "weapon", "appearance"}}

    @staticmethod
    def outfit_key(asset):
        identity = (asset.game, asset.kind, asset.path.casefold(), asset.material, asset.color)
        return sha256(json.dumps(identity).encode("utf-8")).hexdigest()[:32]

    def catalog_item(self, index):
        asset = self.catalog[index]
        armor_labels = {value: label for value, label, _ in ARMOR_FILTERS}
        return {"id": index, "game": asset.game, "kind": asset.kind, "name": asset.path,
                "label": asset.label, "count": asset.count, "aliases": asset.aliases, "shared": asset.shared,
                "key": self.outfit_key(asset),
                "type_label": (armor_labels.get(self.armor_regions.get(index))
                               or dict(WEAPON_FILTERS).get(self.weapon_families.get(index)) or ""),
                "slot": armor_slot(asset.path) if asset.kind == "armor" else ""}

    def asset(self, ref):
        index = int(ref)
        if not 0 <= index < len(self.catalog):
            raise ValueError("unknown asset")
        return self.catalog[index]

    def dd2_file(self, path):
        key = sha256(path.encode()).hexdigest()[:20]
        output = self.cache / f"{key}-{Path(path).name}"
        self.dd2_origins[output] = path
        if output.is_file():
            return output
        for pak in reversed(self.paks):
            if pak.has(path):
                data = pak.read(path)
                if data:
                    output.write_bytes(data)
                    return output
        raise ValueError("This file-list entry is absent from the installed DD2 PAKs.")

    @cached_property
    def dd2_equipment_variants(self):
        variants = {}
        for index, asset in enumerate(self.catalog):
            if asset.game != "DD2" or asset.kind not in {"armor", "weapon"}:
                continue
            variants.setdefault(asset.path, []).append({"id": index, "label": asset.label or "Default material"})
        return variants

    def members(self, asset):
        if asset.game == "DD2":
            return [{"name": asset.path, "path": self.dd2_file(asset.path)}]
        if asset.path == "Fighter attack study":
            return [entry for entry in self.ddon_members("job01.arc", "lmt")
                    if entry["name"].casefold().endswith("/m0001_at.lmt")]
        extension = "lmt" if asset.kind == "animation" else "mod"
        return self.ddon_members(asset.path, extension)

    def ddon_members(self, archive, extension):
        key = sha256(f"{archive}:{extension}".encode()).hexdigest()[:20]
        run = self.cache / f"archive-{key}"
        pointer = run.with_suffix(".resolved")
        destination = self.cache / pointer.read_text(encoding="ascii") if pointer.exists() else run
        manifest = destination / "manifest.json"
        if not manifest.exists():
            destination = run
            retry = 1
            while destination.exists():
                destination = run.with_name(f"{run.name}-retry{retry}")
                retry += 1
            record = extract(self.args.ddon, Path(archive), extension, self.args.skilltool, destination)
            if destination != run:
                pointer.write_text(destination.name, encoding="ascii")
        else:
            record = json.loads(manifest.read_text(encoding="utf-8"))
        entries = [{"name": row["member_path"], "path": destination / row["output_path"]} for row in record["outputs"]]
        if extension == "mod":
            self.ddon_origins.update({item["path"]: (archive, item["name"]) for item in entries})
        return entries

    def selected_member(self, ref, member, kinds=None):
        asset = self.asset(ref)
        if kinds and asset.kind not in kinds:
            raise ValueError("the selected resource has an incompatible category")
        entries = self.members(asset)
        index = int(member)
        if not 0 <= index < len(entries):
            raise ValueError("unknown archive member")
        return asset, entries[index]["path"], entries[index]["name"]

    def revil(self, action, argument, expected):
        with argument.with_suffix(".log").open("wb") as log:
            subprocess.run([str(self.args.revil), action, str(argument)], cwd=argument.parent,
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
        if not expected.is_file():
            raise ValueError(f"Revil produced no {expected.suffix} output. Check the selected rig and format.")
        return expected

    def ddon_model(self, path):
        folder = self.cache / sha256(str(path).encode()).hexdigest()[:20]
        folder.mkdir(exist_ok=True)
        model, output = folder / "rig.mod", folder / "rig.glb"
        if not output.is_file():
            shutil.copyfile(path, model)
            self.revil("mod_to_gltf", model, output)
        return output

    @cached_property
    def ddon_player_models(self):
        return {entry["name"].casefold(): entry["path"] for entry in self.ddon_members("Human.arc", "mod")}

    def ddon_animation(self, path, rig, body="male"):
        if rig is None:
            rig = self.ddon_player_models[DDON_PLAYER_MODELS[body]]
        model = self.ddon_model(rig)
        key = sha256(f"{path}:{model}".encode()).hexdigest()[:20]
        folder = self.cache / key
        folder.mkdir(exist_ok=True)
        batch, output = folder / "input.batch.json", folder / "rig_out.glb"
        if not output.is_file():
            shutil.copyfile(model, folder / "rig.glb")
            shutil.copyfile(path, folder / "motion.lmt")
            batch.write_text(json.dumps([["rig.glb", "motion.lmt"]]), encoding="ascii")
            self.revil("lmt_to_gltf", batch, output)
        return output

    def ddon_rig(self, asset, title, rig):
        if rig is not None or asset.path == "Fighter attack study" or title.casefold().startswith("obj/pl/"):
            return rig
        companion = self.ddon_models.get(asset.path)
        if companion is None:
            raise ValueError("Select the matching DDON model as the rig for this motion list.")
        candidates = self.members(companion)
        base = title.casefold().split("/motion/")[0]
        same_actor = [item for item in candidates if item["name"].casefold().startswith(base + "/")]
        if not same_actor:
            raise ValueError("Select the matching DDON model as the rig for this motion list.")
        return same_actor[0]["path"]

    def clip_info(self, asset, title, path, rig, body="male"):
        game = asset.game
        if game == "DD2":
            return self.native(path, "list")
        slots = lmt_slots(path)
        try:
            rig = self.ddon_rig(asset, title, rig)
        except ValueError as error:
            return {"clips": [{"key": "", "name": f"Slot {slot} · rig required"} for slot in slots],
                    "slots": len(slots), "unavailable": len(slots), "message": str(error)}
        try:
            gltf, binary = read_glb(self.ddon_animation(path, rig, body))
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            return {"clips": [{"key": "", "name": f"Slot {slot} · conversion unavailable"} for slot in slots],
                    "slots": len(slots), "unavailable": len(slots), "message": str(error)}
        events = self.ddon_events(path)
        clips = []
        for i, clip in enumerate(gltf.get("animations", [])):
            slot = re.search(r"\[(\d+)\]", clip["name"])
            clips.append({"key": str(i), "name": clip["name"],
                          "duration": float(animation_duration(gltf, binary, clip)),
                          "events": events.get(int(slot.group(1)), []) if slot else []})
        exported = {int(m.group(1)) for clip in clips if (m := re.search(r"\[(\d+)\]", clip["name"]))}
        missing = sorted(set(slots) - exported)
        clips.extend({"key": "", "name": f"Slot {slot} · unavailable on this rig"} for slot in missing)
        return {"clips": clips, "slots": len(slots), "unavailable": len(missing),
                "dd2_export": rig is None and is_ddon_player_motion(asset, title)}

    def export_dd2(self, asset_ref, member, clip, body):
        asset, path, title = self.selected_member(asset_ref, member, {"animation"})
        if not is_ddon_player_motion(asset, title):
            raise ValueError("DD2 export supports standard DDON player motions.")
        gltf, binary = read_glb(self.ddon_animation(path, None, body))
        clips = gltf.get("animations", [])
        index = int(clip)
        if not 0 <= index < len(clips):
            raise ValueError("Select a playable DDON clip to export.")
        animation = clips[index]
        return build_export(self, asset, title, (gltf, binary, animation), body,
                            animation_duration(gltf, binary, animation))

    def effects(self, asset):
        if asset.game != "DDON" or asset.path not in self.ddon_models:
            return []
        return [entry for entry in self.members(self.ddon_models[asset.path])
                if entry["name"].casefold().startswith("effect/")]

    def ddon_events(self, path):
        source = path.with_suffix(".json")
        try:
            if not source.exists():
                self.revil("lmt_to_json", path, source)
            data = json.loads(source.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError, subprocess.SubprocessError):
            return {}
        result = {}
        for name, groups in data.items():
            slot = re.search(r"\[(\d+)\]", name)
            if slot is None or not isinstance(groups, dict):
                continue
            entries = []
            for group, records in groups.items():
                for record in records if isinstance(records, list) else []:
                    if isinstance(record, list) and len(record) == 2:
                        when, codes = record
                        if isinstance(when, (int, float)) and math.isfinite(when) and when >= 0:
                            entries.append({"time": when, "group": group, "codes": codes})
            result[int(slot.group(1))] = sorted(entries, key=lambda row: row["time"])
        return result

    def native(self, path, action, clip=""):
        command = [str(self.args.python315), "-B", str(Path(__file__).with_name("viewer_native_motion.py")),
                   str(self.args.reasy), str(path), action, clip]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=90)
        if result.returncode:
            raise ValueError(next((line for line in reversed(result.stderr.splitlines()) if line.strip()),
                                  "Native animation could not be decoded."))
        return json.loads(result.stdout)

    def static_geometry(self, ref, member):
        asset, path, _ = self.selected_member(ref, member)
        return self.static_model_geometry(asset.game, path, asset.material, asset.color)

    def static_model_geometry(self, game, path, material=None, color=None):
        if game == "DD2":
            return list(static_dd2(self.reader, path, self.materials.dd2(path, material)))
        gltf, binary = read_glb(self.ddon_model(path))
        world, _ = source_pose(gltf, binary, None, np.array([0.0]))
        return [part.moved(part.vertices[0]) for part in source_mesh(gltf, binary, world, self.materials.ddon(path, color))]

    def ddon_outfit(self, gltf, binary, world, path, color=None):
        rest, _ = source_pose(gltf, binary, None, np.array([0.0]))
        deltas = world @ np.linalg.inv(rest[0])
        gear, gear_bin = read_glb(self.ddon_model(path))
        gear_rest, _ = source_pose(gear, gear_bin, None, np.array([0.0]))
        # The first field is local to each MOD. The second is the player's
        # shared bone ID, including the optional scaled companion node.
        def shared_joints(model):
            return {index: match[1] for index, node in enumerate(model["nodes"])
                    if (match := re.fullmatch(r"\d+:(\d+(?:_s)?)", node.get("name", ""))) and match[1] != "255"}

        source_names = {name: index for index, name in shared_joints(gltf).items()}
        indices = {joint for skin in gear.get("skins", []) for joint in skin["joints"]}
        matches = {index: source_names[name] for index, name in shared_joints(gear).items()
                   if index in indices and name in source_names}
        if not matches:
            raise ValueError("This DDON part has no matching skin joints on the selected character.")
        posed = np.broadcast_to(gear_rest, (len(world), *gear_rest.shape[1:])).copy()
        for index, source in matches.items():
            posed[:, index] = deltas[:, source] @ gear_rest[0, index]
        return source_mesh(gear, gear_bin, posed, self.materials.ddon(path, color))

    def base_model(self, gender):
        letter = {"male": "m", "female": "f"}[gender]
        prefix = "natives/stm/character/_kit"
        body = self.dd2_file(f"{prefix}/body/body_000_{letter}/body_000_{letter}.mesh.260421070")
        head = self.dd2_file(f"{prefix}/head/head_000_{letter}/head_000_{letter}.mesh.260421070")
        from dd2_bind_pose import read_bind_pose
        return read_bind_pose(body.read_bytes())["joints"], body, head

    def model_joints(self, ref, member):
        asset, path, _ = self.selected_member(ref, member)
        if asset.game == "DD2":
            from dd2_bind_pose import read_bind_pose
            return {joint["name"]: joint["world_matrix"][12:15] for joint in read_bind_pose(path.read_bytes())["joints"]}
        gltf, binary = read_glb(self.ddon_model(path))
        pose, _ = source_pose(gltf, binary, None, np.array([0.0]))
        return {node["name"]: pose[0, i, :3, 3].tolist()
                for i, node in enumerate(gltf["nodes"]) if node.get("name")}

    def animated_target(self, get, time):
        mob, body, _ = self.selected_member(get("mob"), get("mob_member", "0"))
        motion, path, _ = self.selected_member(get("mob_motion"), get("mob_motion_member", "0"), {"animation"})
        if mob.game != "DDON" or motion.game != "DDON":
            raise ValueError("Target motion currently requires a DDON model and a matching DDON motion list.")
        gltf, binary = read_glb(self.ddon_animation(path, body))
        clip = gltf.get("animations", [])[int(get("mob_motion_clip", "0"))]
        end = max(accessor(gltf, binary, item["input"])[-1, 0] for item in clip["samplers"])
        pose, _ = source_pose(gltf, binary, clip["name"], np.minimum(time, end))
        joints = {node["name"]: pose[:, i, :3, 3] for i, node in enumerate(gltf["nodes"]) if node.get("name")}
        return source_mesh(gltf, binary, pose, self.materials.ddon(body)), joints

    @cached_property
    def alchemist_example(self):
        def ref(path, category):
            return next(str(i) for i, item in enumerate(self.catalog)
                        if item.game == "DDON" and item.kind == category and item.path.casefold() == path.casefold())
        return self.scene({
            "asset": [ref("job09_cs01.arc", "animation")], "clip": ["0"],
            "mob": [ref("EM/EM010100.arc", "model")],
            "object": [ref("job09_cs01.arc", "model")], "hit": ["8"],
        })

    @cached_property
    def comparison(self):
        if not (self.args.fighter / "rig_out.glb").is_file():
            return self.scene({"asset": ["0"], "clip": ["1"]})
        gltf, binary = read_glb(self.args.fighter / "rig_out.glb")
        prepared = [json.loads((self.args.work / f"{name}.json").read_text(encoding="utf-8"))
                    for name in ("DD2 previous retarget", "DD2 arm-aligned retarget")]
        clip = prepared[0]
        frames, fps = clip["frames"], clip["fps"]
        world, indices = source_pose(gltf, binary, clip["source_clip"], np.asarray(frames) / fps, True)
        view = render_data("DDON source · fitted feet", world, indices)
        view["mesh"] = packed_mesh(source_mesh(gltf, binary, world, self.materials.ddon(None)))
        views = [view]
        root = world[:, indices["root"], :3, 3]
        feet = {side: world[:, indices[f"{side}_Leg_Ankle"], :3, 3] - root for side in "RL"}
        joints, body, head = self.base_model("male")
        parts = read_dd2_parts(self.reader, [body, head], self.materials.dd2)
        for label, motion in zip(("DD2 previous retarget", "DD2 arm-aligned retarget"), prepared):
            world, indices = target_pose(joints, motion, feet)
            view = render_data(label + " · fitted feet", world, indices)
            view["mesh"] = packed_mesh(dd2_mesh(parts, world, indices, joints))
            views.append(view)
        return scene_html(clip["source_clip"], fps, frames, views)

    def player_idle(self, title, asset, rig, fps, body):
        job = re.search(r"/motion/(m00\d{2})/", title, re.IGNORECASE)
        stem = job[1].casefold() if job else ("m0001" if asset.path == "Fighter attack study" else "")
        if not stem:
            return None
        archive = next((item for item in self.catalog if item.game == "DDON" and item.kind == "animation"
                        and item.path.casefold() == f"job{stem[-2:]}.arc"), None)
        if archive is None:
            return None
        resource = next((item for item in self.members(archive) if item["name"].casefold().endswith(f"/{stem}_co.lmt")), None)
        if resource is None:
            return None
        gltf, binary = read_glb(self.ddon_animation(resource["path"], rig, body))
        idle = next((clip for clip in gltf["animations"] if clip["name"] == "Motion[0]_loop"), None)
        if idle is None:
            return None
        end = max(accessor(gltf, binary, sample["input"])[-1, 0] for sample in idle["samplers"])
        if end <= 0:
            return None
        blend = max(1, round(0.12 * fps))
        times = (np.arange(max(2, round(0.45 * fps)) + blend) / fps) % end
        return gltf, binary, idle["name"], times, blend

    @cached_property
    def equipment_labels(self):
        return {(asset.game, asset.path.casefold(), (asset.material or "").casefold(), asset.color): asset.label
                for asset in self.catalog if asset.kind in {"weapon", "armor"}}

    @cached_property
    def installed_gear_paks(self):
        result = []
        for path in sorted((self.args.dd2 / "pak_mods").glob("*.pak")):
            try:
                result.append(self.pak_tools.Pak(path))
            except (OSError, ValueError, struct.error):
                continue
        return result

    def equipment_row(self, game, path, parts, weapon=False, skin="", color=None):
        if game == "DD2":
            resource = mesh = self.dd2_origins[path]
        else:
            resource, mesh = self.ddon_origins[path]
        if game == "DDON" and resource.casefold() in self.ddon_looks and color is None:
            color = self.ddon_default_color(resource)
        label = self.equipment_labels.get((game, resource.casefold(), (skin or "").casefold(), color))
        materials = sorted({part.appearance.source for part in parts if part.appearance.source})
        textures = sorted({part.appearance.color_texture for part in parts if part.appearance.color_texture})
        overrides = []
        if game == "DD2":
            texture_paths = [asset.replace("\\", "/").casefold() for asset in textures]
            texture_paths = [asset if asset.startswith("natives/") else "natives/stm/" + asset for asset in texture_paths]
            candidates = [mesh, *materials, *(f"{asset}.251211553" for asset in texture_paths)]
            overrides = [asset for asset in candidates if (self.args.dd2 / asset).is_file()]
            overrides.extend(pak.path.name for pak in self.installed_gear_paks
                             if any(pak.has(asset) for asset in candidates))
        return {"slot": "weapon" if weapon else armor_slot(resource), "resource": resource, "mesh": mesh,
                "name": label or Path(mesh).name, "materials": materials, "textures": textures,
                "mapped": sum(bool(part.appearance.image and part.uv is not None) for part in parts),
                "groups": len(parts), "overrides": overrides}

    def equipment_check(self, game, rows):
        snapshot = self.saved_dd2 if game == "DD2" else self.saved_ddon
        reference = {}
        if snapshot:
            reference = {armor_slot(piece["mesh"]): piece for piece in snapshot.get("armor", [])}
            if snapshot.get("weapon"):
                weapon = snapshot["weapon"]
                if game == "DDON":
                    weapon_mesh = self.ddon_origins[self.ddon_model_path(weapon)][1]
                else:
                    weapon_mesh = weapon
                reference["weapon"] = {"mesh": weapon_mesh}
        result = []
        for row in rows:
            expected = reference.get(row["slot"])
            status = "Unverified"
            if expected:
                matches = row["mesh"].casefold() == expected["mesh"].casefold()
                status = "Matches saved mesh" if matches else "Different mesh"
            row["mesh_status"] = status
            if expected:
                row["expected"] = expected
                variant = expected.get("material")
                if variant and status == "Matches saved mesh":
                    sources = {path.casefold() for path in row["materials"]}
                    if sources == {variant.casefold()}:
                        row["variant_status"] = "Saved material variant"
                    elif variant.casefold() in sources:
                        row["variant_status"] = "Saved variant with fallback"
                    else:
                        row["variant_status"] = "Saved material variant absent"
            result.append(row)
        for slot, expected in reference.items():
            if not any(row["slot"] == slot for row in rows):
                result.append({"name": Path(expected["mesh"]).name, "slot": slot, "expected": expected,
                               "mesh_status": "Saved mesh absent from preview"})
        if not result:
            return None
        return {"reference": (snapshot["name"] + (" · last inn rest" if game == "DD2" else " · Sorcerer save"))
                if snapshot else "No saved equipment reference", "game": game, "rows": result}

    def dd2_preview(self, get, outfit, joints, body, head, motions, label, idle=None, skins=None, masks=None,
                    skin_color=None):
        if get("head"):
            item, head, _ = self.selected_member(get("head"), "0", {"appearance"})
            if item.game != "DD2" or "/head/" not in item.path:
                raise ValueError("The selected asset is not a DD2 head.")
        poses = [target_pose(joints, motion, limb_targets=targets) for motion, targets in motions]
        index = poses[0][1]
        world = np.concatenate([world for world, _ in poses]) if len(poses) > 1 else poses[0][0]
        if idle:
            idle_motion, idle_targets, blend = idle
            idle_world, _ = target_pose(joints, idle_motion, limb_targets=idle_targets)
            ids = {joint["id"]: i for i, joint in enumerate(joints)}
            parents = {i: ids[joint["parent"]] for i, joint in enumerate(joints) if joint["parent"] is not None}
            world = with_idle(world, idle_world, parents, index["root"], blend)
        paths = [body, head, *outfit]

        def player_materials(path):
            if path == body:
                return self.materials.dd2_player_body(body, head, skin_color)
            table = self.materials.dd2(path, (skins or {}).get(path))
            if path == head and skin_color is not None and (face := table.get("head_mat")):
                tint = (np.asarray(skin_color) / 255) ** 2.2
                return {**table, "head_mat": replace(face, base=tuple(np.asarray(face.base) * tint))}
            return table

        parts = read_dd2_parts(self.reader, paths, player_materials, masks)
        geometry = dd2_mesh(parts, world, index, joints)
        view = render_data(label, world, index)
        if all(f"{side}_Arm_Hand" in index for side in ("R", "L")):
            palms = []
            for side in ("R", "L"):
                wrist = world[:, index[f"{side}_Arm_Hand"], :3, 3]
                middle = index.get(f"{side}_Hand_MiddleF_1")
                palms.append(wrist if middle is None else wrist + .58 * (world[:, middle, :3, 3] - wrist))
            view["between_palms"] = (palms[0] + palms[1]) * .5
        view["equipment"] = [
            self.equipment_row("DD2", path, [part for part, _, _ in meshes], skin=(skins or {}).get(path, ""))
            for path, (_, meshes) in zip(paths, parts, strict=True)
            if "/_equipment/" in self.dd2_origins[path].casefold()
        ]
        return view, geometry, world[:, index["R_PropA"]]

    def effect_scene(self, key):
        effect = load_effect(self, key)
        fps = effect["fps"]
        frames = list(range(len(effect["cells"])))
        effect = {**effect, "start": 0, "end": frames[-1], "pose_count": 1, "solo": True, "size": 1.6}
        view = {"label": effect["name"], "root": [[0, 0, 0]], "effect_anchor": [[0, 0.91, 0]]}
        return scene_html("Effect preview", fps, frames, [view], False,
                          heading=effect["name"], note=effect["note"], effect=effect)

    def scene(self, params):
        get = lambda key, default="": params.get(key, [default])[0]
        asset, path, title = self.selected_member(get("asset"), get("member", "0"))
        if asset.kind != "animation":
            geometry = self.static_geometry(get("asset"), get("member", "0"))
            points = np.concatenate([part.vertices for part in geometry])
            lo, hi = points.min(axis=0), points.max(axis=0)
            scale = 1.45 / max(hi - lo)
            offset = (lo + hi) / 2
            offset[1] = lo[1]
            mesh = packed_mesh([part.moved(((part.vertices - offset) * scale)[None]) for part in geometry])
            return scene_html(title, 1, [0], [{"label": asset.game, "root": [[0, 0, 0]], "mesh": mesh}], False,
                              heading=asset.label or Path(title).stem)
        clip = get("clip")
        sequence = params.get("stage", [])
        all_clips = get("sequence") == "all"
        if (sequence or all_clips) and asset.game != "DDON":
            raise ValueError("Sequences require a DDON motion list.")
        frame_times = None
        stages = []
        outfit = {"DDON": [], "DD2": []}
        manual_armor = {"DDON": set(), "DD2": set()}
        target_skins, target_masks, source_colors = {}, {}, {}
        ddon_set = self.armor_sets.get(get("armor_set_ddon"))
        dd2_set = self.armor_sets.get(get("armor_set_dd2"))
        requested_pieces = params.get("wear", [])
        if ddon_set and ddon_set["game"] == "DDON":
            own_slots = {armor_slot(self.asset(ref.partition(":")[0]).path) for ref in requested_pieces
                         if self.asset(ref.partition(":")[0]).game == "DDON"}
            requested_pieces = [*(str(index) for index in ddon_set["refs"]
                                  if armor_slot(self.catalog[index].path) not in own_slots), *requested_pieces]
        for selection in requested_pieces:
            asset_ref, _, member = selection.partition(":")
            item, file, _ = self.selected_member(asset_ref, member or "0", {"armor", "appearance"})
            outfit[item.game].append(file)
            if item.kind == "armor":
                manual_armor[item.game].add(armor_slot(item.path))
            if item.material:
                target_skins[file] = item.material
            if item.color is not None:
                source_colors[file] = item.color
        equipped_test = get("test_armor", "true") == "true"
        armor_defaults = {game: equipped_test and game not in params.get("clear_armor", [])
                          for game in ("DDON", "DD2")}
        saved_dd2 = self.saved_dd2 if get("dd2_character", "saved") == "saved" else None
        target_body = saved_dd2["body"] if saved_dd2 else get("body", "male")
        saved_color = saved_dd2.get("skin_color") if saved_dd2 else None
        skin_color = None
        if isinstance(saved_color, list) and len(saved_color) == 3 and all(isinstance(c, int) and 0 <= c <= 255 for c in saved_color):
            skin_color = tuple(saved_color)
        skeletal = True
        pose = None
        comparison = None
        transition = None
        note = ""
        saved_weapon = None
        use_saved_set = dd2_set and dd2_set["id"] == "dd2-saved" and dd2_set["body"] == target_body
        use_training_set = dd2_set and dd2_set["id"] == "dd2-training"
        saved_armor = self.saved_dd2 if use_saved_set else saved_dd2 if armor_defaults["DD2"] and not use_training_set else None
        default_dd2_armor = (armor_defaults["DD2"] or use_training_set) and not (saved_armor and saved_armor.get("armor"))
        pieces = (saved_dd2 or {}).get("grooming", [])
        if saved_armor:
            pieces = [*pieces, *(piece for piece in saved_armor.get("armor", [])
                                 if armor_slot(piece["mesh"]) not in manual_armor["DD2"])]
        if pieces:
            if "helm" in manual_armor["DD2"] or any(armor_slot(piece["mesh"]) == "helm" for piece in pieces):
                pieces = [piece for piece in pieces if "/hair/" not in piece["mesh"]]
            for piece in pieces:
                mesh = self.dd2_file(piece["mesh"])
                if mesh not in outfit["DD2"]:
                    outfit["DD2"].append(mesh)
                if piece.get("material"):
                    target_skins[mesh] = piece["material"]
                if "parts_mask" in piece:
                    target_masks[mesh] = piece["parts_mask"]
                if piece.get("hide_tops_parts"):
                    for top in outfit["DD2"]:
                        if armor_slot(self.dd2_origins[top]) == "tops_bd":
                            target_masks[top] = target_masks.get(top, -1) & ~piece["hide_tops_parts"]
        if asset.game == "DDON":
            rig = self.selected_member(get("rig"), get("rig_member", "0"), {"model", "armor", "appearance"})[1] if get("rig") else None
            rig = self.ddon_rig(asset, title, rig)
            ddon_body = get("ddon_body", "male")
            gltf, binary = read_glb(self.ddon_animation(path, rig, ddon_body))
            animations = gltf.get("animations", [])
            if all_clips:
                sequence = list(range(len(animations)))
            if sequence and not 2 <= len(sequence) <= max(8, len(animations)):
                raise ValueError("Choose at least two clips within the motion list's sequence limit.")
            indices = [int(value) for value in (sequence or [clip])]
            if any(not 0 <= i < len(animations) for i in indices):
                raise ValueError("Unknown clip in the selected motion list.")
            available_animations = animations
            animations = [available_animations[i] for i in indices]
            fps, samples = sequence_samples(gltf, binary, animations)
            if sequence:
                duration = [animation_duration(gltf, binary, clip) for clip in animations]
                offsets = np.cumsum([0, *duration[:-1]])
                frame_times = np.concatenate([offset + times for offset, times in zip(offsets, samples)])
            count = sum(map(len, samples))
            frames = list(range(count))
            player_motion = is_ddon_player_motion(asset, title)
            player_rig = rig is None or self.ddon_origins.get(rig, ("", ""))[1].casefold() in DDON_PLAYER_MODELS.values()
            compatible = player_motion and player_rig
            saved = self.saved_ddon if compatible and get("ddon_character", "saved") == "saved" else None
            if saved:
                for archive in saved["parts"]:
                    piece = self.ddon_model_path(archive)
                    if piece not in outfit["DDON"]:
                        outfit["DDON"].append(piece)
                if saved.get("weapon") and saved.get("vocation") == asset.vocation:
                    saved_weapon = saved["weapon"]
            if compatible and armor_defaults["DDON"] and not ddon_set:
                outfit["DDON"].extend(piece for piece in self.test_armor("DDON", target_body)
                                      if armor_slot(self.ddon_origins[piece][0]) not in manual_armor["DDON"])
            fit_feet = compatible and get("feet", "true") == "true"
            source_stages = []
            for animation, times in zip(animations, samples):
                limb_targets = {}
                name = animation["name"]
                try:
                    world, index = source_pose(gltf, binary, name, times, fit_feet,
                                               fit_hands=compatible, limb_targets=limb_targets)
                except (ValueError, KeyError, np.linalg.LinAlgError):
                    if not compatible:
                        raise
                    limb_targets.clear()
                    world, index = source_pose(gltf, binary, name, times)
                    note = "Limb reconstruction was unavailable for part of this preview."
                source_stages.append((world, index, limb_targets))
            world = np.concatenate([pose for pose, _, _ in source_stages]) if sequence else source_stages[0][0]
            motion_title = "Clip sequence" if sequence else animations[0]["name"]
            label = f"{Path(title).stem} · {motion_title}"
            heading = f"{asset.label or Path(title).stem} · {motion_title}"
            if compatible:
                try:
                    joints, body, head = self.base_model(target_body)
                    if saved_dd2:
                        head = self.dd2_file(saved_dd2["head"])
                    if default_dd2_armor:
                        outfit["DD2"].extend(piece for piece in self.test_armor("DD2", target_body)
                                             if armor_slot(self.dd2_origins[piece]) not in manual_armor["DD2"])
                    motions = [
                        (retarget(gltf, binary, joints, animation["name"], sword_grip="R_PropA" in stage_index,
                                  align_arms=True, sample_times=times, solved_world=stage if fit else None,
                                  marker_joints={LIMBS[role][1][-1] for role in fit}), fit)
                        for animation, times, (stage, stage_index, fit) in zip(animations, samples, source_stages)
                    ]
                    source_sequence, target_idle = world, None
                    if get("idle", "true") == "true" and count > 1:
                        try:
                            idle = self.player_idle(title, asset, rig, fps, ddon_body)
                            if idle:
                                idle_gltf, idle_bin, idle_name, idle_times, blend = idle
                                if [n.get("name") for n in idle_gltf["nodes"]] != [n.get("name") for n in gltf["nodes"]]:
                                    raise ValueError("idle rig differs from the selected clip")
                                idle_targets = {}
                                idle_world, _ = source_pose(idle_gltf, idle_bin, idle_name, idle_times, fit_feet,
                                                            fit_hands=True, limb_targets=idle_targets)
                                idle_motion = retarget(idle_gltf, idle_bin, joints, idle_name, sword_grip="R_PropA" in index,
                                                       align_arms=True, sample_times=idle_times, solved_world=idle_world,
                                                       marker_joints={LIMBS[role][1][-1] for role in idle_targets})
                                parents = {child: i for i, node in enumerate(gltf["nodes"]) for child in node.get("children", [])}
                                source_sequence = with_idle(world, idle_world, parents, index["root"], blend)
                                target_idle = (idle_motion, idle_targets, blend)
                        except (ValueError, OSError, KeyError, IndexError, RuntimeError, struct.error,
                                np.linalg.LinAlgError, subprocess.SubprocessError):
                            pass
                        if target_idle is None:
                            note = "Idle preview is unavailable for this clip."
                    target_name = "DD2 retarget preview" + (f" · {saved_dd2['name']}" if saved_dd2 else "")
                    comparison = self.dd2_preview(get, outfit["DD2"], joints, body, head, motions,
                                                  target_name, target_idle, target_skins, target_masks, skin_color)
                    if target_idle:
                        lead = (len(source_sequence) - count) // 2
                        transition = {"start": lead, "end": lead + count - 1}
                        if frame_times is not None:
                            frame_times = np.concatenate((np.arange(lead) / fps, frame_times + lead / fps,
                                                          frame_times[-1] + (lead + np.arange(1, lead + 1)) / fps))
                        world = source_sequence
                        count = len(world)
                        frames = list(range(count))
                except (ValueError, OSError, KeyError, IndexError, RuntimeError, struct.error,
                        np.linalg.LinAlgError) as error:
                    note = f"DD2 comparison unavailable for this clip. {error}"
            else:
                note = "DD2 comparison requires a DDON player clip on the supported player rig."
            if sequence:
                start = transition["start"] if transition else 0
                for animation, times in zip(animations, samples):
                    stages.append({"name": animation["name"], "start": start, "end": start + len(times) - 1})
                    start += len(times)
            mesh_source = rig if rig is not None else self.ddon_player_models[DDON_PLAYER_MODELS[ddon_body]]
            geometry = source_mesh(gltf, binary, world, self.materials.ddon(mesh_source))
            equipment = []
            for piece in outfit["DDON"]:
                parts = self.ddon_outfit(gltf, binary, world, piece, source_colors.get(piece))
                geometry.extend(parts)
                if self.ddon_origins[piece][0].casefold().startswith("armor/"):
                    equipment.append(self.equipment_row("DDON", piece, parts, color=source_colors.get(piece)))
            source_name = f"DDON source · {saved['name']}" if saved else "DDON source"
            try:
                view = render_data(source_name, world, index)
            except KeyError:
                skeletal = False
                view = {"label": "DDON source", "root": [[0, 0, 0]] * count}
            if compatible or equipment:
                view["equipment"] = equipment
            if "R_PropA" in index:
                pose = world[:, index["R_PropA"]]
        else:
            profile = self.native(path, "sample", clip)
            frames, fps, label = profile["frames"], profile["fps"], profile["source_clip"]
            heading = label
            joints, body, head = self.base_model(target_body)
            if saved_dd2:
                head = self.dd2_file(saved_dd2["head"])
            if default_dd2_armor:
                outfit["DD2"].extend(piece for piece in self.test_armor("DD2", target_body)
                                     if armor_slot(self.dd2_origins[piece]) not in manual_armor["DD2"])
            target_name = f"DD2 character · {saved_dd2['name']}" if saved_dd2 else "DD2 character"
            view, geometry, pose = self.dd2_preview(get, outfit["DD2"], joints, body, head, [(profile, None)], target_name,
                                                    skins=target_skins, masks=target_masks, skin_color=skin_color)
            count = len(frames)
        action_start, action_end = (transition["start"], transition["end"]) if transition else (0, count - 1)
        timeline_times = np.asarray(frames) / fps if frame_times is None else frame_times

        def event_sample():
            event_time = get("hit_time")
            if event_time and frame_times is None:
                frame = action_start + round(float(event_time) * fps)
            elif event_time:
                event = timeline_times[action_start] + float(event_time)
                frame = int(np.searchsorted(timeline_times, event, side="left"))
            else:
                frame = action_start + int(get("hit", "0"))
            return max(action_start, min(action_end, frame))

        actors = [(asset.game, view, geometry, pose)]
        if comparison:
            actors.append(("DD2", *comparison))
        height = float(get("height", "1"))
        if not 0.65 <= height <= 1.45:
            raise ValueError("Height scale must be between 0.65 and 1.45.")
        linked_effects = motion_effects(self, asset.game, asset.path.casefold())
        explicit_sprite = get("sprite")
        automatic = not explicit_sprite and get("sprite_off") != "true"
        sprite = None
        orb = None
        cast = None
        sprite_anchor = get("effect_anchor", "body")
        orb_key = explicit_sprite or (linked_effects[0]["id"] if automatic and linked_effects else "")
        if orb_key.startswith("ddon-rex:"):
            if (asset.game != "DDON" or not re.fullmatch(REX_ARCHIVE, asset.path.casefold())
                    or not re.fullmatch(r"ddon-rex:[0-4]", orb_key)):
                raise ValueError("Select a Rex Elementa animation to use this effect.")
            archive = asset.path.casefold()
            element = int(orb_key[-1])
            tint = REX_ELEMENT_TINTS[element]
            # The Fire recording shows a red charge followed by an amber orb.
            charge_tint = (255, 57, 76) if element == 0 else tint
            charge_color = ",".join(map(str, charge_tint))
            rex_stages = stages or [{"name": animations[0]["name"], "start": action_start, "end": action_end}]
            windup = next((clip for clip in available_animations if clip["name"] == "Motion[120]_start"), None)
            loop_offset = float(animation_duration(gltf, binary, windup)) if windup else 0
            cast_cues, release_cues = rex_motion_cues(rex_stages, self.ddon_events(path), timeline_times, loop_offset)
            if cast_cues:
                try:
                    atlas = rex_cast_sprite(self, archive, charge_tint)
                    duration = len(atlas["cells"]) / atlas["fps"]
                    ranges = []
                    for index, (start, limit) in enumerate(cast_cues):
                        cutoff = int(np.searchsorted(timeline_times, timeline_times[start] + duration, side="left")) - 1
                        next_cast = cast_cues[index + 1][0] if index + 1 < len(cast_cues) else count
                        ranges.append([start, min(limit, cutoff, next_cast - 1)])
                    cast = {**atlas, "size": .52, "opacity": .25, "ranges": ranges, "glow": charge_color}
                    if element == 0:
                        flame = ddon_atlas(self, archive, REX_BURST_ATLASES[0], (255, 153, 68))
                        flame_ranges = []
                        for start, end in ranges:
                            if flame_ranges and timeline_times[start] - timeline_times[flame_ranges[-1][1]] <= .25:
                                flame_ranges[-1][1] = end
                            else:
                                flame_ranges.append([start, end])
                        cast["flame"] = {**flame, "size": .27, "opacity": .85, "ranges": flame_ranges}
                except SPRITE_ERRORS as error:
                    note = f"{note} Rex Elementa casting effect unavailable. {error}".strip()
            if release_cues:
                try:
                    name, layers, burst = rex_effect_assets(self, archive, orb_key)
                    cue_starts = sorted({*release_cues, *(start for start, _ in cast_cues), count})
                    release_ranges = [[start, cue_starts[cue_starts.index(start) + 1] - 1]
                                      for start in release_cues]
                    pieces, properties = [], []
                    observed = len(layers) == 3
                    layer_sizes = (.50, 1.0, .50) if observed else (1.0, .56)
                    for layer, ((mesh, rgba, tracks), size) in enumerate(zip(layers, layer_sizes, strict=True)):
                        settings = {}
                        if observed:
                            settings = (
                                {"emission": .32},
                                {"opacity_scale": .42, "emission": .24, "depth_write": True, "scale_floor": .75,
                                 "tint": tint, "cage": element == 0},
                                {"brightness": .48},
                            )[layer]
                        for part in self.static_model_geometry("DDON", mesh):
                            pieces.append(part.moved(part.vertices[None] * size))
                            properties.append({"rgba": rgba, **tracks, **settings})
                    model = packed_mesh(pieces)
                    for group, props in zip(model["groups"], properties, strict=True):
                        group["fx"] = props
                    orb = {**burst, "name": name, "mesh": model, "release": release_ranges,
                           "ranges": release_ranges, "pose_count": count, "size": 1.05}
                    if observed:
                        orb["glow"] = ",".join(map(str, tint))
                        orb["opacity"] = .42
                    if element == 0:
                        orb["cage_color"] = "#ff8a38"
                except SPRITE_ERRORS as error:
                    note = f"{note} Rex Elementa orb unavailable. {error}".strip()
            if cast or orb:
                note = f"{note} Rex Elementa uses native sprite and mesh assets with preview placement and blending.".strip()
            elif not cast_cues and not release_cues:
                note = f"{note} This clip has no mapped Rex Elementa effect cues.".strip()
        elif explicit_sprite:
            sprite = load_effect(self, explicit_sprite)
        elif automatic:
            for effect in linked_effects:
                try:
                    sprite = load_effect(self, effect["id"])
                    sprite_anchor = get("effect_anchor", effect["anchor"])
                    break
                except SPRITE_ERRORS:
                    continue
            if linked_effects and sprite is None:
                note = f"{note} The archive sprites could not load.".strip()
        scene_parts = []
        target_anchors = None
        framing = {}
        if get("mob"):
            target = self.static_geometry(get("mob"), get("mob_member", "0"))
            points = np.concatenate([part.vertices for part in target])
            lo, hi = points.min(axis=0), points.max(axis=0)
            center = (lo + hi) / 2
            center[1] = lo[1]
            offset = np.array([float(get("distance", "1.3")), 0, 0])
            if not 0.3 <= offset[0] <= 4:
                raise ValueError("Target distance must be between 0.3 and 4.")
            if get("mob_motion"):
                start_time, end_time = timeline_times[[action_start, action_end]]
                mob_times = np.clip(timeline_times, start_time, end_time) - start_time
                animated, animated_joints = self.animated_target(get, mob_times)
                scene_parts.extend(part.moved(part.vertices - center + offset) for part in animated)
            else:
                animated_joints = {}
                scene_parts.extend(part.moved(np.broadcast_to(part.vertices - center + offset,
                                                              (count, len(part.vertices), 3))) for part in target)
            framing = {"centerX": float(offset[0]) / 2, "span": max(3, float(offset[0]) + 2)}
            if get("object") or (sprite and sprite_anchor == "target"):
                anchor = offset + np.array([0, (hi[1] - lo[1]) * float(get("anchor", "0.65")), 0])
                if get("joint"):
                    joints = self.model_joints(get("mob"), get("mob_member", "0"))
                    if get("joint") not in joints:
                        raise ValueError("The selected target joint is not in this mob.")
                    anchor = offset + np.asarray(animated_joints.get(get("joint"), joints[get("joint")])) - center
                target_anchors = np.broadcast_to(anchor, (count, 3))
            if get("object"):
                hit_frame = event_sample()
                object_parts = self.static_geometry(get("object"), get("object_member", "0"))
                points = np.concatenate([part.vertices for part in object_parts])
                pivot = (points.min(axis=0) + points.max(axis=0)) / 2
                for part in object_parts:
                    placed = (part.vertices[None] - pivot) * float(get("object_scale", "0.3")) + target_anchors[:, None]
                    history = np.zeros((count, len(part.vertices), 3))
                    history[hit_frame:] = placed[hit_frame:]
                    scene_parts.append(part.moved(history))
                label += f" · attachment preview at action sample {hit_frame - action_start}"
        views = []
        effects = []
        if sprite:
            anchor = sprite_anchor
            if anchor not in {"hand", "body", "ground", "target"}:
                raise ValueError("Unknown effect placement.")
            if anchor == "target" and target_anchors is None:
                raise ValueError("Select a target model before placing an effect on it.")
            trigger = get("effect_trigger", "action")
            if trigger not in {"action", "marker"}:
                raise ValueError("Unknown effect trigger.")
            start = action_start if trigger == "action" else event_sample()
            duration = len(sprite["cells"]) / sprite["fps"]
            starts = [start]
            if stages and trigger == "action":
                starts = [stage["start"] for stage in stages]
            cutoff = float(timeline_times[starts[-1]]) + duration
            # Keep the last character pose if the selected effect outlasts recovery.
            if cutoff > timeline_times[-1]:
                extra = max(1, math.ceil((cutoff - timeline_times[-1]) * sprite["fps"]))
                timeline_times = np.r_[timeline_times, timeline_times[-1] + np.arange(1, extra + 1) / sprite["fps"]]
                frames = [*frames, *range(frames[-1] + 1, frames[-1] + extra + 1)]
                frame_times = timeline_times
            ends = [int(np.searchsorted(timeline_times, timeline_times[index] + duration, side="left")) - 1
                    for index in starts]
            ends = [min(end, starts[i + 1] - 1) if i + 1 < len(starts) else end for i, end in enumerate(ends)]
            sprite = {**sprite, "start": starts[0], "end": ends[-1], "ranges": list(zip(starts, ends)), "pose_count": count,
                      "size": {"hand": .75, "body": 1.35, "ground": 1.5, "target": 1.25}[anchor]}
            note = f"{note} {sprite['name']}. {sprite['note']}".strip()
        if get("effect") and not sprite and not orb_key.startswith("ddon-rex:"):
            effect_member = int(get("effect"))
            members = self.effects(asset)
            if not 0 <= effect_member < len(members):
                raise ValueError("Unknown FX model for this archive.")
            try:
                if not any(pose is not None for _, _, _, pose in actors):
                    raise ValueError("The selected rig has no right-hand FX attachment.")
                effects = self.static_model_geometry("DDON", members[effect_member]["path"])
                points = np.concatenate([part.vertices for part in effects])
                low, high = points.min(axis=0), points.max(axis=0)
                effect_center = (low + high) / 2
                effect_scale = 0.36 / max(0.001, max(high - low))
                effect_start = event_sample()
                if frame_times is None:
                    effect_end = effect_start + max(1, round(0.18 * fps))
                else:
                    cutoff = timeline_times[effect_start] + 0.18
                    effect_end = int(np.searchsorted(timeline_times, cutoff, side="right")) - 1
                effect_end = min(action_end, max(effect_start, effect_end))
                label += f" · FX preview at action sample {effect_start - action_start}"
            except (OSError, ValueError, KeyError, IndexError, RuntimeError, struct.error, subprocess.SubprocessError) as error:
                effects = []
                note = f"{note} FX preview unavailable. {error}".strip()
        for game, view, geometry, pose in actors:
            weapon_slot = f"weapon_{game.lower()}"
            equipped_weapon, weapon_material, weapon_color = None, None, None
            if pose is not None and game not in params.get("clear_weapon", []):
                weapon_ref = get(weapon_slot, get("weapon") if game == asset.game else "")
                if weapon_ref:
                    member = get(weapon_slot + "_member", get("weapon_member", "0"))
                    weapon_asset, equipped_weapon, _ = self.selected_member(weapon_ref, member, {"weapon"})
                    if weapon_asset.game != game:
                        raise ValueError("Select a weapon from the preview character's game.")
                    weapon_material, weapon_color = weapon_asset.material, weapon_asset.color
                elif game == "DDON" and saved_weapon:
                    equipped_weapon = self.ddon_model_path(saved_weapon)
                elif game == "DD2" and saved_dd2 and saved_dd2.get("weapon"):
                    equipped_weapon = self.dd2_file(saved_dd2["weapon"])
            if equipped_weapon:
                weapon = self.static_model_geometry(game, equipped_weapon, weapon_material, weapon_color)
                geometry.extend(part.moved(rigid_vertices(part.vertices, pose)) for part in weapon)
                view.setdefault("equipment", []).append(self.equipment_row(game, equipped_weapon, weapon, weapon=True,
                                                                          skin=weapon_material, color=weapon_color))
            if "equipment" in view:
                view["equipment"] = self.equipment_check(game, view["equipment"])
            if game == "DD2" and (cast and cast.get("flame") or orb and orb.get("cage_color")):
                view["fire_tuning"] = True
            stretch = np.array([1, height, 1])
            if height != 1:
                geometry = [replace(part, vertices=part.vertices * stretch,
                                    normals=part.normals / stretch if part.normals is not None else None)
                            for part in geometry]
                if "positions" in view:
                    view["positions"] = (np.asarray(view["positions"]) * stretch).tolist()
                    view["grip"] = (np.asarray(view["grip"]) * stretch[None, :, None]).tolist()
            if sprite:
                origins = np.array(view["root"], dtype=float)
                origins[:, 1] = .15 if anchor == "ground" else .93 * height
                if anchor == "hand" and pose is not None:
                    origins = pose[:, :3, 3] * stretch
                elif anchor == "target":
                    origins = target_anchors
                view["effect_anchor"] = origins.tolist()
            between_palms = view.pop("between_palms", None)
            if cast:
                origins = np.array(view["root"], dtype=float)
                origins[:, 1] = .93 * height
                if pose is not None:
                    origins = pose[:, :3, 3] * stretch
                if between_palms is not None:
                    origins = between_palms * stretch
                view["cast_anchor"] = origins.tolist()
            if orb:
                origins = []
                for start, _ in orb["release"]:
                    origin = np.array(view["root"][start], dtype=float)
                    origin[1] = .85 * height
                    if pose is not None:
                        origin = pose[start, :3, 3] * stretch
                        if orb.get("glow"):
                            away = origin[[0, 2]] - np.asarray(view["root"][start])[[0, 2]]
                            distance = np.linalg.norm(away)
                            if distance > 1e-6:
                                origin[[0, 2]] += .40 * away / distance
                    origins.append(origin.tolist())
                view["orb_origins"] = origins
            view.update(framing)
            attached_fx = []
            if effects and pose is not None:
                view["fx"] = {"start": effect_start, "end": effect_end, "anchor": (pose[:, :3, 3] * stretch).tolist()}
                attached_fx = [
                    part.effect(rigid_vertices((part.vertices - effect_center) * effect_scale, pose) * stretch,
                                effect_start, effect_end)
                    for part in effects
                ]
            view["mesh"] = packed_mesh([*geometry, *scene_parts, *attached_fx])
            views.append(view)
        if orb and orb.get("glow") and orb["release"][-1][1] == count - 1:
            last_release = orb["release"][-1][0]
            remaining = timeline_times[last_release] + (5 if orb.get("cage_color") else 5.25) - timeline_times[-1]
            if remaining > 0:
                extra = math.ceil(remaining * fps)
                frames += list(range(count, count + extra))
                frame_times = np.r_[timeline_times, timeline_times[-1] + np.arange(1, extra + 1) / fps]
                orb["release"][-1][1] += extra
        effect = orb or sprite
        if cast:
            effect = {**(orb or {}), "name": orb["name"] if orb else f"Rex Elementa · {REX_ELEMENTS[element]} charge",
                      "pose_count": count, "cast": cast,
                      "ranges": sorted([*cast["ranges"], *(orb["release"] if orb else [])])}
        return scene_html(label, fps, frames, views, skeletal, heading=heading, note=note, transition=transition, stages=stages,
                          times=frame_times.tolist() if frame_times is not None else None, effect=effect)


def make_handler(library):
    class Handler(BaseHTTPRequestHandler):
        def send_payload(self, payload, content_type, status=200, filename=None):
            data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if filename:
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.end_headers()
                self.wfile.write(data)
            except ConnectionError:
                self.close_connection = True

        def do_GET(self):
            uri = urlsplit(self.path)
            params = parse_qs(uri.query)
            get = lambda key, default="": params.get(key, [default])[0]
            try:
                if uri.path == "/":
                    return self.send_payload(Path(__file__).with_name("viewer_library.html").read_bytes(), "text/html; charset=utf-8")
                if uri.path == "/comparison":
                    return self.send_payload(library.comparison, "text/html; charset=utf-8")
                if uri.path == "/alchemist-example":
                    return self.send_payload(library.alchemist_example, "text/html; charset=utf-8")
                if uri.path == "/export/dd2":
                    archive = library.export_dd2(get("asset"), get("member", "0"), get("clip"), get("ddon_body", "male"))
                    return self.send_payload(archive, "application/zip", filename="DDON-Reborn-DD2-Preview.zip")
                if uri.path == "/api/catalog":
                    search = get("q").casefold().split()
                    game, kind, vocation, region = get("game"), get("kind"), get("vocation"), get("armor_region")
                    family = get("weapon_family")
                    hits = [(index, asset) for index, asset in enumerate(library.catalog)
                            if (not game or asset.game == game) and (not kind or asset.kind == kind)
                            and (not vocation or asset.vocation == vocation)
                            and (not region or library.armor_regions.get(index) == region)
                            and (not family or library.weapon_families.get(index) == family)
                            and all(word in (asset.path + " " + asset.examples).casefold() for word in search)]
                    if kind in {"armor", "weapon"}:
                        category = library.armor_regions if kind == "armor" else library.weapon_families
                        order = {row[0]: i for i, row in enumerate(ARMOR_FILTERS if kind == "armor" else WEAPON_FILTERS)}
                        hits.sort(key=lambda row: (order[category[row[0]]], not row[1].label,
                                                   row[1].label.casefold(), row[1].path))
                    page = max(0, int(get("page", "0")))
                    result = {"total": len(hits), "vocations": JOBS,
                              "armor_filters": library.armor_filters(game) if kind == "armor" else [],
                              "weapon_filters": library.weapon_filters(game) if kind == "weapon" else [],
                              "ddon_character": library.saved_ddon["name"] if library.saved_ddon else None,
                              "dd2_character": library.saved_dd2["name"] if library.saved_dd2 else None,
                              "items": [library.catalog_item(index) for index, _ in hits[page * 40:(page + 1) * 40]]}
                elif uri.path == "/api/armor-sets":
                    result = [{key: value for key, value in outfit.items() if key != "refs"}
                              for outfit in library.armor_sets.values()]
                elif uri.path == "/api/outfit":
                    result = {}
                    for key in dict.fromkeys(params.get("piece", [])[:128]):
                        if key not in library.outfit_keys:
                            continue
                        index = library.outfit_keys[key]
                        asset = library.catalog[index]
                        try:
                            members = library.members(asset)
                        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                            continue
                        result[key] = {"item": library.catalog_item(index),
                                       "members": [entry["name"] for entry in members]}
                elif uri.path == "/api/members":
                    asset = library.asset(get("asset"))
                    result = {"members": [entry["name"] for entry in library.members(asset)]}
                    if asset.game == "DD2" and asset.kind in {"armor", "weapon"}:
                        result["palettes"] = [{**variant, "key": library.outfit_key(library.catalog[variant["id"]])}
                                              for variant in library.dd2_equipment_variants.get(asset.path, [])]
                elif uri.path == "/api/clips":
                    asset, path, title = library.selected_member(get("asset"), get("member", "0"))
                    rig = library.selected_member(get("rig"), get("rig_member", "0"))[1] if get("rig") else None
                    result = library.clip_info(asset, title, path, rig, get("ddon_body", "male"))
                    if result["clips"]:
                        result["effects"] = [entry["name"] for entry in library.effects(asset)]
                        linked = motion_effects(library, asset.game, asset.path.casefold())
                        result["default_effect"] = linked[0] if linked else None
                        result["sprite_choices"] = [*linked, *(row for row in effect_catalog()
                                                               if not any(row["id"] == item["id"] for item in linked))]
                elif uri.path == "/api/joints":
                    result = {"joints": sorted(library.model_joints(get("asset"), get("member", "0")))}
                elif uri.path == "/api/skill-assets":
                    result = skill_assets(library, get("skill"), get("element"), get("variant"))
                elif uri.path == "/api/effects":
                    result = effect_catalog()
                elif uri.path == "/effect-preview":
                    return self.send_payload(library.effect_scene(get("effect")), "text/html; charset=utf-8")
                elif uri.path == "/scene":
                    return self.send_payload(library.scene(params), "text/html; charset=utf-8")
                else:
                    return self.send_payload("Unknown route", "text/plain", 404)
                self.send_payload(json.dumps(result), "application/json; charset=utf-8")
            except (ValueError, OSError, KeyError, IndexError, RuntimeError, struct.error, subprocess.SubprocessError) as error:
                self.send_payload(json.dumps({"error": str(error)}), "application/json; charset=utf-8", 422)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("ddon", "dd2", "skilltool", "revil"):
        parser.add_argument(f"--{option}", type=Path, required=True)
    for option, default in (
        ("work", "work/motion-preview"), ("fighter", "work/fighter-source"),
        ("reasy", ".local/reasy-v0.8.0"),
    ):
        parser.add_argument(f"--{option}", type=Path, default=Path(default))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--metadata", type=Path, default=Path(".local/arrowgene/Arrowgene.Ddon.Shared"))
    parser.add_argument("--python315", type=Path, default=Path(sys.executable), help="Python 3.15 with NumPy for native DD2 clips")
    parser.add_argument("--saved-characters", action="store_true", help="enable locally authored character presets")
    parser.add_argument("--index-preset-equipment", action="store_true", help="resolve equipment names from installed DDON presets")
    args = parser.parse_args()
    for name, value in vars(args).items():
        if isinstance(value, Path):
            setattr(args, name, value.resolve())
    if not args.metadata.is_dir():
        args.metadata = None
    args.work.mkdir(parents=True, exist_ok=True)
    dd2_index = args.work / "DD2_STM.list"
    if not dd2_index.is_file():
        upstream_list = args.reasy / "resources/data/lists/DD2_STM.list"
        if not upstream_list.is_file():
            parser.error(f"DD2 file list missing. Install REasy v0.8.0 or put DD2_STM.list in {args.work}.")
        shutil.copyfile(upstream_list, dd2_index)
    ddon_index = args.work / "ddon-index.txt"
    if not ddon_index.is_file():
        rom = args.ddon / "nativePC" / "rom"
        if not rom.is_dir():
            parser.error(f"DDON archive folder not found: {rom}")
        print("Indexing local DDON archives…", flush=True)
        pending = ddon_index.with_suffix(".tmp")
        with pending.open("wb") as output:
            subprocess.run([str(args.skilltool), "scan", str(rom), "lmt,mod"], stdout=output, check=True)
        if pending.stat().st_size == 0:
            parser.error("No supported DDON archives were found.")
        pending.replace(ddon_index)
    library = Library(args)
    if args.index_preset_equipment:
        print(f"{library.index_preset_equipment()} DDON model tags indexed")
        return
    with HTTPServer(("127.0.0.1", args.port), make_handler(library)) as server:
        print(f"Motion Library at http://127.0.0.1:{server.server_port} · {len(library.catalog):,} indexed entries",
              flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
