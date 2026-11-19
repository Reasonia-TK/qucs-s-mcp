"""MCP server exposing Qucs-S schematic editing, layout tidying, snapshots and ngspice simulation."""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import components as cdb
from . import layout, plot, qucs_cli, render, simulate
from .config import tools, workspace_dir
from .schematic import Component, Schematic, Wire

INSTRUCTIONS = """\
Qucs-S schematic editor + ngspice simulator.

Coordinates: Qucs units, x to the right, y DOWN, 10-unit grid (keep everything on the grid).
Typical two-terminal parts are 60 long (pins at +-30). rotation is CCW in degrees (0/90/180/270).
Pins are addressed as "<Comp>.<pin>" e.g. "R1.1", "Q1.B", "OP1.OUT", "V1.+"; ground is a GND part.

Recommended workflow for clean, readable schematics:
 1. new_schematic / open_schematic
 2. add_components (place parts on a ~100-200 unit pitch, signal flowing left->right,
    supply at top, ground at bottom; vertical parts use rotation=90)
 3. connect (auto-routes orthogonal wires around symbols) / add_net_label for named nodes
 4. add_simulation (+ add_spice_params / add_nutmeg_equation if needed)
 5. snapshot  <- LOOK at the picture (overlays: grid+coordinates, nets, pins, issues)
 6. check_layout / tidy_schematic / move_components / reroute_wires, then snapshot again
 7. run_simulation -> get_results / plot_results -> create_report for the user
Name nodes you want to plot with add_net_label (ngspice vector v(<label>)).
"""

mcp = MCPServer("qucs-s", instructions=INSTRUCTIONS)


def tool():
    """Register a tool whose errors reach the agent with their message.

    MCP SDK v2 hides the text of unexpected exceptions ("Error executing tool X"); agents
    need the reason (unknown pin, no schematic open, bad argument...) to recover, so every
    exception is re-raised as a ToolError carrying its type and message.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ToolError:
                raise
            except KeyError as e:   # KeyError's str() adds quotes around the message
                raise ToolError(f"KeyError: {e.args[0] if e.args else e}") from e
            except Exception as e:  # noqa: BLE001
                raise ToolError(f"{type(e).__name__}: {e}") from e
        return mcp.tool()(wrapper)
    return deco


# ====================================================================== state
class _State:
    sch: Schematic | None = None

    def need(self) -> Schematic:
        if self.sch is None:
            raise RuntimeError("No schematic open. Call new_schematic or open_schematic first.")
        return self.sch

    @property
    def workdir(self) -> Path | None:
        return self.sch.path.parent if self.sch is not None and self.sch.path else None

    def save(self) -> None:
        if self.sch is not None and self.sch.path:
            self.sch.save()


STATE = _State()
LAST_SIM: dict[str, simulate.SimResult] = {}
_counter = {"snap": 0, "plot": 0}


def _snap_dir() -> Path:
    base = STATE.workdir or workspace_dir()
    d = base / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stem() -> str:
    s = STATE.need()
    return s.path.stem if s.path else "schematic"


def _next_path(kind: str, ext: str = ".png") -> Path:
    _counter[kind] += 1
    return _snap_dir() / f"{_stem()}_{kind}_{time.strftime('%H%M%S')}_{_counter[kind]:03d}{ext}"


def _comp_info(c: Component, conn: layout.Connectivity | None = None, verbose: bool = False) -> dict:
    meta = cdb.info(c.type) or {}
    names = [p["name"] for p in meta.get("props", [])]
    props = {}
    for i, (v, d) in enumerate(c.props):
        n = names[i] if i < len(names) else f"p{i + 1}"
        if verbose or str(d) == "1":
            props[n] = v
    d = {"name": c.name, "type": c.type, "x": c.x, "y": c.y, "rotation": c.rotate * 90,
         "mirror": bool(c.mirror), "props": props}
    if not c.active:
        d["active"] = False
    try:
        pins = cdb.pins(c, STATE.workdir)
    except Exception:  # noqa: BLE001
        pins = []
    if pins and c.type != "GND":
        d["pins"] = {}
        for p in pins:
            entry: Any = [p.x, p.y]
            if conn is not None:
                net = conn.nets[conn.pin_net[(p.comp, p.index)]].name
                entry = [p.x, p.y, net]
            d["pins"][p.name] = entry
    if c.type != "GND":
        d["body"] = [round(v) for v in cdb.body_rect(c, STATE.workdir)]
    return d


def _ok(**kw) -> dict:
    kw.setdefault("ok", True)
    return kw


# ================================================================ file tools
@tool()
def new_schematic(path: str, overwrite: bool = False) -> dict:
    """Create a new, empty Qucs-S schematic (.sch) and make it the current document.

    path: file path (relative paths go to the work directory). Saved automatically after every edit.
    """
    p = Path(path)
    if not p.is_absolute():
        p = workspace_dir() / p
    if p.suffix.lower() != ".sch":
        p = p.with_suffix(".sch")
    if p.exists() and not overwrite:
        raise FileExistsError(f"{p} exists. Use open_schematic or overwrite=true.")
    STATE.sch = Schematic.new()
    STATE.sch.save(p.resolve())
    return _ok(path=str(p))


@tool()
def open_schematic(path: str) -> dict:
    """Open an existing .sch file and make it the current document. Returns a summary."""
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = workspace_dir() / p
    STATE.sch = Schematic.load(p.resolve())
    return get_schematic()


@tool()
def get_schematic(verbose: bool = False) -> dict:
    """Describe the current schematic: components (position, rotation, shown properties,
    absolute pin coordinates + net), wires (with index), nets and bounding box.

    verbose=true lists every property (not only the displayed ones).
    """
    sch = STATE.need()
    conn = layout.connectivity(sch, STATE.workdir)
    comps = [_comp_info(c, conn, verbose) for c in sch.components if c.type != "GND"]
    gnds = [[c.x, c.y] for c in sch.components if c.type == "GND"]
    wires = []
    for i, w in enumerate(sch.wires):
        d: dict = {"i": i, "from": [w.x1, w.y1], "to": [w.x2, w.y2], "net": conn.nets[conn.wire_net[i]].name}
        if w.label:
            d["label"] = w.label
        wires.append(d)
    nets = []
    for n in conn.nets:
        if n.pins or n.labels:
            nets.append({"name": n.name, "pins": [p.ref for p in n.pins if not p.comp.startswith("GND@")]
                         + (["GND"] if any(p.comp.startswith("GND@") for p in n.pins) else []),
                         "labels": n.labels})
    xs, ys = [], []
    for c in sch.components:
        r = cdb.body_rect(c, STATE.workdir)
        xs += [r[0], r[2]]
        ys += [r[1], r[3]]
    for w in sch.wires:
        xs += [w.x1, w.x2]
        ys += [w.y1, w.y2]
    return {"path": str(sch.path) if sch.path else None, "components": comps, "grounds": gnds,
            "wires": wires, "nets": nets,
            "bbox": [min(xs), min(ys), max(xs), max(ys)] if xs else None}


@tool()
def save_schematic(path: str | None = None) -> dict:
    """Save the current schematic (optionally to a new path, which becomes the current file)."""
    sch = STATE.need()
    p = sch.save(Path(path) if path else None)
    return _ok(path=str(p))


@tool()
def open_in_qucs_s() -> dict:
    """Open the current schematic in the Qucs-S GUI (for the user to inspect / edit by hand).
    Re-open the file with open_schematic afterwards if the user changed it."""
    sch = STATE.need()
    STATE.save()
    subprocess.Popen([str(tools().qucs_s), str(sch.path)], close_fds=True)
    return _ok(opened=str(sch.path))


# ========================================================== component types
@tool()
def list_component_types(query: str = "", category: str = "", ngspice_only: bool = True,
                         limit: int = 60) -> list[dict]:
    """Search built-in Qucs-S component types (resistors, sources, transistors, simulations...).

    query matches type id, caption or description. Use get_component_type for full details.
    Common ids: R C L GND Vdc Vac Vpulse Vrect Idc Iac Diode _BJT _MOSFET OpAmp IProbe VProbe
    VCVS VCCS CCVS CCCS Switch .DC .AC .TR .SW .NOISE .FOURIER SpicePar NutmegEq.
    """
    out = []
    q = query.lower()
    for typ, m in cdb.db().items():
        if ngspice_only and not m["ngspice"] and not typ.startswith(".") and typ not in (
                "GND", "SpicePar", "NutmegEq", "SpiceInclude", "SpiceGlobalPar", "SpiceIC", "SpiceNodeset"):
            continue
        hay = f"{typ} {m['caption']} {m['description']} {m['category']}".lower()
        if q and q not in hay:
            continue
        if category and category.lower() not in m["category"].lower():
            continue
        out.append({"type": typ, "caption": m["caption"], "category": m["category"],
                    "pins": m["pin_names"]})
        if len(out) >= limit:
            break
    return out


@tool()
def get_component_type(type: str) -> dict:
    """Full details of a component type: properties (names, defaults, descriptions),
    pin offsets at rotation 0, symbol body box."""
    return cdb.describe_type(type)


def _library_index() -> list[dict]:
    lib_dir = tools().library_dir
    out = []
    if not lib_dir or not lib_dir.is_dir():
        return out
    for f in sorted(lib_dir.glob("*.lib")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"<Component ([^>]+)>(.*?)</Component>", text, re.S):
            body = m.group(2)
            dm = re.search(r"<Description>(.*?)</Description>", body, re.S)
            desc = " ".join((dm.group(1) if dm else "").split())[:160]
            has_spice = "<Spice>" in body or "<SpiceModel>" in body or "<ModelIncludes" in body
            out.append({"library": f.stem, "component": m.group(1).strip(), "description": desc,
                        "spice_model": has_spice})
    return out


@tool()
def search_library(query: str, limit: int = 40) -> list[dict]:
    """Search the Qucs-S component libraries (real op-amps, transistors, diodes, regulators...).
    Place a result with add_component(type="Lib", library=..., library_component=...)."""
    q = query.lower()
    hits = [e for e in _library_index()
            if q in e["component"].lower() or q in e["library"].lower() or q in e["description"].lower()]
    hits.sort(key=lambda e: (not e["spice_model"], e["library"], e["component"]))
    return hits[:limit]


# ================================================================== editing
def _place(sch: Schematic, spec: dict) -> tuple[Component, list[str]]:
    typ = spec.get("type")
    if not typ:
        raise ValueError("component spec needs 'type'")
    x, y = int(spec.get("x", 0)), int(spec.get("y", 0))
    template = None
    if str(typ).lower() == "lib":
        lib, comp = spec.get("library"), spec.get("library_component")
        if not lib or not comp:
            raise ValueError("type=Lib needs 'library' and 'library_component' (see search_library)")
        template = f'Lib X1 1 0 0 30 -26 0 0 "{lib}" 0 "{comp}" 0'
        typ = "Lib"
    elif str(typ).lower() in ("sub", "subcircuit"):
        f = spec.get("file")
        if not f:
            raise ValueError("type=Sub needs 'file' (path of the subcircuit .sch relative to this schematic)")
        template = f'Sub SUB1 1 0 0 30 -26 0 0 "{f}" 0'
        typ = "Sub"
    comp, warns = cdb.make_component(sch, typ, x, y, name=spec.get("name"),
                                     rotation=int(spec.get("rotation", 0)), mirror=bool(spec.get("mirror", False)),
                                     props=spec.get("properties") or spec.get("props"), template=template)
    if comp.type == "Lib":
        comp.name = spec.get("name") or sch.unique_name("X")
    if comp.type == "Sub":
        comp.name = spec.get("name") or sch.unique_name("SUB")
    sch.components.append(comp)
    # pleasant default label position
    if comp.type != "GND":
        try:
            r = cdb.body_rect(comp, STATE.workdir)
            w, h = layout.label_block_size(comp)
            if cdb.is_sim_block(comp):
                pass
            elif (r[3] - r[1]) > (r[2] - r[0]):        # tall symbol -> text on the right
                comp.tx, comp.ty = int(r[2] - comp.x + 8), int(-h / 2)
            else:                                       # wide symbol -> text above
                comp.tx, comp.ty = int(-w / 2), int(r[1] - comp.y - h - 4)
        except Exception:  # noqa: BLE001
            pass
    return comp, warns


@tool()
def add_components(components: list[dict]) -> dict:
    """Place one or more components. Each item:
      {"type": "R", "x": 200, "y": 100, "name": "R1"?, "rotation": 0|90|180|270, "mirror": false,
       "properties": {"R": "4.7k"}}
    type can be a Qucs id (R, C, L, GND, Vdc, Vac, Vpulse, Diode, _BJT, _MOSFET, OpAmp, ...),
    a friendly alias (resistor, npn, opamp, ...), "Lib" with library/library_component, or
    "Sub" with file. Properties are set by name (see get_component_type).
    Returns each part's absolute pin coordinates.
    """
    sch = STATE.need()
    placed, warnings = [], []
    for spec in components:
        comp, w = _place(sch, spec)
        warnings += w
        placed.append(_comp_info(comp))
    STATE.save()
    return _ok(placed=placed, warnings=warnings)


@tool()
def add_component(type: str, x: int, y: int, name: str | None = None, rotation: int = 0,
                  mirror: bool = False, properties: dict | None = None, library: str | None = None,
                  library_component: str | None = None, file: str | None = None) -> dict:
    """Place a single component (see add_components for details). Returns its pins."""
    return add_components([{"type": type, "x": x, "y": y, "name": name, "rotation": rotation,
                            "mirror": mirror, "properties": properties, "library": library,
                            "library_component": library_component, "file": file}])


@tool()
def update_component(name: str, properties: dict | None = None, new_name: str | None = None,
                     label_offset: list[int] | None = None, show_properties: dict | None = None,
                     active: bool | None = None) -> dict:
    """Change a component's properties / name / text-label offset [tx, ty] (relative to its origin)
    / which properties are displayed (show_properties={"R": true}) / active state.
    Use move_components to change position or rotation."""
    sch = STATE.need()
    c = sch.find(name)
    warns = cdb.set_props(c, properties)
    if show_properties:
        warns += cdb.set_props(c, {k: {"show": v} for k, v in show_properties.items()})
    if new_name:
        if any(o.name == new_name for o in sch.components):
            raise ValueError(f"'{new_name}' already exists")
        c.name = new_name
    if label_offset:
        c.tx, c.ty = int(label_offset[0]), int(label_offset[1])
    if active is not None:
        c.active = 1 if active else 0
    STATE.save()
    return _ok(component=_comp_info(c), warnings=warns)


@tool()
def move_components(moves: list[dict], keep_connections: bool = True) -> dict:
    """Move / rotate / mirror components. Each move: {"name": "R1", "x": .., "y": ..} or
    {"name": "R1", "dx": 50, "dy": 0}, optionally "rotation" (deg) and "mirror".

    keep_connections=true (default) rips up the wires attached to the moved parts and
    re-routes them orthogonally afterwards, so the circuit stays connected.
    """
    sch = STATE.need()
    res = layout.move_components(sch, moves, keep_connections, STATE.workdir)
    STATE.save()
    return _ok(**res)


@tool()
def delete_components(names: list[str], delete_attached_wires: bool = True) -> dict:
    """Delete components. With delete_attached_wires the wires that only served them are removed too.
    Ground symbols can be addressed as "GND@x,y"."""
    sch = STATE.need()
    removed = []
    for n in names:
        if n.upper().startswith("GND@"):
            x, y = (int(v) for v in n[4:].split(","))
            c = next(c for c in sch.components if c.type == "GND" and c.x == x and c.y == y)
        else:
            c = sch.find(n)
        pins = cdb.pins(c, STATE.workdir)
        sch.components.remove(c)
        removed.append(c.name if c.type != "GND" else n)
        if delete_attached_wires:
            _prune_dangling(sch, {(p.x, p.y) for p in pins})
    STATE.save()
    return _ok(removed=removed)


def _prune_dangling(sch: Schematic, start_pts: set) -> None:
    """Remove wire chains that end at start_pts and no longer reach any pin/junction."""
    pin_pts = {(p.x, p.y) for p in layout.all_pins(sch, STATE.workdir)}
    changed = True
    frontier = set(start_pts)
    while changed:
        changed = False
        deg: dict = {}
        for w in sch.wires:
            for pt in (w.p1, w.p2):
                deg[pt] = deg.get(pt, 0) + 1
        for i, w in enumerate(sch.wires):
            if w.label:
                continue
            for a, b in ((w.p1, w.p2), (w.p2, w.p1)):
                if a in frontier and a not in pin_pts and deg.get(a, 0) == 1:
                    del sch.wires[i]
                    frontier.add(b)
                    changed = True
                    break
            if changed:
                break


@tool()
def connect(connections: list[list]) -> dict:
    """Auto-route orthogonal wires. connections: [["R1.2", "C1.1"], ["C1.2", [300, 200]], ...]
    Endpoints are pin refs ("R1.2", "Q1.B", "OP1.OUT") or [x, y] points (a point on an
    existing wire joins that wire). The router avoids symbols and never overlaps other nets.
    """
    sch = STATE.need()
    made, failed = [], []
    for a, b in connections:
        try:
            ws = layout.connect(sch, a, b, STATE.workdir)
            made.append({"from": a, "to": b, "segments": [[w.x1, w.y1, w.x2, w.y2] for w in ws]})
        except Exception as e:  # noqa: BLE001
            failed.append({"from": a, "to": b, "error": e.args[0] if isinstance(e, KeyError) and e.args else str(e)})
    STATE.save()
    return _ok(ok=not failed, wired=made, failed=failed)


@tool()
def add_wire(points: list[list[int]], label: str | None = None) -> dict:
    """Draw a wire manually through the given points [[x1,y1],[x2,y2],...] (use orthogonal
    segments). Ends that land on the middle of an existing wire are joined properly."""
    sch = STATE.need()
    ws = layout.add_polyline(sch, [tuple(map(int, p)) for p in points], label or "")
    STATE.save()
    return _ok(segments=[[w.x1, w.y1, w.x2, w.y2] for w in ws])


@tool()
def delete_wires(indices: list[int] | None = None, net: str | None = None,
                 at: list[int] | None = None) -> dict:
    """Delete wires by index (see get_schematic), all wires of a net, or the wires touching point [x, y]."""
    sch = STATE.need()
    kill = set(indices or [])
    if net:
        conn = layout.connectivity(sch, STATE.workdir)
        n = conn.net_by_name(net)
        if n is None:
            raise KeyError(f"no net '{net}'")
        kill.update(n.wires)
    if at:
        for i, w in enumerate(sch.wires):
            if w.contains(at[0], at[1]):
                kill.add(i)
    sch.wires[:] = [w for i, w in enumerate(sch.wires) if i not in kill]
    STATE.save()
    return _ok(deleted=len(kill))


@tool()
def add_net_label(at: Any, name: str, text_offset: list[int] | None = None) -> dict:
    """Name a node. at = pin ref ("R1.2") or [x, y]. The label is attached to the wire at that
    point (or a zero-length wire stub if none). Named nodes appear as v(<name>) in results and
    equal labels connect nets without wires."""
    sch = STATE.need()
    x, y, _ = layout.resolve_point(sch, at, STATE.workdir)
    cands = [w for w in sch.wires if w.contains(x, y) and not w.is_point() and not w.label]
    if cands:
        # prefer the longest attached segment and anchor the label at its middle
        w = max(cands, key=lambda w: abs(w.x2 - w.x1) + abs(w.y2 - w.y1))
        L = abs(w.x2 - w.x1) + abs(w.y2 - w.y1)
        w.label = name
        w.loff = int(L // 2)
        ax, ay = (w.x1 + w.x2) // 2, (w.y1 + w.y2) // 2
        if text_offset:
            w.lx, w.ly = ax + int(text_offset[0]), ay + int(text_offset[1])
        else:
            w.lx, w.ly = layout.best_wire_label_spot(sch, w, ax, ay, STATE.workdir)
        STATE.save()
        return _ok(label=name, anchor=[ax, ay], on_wire=True)
    w = Wire(x, y, x, y, name, 0, 0, 0)
    sch.wires.append(w)
    if text_offset:
        w.lx, w.ly = x + int(text_offset[0]), y + int(text_offset[1])
    else:
        w.lx, w.ly = layout.best_wire_label_spot(sch, w, x, y, STATE.workdir)
    STATE.save()
    return _ok(label=name, at=[x, y], on_wire=False)


# ================================================================ simulation
_SIM_ALIASES = {"tran": ".TR", "transient": ".TR", "ac": ".AC", "dc": ".DC", "op": ".DC",
                "sweep": ".SW", "param_sweep": ".SW", "dc_sweep": ".SW", "noise": ".NOISE",
                "fourier": ".FOURIER", "disto": ".DISTO", "pz": ".PZ", "sens": ".SENS",
                "fft": ".FFT", "custom": ".CUSTOMSIM"}


def _free_spot(sch: Schematic, w: int, h: int) -> tuple[int, int]:
    """Spot below the existing drawing, scanning left to right."""
    rects = [cdb.body_rect(c, STATE.workdir) for c in sch.components]
    if not rects:
        return 0, 0
    min_x = min(r[0] for r in rects)
    blocks = [r for c, r in zip(sch.components, rects) if cdb.is_sim_block(c)]
    circuit = [r for c, r in zip(sch.components, rects) if not cdb.is_sim_block(c)] or rects
    base_y = max(r[3] for r in circuit) + 110
    x = int(min_x // 10 * 10)
    y = int(base_y // 10 * 10)
    for _ in range(40):
        cand = (x, y, x + w, y + h)
        if not any(layout.rects_overlap(cand, b, 20) for b in blocks):
            return x, y
        x += 220
    return x, y + 200


@tool()
def add_simulation(kind: str, properties: dict | None = None, x: int | None = None,
                   y: int | None = None, name: str | None = None) -> dict:
    """Add a simulation block. kind: tran | ac | dc (operating point) | sweep | noise | fourier |
    disto | pz | sens | custom, or a raw Qucs id (.TR .AC .DC .SW ...).

    Property names (case-insensitive), e.g.
      tran : Start="0", Stop="10 ms", Points="1001"  (step = (Stop-Start)/(Points-1))
      ac   : Type="log"|"lin", Start="10 Hz", Stop="1 MHz", Points="201"
      sweep: Sim="DC1" (or "TR1"/"AC1"), Param="V1" (source or SpicePar variable),
             Type="lin", Start, Stop, Points   -> DC transfer curve / parametric sweep
      noise: Type, Start, Stop, Points, Output="v(out)", Source="V1"
    Placed automatically below the circuit unless x/y given.
    """
    sch = STATE.need()
    typ = _SIM_ALIASES.get(kind.lower(), kind)
    meta = cdb.info(typ)
    if meta is None:
        raise KeyError(f"Unknown simulation kind '{kind}'")
    if x is None or y is None:
        x, y = _free_spot(sch, 200, 120)
    comp, warns = cdb.make_component(sch, typ, x, y, name=name, props=properties)
    if not name:
        comp.name = sch.unique_name(meta["prefix"] or typ.strip("."))
    sch.components.append(comp)
    STATE.save()
    return _ok(name=comp.name, type=typ, at=[comp.x, comp.y],
               properties={p["name"]: comp.props[i][0] for i, p in enumerate(meta["props"])
                           if i < len(comp.props)}, warnings=warns)


@tool()
def add_spice_params(params: dict, x: int | None = None, y: int | None = None) -> dict:
    """Add a SPICE .PARAM block, e.g. {"Rload": "1k", "Vin": "1.5"}; use {Rload} in values."""
    sch = STATE.need()
    if x is None or y is None:
        x, y = _free_spot(sch, 160, 30 + 20 * len(params))
    c = Component(type="SpicePar", name=sch.unique_name("SpicePar"), x=x, y=y, tx=-28, ty=18,
                  props=[[f"{k}={v}", 1] for k, v in params.items()])
    sch.components.append(c)
    STATE.save()
    return _ok(name=c.name)


@tool()
def add_nutmeg_equation(analysis: str, equations: dict, x: int | None = None, y: int | None = None) -> dict:
    """Add post-processing equations evaluated by ngspice after an analysis.
    analysis: ac | tran | dc ... ; equations e.g. {"gain_db": "dB(v(out)/v(in))"}.
    Results appear as extra vectors of that analysis."""
    sch = STATE.need()
    if x is None or y is None:
        x, y = _free_spot(sch, 200, 40 + 20 * len(equations))
    c = Component(type="NutmegEq", name=sch.unique_name("NutmegEq"), x=x, y=y, tx=-28, ty=18,
                  props=[[analysis, 1]] + [[f"{k}={v}", 1] for k, v in equations.items()])
    sch.components.append(c)
    STATE.save()
    return _ok(name=c.name)


@tool()
def get_netlist() -> str:
    """Return the ngspice netlist that Qucs-S generates for the current schematic."""
    sch = STATE.need()
    STATE.save()
    with __import__("tempfile").TemporaryDirectory() as td:
        return qucs_cli.netlist_file(sch.path, Path(td) / "n.cir")


@tool()
def run_simulation(timeout_s: float = 300) -> dict:
    """Netlist the schematic with Qucs-S and run all its simulations in ngspice.
    Returns per-analysis summaries (vector names, min/max/final values) plus errors."""
    sch = STATE.need()
    STATE.save()
    res = simulate.simulate(sch.path, timeout=timeout_s)
    LAST_SIM[str(sch.path)] = res
    out = {"ok": res.ok, "elapsed_s": res.elapsed_s, "analyses": [p.summary(40) for p in res.plots],
           "errors": res.errors, "warnings": res.warnings[:10], "workdir": res.workdir}
    if not res.ok:
        out["log_tail"] = res.log[-2500:]
    return out


def _last() -> simulate.SimResult:
    sch = STATE.need()
    r = LAST_SIM.get(str(sch.path))
    if r is None:
        raise RuntimeError("No results yet - call run_simulation first.")
    return r


@tool()
def get_results(analysis: str | None = None, vectors: list[str] | None = None,
                at: list[float] | None = None, max_points: int = 50) -> dict:
    """Numeric results of the last simulation.
    analysis: name like "tr1"/"ac1"/"dc1" or kind "tran"/"ac"/"op".
    vectors: e.g. ["v(out)", "i(vpr1)"] (default all). at: x positions (time/frequency/sweep value)
    at which values are interpolated (complex AC values come back as mag / dB / phase_deg);
    otherwise a decimated table of up to max_points rows (complex values as [re, im]).
    For the full-resolution data use export_results (CSV/JSON file)."""
    import numpy as np
    p = _last().find(analysis)
    names = [plot.match_vector(p, v) for v in vectors] if vectors else [k for k in p.vectors if k != p.scale]
    out: dict = {"analysis": p.name, "kind": p.kind, "scale": p.scale}
    if p.scale is None:
        out["values"] = {n: _num(p.vectors[n][0]) for n in names}
        return out
    x = np.real(p.vectors[p.scale])
    out["units"] = {n: p.units.get(n, "") for n in names}
    if at:
        out["at"] = {}
        # interpolate log sweeps in log(x) so values between points stay accurate
        logx = p.kind in ("ac", "noise") and x.size > 1 and x.min() > 0 and x.max() / x.min() > 50
        xi = np.log10(x) if logx else x
        for xv in at:
            row = {}
            xq = np.log10(xv) if logx and xv > 0 else xv
            for n in names:
                y = p.vectors[n]
                if np.iscomplexobj(y):
                    re_ = np.interp(xq, xi, y.real)
                    im_ = np.interp(xq, xi, y.imag)
                    row[n] = {"mag": _num(abs(complex(re_, im_))), "dB": _num(20 * np.log10(max(abs(complex(re_, im_)), 1e-30))),
                              "phase_deg": _num(np.degrees(np.angle(complex(re_, im_))))}
                else:
                    row[n] = _num(np.interp(xq, xi, y))
            out["at"][str(xv)] = row
        return out
    idx = np.unique(np.linspace(0, len(x) - 1, min(max_points, len(x))).astype(int))
    out["columns"] = [p.scale] + names
    rows = []
    for i in idx:
        row = [_num(x[i])]
        for n in names:
            v = p.vectors[n][i]
            row.append([_num(v.real), _num(v.imag)] if isinstance(v, complex) or np.iscomplexobj(v) and v.imag
                       else _num(np.real(v)))
        rows.append(row)
    out["rows"] = rows
    out["summary"] = {n: simulate.describe_vector(p.vectors[n]) for n in names}
    return out


@tool()
def export_results(analysis: str | None = None, vectors: list[str] | None = None, format: str = "csv",
                   complex_format: str = "real_imag", path: str | None = None) -> dict:
    """Write the full-resolution results of the last simulation to CSV or JSON files.

    analysis: "ac1" / "tr1" / "dc1" / kind ("ac", "tran", "op"); omit to export every analysis
              (one file each). A parameter-sweep base name ("tr1") exports every step.
    vectors : subset to export (default: all). The sweep variable (time/frequency/...) is
              always the first column.
    format  : "csv" (header row + one row per point) or "json" (columns -> arrays).
    complex_format (AC/noise vectors): "real_imag" (lossless, default) | "mag_phase" | "db_phase"
              -> columns like "v(out).re", "v(out).im" or "v(out).dB", "v(out).phase_deg".
    path    : output file (single analysis) or directory; default <schematic>_sim/<analysis>.<ext>.
    """
    res = _last()
    fmt = format.lower()
    if fmt not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")
    plots = res.find_all(analysis) if analysis else list(res.plots)
    base = Path(res.workdir)
    out_dir = None
    single_file = None
    if path:
        pp = Path(path)
        if not pp.is_absolute():
            pp = (STATE.workdir or workspace_dir()) / pp
        if pp.suffix.lower() in (".csv", ".json") and len(plots) == 1:
            single_file = pp
        else:
            out_dir = pp
    files = []
    for p in plots:
        names = None
        if vectors and analysis:
            names = [plot.match_vector(p, v) for v in vectors]
        elif vectors:
            # with several analyses, export only the requested vectors that exist in each
            names = []
            for v in vectors:
                try:
                    names.append(plot.match_vector(p, v))
                except KeyError:
                    pass
            if not names:
                continue
        target = single_file or (out_dir or base) / f"{_stem()}_{p.name}.{fmt}"
        files.append(simulate.export_plot(p, target, names, fmt, complex_format))
    return _ok(files=files)


def _num(v) -> float:
    return float(f"{float(v):.6g}")


@tool()
def plot_results(analysis: str | None = None, vectors: list[str] | None = None, title: str | None = None,
                 mode: str = "auto", log_x: bool | None = None) -> list:
    """Plot vectors of the last simulation and return the image (Bode plot for AC: magnitude dB +
    phase; time plot for transient; table for operating point). mode: auto | bode | real | mag.
    For a parameter sweep pass its base name (e.g. "tr1") to overlay every step in one chart,
    labelled with the swept parameter values (or "tr1_2" for a single step)."""
    plots = _last().find_all(analysis)
    out = _next_path("plot")
    if len(plots) > 1:
        info = plot.plot_family(plots, vectors, out, title=title, logx=log_x)
    else:
        info = plot.plot_vectors(plots[0], vectors, out, title=title, mode=mode, logx=log_x)
    return [json.dumps({"path": info["path"], "stats": info["stats"]}, ensure_ascii=False),
            Image(path=info["path"])]


# ============================================================ layout & view
@tool()
def snapshot(region: list[int] | None = None, focus: list[str] | None = None, nets: bool = False,
             pins: bool = False, pin_names: bool = False, issues: bool = False,
             highlight: list[str] | None = None, grid: bool = True, rulers: bool = True,
             max_px: int = 1600, title: str | None = None, include_diagrams: bool = False) -> list:
    """Render the schematic exactly as Qucs-S draws it (white background) and return the PNG.

    Overlays for reasoning about layout:
      grid/rulers : light 10/50-unit grid + coordinate labels (read positions off the image)
      nets        : colour each net and tag it with its name
      pins        : mark every pin (pin_names adds "R1.1" style tags)
      issues      : run check_layout and mark problems with numbered circles
      highlight   : list of component or net names to emphasise
    region [x1,y1,x2,y2] or focus=[names] zooms in. Set grid/rulers false for a clean picture
    to show the user. The file path is returned too (saved under <schematic dir>/snapshots).
    """
    sch = STATE.need()
    iss = None
    lint_info = None
    if issues:
        an = render.analyse(sch)
        iss, metrics = layout.lint(sch, an.label_boxes, workdir=STATE.workdir)
        lint_info = {"metrics": metrics, "issues": [dict(i.as_dict(), n=k) for k, i in enumerate(iss, 1)][:40]}
    out = _next_path("snap")
    info = render.snapshot(sch, out, region=tuple(region) if region else None, focus=focus,
                           max_px=max_px, grid=grid, rulers=rulers, pins=pins or pin_names,
                           pin_names=pin_names, nets=nets, highlight=highlight, issues=iss,
                           include_diagrams=include_diagrams, title=title, workdir=STATE.workdir)
    meta = {k: v for k, v in info.items() if k != "analysis"}
    if lint_info:
        meta["lint"] = lint_info
    return [json.dumps(meta, ensure_ascii=False), Image(path=info["path"])]


@tool()
def check_layout() -> dict:
    """Lint the schematic for electrical and visual problems:
    errors (unconnected pins, pins touching a wire middle which Qucs-S does NOT connect,
    overlapping parts), warnings (wires through symbols, overlapping wires, dangling nets) and
    style (off-grid, diagonal wires, texts overlapping symbols/wires/each other).
    Also returns wiring metrics (length, bends, crossings) to compare layouts."""
    sch = STATE.need()
    an = render.analyse(sch)
    iss, metrics = layout.lint(sch, an.label_boxes, workdir=STATE.workdir)
    return {"metrics": metrics, "issues": [i.as_dict() for i in iss]}


@tool()
def tidy_schematic(snap_to_grid: bool = True, merge_wires: bool = True,
                   connect_pins_on_wires: bool = False, place_labels: bool = True,
                   reroute_all: bool = False, arrange_simulations: bool = True) -> dict:
    """Clean up the drawing:
      snap_to_grid        : put parts and wire ends on the 10-grid
      merge_wires         : remove zero-length / duplicate wires, merge straight chains
      merge_wires also splits wires at T-junctions (a wire end on another wire's middle; Qucs-S
                            already treats these as connected) so every node is explicit
      connect_pins_on_wires : split wires where a PIN touches their middle - this CONNECTS the
                            pin (Qucs-S leaves a pin on a wire middle unconnected)
      reroute_all         : rip up ALL wires and re-route them orthogonally (labels kept)
      place_labels        : move part name/value texts to the side with free space
      arrange_simulations : line up simulation/equation blocks below the circuit
    Returns what changed plus a fresh check_layout result."""
    sch = STATE.need()
    rep: dict = {}
    if snap_to_grid:
        rep["snapped"] = layout.snap_to_grid(sch)
    if merge_wires or connect_pins_on_wires:
        rep["wires"] = layout.normalize_wires(sch, connect_pins_on_wires, STATE.workdir)
    if reroute_all:
        rep["reroute"] = layout.reroute(sch, workdir=STATE.workdir)
        rep["wires_after_reroute"] = layout.normalize_wires(sch, False, STATE.workdir)
    if arrange_simulations:
        rep["arranged_blocks"] = _arrange_blocks(sch)
    if place_labels:
        an = render.analyse(sch)
        rep["labels_moved"] = layout.place_labels(sch, an.label_sizes, STATE.workdir)
        boxes = []
        for c in sch.components:
            if c.name in an.label_sizes:
                w, h = an.label_sizes[c.name]
                boxes.append((c.x + c.tx, c.y + c.ty, c.x + c.tx + w, c.y + c.ty + h))
        rep["wire_labels_moved"] = layout.place_wire_labels(sch, STATE.workdir, boxes)
    STATE.save()
    an = render.analyse(sch)
    iss, metrics = layout.lint(sch, an.label_boxes, workdir=STATE.workdir)
    rep["metrics"] = metrics
    rep["issues"] = [i.as_dict() for i in iss][:40]
    return rep


def _arrange_blocks(sch: Schematic) -> int:
    blocks = [c for c in sch.components if cdb.is_sim_block(c)]
    circuit = [c for c in sch.components if not cdb.is_sim_block(c)]
    if not blocks or not circuit:
        return 0
    rects = [cdb.body_rect(c, STATE.workdir) for c in circuit]
    for c in circuit:
        if c.name in ("*",):
            continue
    min_x = min(r[0] for r in rects)
    max_y = max(r[3] for r in rects)
    # include circuit text labels roughly
    max_y += 40
    x = int(min_x // 10 * 10)
    y = int((max_y + 60) // 10 * 10)
    moved = 0
    order = sorted(blocks, key=lambda c: (0 if c.type.startswith(".") else 1, c.x))
    right_edge = max(max(r[2] for r in rects), min_x + 700)
    row_h = 0
    for c in order:
        r = cdb.body_rect(c, STATE.workdir)
        w = r[2] - r[0]
        _, h = layout.label_block_size(c)
        if x > min_x and x + max(w, 140) > right_edge:   # wrap to a new row
            x = int(min_x // 10 * 10)
            y += int((row_h + 60) // 10 * 10)
            row_h = 0
        row_h = max(row_h, (r[3] - r[1]) + h + 20)
        if (c.x, c.y) != (x, y):
            c.x, c.y = x, y
            moved += 1
        c.tx, c.ty = 0, int(r[3] - r[1] + 6) if c.type.startswith(".") else c.ty
        x += int(max(w, 140) // 10 * 10) + 60
    return moved


@tool()
def reroute_wires(nets: list[str] | None = None, components: list[str] | None = None) -> dict:
    """Rip up and re-route wiring orthogonally with the auto-router (labels are preserved).
    nets: net names (see get_schematic) ; components: re-route everything attached to these parts.
    Without arguments ALL wiring is re-routed."""
    sch = STATE.need()
    res = layout.reroute(sch, nets=nets, comps=components, workdir=STATE.workdir)
    layout.normalize_wires(sch, False, STATE.workdir)
    STATE.save()
    return _ok(**res, metrics=layout.wiring_metrics(sch))


@tool()
def create_report(title: str, notes: str = "", include_schematic: bool = True,
                  plots: list[dict] | None = None) -> list:
    """Compose a progress/result report image for the user: title bar, notes, a clean schematic
    snapshot and result plots. plots: [{"analysis": "ac1", "vectors": ["v(out)"]}, ...]"""
    sch = STATE.need()
    imgs = []
    if include_schematic:
        p = _next_path("snap")
        render.snapshot(sch, p, grid=False, rulers=False, workdir=STATE.workdir, max_px=1400)
        imgs.append(p)
    for spec in plots or []:
        pl = _last().find(spec.get("analysis"))
        pp = _next_path("plot")
        plot.plot_vectors(pl, spec.get("vectors"), pp, title=spec.get("title"), mode=spec.get("mode", "auto"))
        imgs.append(pp)
    if not imgs:
        raise ValueError("nothing to include")
    out = _next_path("snap").with_name(f"{_stem()}_report_{time.strftime('%H%M%S')}.png")
    plot.compose_report(imgs, out, title=title, notes=notes)
    return [json.dumps({"path": str(out)}), Image(path=str(out))]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
