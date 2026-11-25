"""Package a locally retargeted player clip for an isolated DD2 playback test."""

from io import BytesIO
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np

from preview_legs import LIMBS
from preview_motion import source_pose
from retarget_motion import retarget


ROOT = Path(__file__).resolve().parent
RESOURCE = "natives/stm/animation/ddon_reborn/library_preview.motlist.1057"
TEMPLATE = "natives/stm/animation/ch/ch00/motlist/ch00_000_atk.motlist.1057"
PAK = "pak_mods/DDON_Reborn_Library_Preview.pak"
LUA = "reframework/autorun/ddon_reborn_library_preview.lua"
FPS = 60


def build_export(library, asset, title, clip, body, duration):
    gltf, binary, animation = clip
    name = animation["name"]
    if not 0 < duration <= 30:
        raise ValueError("Choose a DDON player clip between 0 and 30 seconds for the DD2 beta export.")
    times = np.linspace(0, round(duration * FPS), max(1, round(duration * FPS)) + 1) / FPS
    joints, _, _ = library.base_model("male")
    targets = {}
    world, indices = source_pose(gltf, binary, name, times, True, fit_hands=True, limb_targets=targets)
    profile = retarget(gltf, binary, joints, name, sword_grip="R_PropA" in indices, align_arms=True,
                       sample_times=times, solved_world=world,
                       marker_joints={LIMBS[role][1][-1] for role in targets})
    profile["fps"] = FPS
    with TemporaryDirectory(prefix="dd2-export-", dir=library.cache) as work:
        output = Path(work)
        keys = output / "keys.json"
        keys.write_text(json.dumps(profile, allow_nan=False), encoding="utf-8")
        motlist = output / Path(RESOURCE).name
        result = subprocess.run([
            str(library.args.python315), "-B", str(ROOT / "encode_probe.py"), "--reasy", str(library.args.reasy),
            "--name", "ddon_reborn_library_preview", str(keys), str(library.dd2_file(TEMPLATE)), str(motlist),
        ], capture_output=True, text=True, encoding="utf-8", timeout=120)
        if result.returncode:
            detail = next((line for line in reversed(result.stderr.splitlines()) if line.strip()), "")
            raise ValueError(f"DD2 export failed. Check Python 3.15 and REasy v0.8.0. {detail}")
        payload = motlist.read_bytes()
        pak = output / Path(PAK).name
        library.pak_tools.write_pak({RESOURCE: payload}, pak)
        if library.pak_tools.Pak(pak).read(RESOURCE) != payload:
            raise ValueError("The exported DD2 PAK failed readback.")
        runner = (ROOT / "runtime/ddon_reborn_probe.lua").read_text(encoding="utf-8").split("-- Probe runner", 1)[1]
        runner = runner.replace("DDON Reborn - Fighter playback probe", "DDON Reborn - Motion Library beta")
        resource = RESOURCE.removeprefix("natives/stm/").removesuffix(".1057").replace("/", chr(92))
        script = (
            "local motion_id = 1\nlocal probes = {{label = \"Play exported clip\", bank = 21442, "
            f"last = {profile['frames'][-1]}, resource = {json.dumps(resource)}}}}}\n"
        ) + runner
        guide = f"""# DDON Reborn Motion Library beta export

DDON source: {asset.path}, {title}, {name} ({body} source rig)
DD2 target: standard male player rig, 60 fps

Install REFramework for DD2 with PAK mod loading enabled, and install Nick's Library (_NickCore).
Close DD2. Copy the pak_mods and reframework folders into the DD2 game folder.
Replace the matching files from your previous Motion Library export if present.
Start DD2, load your save and stand idle. Open REFramework with Insert and choose
DDON Reborn - Motion Library beta > Play exported clip in 3 seconds. Close REFramework.

The clip is a manual playback test. It has no native skill binding, effects, damage or hit events.
Close DD2 before removing {PAK} and {LUA}.
"""
        archive = BytesIO()
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as package:
            package.write(pak, PAK)
            package.writestr(LUA, script)
            package.writestr("README.txt", guide)
    return archive.getvalue()
