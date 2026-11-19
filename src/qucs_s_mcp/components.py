"""Component database: templates, property names, pin positions, symbol bodies."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import DATA_DIR
from .schematic import Component, Schematic, transform_point, transform_rect

# Symbol aliases that are friendlier for agents
ALIASES = {
    "resistor": "R", "capacitor": "C", "inductor": "L", "ground": "GND", "gnd": "GND",
    "vdc": "Vdc", "vac": "Vac", "idc": "Idc", "iac": "Iac", "vpulse": "Vpulse",
    "opamp": "OpAmp", "diode": "Diode", "npn": "_BJT", "pnp": "_BJT", "bjt": "_BJT",
    "nmos": "_MOSFET", "pmos": "_MOSFET", "mosfet": "_MOSFET",
    "dc": ".DC", "ac": ".AC", "tran": ".TR", "transient": ".TR", "noise": ".NOISE",
    "sweep": ".SW", "param_sweep": ".SW", "fourier": ".FOURIER",
    "spice_param": "SpicePar", "param": "SpicePar", "nutmeg": "NutmegEq", "equation": "NutmegEq",
    "iprobe": "IProbe", "vprobe": "VProbe", "current_probe": "IProbe", "voltage_probe": "VProbe",
}

_lock = threading.Lock()
_dynamic_cache: dict[str, tuple[list, list]] = {}


@lru_cache(maxsize=1)
def db() -> dict[str, dict]:
    p = DATA_DIR / "components.json"
    if not p.exists():
        raise RuntimeError("components.json missing - run tools/build_component_db.py")
    return json.loads(p.read_text(encoding="utf-8"))


def resolve_type(typ: str) -> str:
    d = db()
    if typ in d:
        return typ
    low = typ.lower()
    if low in ALIASES:
        return ALIASES[low]
    for k in d:
        if k.lower() == low:
            return k
    if typ in ("Lib", "Sub", "SpLib", "SPICE"):
        return typ
    raise KeyError(f"Unknown component type '{typ}'. Use list_component_types to search.")


def info(typ: str) -> dict | None:
    return db().get(typ)


@dataclass
class Pin:
    comp: str
    index: int        # 1-based, SPICE node order
    name: str
    x: int
    y: int

    @property
    def ref(self) -> str:
        return f"{self.comp}.{self.name}"


def set_props(comp: Component, props: dict | None) -> list[str]:
    """Set properties by name (case-insensitive) or 1-based index. Returns warnings."""
    if not props:
        return []
    warns = []
    meta = info(comp.type)
    names = [p["name"] for p in meta["props"]] if meta else []
    for key, val in props.items():
        idx = None
        if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
            idx = int(key) - 1
        else:
            for i, n in enumerate(names):
                if n.lower() == str(key).lower():
                    idx = i
                    break
        if idx is None or idx < 0:
            warns.append(f"{comp.name}: unknown property '{key}' (known: {', '.join(names)})")
            continue
        while len(comp.props) <= idx:
            comp.props.append(["", 0])
        if isinstance(val, dict):
            if "value" in val:
                comp.props[idx][0] = str(val["value"])
            if "show" in val:
                comp.props[idx][1] = 1 if val["show"] else 0
        else:
            comp.props[idx][0] = str(val)
    return warns


def make_component(sch: Schematic, typ: str, x: int, y: int, *, name: str | None = None,
                   rotation: int = 0, mirror: bool = False, props: dict | None = None,
                   template: str | None = None) -> tuple[Component, list[str]]:
    typ = resolve_type(typ)
    meta = info(typ)
    if template is None:
        if meta is None:
            raise KeyError(f"No template for '{typ}'")
        template = meta["template"]
    comp = Component.parse(template)
    comp.x, comp.y = int(x), int(y)
    comp.rotate = (int(rotation) // 90) % 4
    comp.mirror = 1 if mirror else 0
    if typ == "GND":
        comp.name = "*"
    else:
        prefix = (meta or {}).get("prefix") or comp.name.rstrip("0123456789") or typ
        comp.name = name or sch.unique_name(prefix)
        if any(c.name == comp.name for c in sch.components):
            raise ValueError(f"A component named '{comp.name}' already exists")
    warns = set_props(comp, props)
    return comp, warns


# ------------------------------------------------------------------ pins
def _dyn_key(c: Component) -> str:
    return c.type + "|" + "|".join(str(v) for v, _ in c.props)


def _dynamic_probe(c: Component, workdir: Path | None) -> tuple[list, list]:
    key = _dyn_key(c)
    with _lock:
        if key in _dynamic_cache:
            return _dynamic_cache[key]
    from . import qucs_cli  # local import: avoids cycles
    base = Component(**{**c.__dict__, "x": 0, "y": 0, "mirror": 0, "rotate": 0,
                        "props": [list(p) for p in c.props]})
    pins = qucs_cli.probe_ports([base], extent=200, step=10, workdir=workdir)[0] or []
    names = [str(i + 1) for i in range(len(pins))]
    with _lock:
        _dynamic_cache[key] = (pins, names)
    return pins, names


def local_pins(c: Component, workdir: Path | None = None) -> tuple[list, list]:
    meta = info(c.type)
    if meta and meta.get("pins") is not None and c.type not in ("Lib", "Sub", "SpLib", "SPICE"):
        pins, names = meta["pins"], meta["pin_names"]
        if pins or not meta.get("pin_error"):
            return pins, names
    if c.type in ("Lib", "Sub", "SpLib", "SPICE") or meta is None:
        try:
            return _dynamic_probe(c, workdir)
        except Exception:  # noqa: BLE001 - e.g. file not found yet
            return [], []
    return [], []


def pins(c: Component, workdir: Path | None = None) -> list[Pin]:
    if c.type == "GND":
        return [Pin(f"GND@{c.x},{c.y}", 1, "gnd", c.x, c.y)]
    lp, names = local_pins(c, workdir)
    out = []
    for i, (px, py) in enumerate(lp):
        dx, dy = transform_point(px, py, c.mirror, c.rotate)
        out.append(Pin(c.name, i + 1, names[i] if i < len(names) else str(i + 1),
                       int(round(c.x + dx)), int(round(c.y + dy))))
    return out


def body_rect(c: Component, workdir: Path | None = None):
    """Absolute bounding box of the symbol drawing (without text labels)."""
    meta = info(c.type)
    r = meta.get("body") if meta else None
    if not r or c.type in ("Lib", "Sub", "SpLib", "SPICE"):
        lp, _ = local_pins(c, workdir)
        if lp:
            xs = [p[0] for p in lp]
            ys = [p[1] for p in lp]
            r = [min(xs) + 10, min(ys) - 10, max(xs) - 10, max(ys) + 10]
            if r[0] > r[2]:
                r[0], r[2] = -10, 10
            if r[1] > r[3]:
                r[1], r[3] = -10, 10
        else:
            r = [-20, -20, 20, 20]
    else:
        # The printed bbox includes line caps; clip it so that pins sit on its edge.
        r = list(r)
        lp = [[0, 0]] if c.type == "GND" else local_pins(c, workdir)[0]
        for px, py in lp:
            if abs(px - r[0]) <= 6:
                r[0] = max(r[0], px)
            if abs(px - r[2]) <= 6:
                r[2] = min(r[2], px)
            if abs(py - r[1]) <= 6:
                r[1] = max(r[1], py)
            if abs(py - r[3]) <= 6:
                r[3] = min(r[3], py)
    x1, y1, x2, y2 = transform_rect(r, c.mirror, c.rotate)
    return (c.x + x1, c.y + y1, c.x + x2, c.y + y2)


def is_sim_block(c: Component) -> bool:
    return c.type.startswith(".") or c.type in ("SpicePar", "NutmegEq", "Eqn", "SpiceInclude",
                                                  "SpiceGlobalPar", "SpiceOptions", "SPICEINIT",
                                                  "SpiceIC", "SpiceNodeset", "SpiceFunc", "SpiceLib")


def describe_type(typ: str) -> dict:
    typ = resolve_type(typ)
    meta = info(typ)
    if meta is None:
        return {"type": typ, "note": "Dynamic component; pins discovered when placed."}
    return {
        "type": typ,
        "caption": meta["caption"],
        "description": meta["description"],
        "category": meta["category"],
        "default_name_prefix": meta["prefix"],
        "pins_at_rotation0": [{"name": n, "dx": p[0], "dy": p[1]}
                              for n, p in zip(meta["pin_names"], meta["pins"])],
        "body_bbox_at_rotation0": meta["body"],
        "properties": meta["props"],
        "ngspice_netlist_supported": meta["ngspice"],
    }
