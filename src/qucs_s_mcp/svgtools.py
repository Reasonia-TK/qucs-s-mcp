"""Parse the SVG that ``qucs-s --print`` produces and map it to schematic coordinates.

Qucs-S prints through Qt's QSvgGenerator.  The page is scaled/offset relative
to the schematic, so a calibration line with a unique colour is injected into
the schematic before printing; locating it in the SVG yields the affine map
``page = s * sch + o`` which is then inverted.
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

SVG_NS = "http://www.w3.org/2000/svg"
CALIB_COLOR = "#fe01fe"
CALIB_LEN = 100

_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def calibration_painting(x: int, y: int) -> str:
    return f"<Line {x} {y} {CALIB_LEN} 0 {CALIB_COLOR} 1 1>"


def _tag(el) -> str:
    return el.tag.split("}")[-1]


def _matrix(s: str | None):
    if not s:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    m = re.search(r"matrix\(([^)]*)\)", s)
    if m:
        v = [float(t) for t in _NUM.findall(m.group(1))]
        if len(v) == 6:
            return tuple(v)
    m = re.search(r"translate\(([^)]*)\)", s)
    if m:
        v = [float(t) for t in _NUM.findall(m.group(1))] + [0.0]
        return (1.0, 0.0, 0.0, 1.0, v[0], v[1])
    return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mul(m1, m2):
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2, a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


def _apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def text_width_em(s: str) -> float:
    w = 0.0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            w += 1.0
        elif ch in "il.,:;|'!":
            w += 0.3
        elif ch.isupper() or ch in "mwMW":
            w += 0.68
        else:
            w += 0.55
    return w


@dataclass
class Shape:
    kind: str
    points: list[tuple[float, float]]    # in page coordinates
    stroke: str = ""

    def bbox(self):
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class Text:
    text: str
    bbox: tuple[float, float, float, float]   # page coordinates
    color: str = ""


@dataclass
class QucsSvg:
    root: ET.Element
    shapes: list[Shape] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)
    scale: float = 1.0           # page = scale * sch + offset
    offset: tuple[float, float] = (0.0, 0.0)

    def to_sch(self, x: float, y: float) -> tuple[float, float]:
        return ((x - self.offset[0]) / self.scale, (y - self.offset[1]) / self.scale)

    def rect_to_sch(self, r):
        x1, y1 = self.to_sch(r[0], r[1])
        x2, y2 = self.to_sch(r[2], r[3])
        return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))

    def inner_xml(self) -> str:
        """All drawing content (without the calibration line) as an SVG string."""
        parts = []
        for child in self.root:
            if _tag(child) in ("title", "desc", "defs"):
                continue
            parts.append(ET.tostring(child, encoding="unicode"))
        txt = "".join(parts)
        return txt.replace(f'xmlns="{SVG_NS}"', "").replace("ns0:", "")

    def sch_transform(self) -> str:
        s = 1.0 / self.scale
        return f"matrix({s:.8g},0,0,{s:.8g},{-self.offset[0] * s:.8g},{-self.offset[1] * s:.8g})"

    def content_bbox_sch(self, include_text: bool = True):
        rects = [self.rect_to_sch(sh.bbox()) for sh in self.shapes]
        if include_text:
            rects += [self.rect_to_sch(t.bbox) for t in self.texts]
        if not rects:
            return None
        return (min(r[0] for r in rects), min(r[1] for r in rects),
                max(r[2] for r in rects), max(r[3] for r in rects))


def parse(svg_text: str, calib_xy: tuple[int, int] | None) -> QucsSvg:
    ET.register_namespace("", SVG_NS)
    root = ET.fromstring(svg_text.encode("utf-8"))
    out = QucsSvg(root=root)
    calib_pts = None
    calib_parents = []

    def walk(el, m, parent):
        nonlocal calib_pts
        tag = _tag(el)
        if tag in ("g", "svg"):
            mm = _mul(m, _matrix(el.get("transform")))
            for ch in list(el):
                walk(ch, mm, el)
            return
        stroke = (el.get("stroke") or (parent.get("stroke") if parent is not None else "") or "").lower()
        pts: list[tuple[float, float]] = []
        if tag in ("polyline", "polygon"):
            nums = [float(v) for v in _NUM.findall(el.get("points", ""))]
            pts = [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
        elif tag == "line":
            pts = [(float(el.get("x1", 0)), float(el.get("y1", 0))),
                   (float(el.get("x2", 0)), float(el.get("y2", 0)))]
        elif tag == "path":
            nums = [float(v) for v in _NUM.findall(el.get("d", ""))]
            pts = [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
        elif tag == "rect":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            w, h = float(el.get("width", 0)), float(el.get("height", 0))
            pts = [(x, y), (x + w, y + h)]
        elif tag in ("circle", "ellipse"):
            cx, cy = float(el.get("cx", 0)), float(el.get("cy", 0))
            rx = float(el.get("rx", el.get("r", 0)))
            ry = float(el.get("ry", el.get("r", 0)))
            pts = [(cx - rx, cy - ry), (cx + rx, cy + ry)]
        elif tag == "text":
            content = "".join(el.itertext())
            if not content.strip():
                return
            fs = float(el.get("font-size") or (parent.get("font-size") if parent is not None else 12) or 12)
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            w = text_width_em(content) * fs
            corners = [_apply(m, x, y - 0.82 * fs), _apply(m, x + w, y + 0.22 * fs)]
            xs = [c[0] for c in corners]
            ys = [c[1] for c in corners]
            fill = (el.get("fill") or "").lower()
            out.texts.append(Text(content, (min(xs), min(ys), max(xs), max(ys)), fill))
            return
        else:
            return
        if not pts:
            return
        page_pts = [_apply(m, x, y) for x, y in pts]
        if stroke == CALIB_COLOR:
            calib_pts = page_pts
            calib_parents.append((parent, el))
            return
        # The page background rectangle / frame is not content
        if tag == "rect" and (el.get("fill") or "").lower() in ("#ffffff", "white") and stroke in ("", "none"):
            return
        out.shapes.append(Shape(tag, page_pts, stroke))

    walk(root, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0), None)

    # remove calibration elements from the tree
    for parent, el in calib_parents:
        if parent is not None:
            parent.remove(el)

    if calib_xy is not None:
        if not calib_pts or len(calib_pts) < 2:
            raise RuntimeError("Calibration marker not found in the qucs-s SVG output")
        (px1, py1), (px2, py2) = calib_pts[0], calib_pts[-1]
        if px2 < px1:
            px1, py1, px2, py2 = px2, py2, px1, py1
        s = (px2 - px1) / CALIB_LEN
        out.scale = s
        out.offset = (px1 - s * calib_xy[0], py1 - s * calib_xy[1])
    return out
