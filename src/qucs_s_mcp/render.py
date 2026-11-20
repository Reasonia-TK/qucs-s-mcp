"""Schematic snapshots: Qucs-S' own drawing + analysis overlays, rasterised with resvg."""

from __future__ import annotations

import html
import math
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import resvg_py

from . import components as cdb
from . import layout, qucs_cli, svgtools
from .schematic import Schematic

NET_COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4", "#f032e6",
              "#9a6324", "#469990", "#800000", "#808000", "#000075", "#bfef45", "#fabed4"]
SEVERITY_COLOR = {"error": "#d00000", "warning": "#e07000", "style": "#7a3db8"}


@dataclass
class Analysis:
    """Geometry measured from an actual qucs-s rendering."""
    q: svgtools.QucsSvg
    substituted: list[str]
    content_bbox: tuple
    label_boxes: dict = field(default_factory=dict)   # component name -> text block rect
    label_sizes: dict = field(default_factory=dict)   # component name -> (w, h)


def _calib_point(sch: Schematic) -> tuple[int, int]:
    if sch.components:
        return sch.components[0].x, sch.components[0].y
    if sch.wires:
        return sch.wires[0].x1, sch.wires[0].y1
    return 0, 0


def analyse(sch: Schematic, include_diagrams: bool = False) -> Analysis:
    cal = _calib_point(sch)
    with tempfile.TemporaryDirectory(prefix="qucsmcp_r_") as td:
        svg, subst = qucs_cli.print_svg(sch, Path(td) / "s.svg", strip_diagrams=not include_diagrams,
                                        extra_paintings=[svgtools.calibration_painting(*cal)])
    q = svgtools.parse(svg, cal)
    _fix_placeholder_captions(q, sch, subst)
    _drop_open_end_marks(q, sch, subst)
    bbox = q.content_bbox_sch() or (0, 0, 100, 100)
    an = Analysis(q=q, substituted=subst, content_bbox=bbox)
    an.label_boxes = measure_label_boxes(q, sch)
    an.label_sizes = {k: (r[2] - r[0], r[3] - r[1]) for k, r in an.label_boxes.items()}
    return an


def _fix_placeholder_captions(q: svgtools.QucsSvg, sch: Schematic, subst: list[str]) -> None:
    """Re-title the Eqn stand-ins used for SPICE-only blocks (e.g. '.PARAM', 'Nutmeg')."""
    if not subst:
        return
    titles = {"SpicePar": ".PARAM", "SpiceGlobalPar": ".GLOBAL_PARAM", "NutmegEq": "Nutmeg",
              "SpiceInclude": ".INCLUDE", "SpiceLib": ".LIB", "SpiceOptions": ".OPTIONS",
              "SpiceIC": ".IC", "SpiceNodeset": ".NODESET", "SpiceFunc": ".FUNC", "SPICEINIT": ".spiceinit"}
    # only pin-less blocks were replaced by an Eqn box; parts with pins got a drawn symbol
    targets = [(c, titles.get(c.type, c.type)) for c in sch.components
               if c.type in subst and qucs_cli._symbol_paintings(c) is None]
    if not targets:
        return
    # find <text> elements nearest to each component origin
    import xml.etree.ElementTree as ET  # noqa: F401
    candidates = []

    def walk(el, m):
        tag = el.tag.split("}")[-1]
        if tag in ("g", "svg"):
            mm = svgtools._mul(m, svgtools._matrix(el.get("transform")))
            for ch in el:
                walk(ch, mm)
        elif tag == "text":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            px, py = svgtools._apply(m, x, y)
            candidates.append((el, q.to_sch(px, py)))

    walk(q.root, (1, 0, 0, 1, 0, 0))
    for c, title in targets:
        best, bd = None, 40.0
        for el, (sx, sy) in candidates:
            d = math.hypot(sx - c.x, sy - c.y)
            if d < bd:
                best, bd = el, d
        if best is not None:
            best.text = title


def _drop_open_end_marks(q: svgtools.QucsSvg, sch: Schematic, subst: list[str]) -> None:
    """Remove the red 'open wire end' circles Qucs draws at the pins of parts that were
    replaced by drawn symbols (the wires do connect to those parts in the real schematic)."""
    if not subst:
        return
    pts = []
    for c in sch.components:
        if c.type in subst:
            try:
                pts += [(p.x, p.y) for p in cdb.pins(c)]
            except Exception:  # noqa: BLE001
                pass
    if not pts:
        return
    doomed = []

    def walk(el, m, parent):
        tag = el.tag.split("}")[-1]
        if tag in ("g", "svg"):
            mm = svgtools._mul(m, svgtools._matrix(el.get("transform")))
            for ch in list(el):
                walk(ch, mm, el)
            return
        if tag not in ("circle", "ellipse", "rect"):
            return
        stroke = (el.get("stroke") or (parent.get("stroke") if parent is not None else "") or "").lower()
        if stroke not in ("#ff0000", "red"):
            return
        if tag == "rect":
            cx = float(el.get("x", 0)) + float(el.get("width", 0)) / 2
            cy = float(el.get("y", 0)) + float(el.get("height", 0)) / 2
        else:
            cx, cy = float(el.get("cx", 0)), float(el.get("cy", 0))
        sx, sy = q.to_sch(*svgtools._apply(m, cx, cy))
        if any(abs(sx - x) < 3 and abs(sy - y) < 3 for x, y in pts):
            doomed.append((parent, el))

    walk(q.root, (1, 0, 0, 1, 0, 0), None)
    for parent, el in doomed:
        if parent is not None:
            parent.remove(el)


def measure_label_boxes(q: svgtools.QucsSvg, sch: Schematic) -> dict:
    texts = [(t, q.rect_to_sch(t.bbox)) for t in q.texts]
    out = {}
    for c in sch.components:
        if c.name == "*" or c.type == "GND":
            continue
        ox, oy = c.x + c.tx, c.y + c.ty
        lines = [r for _, r in texts if abs(r[0] - ox) <= 6 and -8 <= r[1] - oy <= 6]
        if not lines:
            continue
        cur = min(lines, key=lambda r: r[1])
        block = list(cur)
        y_last = cur[1]
        for _ in range(40):
            nxt = [r for _, r in texts if abs(r[0] - ox) <= 6 and 8 <= r[1] - y_last <= 30]
            if not nxt:
                break
            r = min(nxt, key=lambda r: r[1])
            block = [min(block[0], r[0]), min(block[1], r[1]), max(block[2], r[2]), max(block[3], r[3])]
            y_last = r[1]
        out[c.name] = tuple(block)
    return out


# ------------------------------------------------------------------ overlays
def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _grid_layer(r, minor: int, major: int, show_minor: bool) -> str:
    x0, y0, x1, y1 = r
    out = []
    if show_minor:
        dots = []
        sx = math.ceil(x0 / minor) * minor
        sy = math.ceil(y0 / minor) * minor
        for x in range(int(sx), int(x1) + 1, minor):
            dots.append(f"M{x},{sy}V{y1}")
        out.append(f'<path d="{"".join(dots)}" stroke="#eef1f6" stroke-width="0.6" fill="none"/>')
        dots = []
        for y in range(int(sy), int(y1) + 1, minor):
            dots.append(f"M{x0},{y}H{x1}")
        out.append(f'<path d="{"".join(dots)}" stroke="#eef1f6" stroke-width="0.6" fill="none"/>')
    d = []
    sx = math.ceil(x0 / major) * major
    sy = math.ceil(y0 / major) * major
    for x in range(int(sx), int(x1) + 1, major):
        d.append(f"M{x},{y0}V{y1}")
    for y in range(int(sy), int(y1) + 1, major):
        d.append(f"M{x0},{y}H{x1}")
    out.append(f'<path d="{"".join(d)}" stroke="#d9dee8" stroke-width="0.8" fill="none"/>')
    return "".join(out)


def _ruler_layer(r, major: int, fs: float) -> str:
    x0, y0, x1, y1 = r
    out = [f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{fs * 1.6}" fill="#f4f6fa" opacity="0.95"/>',
           f'<rect x="{x0}" y="{y0}" width="{fs * 3.4}" height="{y1 - y0}" fill="#f4f6fa" opacity="0.95"/>']
    sx = math.ceil((x0 + fs * 3.6) / major) * major
    for x in range(int(sx), int(x1) + 1, major):
        out.append(f'<text x="{x}" y="{y0 + fs * 1.2}" font-size="{fs}" fill="#5a6475" '
                   f'text-anchor="middle" font-family="Consolas, monospace">{x}</text>')
    sy = math.ceil((y0 + fs * 2) / major) * major
    for y in range(int(sy), int(y1) + 1, major):
        out.append(f'<text x="{x0 + fs * 3.1}" y="{y + fs * 0.35}" font-size="{fs}" fill="#5a6475" '
                   f'text-anchor="end" font-family="Consolas, monospace">{y}</text>')
    return "".join(out)


def _net_layer(sch: Schematic, conn, fs: float, show_names: bool) -> str:
    out = []
    color_of = {}
    for n in conn.nets:
        color_of[n.id] = NET_COLORS[n.id % len(NET_COLORS)] if n.name != "gnd" else "#7f8c8d"
    for i, w in enumerate(sch.wires):
        col = color_of[conn.wire_net[i]]
        if w.is_point():
            out.append(f'<circle cx="{w.x1}" cy="{w.y1}" r="5" fill="{col}" opacity="0.35"/>')
        else:
            out.append(f'<line x1="{w.x1}" y1="{w.y1}" x2="{w.x2}" y2="{w.y2}" stroke="{col}" '
                       f'stroke-width="7" stroke-linecap="round" opacity="0.28"/>')
    if show_names:
        for n in conn.nets:
            if not n.wires and len(n.pins) < 2:
                continue
            # tag on the longest wire of the net (or first pin)
            best, bl = None, -1
            for i in n.wires:
                w = sch.wires[i]
                ln = abs(w.x2 - w.x1) + abs(w.y2 - w.y1)
                if ln > bl:
                    best, bl = w, ln
            if best is not None:
                tx, ty = (best.x1 + best.x2) / 2, (best.y1 + best.y2) / 2
            else:
                tx, ty = n.pins[0].x, n.pins[0].y
            label = n.name
            wpx = svgtools.text_width_em(label) * fs + 6
            col = color_of[n.id]
            out.append(f'<rect x="{tx - wpx / 2}" y="{ty - fs * 0.75}" width="{wpx}" height="{fs * 1.5}" '
                       f'rx="{fs * 0.4}" fill="white" stroke="{col}" stroke-width="1.2" opacity="0.95"/>'
                       f'<text x="{tx}" y="{ty + fs * 0.38}" font-size="{fs}" fill="{col}" text-anchor="middle" '
                       f'font-family="Consolas, monospace" font-weight="bold">{_esc(label)}</text>')
    return "".join(out)


def _pins_layer(conn, fs: float, names: bool) -> str:
    out = []
    for p in conn.pins:
        if p.comp.startswith("GND@"):
            continue
        out.append(f'<circle cx="{p.x}" cy="{p.y}" r="2.6" fill="#ff006e" stroke="white" stroke-width="0.8"/>')
        if names:
            out.append(f'<text x="{p.x + 3}" y="{p.y - 3}" font-size="{fs * 0.8}" fill="#ff006e" '
                       f'font-family="Consolas, monospace">{_esc(p.ref)}</text>')
    return "".join(out)


def _highlight_layer(sch: Schematic, names: list[str], conn, workdir) -> str:
    out = []
    wanted = {n.lower() for n in names}
    for c in sch.components:
        if c.name.lower() in wanted:
            r = cdb.body_rect(c, workdir)
            out.append(f'<rect x="{r[0] - 6}" y="{r[1] - 6}" width="{r[2] - r[0] + 12}" '
                       f'height="{r[3] - r[1] + 12}" rx="6" fill="#ffd60a" opacity="0.35" '
                       f'stroke="#ffb703" stroke-width="2"/>')
    for n in conn.nets:
        if n.name.lower() in wanted:
            for i in n.wires:
                w = sch.wires[i]
                out.append(f'<line x1="{w.x1}" y1="{w.y1}" x2="{w.x2}" y2="{w.y2}" stroke="#ffb703" '
                           f'stroke-width="9" stroke-linecap="round" opacity="0.45"/>')
    return "".join(out)


def _issues_layer(issues, fs: float) -> str:
    out = []
    for k, it in enumerate(issues, 1):
        if it.x is None:
            continue
        col = SEVERITY_COLOR.get(it.severity, "#d00000")
        out.append(f'<circle cx="{it.x}" cy="{it.y}" r="{fs * 0.95}" fill="none" stroke="{col}" '
                   f'stroke-width="2"/>'
                   f'<circle cx="{it.x + fs}" cy="{it.y - fs}" r="{fs * 0.7}" fill="{col}"/>'
                   f'<text x="{it.x + fs}" y="{it.y - fs + fs * 0.35}" font-size="{fs * 0.9}" fill="white" '
                   f'text-anchor="middle" font-family="Consolas, monospace" font-weight="bold">{k}</text>')
    return "".join(out)


def _title_layer(r, title: str, fs: float, below_ruler: bool = False) -> str:
    x0, y0, x1, _ = r
    ty = y0 + fs * (3.0 if below_ruler else 1.25)
    return (f'<text x="{x1 - fs * 0.6}" y="{ty}" font-size="{fs * 1.1}" fill="#2b2d42" '
            f'text-anchor="end" font-family="Segoe UI, sans-serif" font-weight="bold">{_esc(title)}</text>')


# ------------------------------------------------------------------- main
def snapshot(sch: Schematic, out_png: Path, *, region=None, focus: list[str] | None = None,
             max_px: int = 1600, scale: float | None = None, grid: bool = True, rulers: bool = True,
             pins: bool = False, pin_names: bool = False, nets: bool = False, net_names: bool = True,
             highlight: list[str] | None = None, issues: list | None = None,
             include_diagrams: bool = False, title: str | None = None, margin: int = 30,
             workdir: Path | None = None, save_svg: bool = False) -> dict:
    an = analyse(sch, include_diagrams=include_diagrams)
    q = an.q
    need_conn = pins or nets or highlight
    conn = layout.connectivity(sch, workdir) if need_conn else None

    if region is None and focus:
        rects = []
        for name in focus:
            try:
                c = sch.find(name)
                rects.append(cdb.body_rect(c, workdir))
                if c.name in an.label_boxes:
                    rects.append(an.label_boxes[c.name])
            except KeyError:
                if conn is None:
                    conn = layout.connectivity(sch, workdir)
                n = conn.net_by_name(name)
                if n:
                    for i in n.wires:
                        w = sch.wires[i]
                        rects.append((min(w.x1, w.x2), min(w.y1, w.y2), max(w.x1, w.x2), max(w.y1, w.y2)))
        if rects:
            region = (min(r[0] for r in rects) - 60, min(r[1] for r in rects) - 60,
                      max(r[2] for r in rects) + 60, max(r[3] for r in rects) + 60)
    if region is None:
        bb = an.content_bbox
        region = (bb[0] - margin, bb[1] - margin, bb[2] + margin, bb[3] + margin)
    x0, y0, x1, y1 = region
    w, h = max(x1 - x0, 20), max(y1 - y0, 20)
    if scale is None:
        scale = min(max_px / max(w, h), 3.0)
    px_w, px_h = int(round(w * scale)), int(round(h * scale))
    fs = max(5.0, 13.0 / max(scale, 0.3))  # overlay font size in schematic units (~13 px)

    r = (x0, y0, x0 + w, y0 + h)
    body = [f'<rect x="{x0}" y="{y0}" width="{w}" height="{h}" fill="#ffffff"/>']
    if grid:
        body.append(_grid_layer(r, 10, 50, show_minor=scale >= 1.2))
    if conn is not None and nets:
        body.append(_net_layer(sch, conn, fs, show_names=False))
    if conn is not None and highlight:
        body.append(_highlight_layer(sch, highlight, conn, workdir))
    body.append(f'<g transform="{q.sch_transform()}">{q.inner_xml()}</g>')
    if conn is not None and nets and net_names:
        body.append(_net_names_only(sch, conn, fs))
    if conn is not None and pins:
        body.append(_pins_layer(conn, fs, pin_names))
    if issues:
        body.append(_issues_layer(issues, fs))
    if rulers:
        body.append(_ruler_layer(r, 50 if w < 900 else 100, fs))
    if title:
        body.append(_title_layer(r, title, fs, rulers))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           f'viewBox="{x0} {y0} {w} {h}" width="{px_w}" height="{px_h}">' + "".join(body) + "</svg>")
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    png = resvg_py.svg_to_bytes(svg_string=svg, width=px_w, height=px_h, background="white")
    out_png.write_bytes(bytes(png))
    if save_svg:
        out_png.with_suffix(".svg").write_text(svg, encoding="utf-8")
    return {
        "path": str(out_png),
        "pixels": [px_w, px_h],
        "region": [round(v) for v in r],
        "px_per_unit": round(scale, 4),
        "coordinate_mapping": f"pixel = (x - {round(x0)}, y - {round(y0)}) * {round(scale, 4)}",
        "substituted_for_print": an.substituted,
        "analysis": an,
    }


def _net_names_only(sch, conn, fs) -> str:
    full = _net_layer(sch, conn, fs, show_names=True)
    # keep only the name tags (rect+text pairs come after the wire strokes)
    idx = full.find("<rect")
    return full[idx:] if idx >= 0 else ""
