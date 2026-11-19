"""Reading / writing Qucs-S schematic (.sch) files.

Only the parts that matter for editing (components and wires) are modelled;
every other section is kept verbatim so files round-trip safely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_TOKEN = re.compile(r'"[^"]*"|\S+')

DEFAULT_VERSION = "26.1.1"


def tokenize(body: str) -> list[str]:
    return _TOKEN.findall(body)


def unquote(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return tok[1:-1]
    return tok


def quote(val: str) -> str:
    return '"' + str(val).replace('"', "'") + '"'


# --------------------------------------------------------------------------
# Geometry helpers (Qucs conventions: x to the right, y downwards)
# --------------------------------------------------------------------------

def transform_point(px: float, py: float, mirror: int, rotate: int) -> tuple[float, float]:
    """Map a point from symbol-local coordinates to the placed orientation.

    Qucs applies the X-axis mirror first (y -> -y), then ``rotate`` times a
    90 degree counter-clockwise rotation (x, y) -> (y, -x).
    """
    if mirror:
        py = -py
    for _ in range(rotate % 4):
        px, py = py, -px
    return px, py


def transform_rect(rect, mirror: int, rotate: int):
    x1, y1, x2, y2 = rect
    pts = [transform_point(x, y, mirror, rotate) for x, y in ((x1, y1), (x2, y2))]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class Component:
    type: str
    name: str
    x: int
    y: int
    tx: int = 0          # label text offset (relative to x, y)
    ty: int = 0
    mirror: int = 0      # mirror about X axis
    rotate: int = 0      # 0..3, each step 90 deg counter-clockwise
    active: int = 1      # 1 active, 0 deactivated, 2 shorted
    props: list[list] = field(default_factory=list)  # [[value, display(0/1)], ...]

    @classmethod
    def parse(cls, body: str) -> "Component":
        t = tokenize(body)
        if len(t) < 9:
            raise ValueError(f"Malformed component line: <{body}>")
        props = []
        rest = t[9:]
        for i in range(0, len(rest) - 1, 2):
            props.append([unquote(rest[i]), int(rest[i + 1]) if rest[i + 1].lstrip("-").isdigit() else rest[i + 1]])
        return cls(
            type=t[0], name=t[1], active=int(t[2]), x=int(float(t[3])), y=int(float(t[4])),
            tx=int(float(t[5])), ty=int(float(t[6])), mirror=int(t[7]), rotate=int(t[8]), props=props,
        )

    def serialize(self) -> str:
        parts = [self.type, self.name, str(self.active), str(self.x), str(self.y),
                 str(self.tx), str(self.ty), str(self.mirror), str(self.rotate)]
        for val, disp in self.props:
            parts.append(quote(val))
            parts.append(str(disp))
        return "<" + " ".join(parts) + ">"


@dataclass
class Wire:
    x1: int
    y1: int
    x2: int
    y2: int
    label: str = ""
    lx: int = 0
    ly: int = 0
    loff: int = 0
    extra: str = ""

    @classmethod
    def parse(cls, body: str) -> "Wire":
        t = tokenize(body)
        w = cls(int(float(t[0])), int(float(t[1])), int(float(t[2])), int(float(t[3])))
        if len(t) > 4:
            w.label = unquote(t[4])
        if len(t) > 7:
            w.lx, w.ly, w.loff = int(float(t[5])), int(float(t[6])), int(float(t[7]))
        if len(t) > 8:
            w.extra = unquote(t[8])
        return w

    def serialize(self) -> str:
        return (f"<{self.x1} {self.y1} {self.x2} {self.y2} {quote(self.label)} "
                f"{self.lx} {self.ly} {self.loff} {quote(self.extra)}>")

    @property
    def p1(self):
        return (self.x1, self.y1)

    @property
    def p2(self):
        return (self.x2, self.y2)

    def is_point(self) -> bool:
        return self.x1 == self.x2 and self.y1 == self.y2

    def contains(self, x: float, y: float, include_ends: bool = True) -> bool:
        """True when (x, y) lies on this (orthogonal or diagonal) segment."""
        x1, y1, x2, y2 = self.x1, self.y1, self.x2, self.y2
        if (x, y) in ((x1, y1), (x2, y2)):
            return include_ends
        if min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2):
            return (x2 - x1) * (y - y1) == (y2 - y1) * (x - x1)
        return False


@dataclass
class Schematic:
    version: str = DEFAULT_VERSION
    properties: list[str] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    wires: list[Wire] = field(default_factory=list)
    # verbatim sections: name -> list of raw lines (without the section tags)
    raw_sections: dict[str, list[str]] = field(default_factory=dict)
    section_order: list[str] = field(default_factory=list)
    path: Path | None = None

    # ------------------------------------------------------------------ io
    @classmethod
    def new(cls) -> "Schematic":
        s = cls()
        s.properties = [
            "<View=0,0,1200,900,1,0,0>",
            "<Grid=10,10,1>",
            "<DataSet=>",
            "<DataDisplay=>",
            "<OpenDisplay=0>",
            "<Script=>",
            "<RunScript=0>",
            "<showFrame=0>",
            "<FrameText0=Title>",
            "<FrameText1=Drawn By:>",
            "<FrameText2=Date:>",
            "<FrameText3=Revision:>",
        ]
        s.raw_sections = {"Symbol": [], "Diagrams": [], "Paintings": []}
        s.section_order = ["Properties", "Symbol", "Components", "Wires", "Diagrams", "Paintings"]
        return s

    @classmethod
    def load(cls, path: str | Path) -> "Schematic":
        path = Path(path)
        s = cls.parse(path.read_text(encoding="utf-8", errors="replace"))
        s.path = path
        return s

    @classmethod
    def parse(cls, text: str) -> "Schematic":
        s = cls()
        lines = text.splitlines()
        if not lines or not lines[0].startswith("<Qucs Schematic"):
            raise ValueError("Not a Qucs schematic (missing '<Qucs Schematic ...>' header)")
        s.version = lines[0].strip()[len("<Qucs Schematic"):].rstrip(">").strip() or DEFAULT_VERSION
        current = None
        buf: list[str] = []
        for raw in lines[1:]:
            line = raw.strip()
            if current is None:
                m = re.fullmatch(r"<([A-Za-z]+)>", line)
                if m:
                    current = m.group(1)
                    buf = []
                continue
            if line == f"</{current}>":
                s.section_order.append(current)
                if current == "Components":
                    s.components = [Component.parse(b[1:-1]) for b in buf if b.strip()]
                elif current == "Wires":
                    s.wires = [Wire.parse(b[1:-1]) for b in buf if b.strip()]
                elif current == "Properties":
                    s.properties = [b.strip() for b in buf if b.strip()]
                else:
                    s.raw_sections[current] = buf
                current = None
                continue
            if current in ("Components", "Wires", "Properties"):
                if line:
                    buf.append(line)
            else:
                buf.append(raw)
        for sec in ("Properties", "Symbol", "Components", "Wires", "Diagrams", "Paintings"):
            if sec not in s.section_order:
                s.section_order.append(sec)
                if sec not in ("Properties", "Components", "Wires"):
                    s.raw_sections.setdefault(sec, [])
        return s

    def serialize(self) -> str:
        out = [f"<Qucs Schematic {self.version}>"]
        for sec in self.section_order:
            out.append(f"<{sec}>")
            if sec == "Properties":
                out += ["  " + p for p in self.properties]
            elif sec == "Components":
                out += ["  " + c.serialize() for c in self.components]
            elif sec == "Wires":
                out += ["  " + w.serialize() for w in self.wires]
            else:
                out += self.raw_sections.get(sec, [])
            out.append(f"</{sec}>")
        return "\n".join(out) + "\n"

    def save(self, path: str | Path | None = None) -> Path:
        path = Path(path) if path else self.path
        if path is None:
            raise ValueError("No path given for saving the schematic")
        path.parent.mkdir(parents=True, exist_ok=True)
        stem = path.stem
        self.set_property("DataSet", f"{stem}.dat")
        self.set_property("DataDisplay", f"{stem}.dpl")
        path.write_text(self.serialize(), encoding="utf-8")
        self.path = path
        return path

    def copy(self) -> "Schematic":
        return Schematic.parse(self.serialize())

    # ------------------------------------------------------------ helpers
    def get_property(self, key: str) -> str | None:
        for p in self.properties:
            m = re.fullmatch(rf"<{re.escape(key)}=(.*)>", p)
            if m:
                return m.group(1)
        return None

    def set_property(self, key: str, value: str) -> None:
        for i, p in enumerate(self.properties):
            if p.startswith(f"<{key}="):
                self.properties[i] = f"<{key}={value}>"
                return
        self.properties.append(f"<{key}={value}>")

    def find(self, name: str) -> Component:
        for c in self.components:
            if c.name == name:
                return c
        for c in self.components:
            if c.name.lower() == name.lower():
                return c
        raise KeyError(f"No component named '{name}'. Existing: "
                       + ", ".join(c.name for c in self.components if c.name != "*"))

    def unique_name(self, prefix: str) -> str:
        used = {c.name for c in self.components}
        i = 1
        while f"{prefix}{i}" in used:
            i += 1
        return f"{prefix}{i}"

    def paintings(self) -> list[str]:
        return self.raw_sections.setdefault("Paintings", [])

    def diagrams(self) -> list[str]:
        return self.raw_sections.setdefault("Diagrams", [])
