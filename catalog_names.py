"""Resolve DDON catalog labels from installed and version-matched metadata."""

from collections import defaultdict
import json
import re
import struct


JOBS = ("Fighter", "Seeker", "Hunter", "Priest", "Shield Sage", "Sorcerer", "Warrior",
        "Element Archer", "Alchemist", "Spirit Lancer", "High Scepter")
EQUIPMENT_CODE = re.compile(r"(?:wp|aa|ab|ac|ah|al|ao|lt|np|om|wb|wl)\d{6}", re.IGNORECASE)
NPC_PLACEHOLDER = re.compile(r"for\s+(?:npc|human enemy)", re.IGNORECASE)
JOB_ARCHIVE = re.compile(r"job(\d{2})(?:_cs(\d+))?(.*)\.arc$", re.IGNORECASE)


def ddon_vocation(path):
    job = JOB_ARCHIVE.match(path.rsplit("/", 1)[-1])
    if job and 1 <= int(job[1]) <= len(JOBS):
        return JOBS[int(job[1]) - 1]
    return ""


def friendly(symbol):
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", symbol)


def source(path):
    return path.read_text(encoding="utf-8-sig") if path.is_file() else ""


def enemy_names(metadata):
    enum = source(metadata / "Model" / "EnemyId.cs").partition("public enum EnemyId")[2].split("}", 1)[0]
    return {int(value, 16): friendly(name) for name, value in re.findall(r"\b(\w+)\s*=\s*0x([\da-fA-F]+)", enum)}


def skill_names(metadata):
    text = source(metadata / "Model" / "CustomSkillId.cs")
    releases = {name: int(number) for name, number in re.findall(r"\[CustomSkillId\.(\w+)\]\s*=\s*(\d+)\s*,", text)}
    names = {}
    for job, group in re.findall(r"\[Model\.JobId\.(\w+)\]\s*=\s*\[(.*?)\]", text, re.DOTALL):
        for name in re.findall(r"CustomSkillId\.(\w+)", group):
            if name in releases:
                names[job, releases[name]] = friendly(name)
    return names


def ddon_label(path, category, enemies, skills, equipment):
    filename = path.rsplit("/", 1)[-1]
    vocation = ddon_vocation(path)
    if vocation:
        job = JOB_ARCHIVE.match(filename)
        if job[2]:
            skill = skills.get((vocation.replace(" ", ""), int(job[2])), f"Skill {job[2]}")
            variant = re.search(r"_ex(\d+)", job[3], re.IGNORECASE)
            return f"{vocation} · {skill}" + (f" · EX {variant[1]}" if variant else ""), ()
        return f"{vocation} · Motion set" if category == "animation" else vocation, ()
    enemy = re.match(r"em([\da-f]{6})(?:[^.]*)\.arc$", filename, re.IGNORECASE)
    if enemy and (name := enemies.get(int(enemy[1], 16))):
        return (f"{name} · Motions" if category == "animation" else name), ()
    key = EQUIPMENT_CODE.match(filename)
    if key and category in {"weapon", "armor"}:
        names = equipment.get(key[0].casefold(), ())
        if names:
            suffix = f" (+{len(names) - 1} items)" if len(names) > 1 else ""
            return names[0] + suffix, names
    return "", ()


def equipment_names(items_path, tags_path, metadata):
    if not items_path.is_file():
        return {}
    groups = json.loads(items_path.read_text(encoding="utf-8-sig"))
    tags = json.loads(tags_path.read_text(encoding="utf-8-sig")) if tags_path.is_file() else {}
    items = (*groups["ItemEquipWeaponGroupList"], *groups["ItemEquipProtectorGroupList"])
    # Localized NPC entries and enum symbols can explicitly name the archive.
    # A model tag can also join those entries to playable item names.
    explicit = {}
    enum = source(metadata / "Model" / "ItemId.cs")
    for label, value in re.findall(r"\b((?:ForNpc|ForHumanEnemy)\w+)\s*=\s*(\d+)\s*,", enum, re.IGNORECASE):
        if model := EQUIPMENT_CODE.search(label):
            explicit[int(value)] = model[0].casefold()

    npc = groups.get("ItemEquipNpcProtectorList", ())
    evidence = defaultdict(set)
    for group in (*items, *npc):
        tag = group.get("ModelBase", {}).get("ModelTagId")
        if not tag:
            continue
        for label in group.get("ItemName", {}).values():
            evidence[tag].update(code[0].casefold() for code in EQUIPMENT_CODE.finditer(label or ""))
        entries = group.get("WeaponGroup", group.get("ArmorGroup", (group,)))
        evidence[tag].update(explicit[entry["ItemId"]] for entry in entries if entry.get("ItemId") in explicit)
    for tag, paths in evidence.items():
        if len(paths) == 1:
            tags[str(tag)] = sorted(paths)

    names = defaultdict(dict)
    for group in (*items, *npc):
        item = group.get("ItemName", {})
        label = (item.get("En") or item.get("Jp") or "").strip()
        if not label or NPC_PLACEHOLDER.match(label) or EQUIPMENT_CODE.search(label):
            continue
        base = group.get("ModelBase", {})
        keys = tags.get(str(base.get("ModelTagId")), [])
        kind = "weapon" if "WeaponGroup" in group else "armor" if "ArmorGroup" in group else ""
        # Prefer a playable name and then the item's default color for shared models.
        priority = (not kind, base.get("ColorNo", 0) != 0, label.casefold())
        for key in keys:
            key = key.casefold()
            if kind and key.startswith("wp") != (kind == "weapon"):
                continue
            names[key][label] = min(priority, names[key].get(label, priority))
    return {key: sorted(labels, key=labels.get) for key, labels in names.items()}


def preset_model_tags(items, presets):
    groups = {}
    for group in (*items["ItemEquipWeaponGroupList"], *items["ItemEquipProtectorGroupList"]):
        for item in group.get("WeaponGroup", group.get("ArmorGroup", ())):
            groups[item["ItemId"]] = group
    evidence = defaultdict(set)
    armor = ("ah", "ab", "wb", "aa", "al", "wl", "ac")
    slots = ("MAIN", "SUB", "HELM", "BODY", "BODY", "ARM", "LEG", "LEG", "ACCESSORY")
    dual_weapons = {"01": ("wp300", "wp301"), "05": ("wp303", "wp304")}
    for archive, data, members in presets:
        if len(data) != 48:
            continue
        version, count, reserved, *item_ids = struct.unpack("<12I", data)
        if (version, count, reserved) != (7, 10, 0):
            continue
        models = {code[0].casefold() for name in members
                  if (code := EQUIPMENT_CODE.match(name.rsplit("/", 1)[-1]))}
        job = re.fullmatch(r"equip_preset_job(\d{2})[.]arc", archive, re.IGNORECASE)
        weapon_prefixes = dual_weapons.get(job[1] if job else "", ("wp", "") if item_ids[1] == 0 else ("", ""))
        for item_id, prefix, slot in zip(item_ids, (*weapon_prefixes, *armor), slots):
            group = groups.get(item_id)
            if not group or not prefix or group.get("EquipSlotName", "").rsplit("_", 1)[-1] != slot:
                continue
            matches = {model for model in models if model.startswith(prefix)}
            if len(matches) == 1 and (tag := group["ModelBase"]["ModelTagId"]):
                evidence[tag].update(matches)
    return {str(tag): sorted(models) for tag, models in sorted(evidence.items()) if len(models) == 1}


def ddon_equipment_looks(items, table):
    """Resolve the item name and default color for each equipment archive."""
    version, count = struct.unpack_from("<II", table)
    if version != 11 or count > (len(table) - 8) // 65:
        raise ValueError("unsupported DDON weapon resource table")
    names = defaultdict(list)
    pos = 8
    for _ in range(count):
        tag, = struct.unpack_from("<I", table, pos)
        end = table.find(b"\0", pos + 4, pos + 260)
        if end < 0 or end + 61 > len(table):
            raise ValueError("invalid DDON weapon resource entry")
        archive = table[pos + 4:end].decode("ascii").replace("\\", "/").casefold()
        pos = end + 61
        if "/" not in archive and EQUIPMENT_CODE.match(archive):
            folder = "wp" if archive.startswith("wp") else "armor"
            names[tag].append(f"{folder}/{archive}.arc")
    if pos != len(table):
        raise ValueError("unexpected data after DDON weapon resource table")

    looks = defaultdict(dict)
    groups = (*items["ItemEquipWeaponGroupList"], *items["ItemEquipProtectorGroupList"])
    for item in groups:
        labels = item.get("ItemName", {})
        name = (labels.get("En") or labels.get("Jp") or "").strip()
        if not name or NPC_PLACEHOLDER.match(name):
            continue
        model = item["ModelBase"]
        color = model["ColorNo"]
        priority = (color != 0, name.casefold(), color)
        for archive in names[model["ModelTagId"]]:
            if archive.startswith("wp/") != ("WeaponGroup" in item):
                continue
            looks[archive][name] = min(priority, looks[archive].get(name, priority))
    return {archive: tuple((name, labels[name][2]) for name in sorted(labels, key=labels.get))
            for archive, labels in looks.items()}
