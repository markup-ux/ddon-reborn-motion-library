"""Resolve skill labels to local DDON archive evidence."""
from collections import deque
from hashlib import sha256
from pathlib import PurePosixPath
import re
import struct

from viewer_effects import REX_ARCHIVE, REX_ELEMENTS, rex_list


RESOURCE = re.compile(rb"effect[\\/](efl|mod|tex)[\\/][a-z0-9_./\\-]+", re.I)
CUES = {5504: "Charge", 7503: "Charge pulse", 5505: "Early release", 5501: "Orb spawn"}


def skill_assets(library, skill, element, variant):
    skill = " ".join(skill.casefold().split())
    candidates = []
    for archive in library.catalog:
        if archive.game != "DDON" or archive.kind != "animation":
            continue
        label = archive.label.casefold().split(" · ")
        if skill and (skill == archive.path.casefold() or (len(label) >= 2 and skill == label[1])):
            candidates.append(archive)
    variant = variant.casefold()
    element_index = next((i for i, name in enumerate(REX_ELEMENTS) if name.casefold() == element.casefold()), None)
    if variant in {"base", "ex 01", "ex 02"}:
        suffix = {"ex 01": "_ex01.arc", "ex 02": "_ex02.arc"}.get(variant)
        candidates = [archive for archive in candidates if (
            archive.path.casefold().endswith(suffix) if suffix else not re.search(r"_ex\d+[.]arc$", archive.path, re.I)
        )]
    # Ambiguous labels remain candidates instead of assigning one archive to gameplay.
    output = {"provenance": "Installed DDON files and offline decoding", "archives": []}
    for archive in candidates[:8]:
        motion_lists = []
        package = archive.path.casefold()
        rex = bool(re.fullmatch(REX_ARCHIVE, package))
        for member in library.members(archive):
            result = library.clip_info(archive, member["name"], member["path"], None)
            clips = []
            windup_seconds = next((clip["duration"] for clip in result["clips"] if clip["name"] == "Motion[120]_start"), 0)
            for clip in result["clips"]:
                row = {key: clip[key] for key in ("name", "duration", "events") if key in clip}
                if match := re.search(r"\[(\d+)\]", row["name"]):
                    row["source_slot"] = int(match[1])
                if rex and "events" in row:
                    offset = windup_seconds if row["name"] == "Motion[120]_loop" else 0
                    row["mapped_cues"] = [
                        {"clip_seconds": round(event["time"] - offset, 6), "code": code, "preview_meaning": CUES[code]}
                        for event in row["events"] if event["group"] == "3"
                        and 0 <= event["time"] - offset <= row["duration"]
                        for code in event["codes"] if code in CUES
                    ]
                clips.append(row)
            motion_lists.append({"member": member["name"], "sha256": file_digest(member["path"]), "clips": clips})
        roots = []
        if rex:
            roots = ["l_pl0009_45"]
            if element_index is not None:
                roots.append(rex_list(package, element_index))
        output["archives"].append({"archive": archive.path, "label": archive.label, "motion_lists": motion_lists,
                                    "effect_graph": effect_graph(library, archive.path, roots, restrict=rex)})
    return output


def file_digest(path):
    return sha256(path.read_bytes()).hexdigest()


def effect_graph(library, archive, roots, restrict=False):
    members = {PurePosixPath(row["name"]).stem.casefold(): row for row in library.ddon_members(archive, "efl")}
    queue = deque(roots if restrict else sorted(members))
    found = {}
    while queue and len(found) < 128:
        stem = queue.popleft()
        if stem in found or stem not in members:
            continue
        row = members[stem]
        data = row["path"].read_bytes()
        if len(data) < 48 or data[:4] != b"EFL\x00":
            continue
        version, _, fps, units, joints = struct.unpack_from("<IIfHH", data, 4)
        references = set()
        for ref in RESOURCE.finditer(data):
            reference = ref[0].decode("ascii").replace("\\", "/").casefold()
            kind = ref[1].decode("ascii").casefold()
            target = PurePosixPath(reference)
            # Game strings often omit the extension.
            if not target.suffix:
                suffix = ("tex" if target.name.endswith("_bm") else "ean") if kind == "tex" else kind
                target = target.with_suffix("." + suffix)
            references.add(target.as_posix())
            if target.suffix == ".efl":
                queue.append(target.stem)
        found[stem] = {"member": row["name"], "sha256": sha256(data).hexdigest(), "version": f"0x{version:08X}",
                       "base_fps": fps, "units": units, "joints": joints, "references": sorted(references)}
    return {"roots": roots, "records": list(found.values()), "archive_only": True}
