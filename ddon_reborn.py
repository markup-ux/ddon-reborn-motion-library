"""Read-only asset inspection and staging for DDON Reborn."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess


ARCHIVE_MAGICS = {b"ARCC", b"ARCS", b"ARC\0"}
ASSET_KINDS = {
    ".aps": "pawn_skill_parameters",
    ".abd": "ability_data",
    ".csd": "custom_skill_data",
    ".lmt": "animation",
    ".mod": "model",
    ".nsd": "normal_skill_data",
    ".tex": "texture",
    ".gmd": "message_table",
    ".sngw": "audio",
    ".spc": "audio_candidate",
}


def inspect_archive(path):
    with path.open("rb") as source:
        header = source.read(8)
        size = os.fstat(source.fileno()).st_size
    magic = header[:4]
    recognized = len(header) == 8 and magic in ARCHIVE_MAGICS
    return {
        "magic_hex": magic.hex(),
        "format": magic.rstrip(b"\0").decode("ascii") if recognized else None,
        "version": struct.unpack_from("<H", header, 4)[0] if recognized else None,
        "entry_count": struct.unpack_from("<H", header, 6)[0]
        if recognized and magic != b"ARCS" else None,
        "size": size,
        "status": "unrecognized" if not recognized else "identified",
    }


def files_under(root):
    def fail(error):
        raise error

    for directory, dirs, files in os.walk(root, onerror=fail):
        parent = Path(directory)
        dirs[:] = sorted(name for name in dirs if (parent / name).resolve() == parent / name)
        for name in sorted(files):
            path = parent / name
            if not path.is_symlink() and path.is_file():
                yield path


def inventory(root):
    return {
        "schema_version": 1,
        "archives": [
            {"source_path": path.relative_to(root).as_posix(), **inspect_archive(path)}
            for path in files_under(root)
            if path.suffix.lower() == ".arc"
        ],
    }


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract(client, archive, extension, extractor, output):
    version_file = client / "dlinfo" / "versionlist"
    if not version_file.is_file():
        raise ValueError("client must contain dlinfo/versionlist")
    rom = (client / "nativePC" / "rom").resolve(strict=True)
    if not rom.is_relative_to(client):
        raise ValueError("nativePC/rom must resolve inside the client")
    if archive.anchor or ".." in archive.parts:
        raise ValueError("archive must be a relative path inside nativePC/rom")
    source = (rom / archive).resolve(strict=True)
    if not source.is_relative_to(rom) or source.suffix.lower() != ".arc":
        raise ValueError("archive must be an .arc path inside the client's nativePC/rom")
    if output.is_relative_to(client):
        raise ValueError("output must be outside the DDON client")
    if not extractor.is_file() or extractor.suffix.lower() != ".exe":
        raise ValueError("provide the Windows skilltool .exe")
    extension = extension.removeprefix(".").lower()
    if not re.fullmatch(r"[a-z0-9_]+", extension):
        raise ValueError("extension must contain only letters, digits or underscores")

    metadata = inspect_archive(source)
    if (metadata["format"], metadata["version"]) != ("ARCC", 7):
        raise ValueError("this extraction workflow requires an ARCC version 7 archive")

    relative_source = source.relative_to(rom)
    output.mkdir(parents=True, exist_ok=False)
    staged_archive = output / "input" / relative_source
    staged_archive.parent.mkdir(parents=True)
    shutil.copyfile(source, staged_archive)
    client_build = version_file.read_text(encoding="utf-8-sig").strip()
    archive_hash = sha256(staged_archive)
    log_path = output / "extractor.log"
    with log_path.open("wb") as log:
        subprocess.run(
            [str(extractor), "dump", str(staged_archive), extension],
            cwd=output,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )

    assets = []
    seen = set()
    dump_root = output / "dump"
    for line in log_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"  (.+)  size=(\d+)  -> (.+)", line)
        if not match:
            continue
        member, size, relative_output = match.groups()
        path = (output / relative_output).resolve(strict=True)
        if path.parent != dump_root or path in seen or path.stat().st_size != int(size):
            raise ValueError(f"inconsistent extractor output. See {log_path}")
        seen.add(path)
        suffix = path.suffix.lower()
        assets.append({
            "member_path": member.replace("\\", "/"),
            "output_path": path.relative_to(output).as_posix(),
            "extension": suffix or None,
            "kind": ASSET_KINDS.get(suffix, "unclassified"),
            "size": path.stat().st_size,
            "sha256": sha256(path),
            "status": "extracted",
        })
    if not assets:
        raise ValueError(f"extractor reported no matching files. See {log_path}")
    if set(files_under(dump_root)) != seen:
        raise ValueError(f"extractor output disagrees with its log. See {log_path}")
    manifest = {
        "schema_version": 1,
        "client_build": client_build,
        "archive": {
            **metadata,
            "source_path": source.relative_to(client).as_posix(),
            "sha256": archive_hash,
        },
        "outputs": assets,
    }
    with (output / "manifest.json").open("x", encoding="utf-8") as destination:
        json.dump(manifest, destination, indent=2, ensure_ascii=True)
        destination.write("\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="identify one archive header")
    inspect.add_argument("archive", type=Path)
    scan = commands.add_parser("scan", help="inventory .arc files under a directory")
    scan.add_argument("root", type=Path)
    unpack = commands.add_parser("extract", help="stage and unpack one DDON ARCC v7 archive")
    unpack.add_argument("--client", type=Path, required=True)
    unpack.add_argument("--archive", type=Path, required=True, help="path relative to nativePC/rom")
    unpack.add_argument("--extension", required=True, help="archive member extension, such as .lmt")
    unpack.add_argument("--extractor", type=Path, required=True, help="path to the Windows skilltool executable")
    unpack.add_argument("--output", type=Path, required=True, help="new directory outside the client")
    args = parser.parse_args()

    try:
        if args.command == "inspect":
            result = inspect_archive(args.archive)
        elif args.command == "scan":
            root = args.root.resolve(strict=True)
            if not root.is_dir():
                raise ValueError("scan root must be a directory")
            result = inventory(root)
        else:
            output = args.output.resolve()
            result = extract(
                args.client.resolve(strict=True), args.archive, args.extension,
                args.extractor.resolve(strict=True), output,
            )
    except subprocess.CalledProcessError as error:
        parser.exit(2, f"error: extractor exited with code {error.returncode}. See {output / 'extractor.log'}\n")
    except (OSError, ValueError) as error:
        parser.exit(2, f"error: {error}\n")
    print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
