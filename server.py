"""Bambu Local MCP | Windows, Python 3.10+, Bambu Lab P1S/P2S

Install Bambu Studio and put the bambu CLI v0.3.0 Windows executable on PATH
or next to this file as bambu.exe:
https://github.com/voska/bambu/releases/tag/v0.3.0

Register the server with Codex:
    codex mcp add bambu-local -- python -X utf8 "<absolute path to server.py>"

Put STL files in the prints directory next to this file, or set
BAMBU_MCP_FILES to your preferred model directory. Use slice_stl and
job_status to get a print package. Slicing needs no printer connection.
Set BAMBU_STUDIO or BAMBU_CLI if those executables are installed elsewhere.

For LAN printing, enable the printer's LAN Only / Developer Mode where
applicable. Use register_printer with its LAN address, serial and a local
nickname. Save the LAN access code in the OS keychain using the CLI. In
PowerShell 7, with the default MCP data directory:
    (Read-Host 'LAN access code' -MaskInput) | bambu auth set <nickname> --config "$env:LOCALAPPDATA/BambuLocalMCP/printers.toml"

Run printer_status and preflight before start_print. Print submission uses
an AMS slot, a 0.4 mm nozzle, Bambu PLA Basic, and a Textured PEI plate.
Only confirm a clear plate after checking the actual printer.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import msvcrt
import re
import shutil
import struct
import subprocess
import sys
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

APP = Path(__file__).resolve().parent
FILES = Path(os.environ.get("BAMBU_MCP_FILES", APP / "prints")).resolve()
STATE = Path(os.environ.get("BAMBU_MCP_HOME", Path(os.environ["LOCALAPPDATA"]) / "BambuLocalMCP")).resolve()
JOBS = STATE / "jobs"
BAMBU = os.environ.get("BAMBU_CLI") or shutil.which("bambu.exe") or str(APP / "bambu.exe")
STUDIO = Path(os.environ.get(
    "BAMBU_STUDIO", Path(os.environ["ProgramFiles"]) / "Bambu Studio" / "bambu-studio.exe"
)).resolve()
CONFIG = STATE / "printers.toml"
MODELS = ("P1S", "P2S")
PRESETS = {
    "P1S": ("0.20mm Standard @BBL X1C", "Bambu PLA Basic @BBL P1S 0.4 nozzle"),
    "P2S": ("0.20mm Standard @BBL P2S", "Bambu PLA Basic @BBL P2S"),
}
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
TRIANGLE = struct.Struct("<12fH")
CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def data(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path: Path, value: dict) -> None:
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    staging.replace(path)


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def local_file(value: str, extension: str) -> Path:
    path = Path(value)
    path = (FILES / path).resolve()
    from_slice = extension == ".gcode.3mf" and path.is_relative_to(JOBS)
    if not (from_slice or path.is_relative_to(FILES)) or not path.is_file() or not path.name.lower().endswith(extension):
        raise ValueError(f"Provide an existing {extension} inside the configured prints directory or a saved slice job.")
    return path


def cli(*args: str, timeout: int = 45) -> dict:
    STATE.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [str(BAMBU), *args, "--config", str(CONFIG), "--json", "--no-input"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
        encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        result = {"message": completed.stderr.strip()[-1200:]}
    code = completed.returncode
    if args[:2] in {("printer", "list"), ("printer", "discover")} and result.get("printers") == []:
        code = 0
    return {"exit_code": code, **result}


def binary_triangles(source: Path):
    blob = source.read_bytes()
    if len(blob) >= 84 and struct.unpack_from("<I", blob, 80)[0] * TRIANGLE.size == len(blob) - 84:
        for item in TRIANGLE.iter_unpack(memoryview(blob)[84:]):
            yield tuple(tuple(item[i:i + 3]) for i in (3, 6, 9))
        return
    vertex = re.compile(rb"(?im)^\s*vertex\s+(\S+)\s+(\S+)\s+(\S+)")
    points = [tuple(map(float, q.groups())) for q in vertex.finditer(blob)]
    if not points or len(points) % 3:
        raise ValueError("Invalid or empty STL.")
    for i in range(0, len(points), 3):
        yield tuple(points[i:i + 3])


def centered_model(source: Path, target: Path, rotate: bool, offset=(0.0, 0.0)) -> list[float]:
    indices = {}
    vertices = []
    triangles = []
    for triangle in binary_triangles(source):
        face = []
        for point in triangle:
            x, y, z = point
            point = (-y, x, z) if rotate else point
            if not all(math.isfinite(field) for field in point):
                raise ValueError("The mesh contains a non-finite coordinate.")
            if point not in indices:
                indices[point] = len(vertices)
                vertices.append(point)
            face.append(indices[point])
        triangles.append(face)
    if not triangles:
        raise ValueError("The mesh is empty.")
    minimum = [min(v[i] for v in vertices) for i in range(3)]
    maximum = [max(v[i] for v in vertices) for i in range(3)]
    size = [round(hi - lo, 3) for lo, hi in zip(minimum, maximum)]
    if min(size) <= 0 or max(size) > 256:
        raise ValueError(f"Model measures {size} mm and exceeds the supported 256 mm build volume.")
    # Keep the mesh at its design scale and hold its original Z orientation.
    shift = [-(minimum[0] + maximum[0]) / 2, -(minimum[1] + maximum[1]) / 2, -minimum[2]]
    ET.register_namespace("", CORE)
    root = ET.Element(f"{{{CORE}}}model", {"unit": "millimeter"})
    mesh = ET.SubElement(ET.SubElement(ET.SubElement(root, "resources"), "object",
                           id="1", type="model", name="Print part"), "mesh")
    nodes = ET.SubElement(mesh, "vertices")
    facets = ET.SubElement(mesh, "triangles")
    for v in vertices:
        ET.SubElement(nodes, "vertex", {axis: f"{number + offset:.9g}"
                      for axis, number, offset in zip("xyz", v, shift)})
    for face in triangles:
        ET.SubElement(facets, "triangle", {f"v{i+1}": str(index) for i, index in enumerate(face)})
    cx, cy = (128.0 + d for d in offset)
    ET.SubElement(ET.SubElement(root, "build"), "item", objectid="1",
                  transform=f"1 0 0 0 1 0 0 0 1 {cx:.5f} {cy:.5f} 0")
    types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        '</Types>'
    )
    relations = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        '</Relationships>'
    )
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("_rels/.rels", relations)
        archive.writestr("3D/3dmodel.model", ET.tostring(root, encoding="utf-8", xml_declaration=True))
    return size


def verify_package(path: Path) -> None:
    with ZipFile(path) as archive:
        payload = [name for name in archive.namelist()
                   if re.fullmatch(r"Metadata/plate_\d+\.gcode", name)]
        if len(payload) != 1 or archive.getinfo(payload[0]).file_size == 0 or archive.testzip() is not None:
            raise ValueError("The sliced package must have one valid print plate.")


def job_folder(kind: str) -> Path:
    folder = JOBS / uuid4().hex
    folder.mkdir(parents=True)
    save(folder / "state.json", {"state": "queued", "action": kind, "job_id": folder.name})
    return folder


def run_worker(folder: Path, kind: str) -> None:
    cmd = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--worker", kind, folder.name]
    with (folder / "worker.log").open("ab", buffering=0) as log:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=APP,
                         creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)


def find_job(job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Invalid job ID.")
    folder = JOBS / job_id
    if not (folder / "state.json").is_file():
        raise ValueError("Job was not found.")
    return folder


def slice_start(path: str, model: str, rotate_z: bool = False) -> dict:
    source = local_file(path, ".stl")
    if model not in MODELS:
        raise ValueError("Supported profiles are P1S and P2S.")
    folder = job_folder("slice")
    save(folder / "input.json", {"source": str(source), "sha256": digest(source),
                                 "model": model, "rotate_z": rotate_z})
    run_worker(folder, "slice")
    return data(folder / "state.json")


def studio_preset(directory: Path, name: str) -> dict:
    preset = data(directory / f"{name}.json")
    parent = preset.pop("inherits", None)
    includes = preset.pop("include", [])
    settings = studio_preset(directory, parent) if parent else {}
    for name in includes:
        template = studio_preset(directory, name)
        settings.update({key: value for key, value in template.items()
                         if key not in {"name", "type", "from"}})
    preset.pop("instantiation", None)
    return {**settings, **preset}


def write_profiles(folder: Path, model: str) -> Path:
    root = STUDIO.parent / "resources" / "profiles" / "BBL"
    if not STUDIO.is_file() or not root.is_dir():
        raise ValueError("Install Bambu Studio or set BAMBU_STUDIO to its executable.")
    process, filament = PRESETS[model]
    profiles = folder / "profiles"
    profiles.mkdir()
    for kind, name in (("machine", f"Bambu Lab {model} 0.4 nozzle"),
                       ("process", process), ("filament", filament)):
        settings = studio_preset(root / kind, name)
        if kind == "process":
            settings.update(curr_bed_type="Textured PEI Plate", wall_loops="4",
                            sparse_infill_pattern="gyroid", sparse_infill_density="15%",
                            enable_support="1", support_type="tree(auto)",
                            support_on_build_plate_only="1", brim_type="outer_only", brim_width="3")
        save(profiles / f"{kind}.json", settings)
    return profiles


def slice_work(folder: Path, values: dict) -> dict:
    source = local_file(values["source"], ".stl")
    if digest(source) != values["sha256"]:
        raise ValueError("The STL changed after the job was queued.")
    model = values["model"]
    if model not in MODELS:
        raise ValueError("Unsupported printer profile.")
    model_file = folder / "model.3mf"
    dimensions = centered_model(source, model_file, values["rotate_z"])
    profile = write_profiles(folder, model)

    def slice_once(destination: Path):
        command = [
            str(STUDIO), "--slice", "0", "--arrange", "0", "--orient", "0",
            "--load-settings", f"{profile / 'machine.json'};{profile / 'process.json'}",
            "--load-filaments", str(profile / "filament.json"),
            "--outputdir", str(destination), "--export-3mf", "print.gcode.3mf", str(model_file),
        ]
        with (destination / "studio.log").open("ab") as log:
            run = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 timeout=600, cwd=destination, creationflags=subprocess.CREATE_NO_WINDOW)
        summary = destination / "result.json"
        info = data(summary) if summary.is_file() else {}
        plates = info.get("sliced_plates", [])
        if run.returncode != 0 or len(plates) != 1 or plates[0].get("warning_message"):
            raise ValueError(info.get("error_string") or "Bambu Studio did not produce one warning-free plate.")
        package = destination / "print.gcode.3mf"
        verify_package(package)
        with ZipFile(package) as archive:
            bounds = json.loads(archive.read("Metadata/plate_1.json"))["bbox_all"]
        if len(bounds) != 4 or not all(math.isfinite(v) for v in bounds):
            raise ValueError("The sliced placement could not be verified.")
        margin = min(*bounds[:2], *(256.0 - v for v in bounds[2:]))
        return package, plates[0], bounds, margin

    package, plate, bounds, margin = slice_once(folder)
    if margin < 1.0:
        # Re-center the actual support and brim footprint, which can be offset
        # from the STL's center. Keep full scale and the chosen orientation.
        offset = tuple(128 - (bounds[i] + bounds[i+2]) / 2 for i in range(2))
        if any(abs(d) > 0.10 for d in offset):
            retry = folder / "adjusted"
            retry.mkdir()
            centered_model(source, model_file, values["rotate_z"], offset)
            try:
                adjusted = slice_once(retry)
            except (OSError, ValueError, KeyError, BadZipFile, subprocess.SubprocessError):
                if margin < 0:
                    raise
            else:
                if adjusted[3] > margin:
                    package, plate, bounds, margin = adjusted
    if margin < 0:
        raise ValueError("The sliced plate exceeds the bed. Try rotating the STL.")
    return {"state": "ready", "package": str(package), "sha256": digest(package),
            "model": model, "nozzle_mm": .4, "bed": "textured_plate", "material": "Bambu PLA Basic",
            "first_layer_bbox_mm": [round(v, 3) for v in bounds], "min_edge_mm": round(margin, 3),
            "dimensions_mm": dimensions, "seconds": round(plate["total_predication"]),
            "grams": round(sum(x["total_used_g"] for x in plate["filaments"]), 2)}


def printer_add(name: str, ip: str, serial: str, model: str) -> dict:
    addr = ipaddress.ip_address(ip)
    if addr.version != 4 or not addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified:
        raise ValueError("Use the printer's local IPv4 address.")
    if model not in MODELS or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise ValueError("Use a short printer name and P1S or P2S.")
    if not re.fullmatch(r"[A-Z0-9]{12,20}", serial):
        raise ValueError("Use the printer serial shown on its device screen.")
    return cli("printer", "add", name, "--host", ip, "--serial", serial, "--model", model,
               "--nozzle", "0.4", "--plate", "textured_plate")


def selected(name: str, package: Path) -> dict:
    devices = cli("printer", "list").get("printers", [])
    printer = next((info for info in devices if info["name"] == name), None)
    if not printer:
        raise ValueError("The requested printer has not been registered.")
    if printer["model"] not in MODELS or float(printer["nozzle"]) != .4 or printer["plate"] != "textured_plate":
        raise ValueError("The printer must use the model-specific 0.4 mm / Textured PEI profile.")
    # Bambu Studio records the exact printer in the sliced archive, even for exported files.
    with ZipFile(package) as z:
        settings = json.loads(z.read("Metadata/project_settings.config"))
    expected = f"Bambu Lab {printer['model']} 0.4 nozzle"
    if settings.get("printer_settings_id") != expected:
        raise ValueError(f"The sliced file must target {expected}.")
    return printer


def print_arguments(path: str, printer: str, ams_slot: str) -> tuple[Path, list[str]]:
    p = local_file(path, ".gcode.3mf")
    verify_package(p)
    selected(printer, p)
    if not re.fullmatch(r"[A-D][1-4]", ams_slot):
        raise ValueError("Provide the loaded PLA's AMS slot, such as A1.")
    return p, ["--printer", printer, "--slot", ams_slot, "--strict"]


def preflight(path: str, printer: str, ams_slot: str) -> dict:
    p, args = print_arguments(path, printer, ams_slot)
    return cli("print", "send", str(p), *args, "--dry-run")


def print_start(path: str, printer: str, ams_slot: str, plate_clear: bool) -> dict:
    if plate_clear is not True:
        raise ValueError("Confirm the physical build plate is clear before starting this exact print.")
    p, _ = print_arguments(path, printer, ams_slot)
    folder = job_folder("print")
    save(folder / "input.json", {"path": str(p), "sha256": digest(p), "printer": printer,
                                 "ams_slot": ams_slot})
    run_worker(folder, "print")
    return data(folder / "state.json")


def print_work(values: dict) -> dict:
    p, args = print_arguments(values["path"], values["printer"], values["ams_slot"])
    if digest(p) != values["sha256"]:
        raise ValueError("The sliced print file changed after approval.")
    key = hashlib.sha256(values["printer"].encode()).hexdigest()
    marker = JOBS / f"printer-{key}.lock"
    with marker.open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ValueError("A print submission is already running for this printer.") from None
        dry = cli("print", "send", str(p), *args, "--dry-run")
        if dry["exit_code"] != 0:
            return {"state": "preflight_failed", "details": dry}
        result = cli("print", "send", str(p), *args, "--confirm", "--wait", "90s", timeout=180)
    return {"state": "started" if result["exit_code"] == 0 else "check_printer", "details": result}


def worker(kind: str, job_id: str) -> None:
    folder = find_job(job_id)
    state = {"action": kind, "job_id": job_id, "state": "running"}
    save(folder / "state.json", state)
    try:
        values = data(folder / "input.json")
        updates = slice_work(folder, values) if kind == "slice" else print_work(values)
        state.update(updates)
    except (OSError, ValueError, KeyError, BadZipFile, subprocess.SubprocessError) as error:
        state.update(state="failed", error=str(error))
    save(folder / "state.json", state)


STRING = {"type": "string"}
MODEL = {"type": "string", "enum": MODELS}
PRINT = {"path": STRING, "printer": STRING, "ams_slot": STRING}
TOOLS = {
    "discover_printers": ("Find Bambu printers announcing themselves on the LAN.", {}, [],
                          lambda: cli("printer", "discover", "--timeout", "10s")),
    "list_printers": ("List known printer settings; no access codes are returned.", {}, [],
                      lambda: cli("printer", "list")),
    "register_printer": ("Save a LAN printer address and serial; access codes stay in Windows credential storage.",
                         {"name": STRING, "ip": STRING, "serial": STRING, "model": MODEL},
                         ["name", "ip", "serial", "model"], printer_add),
    "printer_status": ("Read print state, loaded material and errors from the named device.",
                       {"printer": STRING}, ["printer"], lambda printer: cli("status", "--printer", printer)),
    "slice_stl": ("Queue a background Bambu Studio slice from a local STL. 0.4 mm Textured PEI, PLA Basic, 0.20 mm.",
                  {"path": STRING, "model": MODEL, "rotate_z": {"type": "boolean"}}, ["path", "model"], slice_start),
    "preflight": ("Check this sliced job against the live printer and AMS spool without sending a print.",
                  PRINT, list(PRINT), preflight),
    "start_print": ("Send the exact approved print file after checking a clear plate and live device preflight.",
                    {**PRINT, "plate_clear": {"type": "boolean"}}, [*PRINT, "plate_clear"], print_start),
    "job_status": ("Read the current state of a background slice or print dispatch.",
                   {"job_id": STRING}, ["job_id"], lambda job_id: data(find_job(job_id) / "state.json")),
}


def text_result(body: Any, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "isError": error}


def rpc(message: dict) -> dict:
    name = message.get("method")
    params = message.get("params") or {}
    if name == "initialize":
        version = params.get("protocolVersion")
        return {"protocolVersion": version if version in VERSIONS else VERSIONS[0],
                "serverInfo": {"name": "bambu-local", "version": "1.0.0"},
                "capabilities": {"tools": {"listChanged": False}}}
    if name == "ping":
        return {}
    if name == "tools/list":
        return {"tools": [
            {"name": key, "description": descr, "inputSchema": {
                "type": "object", "properties": props, "required": required, "additionalProperties": False},
             "annotations": {"readOnlyHint": key in {"discover_printers", "list_printers", "printer_status",
                                                       "preflight", "job_status"}}}
            for key, (descr, props, required, _) in TOOLS.items()
        ]}
    if name == "tools/call":
        try:
            _, fields, required, fn = TOOLS[params.get("name")]
            args = params.get("arguments", {})
            if not isinstance(args, dict) or set(args) - fields.keys() or set(required) - args.keys():
                raise ValueError("Invalid tool arguments.")
            for field, value in args.items():
                datatype = fields[field]["type"]
                if not isinstance(value, str if datatype == "string" else bool):
                    raise ValueError(f"Invalid {field}.")
            result = fn(**args)
            return text_result(result, result.get("exit_code", 0) != 0)
        except (KeyError, ValueError, OSError, BadZipFile, subprocess.SubprocessError) as exc:
            return text_result({"error": str(exc)}, True)
    raise LookupError("Method not found")


def stdio() -> None:
    for line in sys.stdin.buffer:
        message = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("Expected a JSON-RPC request.")
            if "id" not in message:
                continue
            response = {"jsonrpc": "2.0", "id": message["id"], "result": rpc(message)}
        except LookupError as error:
            response = {"jsonrpc": "2.0", "id": message.get("id"),
                        "error": {"code": -32601, "message": str(error)}}
        except (ValueError, TypeError) as error:
            response = {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                        "error": {"code": -32600, "message": str(error)}}
        sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    if sys.argv[1:] in (["--help"], ["-h"]):
        print(__doc__)
    elif len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker(sys.argv[2], sys.argv[3])
    else:
        stdio()
