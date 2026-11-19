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


def print_svg(sch: Schematic, out_svg: Path, *, strip_diagrams: bool = True,
              extra_paintings: list[str] | None = None) -> tuple[str, list[str]]:
    """Render ``sch`` to SVG with qucs-s. Returns (svg_text, substituted_types)."""
    work = sch.copy()
    if strip_diagrams:
        work.raw_sections["Diagrams"] = []
    if extra_paintings:
        work.paintings().extend("  " + p for p in extra_paintings)
    substituted: list[str] = []
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
        substituted.append(bad)
        work.components = [
            _placeholder_for(c) if c.type == bad else c for c in work.components
        ]
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
