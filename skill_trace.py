"""Add offline asset matches and optional measured file I/O to a skill report."""
import csv
from datetime import datetime, timedelta
import json
from pathlib import PureWindowsPath
import re
from urllib.parse import urlencode
from urllib.request import urlopen


ASSET_SUFFIXES = {".arc", ".lmt", ".efl", ".ean", ".mod", ".tex", ".mrl", ".sbc", ".sew"}
ACCESS_COLUMNS = {"Time of Day", "Process Name", "PID", "Operation", "Path", "Result"}


def save_json(path, data):
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def resolve_assets(report_path):
    report = json.loads(report_path.read_text(encoding="utf-8"))
    query = urlencode({key: report[key] for key in ("skill", "element", "variant")})
    with urlopen("http://127.0.0.1:8765/api/skill-assets?" + query, timeout=55) as response:
        payload = json.load(response)
    save_json(report_path.with_suffix(".assets.json"), payload)
    return payload


def import_file_access(report_path, csv_path):
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("first_frame_observed_utc") or not report.get("recording_end_utc"):
        raise ValueError("Use a new skill recording so the clip has start and end timestamps.")
    with csv_path.open("rb") as stream:
        bom = stream.read(2)
    encoding = "utf-16" if bom in {b"\xff\xfe", b"\xfe\xff"} else "utf-8-sig"
    with csv_path.open("r", encoding=encoding, newline="") as stream:
        reader = csv.DictReader(stream)
        if not ACCESS_COLUMNS.issubset(reader.fieldnames or ()):
            raise ValueError("Export Process Monitor CSV with Time of Day, Process Name, PID, Operation, Path and Result.")
        access = file_access(report, reader)
    payload = {"source": "Process Monitor CSV", "events": access}
    save_json(report_path.with_suffix(".io.json"), payload)
    return payload


def file_access(report, rows):
    start = datetime.fromisoformat(report["first_frame_observed_utc"])
    end = datetime.fromisoformat(report["recording_end_utc"])
    local_start = start.astimezone()
    pid = report.get("client_pid")
    paths = {}
    for row in rows:
        if (row["Process Name"].casefold() != "ddo.exe" or row["Result"] != "SUCCESS"
                or row["Operation"] not in {"ReadFile", "CreateFile"} or (pid and row["PID"] != str(pid))):
            continue
        path = PureWindowsPath(row["Path"])
        parts = [part.casefold() for part in path.parts]
        rom = next((i for i in range(len(parts) - 1) if parts[i:i + 2] == ["nativepc", "rom"]), None)
        if rom is None or path.suffix.casefold() not in ASSET_SUFFIXES or ".." in parts:
            continue
        stamp = parse_trace_clock(row["Time of Day"], local_start)
        if stamp is None or not start <= stamp <= end:
            continue
        seconds = round((stamp - start).total_seconds(), 6)
        relative = "/".join(path.parts[rom + 2:])
        item = paths.setdefault(relative.casefold(), {"file": relative, "first_video_seconds": seconds,
                                "last_video_seconds": seconds, "opens": 0, "reads": 0})
        item["first_video_seconds"] = min(item["first_video_seconds"], seconds)
        item["last_video_seconds"] = max(item["last_video_seconds"], seconds)
        item["reads" if row["Operation"] == "ReadFile" else "opens"] += 1
    return sorted(paths.values(), key=lambda row: (row["first_video_seconds"], row["file"].casefold()))


def parse_trace_clock(value, local_start):
    # Process Monitor time of day may include a seventh fractional digit.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.strip())
    for fmt in ("%I:%M:%S.%f %p", "%H:%M:%S.%f", "%I:%M:%S %p", "%H:%M:%S"):
        try:
            clock = datetime.strptime(value, fmt).time()
            break
        except ValueError:
            continue
    else:
        return None
    same_day = datetime.combine(local_start.date(), clock)
    # A short capture may cross midnight or a local daylight-saving boundary.
    candidates = [(same_day + timedelta(days=day)).astimezone() for day in (-1, 0, 1)]
    return min(candidates, key=lambda stamp: abs((stamp - local_start).total_seconds()))
