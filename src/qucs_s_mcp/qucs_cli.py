"""Thin wrappers around the qucs-s command line (netlisting and printing)."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from .config import tools
from .schematic import Component, Schematic, Wire

_CREATE_NO_WINDOW = 0x08000000 if hasattr(subprocess, "STARTUPINFO") else 0


class QucsError(RuntimeError):
    pass


def _run(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [str(tools().qucs_s), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
    )
    return proc


def _errors(proc: subprocess.CompletedProcess) -> str:
    lines = (proc.stderr or "").splitlines() + (proc.stdout or "").splitlines()
    keep = [l for l in lines if not l.startswith("Warning:")]
    return "\n".join(keep).strip()


def netlist_file(sch_path: Path, out_path: Path) -> str:
    """Create an ngspice netlist with qucs-s. Returns the netlist text."""
    if out_path.exists():
        out_path.unlink()
    proc = _run(["-n", "--ngspice", "-i", str(sch_path), "-o", str(out_path)])
    if not out_path.exists():
        raise QucsError(f"qucs-s failed to create a netlist:\n{_errors(proc)}")
    return out_path.read_text(encoding="utf-8", errors="replace")


def netlist(sch: Schematic, workdir: Path | None = None, stem: str = "netlist") -> str:
    """Netlist an in-memory schematic (written to ``workdir`` first)."""
    with tempfile.TemporaryDirectory(prefix="qucsmcp_") as td:
        wd = Path(workdir) if workdir else Path(td)
        wd.mkdir(parents=True, exist_ok=True)
        sch_path = wd / f"{stem}.sch"
        sch_path.write_text(sch.serialize(), encoding="utf-8")
        return netlist_file(sch_path, wd / f"{stem}.cir")


# ------------------------------------------------------------------ printing
_UNKNOWN = re.compile(r"Unknown device (\S+?)\"?\s")


def _placeholder_for(comp: Component) -> Component:
    """Stand-in used when the qucs-s *print* mode cannot load a component.

    The CLI printer only knows the Qucsator component set, so SPICE-only blocks
    (SpicePar, NutmegEq, SpiceInclude, ...) are drawn as an ``Eqn`` block that
    shows the same text lines.
    """
    lines = []
    for k, (val, disp) in enumerate(comp.props):
        if isinstance(val, str) and val and str(disp) == "1":
            if "=" in val:
                lines.append([val, 1])
            elif comp.type == "NutmegEq" and k == 0:
                lines.append([f"analysis={val}", 1])
            else:
                lines.append([f"{comp.type}={val}", 1])
    if not lines:
        lines = [[f"{comp.type}=...", 1]]
    return Component(type="Eqn", name=comp.name, x=comp.x, y=comp.y, tx=comp.tx, ty=comp.ty,
                     props=lines + [["no", 0]])


_SOURCE_HINT = ("eqndef", "PWL", "Sffm", "NOISE", "RANDOM", "AmpMod", "S4Q_V", "S4Q_I", "eNL", "gNL")
_INK = "#000080"


def _symbol_paintings(comp: Component) -> list[str] | None:
    """Draw a generic symbol for a component the CLI printer cannot load.

    Uses Qucs paintings (lines, ellipse, rectangle, text) at the component's real pin
    positions, so wiring still meets the symbol and the label shows name and values.
    Returns None for pin-less blocks (handled by the Eqn stand-in instead).
    """
    from . import components as cdb  # local import: components imports this module lazily

    try:
        pins = cdb.pins(comp)
    except Exception:  # noqa: BLE001
        pins = []
    if not pins or comp.type == "GND":
        return None
    cx, cy = comp.x, comp.y
    out: list[str] = []
    is_source = len(pins) == 2 and any(h.lower() in comp.type.lower() for h in _SOURCE_HINT)
    if is_source:
        r = 15
        out.append(f"<Ellipse {cx - r} {cy - r} {2 * r} {2 * r} {_INK} 2 1 #c0c0c0 1 0>")
        for p in pins:
            dx, dy = p.x - cx, p.y - cy
            d = max((dx * dx + dy * dy) ** 0.5, 1e-9)
            if d > r:
                ex, ey = cx + dx / d * r, cy + dy / d * r
                out.append(f"<Line {p.x} {p.y} {round(ex - p.x)} {round(ey - p.y)} {_INK} 2 1>")
        is_current = comp.type in ("S4Q_I", "S4Q_Ieqndef", "gNL", "iPWL", "iTRNOISE", "IAmpMod", "I")
        if is_current:   # arrow pin1 -> pin2 inside the circle (direction of positive current)
            p1, p2 = pins[0], pins[1]
            dx, dy = p2.x - p1.x, p2.y - p1.y
            d = max((dx * dx + dy * dy) ** 0.5, 1e-9)
            ux, uy = dx / d, dy / d
            out.append(f"<Arrow {round(cx - ux * 9)} {round(cy - uy * 9)} {round(ux * 18)} {round(uy * 18)} "
                       f"7 3 {_INK} 2 1 1>")
        else:            # '+' near pin 1
            p1 = pins[0]
            dx, dy = p1.x - cx, p1.y - cy
            d = max((dx * dx + dy * dy) ** 0.5, 1e-9)
            px, py = cx + dx / d * 8 + (-dy / d) * 0, cy + dy / d * 8
            out.append(f'<Text {round(px - 4)} {round(py - 9)} 10 #ff0000 0 "+">')
            glyph = "B" if comp.type in ("src_eqndef", "eNL") else ("PWL" if "PWL" in comp.type else "~")
            out.append(f'<Text {cx - 4 * len(glyph)} {cy - 8} 10 {_INK} 0 "{glyph}">')
    else:
        xs = [p.x for p in pins]
        ys = [p.y for p in pins]
        x1, x2 = min(xs) + 10, max(xs) - 10
        y1, y2 = min(ys) + 10, max(ys) - 10
        if x2 - x1 < 24:
            x1, x2 = cx - 12, cx + 12
        if y2 - y1 < 24:
            y1, y2 = cy - 12, cy + 12
        out.append(f"<Rectangle {x1} {y1} {x2 - x1} {y2 - y1} {_INK} 2 1 #c0c0c0 1 0>")
        for p in pins:
            ex = min(max(p.x, x1), x2)
            ey = min(max(p.y, y1), y2)
            if (ex, ey) != (p.x, p.y):
                out.append(f"<Line {p.x} {p.y} {ex - p.x} {ey - p.y} {_INK} 2 1>")
        tag = comp.type.replace("_SPICE", "")[:6]
        out.append(f'<Text {round((x1 + x2) / 2 - 4 * len(tag))} {round((y1 + y2) / 2 - 8)} 9 {_INK} 0 "{tag}">')
    # name + displayed properties, where Qucs would put the component text
    meta = cdb.info(comp.type) or {}
    names = [q["name"] for q in meta.get("props", [])]
    lines = [comp.name]
    for i, (val, disp) in enumerate(comp.props):
        if str(disp) == "1" and str(val):
            n = names[i] if i < len(names) else ""
            n = "" if n.startswith(("Line_", "p")) and n[-1:].isdigit() or "_Line" in n else n
            lines.append(f"{n}={val}" if n else str(val))
    text = "\\n".join(s.replace('"', "'") for s in lines)   # Qucs Text uses a literal \n
    out.append(f'<Text {comp.x + comp.tx} {comp.y + comp.ty} 12 #000000 0 "{text}">')
    return [o for o in out if o]


def print_svg(sch: Schematic, out_svg: Path, *, strip_diagrams: bool = True,
              extra_paintings: list[str] | None = None) -> tuple[str, list[str]]:
    """Render ``sch`` to SVG with qucs-s. Returns (svg_text, substituted_types)."""
    from . import components as cdb

    work = sch.copy()
    if strip_diagrams:
        work.raw_sections["Diagrams"] = []
    if extra_paintings:
        work.paintings().extend("  " + p for p in extra_paintings)
    substituted: list[str] = []

    def substitute(pred) -> None:
        keep = []
        for c in work.components:
            if not pred(c):
                keep.append(c)
                continue
            if c.type not in substituted:
                substituted.append(c.type)
            paint = _symbol_paintings(c)
            if paint is None:
                keep.append(_placeholder_for(c))
            else:
                work.paintings().extend("  " + s for s in paint)
        work.components = keep

    # types already known to be unprintable are replaced up front (saves one qucs-s run each)
    substitute(lambda c: (cdb.info(c.type) or {}).get("print_supported") is False)
    for _attempt in range(12):
        with tempfile.TemporaryDirectory(prefix="qucsmcp_p_") as td:
            sch_path = Path(td) / "print.sch"
            sch_path.write_text(work.serialize(), encoding="utf-8")
            if out_svg.exists():
                out_svg.unlink()
            proc = _run(["-p", "-i", str(sch_path), "-o", str(out_svg)])
        if out_svg.exists():
            return out_svg.read_text(encoding="utf-8", errors="replace"), substituted
        err = _errors(proc)
        m = _UNKNOWN.search(err + " ")
        if not m:
            raise QucsError(f"qucs-s failed to print the schematic:\n{err}")
        bad = m.group(1).strip('"')
        if bad in substituted:
            raise QucsError(f"qucs-s print cannot load '{bad}':\n{err}")
        substitute(lambda c, bad=bad: c.type == bad)
    raise QucsError("qucs-s print kept failing on unknown devices: " + ", ".join(substituted))


# --------------------------------------------------------------- port probe
def probe_ports(comps: list[Component], extent: int = 160, step: int = 10,
                spacing: int = 2000, workdir: Path | None = None) -> list[list[tuple[int, int]] | None]:
    """Discover the pin coordinates of components empirically.

    Each component is placed (in its given orientation) at the centre of an
    isolated area that is filled with labelled zero-length wires on a grid.
    After netlisting, the node names on the component's netlist line tell
    which grid points are pins.  The result lists pin offsets relative to the
    component origin, in netlist (SPICE node) order.  ``None`` if not found.
    """
    sch = Schematic.new()
    labels: dict[str, tuple[int, int, int]] = {}
    for k, c in enumerate(comps):
        cx, cy = (k % 10) * spacing, (k // 10) * spacing
        placed = Component(**{**c.__dict__, "x": cx, "y": cy, "props": [list(p) for p in c.props]})
        sch.components.append(placed)
        for ix in range(-extent, extent + 1, step):
            for iy in range(-extent, extent + 1, step):
                name = f"pq{k}x{len(labels)}"
                labels[name.lower()] = (k, ix, iy)
                x, y = cx + ix, cy + iy
                sch.wires.append(Wire(x, y, x, y, name, x, y, 0, ""))
    if workdir is not None:
        # subcircuits referenced by relative path need the schematic's directory
        import uuid
        stem = f"_qucsmcp_probe_{uuid.uuid4().hex[:8]}"
        try:
            text = netlist(sch, workdir=Path(workdir), stem=stem)
        finally:
            for ext in (".sch", ".cir"):
                (Path(workdir) / f"{stem}{ext}").unlink(missing_ok=True)
    else:
        text = netlist(sch, stem="probe")
    result: list[list[tuple[int, int]] | None] = [None] * len(comps)
    found: dict[int, list[tuple[int, int]]] = {}
    for line in text.splitlines():
        if line.startswith(("*", ".")) or not line.strip():
            continue
        for tok in re.findall(r"[A-Za-z0-9_]+", line):
            tl = tok.lower()
            if tl in labels:
                k, ix, iy = labels[tl]
                lst = found.setdefault(k, [])
                if (ix, iy) not in lst:
                    lst.append((ix, iy))
    for k, pts in found.items():
        result[k] = pts
    return result
