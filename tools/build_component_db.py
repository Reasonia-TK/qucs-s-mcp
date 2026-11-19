"""Generate src/qucs_s_mcp/data/components.json from the installed Qucs-S.

Collects for every component type:
  * caption / description / category / property names  (qucs-s --doc)
  * a default schematic line                            (qucs-s --list-entries,
                                                         plus the bundled examples)
  * pin coordinates in SPICE node order                (empirical netlist probe)
  * symbol body bounding box                           (qucs-s --print SVG)

Run:  uv run python tools/build_component_db.py
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from qucs_s_mcp import qucs_cli, svgtools
from qucs_s_mcp.config import DATA_DIR, tools
from qucs_s_mcp.schematic import Component, Schematic

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def run_qucs(args, cwd):
    return subprocess.run([str(tools().qucs_s), *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=300)


def read_entries(tmp: Path) -> dict[str, str]:
    proc = run_qucs(["--list-entries"], tmp)
    entries: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split(";", 2)
        if len(parts) == 3 and parts[1].strip() == "qucs":
            typ = parts[0].strip()
            body = parts[2].strip()
            if typ not in entries and body.startswith("<"):
                entries[typ] = body[1:-1]
    return entries


def read_doc(tmp: Path) -> dict[str, dict]:
    run_qucs(["--doc"], tmp)
    out: dict[str, dict] = {}
    for data_csv in tmp.glob("*/*_data.csv"):
        info: dict = {}
        for line in data_csv.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#") or ";" not in line:
                continue
            k, v = line.split(";", 1)
            info[k.strip()] = v.strip().strip("`")
        ident = info.get("Identifier")
        if not ident:
            continue
        props = []
        prop_csv = data_csv.with_name(data_csv.name.replace("_data", "_prop"))
        if prop_csv.exists():
            rows = [l for l in prop_csv.read_text(encoding="utf-8", errors="replace").splitlines()
                    if l and not l.startswith("#")]
            for row in csv.reader(rows, delimiter=";", skipinitialspace=True):
                if len(row) >= 4:
                    props.append({"name": row[0].strip(), "default": row[1].strip().strip('"'),
                                  "display": row[2].strip() == "yes",
                                  "description": ";".join(row[3:]).strip().strip('"')})
        out.setdefault(ident, {
            "caption": info.get("Caption", ""),
            "description": info.get("Description", ""),
            "category": info.get("Category", data_csv.parent.name),
            "prefix": info.get("Default name", ""),
            "props": props,
        })
    return out


def example_lines() -> dict[str, str]:
    """Component lines found in the bundled examples (covers SPICE-only blocks)."""
    found: dict[str, str] = {}
    ex = tools().examples_dir
    if not ex:
        return found
    for f in ex.rglob("*.sch"):
        try:
            s = Schematic.load(f)
        except Exception:
            continue
        for c in s.components:
            if c.type not in found:
                found[c.type] = c.serialize()[1:-1]
    return found


def probe_one(typ: str, body: str) -> dict:
    res: dict = {"pins": None, "body": None, "netlist_ok": False}
    comp = Component.parse(body)
    comp.x = comp.y = 0
    comp.mirror = comp.rotate = 0
    # --- pins
    try:
        pins = qucs_cli.probe_ports([comp], extent=120, step=10)[0]
        if pins is None or _has_unresolved(comp):
            pins2 = qucs_cli.probe_ports([comp], extent=200, step=5)[0]
            if pins2 and (pins is None or len(pins2) > len(pins)):
                pins = pins2
        res["pins"] = pins
    except Exception as e:  # noqa: BLE001
        res["pin_error"] = str(e)[:200]
    # --- does ngspice netlisting produce something for it?
    try:
        s = Schematic.new()
        s.components.append(comp)
        text = qucs_cli.netlist(s, stem="nl")
        body_lines = [l for l in text.splitlines()
                      if l.strip() and not l.startswith(("*", ".INCLUDE", ".control", ".endc", ".END", "exit"))]
        res["netlist_ok"] = any(comp.name.lower() in l.lower() for l in body_lines)
        res["netlist_sample"] = body_lines[:3]
    except Exception as e:  # noqa: BLE001
        res["netlist_error"] = str(e)[:200]
    # --- symbol geometry
    try:
        s = Schematic.new()
        s.components.append(comp)
        with tempfile.TemporaryDirectory() as td:
            svg, subst = qucs_cli.print_svg(s, Path(td) / "c.svg",
                                            extra_paintings=[svgtools.calibration_painting(0, 0)])
        q = svgtools.parse(svg, (0, 0))
        if q.shapes and not subst:
            rects = [q.rect_to_sch(sh.bbox()) for sh in q.shapes]
            res["body"] = [round(min(r[0] for r in rects)), round(min(r[1] for r in rects)),
                           round(max(r[2] for r in rects)), round(max(r[3] for r in rects))]
        if subst:
            res["print_unsupported"] = True
    except Exception as e:  # noqa: BLE001
        res["print_error"] = str(e)[:200]
    return res


def _has_unresolved(comp) -> bool:
    s = Schematic.new()
    s.components.append(comp)
    text = qucs_cli.netlist(s, stem="nl")
    return bool(re.search(r"\b_net\d+\b", text))


SEMANTIC_PINS = {
    # SPICE node order -> names
    "_BJT": ["C", "B", "E"], "BJT": ["C", "B", "E", "S"],
    "NPN_SPICE": ["C", "B", "E"], "PNP_SPICE": ["C", "B", "E"], "BJT_SPICE": ["C", "B", "E", "S"],
    "_MOSFET": ["D", "G", "S"], "MOSFET": ["D", "G", "S", "B"],
    "NMOS_SPICE": ["D", "G", "S", "B"], "PMOS_SPICE": ["D", "G", "S", "B"], "MOS_SPICE": ["D", "G", "S", "B"],
    "JFET": ["D", "G", "S"], "NJF_SPICE": ["D", "G", "S"], "PJF_SPICE": ["D", "G", "S"],
    "Diode": ["A", "K"], "DIODE_SPICE": ["A", "K"],
    "Vdc": ["+", "-"], "Vac": ["+", "-"], "Vpulse": ["+", "-"], "Vrect": ["+", "-"], "Vexp": ["+", "-"],
    "Idc": ["+", "-"], "Iac": ["+", "-"], "Ipulse": ["+", "-"], "Irect": ["+", "-"],
    "OpAmp": ["OUT", "IN+", "IN-"],
}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="qucsdb_") as td:
        tmp = Path(td)
        entries = read_entries(tmp)
        doc = read_doc(tmp)
    examples = example_lines()
    print(f"entries={len(entries)} doc={len(doc)} example-types={len(examples)}")
    templates = dict(entries)
    for typ, body in examples.items():
        templates.setdefault(typ, body)
    templates.pop("Lib", None)  # library components are handled dynamically

    db: dict[str, dict] = {}
    items = sorted(templates.items())
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda kv: probe_one(*kv), items))
    for (typ, body), res in zip(items, results):
        d = doc.get(typ, {})
        comp = Component.parse(body)
        props = d.get("props") or []
        if len(props) != len(comp.props):
            # names unknown (or mismatching) -> positional names
            props = [{"name": p["name"] if i < len(props) else f"p{i + 1}",
                      "default": v, "display": str(disp) == "1",
                      "description": p.get("description", "") if i < len(props) else ""}
                     for i, ((v, disp), p) in enumerate(zip(comp.props, props + [{}] * len(comp.props)))]
        pins = res.get("pins") or []
        names = SEMANTIC_PINS.get(typ)
        pin_names = names if names and len(names) == len(pins) else [str(i + 1) for i in range(len(pins))]
        db[typ] = {
            "type": typ,
            "caption": d.get("caption", ""),
            "description": d.get("description", ""),
            "category": d.get("category", "SPICE / other" if typ in examples else ""),
            "prefix": d.get("prefix") or comp.name.rstrip("0123456789") or typ,
            "template": body,
            "props": props,
            "pins": [list(p) for p in pins],
            "pin_names": pin_names,
            "body": res.get("body"),
            "ngspice": bool(res.get("netlist_ok")),
            "print_supported": not res.get("print_unsupported", False),
        }
        if not pins and res.get("pin_error"):
            # keep it generic: raw errors contain local temp paths
            db[typ]["pin_error"] = "not supported by the qucs-s ngspice netlister"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / "components.json"
    out.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {len(db)} component types to {out}")
    print("ngspice-capable:", sum(1 for v in db.values() if v["ngspice"]))


if __name__ == "__main__":
    main()
