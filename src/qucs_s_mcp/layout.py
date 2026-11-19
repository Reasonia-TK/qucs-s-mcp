"""Connectivity analysis, layout lint, orthogonal auto-routing and tidy-up helpers."""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from pathlib import Path

from . import components as cdb
from .schematic import Component, Schematic, Wire

GRID = 10


# ===================================================================== utils
class UF:
    def __init__(self):
        self.p: dict = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def rects_overlap(a, b, pad: float = 0.0) -> bool:
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def rect_area_overlap(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def seg_intersects_rect(x1, y1, x2, y2, r, shrink: float = 0.5) -> bool:
    """Orthogonal segment passes through the (slightly shrunken) interior of r."""
    rx1, ry1, rx2, ry2 = r[0] + shrink, r[1] + shrink, r[2] - shrink, r[3] - shrink
    if rx1 >= rx2 or ry1 >= ry2:
        return False
    if y1 == y2:
        return ry1 < y1 < ry2 and max(x1, x2) > rx1 and min(x1, x2) < rx2
    if x1 == x2:
        return rx1 < x1 < rx2 and max(y1, y2) > ry1 and min(y1, y2) < ry2
    # diagonal: sample
    for i in range(21):
        t = i / 20
        x, y = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
        if rx1 < x < rx2 and ry1 < y < ry2:
            return True
    return False


def seg_rect_length_inside(x1, y1, x2, y2, r) -> float:
    if y1 == y2 and r[1] < y1 < r[3]:
        return max(0.0, min(max(x1, x2), r[2]) - max(min(x1, x2), r[0]))
    if x1 == x2 and r[0] < x1 < r[2]:
        return max(0.0, min(max(y1, y2), r[3]) - max(min(y1, y2), r[1]))
    return 0.0


# ============================================================== connectivity
@dataclass
class Net:
    id: int
    name: str
    pins: list = field(default_factory=list)          # [Pin]
    wires: list = field(default_factory=list)         # indices into sch.wires
    labels: list = field(default_factory=list)        # label names
    clusters: list = field(default_factory=list)      # list of physical cluster ids


@dataclass
class Connectivity:
    nets: list[Net]
    pin_net: dict          # (comp, pin_index) -> net id
    wire_net: dict         # wire index -> net id
    cluster_of_wire: dict  # wire index -> cluster id (physical, ignores labels / gnd)
    cluster_pins: dict     # cluster id -> [Pin]
    cluster_wires: dict    # cluster id -> [wire idx]
    pins: list             # all pins
    point_net: dict        # (x, y) -> net id (wire ends & pins)

    def net_by_name(self, name: str) -> Net | None:
        for n in self.nets:
            if n.name.lower() == name.lower():
                return n
        return None

    def net_of_pin(self, comp: str, pin) -> Net:
        for p in self.pins:
            if p.comp == comp and (str(p.index) == str(pin) or p.name.lower() == str(pin).lower()):
                return self.nets[self.pin_net[(p.comp, p.index)]]
        raise KeyError(f"Pin {comp}.{pin} not found")


def all_pins(sch: Schematic, workdir: Path | None = None):
    out = []
    for c in sch.components:
        try:
            out.extend(cdb.pins(c, workdir))
        except Exception:  # noqa: BLE001 - unknown component -> no pins
            pass
    return out


def connectivity(sch: Schematic, workdir: Path | None = None) -> Connectivity:
    pins = all_pins(sch, workdir)
    phys = UF()
    for i, w in enumerate(sch.wires):
        phys.union(("w", i), w.p1)
        phys.union(("w", i), w.p2)
    for p in pins:
        phys.union(("p", p.comp, p.index), (p.x, p.y))
    # logical = physical + equal labels + all grounds
    logi = UF()
    for k in list(phys.p.keys()):
        logi.union(k, phys.find(k))
    label_anchor: dict[str, tuple] = {}
    for i, w in enumerate(sch.wires):
        if w.label:
            key = w.label.lower()
            if key in label_anchor:
                logi.union(("w", i), label_anchor[key])
            else:
                label_anchor[key] = ("w", i)
    gnd_anchor = None
    for p in pins:
        if p.comp.startswith("GND@"):
            if gnd_anchor is None:
                gnd_anchor = ("p", p.comp, p.index)
            logi.union(("p", p.comp, p.index), gnd_anchor)
    # GND pins all share comp name "GND" -> make index unique
    roots: dict = {}
    nets: list[Net] = []

    def net_for(key) -> Net:
        r = logi.find(key)
        if r not in roots:
            roots[r] = len(nets)
            nets.append(Net(id=len(nets), name=""))
        return nets[roots[r]]

    pin_net, wire_net, cluster_of_wire = {}, {}, {}
    cluster_ids: dict = {}
    cluster_pins: dict = {}
    cluster_wires: dict = {}

    def cid(key):
        r = phys.find(key)
        if r not in cluster_ids:
            cluster_ids[r] = len(cluster_ids)
        return cluster_ids[r]

    point_net = {}
    for p in pins:
        n = net_for(("p", p.comp, p.index))
        n.pins.append(p)
        pin_net[(p.comp, p.index)] = n.id
        c = cid(("p", p.comp, p.index))
        cluster_pins.setdefault(c, []).append(p)
        if c not in n.clusters:
            n.clusters.append(c)
        point_net[(p.x, p.y)] = n.id
    for i, w in enumerate(sch.wires):
        n = net_for(("w", i))
        n.wires.append(i)
        wire_net[i] = n.id
        c = cid(("w", i))
        cluster_of_wire[i] = c
        cluster_wires.setdefault(c, []).append(i)
        if c not in n.clusters:
            n.clusters.append(c)
        if w.label and w.label not in n.labels:
            n.labels.append(w.label)
        point_net[w.p1] = n.id
        point_net[w.p2] = n.id
    auto = itertools.count(1)
    for n in nets:
        if any(p.comp.startswith("GND@") for p in n.pins):
            n.name = "gnd"
        elif n.labels:
            n.name = n.labels[0]
        else:
            n.name = f"_n{next(auto)}"
    return Connectivity(nets, pin_net, wire_net, cluster_of_wire, cluster_pins, cluster_wires,
                        pins, point_net)


# ===================================================================== lint
@dataclass
class Issue:
    severity: str      # error | warning | style
    code: str
    message: str
    x: float | None = None
    y: float | None = None
    items: list = field(default_factory=list)

    def as_dict(self):
        d = {"severity": self.severity, "code": self.code, "message": self.message}
        if self.x is not None:
            d["at"] = [round(self.x), round(self.y)]
        if self.items:
            d["items"] = self.items
        return d


def lint(sch: Schematic, label_boxes: dict | None = None, wire_label_boxes: list | None = None,
         workdir: Path | None = None) -> tuple[list[Issue], dict]:
    """Check electrical sanity and visual tidiness. Returns (issues, metrics)."""
    issues: list[Issue] = []
    conn = connectivity(sch, workdir)
    bodies = {c.name: cdb.body_rect(c, workdir) for c in sch.components if c.name != "*" or True}
    comps = sch.components

    # --- grid
    for c in comps:
        if c.x % GRID or c.y % GRID:
            issues.append(Issue("style", "off_grid_component",
                                f"{c.name} ({c.type}) at ({c.x},{c.y}) is not on the {GRID}-grid",
                                c.x, c.y, [c.name]))
    for i, w in enumerate(sch.wires):
        if any(v % GRID for v in (w.x1, w.y1, w.x2, w.y2)):
            issues.append(Issue("style", "off_grid_wire", f"wire #{i} has off-grid end points",
                                w.x1, w.y1, [f"wire#{i}"]))
        if w.x1 != w.x2 and w.y1 != w.y2:
            issues.append(Issue("style", "diagonal_wire", f"wire #{i} is diagonal; use orthogonal segments",
                                w.x1, w.y1, [f"wire#{i}"]))
        if w.is_point() and not w.label:
            issues.append(Issue("style", "zero_length_wire", f"wire #{i} has zero length",
                                w.x1, w.y1, [f"wire#{i}"]))

    # --- endpoints / pins lying on wire interiors (look connected but are not)
    endpoints = {}
    for i, w in enumerate(sch.wires):
        endpoints.setdefault(w.p1, []).append(i)
        endpoints.setdefault(w.p2, []).append(i)
    for i, w in enumerate(sch.wires):
        if w.is_point():
            continue
        for (x, y), idxs in endpoints.items():
            if i in idxs:
                continue
            if w.contains(x, y, include_ends=False):
                issues.append(Issue("error", "t_junction_unconnected",
                                    f"wire end at ({x},{y}) touches the middle of wire #{i} but is NOT "
                                    "connected (Qucs only joins at wire end points). Split the wire "
                                    "or run tidy_schematic(connect_t_junctions=true).",
                                    x, y, [f"wire#{i}"] + [f"wire#{j}" for j in idxs]))
        for p in conn.pins:
            if w.contains(p.x, p.y, include_ends=False):
                issues.append(Issue("error", "pin_on_wire_unconnected",
                                    f"pin {p.ref} at ({p.x},{p.y}) lies on the middle of wire #{i} but is "
                                    "NOT connected. Move the part or split the wire.",
                                    p.x, p.y, [p.ref, f"wire#{i}"]))

    # --- overlapping collinear wires
    for (i, a), (j, b) in itertools.combinations(enumerate(sch.wires), 2):
        if a.is_point() or b.is_point():
            continue
        if a.y1 == a.y2 == b.y1 == b.y2:
            lo = max(min(a.x1, a.x2), min(b.x1, b.x2))
            hi = min(max(a.x1, a.x2), max(b.x1, b.x2))
            if hi > lo:
                issues.append(Issue("warning", "overlapping_wires",
                                    f"wires #{i} and #{j} overlap on y={a.y1}", lo, a.y1,
                                    [f"wire#{i}", f"wire#{j}"]))
        elif a.x1 == a.x2 == b.x1 == b.x2:
            lo = max(min(a.y1, a.y2), min(b.y1, b.y2))
            hi = min(max(a.y1, a.y2), max(b.y1, b.y2))
            if hi > lo:
                issues.append(Issue("warning", "overlapping_wires",
                                    f"wires #{i} and #{j} overlap on x={a.x1}", a.x1, lo,
                                    [f"wire#{i}", f"wire#{j}"]))

    # --- wires through component bodies
    for i, w in enumerate(sch.wires):
        if w.is_point():
            continue
        for c in comps:
            r = bodies[c.name] if c.name in bodies else cdb.body_rect(c, workdir)
            r = cdb.body_rect(c, workdir)
            if seg_intersects_rect(w.x1, w.y1, w.x2, w.y2, r, shrink=2):
                issues.append(Issue("warning", "wire_through_component",
                                    f"wire #{i} runs through the symbol of {c.name}",
                                    (w.x1 + w.x2) / 2, (w.y1 + w.y2) / 2, [f"wire#{i}", c.name]))

    # --- overlapping component bodies
    rects = [(c, cdb.body_rect(c, workdir)) for c in comps]
    for (c1, r1), (c2, r2) in itertools.combinations(rects, 2):
        if rect_area_overlap(r1, r2) > 4:
            issues.append(Issue("error" if not (cdb.is_sim_block(c1) or cdb.is_sim_block(c2)) else "warning",
                                "overlapping_components", f"{c1.name} and {c2.name} overlap",
                                (max(r1[0], r2[0]) + min(r1[2], r2[2])) / 2,
                                (max(r1[1], r2[1]) + min(r1[3], r2[3])) / 2, [c1.name, c2.name]))

    # --- unconnected pins / single-pin nets
    point_count: dict = {}
    for p in conn.pins:
        point_count[(p.x, p.y)] = point_count.get((p.x, p.y), 0) + 1
    for w in sch.wires:
        for pt in (w.p1, w.p2):
            point_count[pt] = point_count.get(pt, 0) + 1
    for p in conn.pins:
        if p.comp.startswith("GND@"):
            if point_count.get((p.x, p.y), 0) < 2:
                issues.append(Issue("warning", "floating_ground", "ground symbol is not connected",
                                    p.x, p.y))
            continue
        if point_count.get((p.x, p.y), 0) < 2:
            issues.append(Issue("error", "unconnected_pin", f"pin {p.ref} is not connected", p.x, p.y,
                                [p.ref]))
    for n in conn.nets:
        real = [p for p in n.pins]
        if len(real) == 1 and n.wires and not real[0].comp.startswith("GND@") and not n.labels:
            issues.append(Issue("warning", "dangling_net",
                                f"net {n.name} only reaches {real[0].ref}", real[0].x, real[0].y,
                                [real[0].ref]))
        if not n.pins and n.wires:
            w = sch.wires[n.wires[0]]
            issues.append(Issue("warning", "orphan_wires",
                                f"wires {['#%d' % i for i in n.wires]} connect to no pin", w.x1, w.y1))

    # --- label text overlaps (needs measured boxes from a render)
    if label_boxes:
        for name, box in label_boxes.items():
            for c, r in rects:
                if c.name == name:
                    continue
                if rect_area_overlap(box, r) > 6:
                    issues.append(Issue("style", "label_overlaps_symbol",
                                        f"text of {name} overlaps symbol {c.name}",
                                        (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, [name, c.name]))
            for i, w in enumerate(sch.wires):
                if w.is_point():
                    continue
                if seg_intersects_rect(w.x1, w.y1, w.x2, w.y2, box, shrink=1.5):
                    issues.append(Issue("style", "label_overlaps_wire",
                                        f"text of {name} is crossed by wire #{i}",
                                        (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, [name, f"wire#{i}"]))
        for (n1, b1), (n2, b2) in itertools.combinations(label_boxes.items(), 2):
            if rect_area_overlap(b1, b2) > 6:
                issues.append(Issue("style", "labels_overlap", f"texts of {n1} and {n2} overlap",
                                    (b1[0] + b1[2]) / 2, (b1[1] + b1[3]) / 2, [n1, n2]))

    # de-duplicate
    seen, uniq = set(), []
    for it in issues:
        k = (it.code, it.message)
        if k not in seen:
            seen.add(k)
            uniq.append(it)

    metrics = wiring_metrics(sch)
    metrics["nets"] = len(conn.nets)
    metrics["errors"] = sum(1 for i in uniq if i.severity == "error")
    metrics["warnings"] = sum(1 for i in uniq if i.severity == "warning")
    metrics["style_issues"] = sum(1 for i in uniq if i.severity == "style")
    return uniq, metrics


def wiring_metrics(sch: Schematic) -> dict:
    length = 0
    segs = [w for w in sch.wires if not w.is_point()]
    for w in segs:
        length += abs(w.x2 - w.x1) + abs(w.y2 - w.y1)
    crossings = 0
    for a, b in itertools.combinations(segs, 2):
        if a.y1 == a.y2 and b.x1 == b.x2:
            h, v = a, b
        elif a.x1 == a.x2 and b.y1 == b.y2:
            h, v = b, a
        else:
            continue
        if (min(h.x1, h.x2) < v.x1 < max(h.x1, h.x2)) and (min(v.y1, v.y2) < h.y1 < max(v.y1, v.y2)):
            crossings += 1
    # bends: points where exactly one horizontal and one vertical segment meet
    deg: dict = {}
    for w in segs:
        o = "h" if w.y1 == w.y2 else "v"
        for pt in (w.p1, w.p2):
            deg.setdefault(pt, []).append(o)
    bends = sum(1 for v in deg.values() if len(v) == 2 and v[0] != v[1])
    return {"wire_segments": len(segs), "total_wire_length": length, "wire_crossings": crossings,
            "wire_bends": bends}


# ================================================================== router
DIRS = [(1, 0), (0, 1), (-1, 0), (0, -1)]


def pin_outward(pin, comp: Component | None, workdir=None) -> tuple[int, int] | None:
    if comp is None:
        return None
    r = cdb.body_rect(comp, workdir)
    if pin.x <= r[0]:
        return (-1, 0)
    if pin.x >= r[2]:
        return (1, 0)
    if pin.y <= r[1]:
        return (0, -1)
    if pin.y >= r[3]:
        return (0, 1)
    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
    dx, dy = pin.x - cx, pin.y - cy
    if abs(dx) >= abs(dy):
        return (1 if dx > 0 else -1, 0)
    return (0, 1 if dy > 0 else -1)


class Router:
    """Grid A* router producing orthogonal wires with few bends."""

    BEND = 12
    CROSS = 8
    STEP = 1
    NEAR_BODY = 2

    def __init__(self, sch: Schematic, workdir: Path | None = None, avoid_boxes: list | None = None):
        self.sch = sch
        self.workdir = workdir
        self.conn = connectivity(sch, workdir)
        self.comp_by_name = {c.name: c for c in sch.components}
        self.blocked: set = set()
        self.near: set = set()
        self.soft: set = set()
        xs, ys = [], []
        for c in sch.components:
            r = cdb.body_rect(c, workdir)
            xs += [r[0], r[2]]
            ys += [r[1], r[3]]
            gx1, gy1 = _ceil(r[0]), _ceil(r[1])
            gx2, gy2 = _floor(r[2]), _floor(r[3])
            for gx in range(gx1, gx2 + 1, GRID):
                for gy in range(gy1, gy2 + 1, GRID):
                    self.blocked.add((gx, gy))
            for gx in range(gx1 - GRID, gx2 + 2 * GRID, GRID):
                for gy in range(gy1 - GRID, gy2 + 2 * GRID, GRID):
                    self.near.add((gx, gy))
        for b in avoid_boxes or []:
            for gx in range(_ceil(b[0]), _floor(b[2]) + 1, GRID):
                for gy in range(_ceil(b[1]), _floor(b[3]) + 1, GRID):
                    self.soft.add((gx, gy))
        for w in sch.wires:
            xs += [w.x1, w.x2]
            ys += [w.y1, w.y2]
        for p in self.conn.pins:
            xs.append(p.x)
            ys.append(p.y)
        if not xs:
            xs, ys = [0], [0]
        m = 200
        self.bounds = (_floor(min(xs)) - m, _floor(min(ys)) - m, _ceil(max(xs)) + m, _ceil(max(ys)) + m)
        self._rebuild_wire_index()

    def _rebuild_wire_index(self):
        self.conn = connectivity(self.sch, self.workdir)
        # grid point -> net ids occupying it; edge occupancy for collinear overlap
        self.point_owner: dict = {}
        self.hedge: dict = {}
        self.vedge: dict = {}
        for i, w in enumerate(self.sch.wires):
            net = self.conn.wire_net[i]
            for pt in _grid_points(w):
                self.point_owner.setdefault(pt, set()).add(net)
            if w.y1 == w.y2:
                for x in range(min(w.x1, w.x2), max(w.x1, w.x2), GRID):
                    self.hedge.setdefault((x, w.y1), set()).add(net)
            elif w.x1 == w.x2:
                for y in range(min(w.y1, w.y2), max(w.y1, w.y2), GRID):
                    self.vedge.setdefault((w.x1, y), set()).add(net)
        self.pin_owner = {}
        for p in self.conn.pins:
            self.pin_owner.setdefault((p.x, p.y), set()).add(self.conn.pin_net[(p.comp, p.index)])

    def route(self, start, goals: set, net_id: int | None, start_dir=None, goal_dirs: dict | None = None,
              allowed: set | None = None):
        """A* from start point to any of goals. Returns list of points or None."""
        allowed = set(allowed or ()) | {start} | set(goals)
        bx1, by1, bx2, by2 = self.bounds
        gl = list(goals)

        def h(pt):
            return min(abs(pt[0] - g[0]) + abs(pt[1] - g[1]) for g in gl) // GRID

        counter = itertools.count()
        openh = [(h(start), next(counter), 0, start, -1)]
        best = {(start, -1): 0}
        parent = {}
        while openh:
            f, _, g, pt, d = heapq.heappop(openh)
            if pt in goals and pt != start:
                path = [pt]
                key = (pt, d)
                while key in parent:
                    key = parent[key]
                    path.append(key[0])
                return list(reversed(path))
            if g > best.get((pt, d), 1e18):
                continue
            for nd, (dx, dy) in enumerate(DIRS):
                if d >= 0 and (nd + 2) % 4 == d:
                    continue  # no U-turn
                nx, ny = pt[0] + dx * GRID, pt[1] + dy * GRID
                npt = (nx, ny)
                if not (bx1 <= nx <= bx2 and by1 <= ny <= by2):
                    continue
                if npt in self.blocked and npt not in allowed:
                    continue
                # cannot leave a pin into its own body
                if pt == start and pt in self.blocked and npt in self.blocked:
                    continue
                # collinear overlap with a foreign wire?
                if dx:
                    ek = (min(pt[0], nx), ny)
                    owners = self.hedge.get(ek)
                else:
                    ek = (nx, min(pt[1], ny))
                    owners = self.vedge.get(ek)
                if owners and (net_id is None or owners - {net_id}):
                    continue
                cost = self.STEP
                po = self.point_owner.get(npt)
                pin_o = self.pin_owner.get(npt)
                if npt not in goals:
                    if pin_o and (net_id is None or pin_o - {net_id}):
                        continue
                    if pin_o:
                        cost += 30  # do not pass through own pins
                    if po and (net_id is None or po - {net_id}):
                        # crossing a foreign wire: only allowed perpendicular through
                        # its interior, never on one of its end points (would connect)
                        if npt in self.conn.point_net and self.conn.point_net[npt] != net_id:
                            continue
                        cost += self.CROSS
                    elif po and npt not in goals:
                        cost += 4  # running along own net is fine but unnecessary
                if d >= 0 and nd != d:
                    cost += self.BEND
                if pt == start and start_dir is not None and DIRS[nd] != start_dir:
                    cost += self.BEND * 2
                if npt in goals and goal_dirs and goal_dirs.get(npt) is not None:
                    gd = goal_dirs[npt]
                    if (dx, dy) != (-gd[0], -gd[1]):
                        cost += self.BEND * 2
                if npt in self.near:
                    cost += self.NEAR_BODY
                if npt in self.soft:
                    cost += 3
                ng = g + cost
                key = (npt, nd)
                if ng < best.get(key, 1e18):
                    best[key] = ng
                    parent[key] = (pt, d)
                    heapq.heappush(openh, (ng + h(npt), next(counter), ng, npt, nd))
        return None


def _floor(v):
    return int(v // GRID) * GRID


def _ceil(v):
    return -int((-v) // GRID) * GRID


def _grid_points(w: Wire):
    if w.y1 == w.y2:
        lo, hi = sorted((w.x1, w.x2))
        return [(x, w.y1) for x in range(_ceil(lo), hi + 1, GRID)]
    if w.x1 == w.x2:
        lo, hi = sorted((w.y1, w.y2))
        return [(w.x1, y) for y in range(_ceil(lo), hi + 1, GRID)]
    return [w.p1, w.p2]


def compress(path):
    if not path:
        return []
    out = [path[0]]
    for i in range(1, len(path) - 1):
        a, b, c = out[-1], path[i], path[i + 1]
        if (a[0] == b[0] == c[0]) or (a[1] == b[1] == c[1]):
            continue
        out.append(b)
    out.append(path[-1])
    return out


def split_wire_at(sch: Schematic, x: int, y: int) -> bool:
    """If (x, y) is on the interior of a wire, split it there. Returns True if split."""
    for i, w in enumerate(sch.wires):
        if not w.is_point() and w.contains(x, y, include_ends=False):
            a = Wire(w.x1, w.y1, x, y, w.label, w.lx, w.ly, w.loff, w.extra)
            b = Wire(x, y, w.x2, w.y2)
            sch.wires[i:i + 1] = [a, b]
            return True
    return False


def add_polyline(sch: Schematic, pts, label: str = "") -> list[Wire]:
    pts = compress(pts)
    new = []
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        if (x1, y1) != (x2, y2):
            new.append(Wire(int(x1), int(y1), int(x2), int(y2)))
    for x, y in (pts[0], pts[-1]):
        split_wire_at(sch, int(x), int(y))
    existing_ends = {w.p1 for w in sch.wires} | {w.p2 for w in sch.wires}
    final = []
    for w in new:
        # split the new wire where existing wire ends touch its interior (router only
        # allows that for the same net) so that Qucs really connects them
        pts_on = sorted((pt for pt in existing_ends if w.contains(pt[0], pt[1], include_ends=False)),
                        key=lambda pt: abs(pt[0] - w.x1) + abs(pt[1] - w.y1))
        prev = w.p1
        for pt in pts_on:
            final.append(Wire(prev[0], prev[1], pt[0], pt[1]))
            prev = pt
        final.append(Wire(prev[0], prev[1], w.x2, w.y2))
    new = final
    sch.wires.extend(new)
    if label and new:
        w0 = new[0]
        w0.label = label
        w0.lx, w0.ly = w0.x1 + 10, w0.y1 - 20
    return new


def resolve_point(sch: Schematic, ref, workdir=None):
    """'R1.1' / 'R1.2' / 'Q1.B' / [x, y] -> (x, y, pin_or_None)."""
    if isinstance(ref, (list, tuple)) and len(ref) == 2:
        return int(ref[0]), int(ref[1]), None
    s = str(ref).strip()
    if s.count(",") == 1 and s.replace(",", "").replace("-", "").replace(" ", "").isdigit():
        a, b = s.split(",")
        return int(a), int(b), None
    if "." in s or ":" in s:
        sep = "." if "." in s else ":"
        comp, pin = s.rsplit(sep, 1)
    else:
        comp, pin = s, "1"
    c = sch.find(comp)
    plist = cdb.pins(c, workdir)
    for p in plist:
        if str(p.index) == pin or p.name.lower() == pin.lower():
            return p.x, p.y, p
    raise KeyError(f"{c.name} has no pin '{pin}'. Pins: " + ", ".join(f"{p.index}/{p.name}" for p in plist))


def connect(sch: Schematic, a, b, workdir=None, avoid_boxes=None) -> list[Wire]:
    """Auto-route an orthogonal wire between two pins / points."""
    ax, ay, pa = resolve_point(sch, a, workdir)
    bx, by, pb = resolve_point(sch, b, workdir)
    router = Router(sch, workdir, avoid_boxes)
    net_a = router.conn.point_net.get((ax, ay))
    net_b = router.conn.point_net.get((bx, by))
    net_id = net_a if net_a is not None else net_b
    goals = {(bx, by)}
    # allow finishing on any wire *physically* attached to the target (not on wires that are
    # only logically the same net via labels / other ground symbols)
    for i in physical_wires_at(sch, (bx, by)):
        goals.update(_grid_points(sch.wires[i]))
    goals.discard((ax, ay))
    if not goals:
        return []
    sd = pin_outward(pa, router.comp_by_name.get(pa.comp), workdir) if pa else None
    gd = {(bx, by): pin_outward(pb, router.comp_by_name.get(pb.comp), workdir)} if pb else None
    # treat both nets as "own" for collision purposes
    if net_a is not None and net_b is not None and net_a != net_b:
        _merge_owner(router, net_b, net_a)
    path = router.route((ax, ay), goals, net_id, sd, gd)
    if path is None:
        raise RuntimeError(f"No orthogonal route found from {a} to {b}; move parts apart or route manually.")
    return add_polyline(sch, path)


def physical_wires_at(sch: Schematic, pt) -> set[int]:
    """Indices of wires connected (through wire end points only) to point pt."""
    start = {i for i, w in enumerate(sch.wires) if w.contains(pt[0], pt[1])}
    seen = set(start)
    frontier = [sch.wires[i] for i in start]
    while frontier:
        w = frontier.pop()
        for j, o in enumerate(sch.wires):
            if j in seen:
                continue
            if o.p1 in (w.p1, w.p2) or o.p2 in (w.p1, w.p2):
                seen.add(j)
                frontier.append(o)
    return seen


def wire_label_box(w: Wire) -> tuple:
    from .svgtools import text_width_em
    tw = text_width_em(w.label) * 17.5
    return (w.lx, w.ly, w.lx + tw, w.ly + 21)


def best_wire_label_spot(sch: Schematic, w: Wire, ax: int, ay: int, workdir=None,
                         extra_boxes: list | None = None) -> tuple[int, int]:
    """Choose a text position for a wire label anchored at (ax, ay) that avoids symbols/texts."""
    from .svgtools import text_width_em
    tw = text_width_em(w.label) * 17.5
    th = 21
    bodies = [cdb.body_rect(c, workdir) for c in sch.components]
    segs = [o for o in sch.wires if not o.is_point()]
    others = list(extra_boxes or []) + [wire_label_box(o) for o in sch.wires if o.label and o is not w]
    cands = [(12, -th - 12), (12, 10), (-tw - 12, -th - 12), (-tw - 12, 10),
             (-tw / 2, -th - 18), (-tw / 2, 14), (20, -th - 30), (-tw - 20, -th - 30)]
    best, bc = cands[0], 1e18
    for k, (dx, dy) in enumerate(cands):
        box = (ax + dx, ay + dy, ax + dx + tw, ay + dy + th)
        cost = k
        for r in bodies:
            cost += rect_area_overlap(box, r) * 3
        for r in others:
            cost += rect_area_overlap(box, r) * 3
        for o in segs:
            cost += seg_rect_length_inside(o.x1, o.y1, o.x2, o.y2, box) * 25
        if cost < bc:
            best, bc = (dx, dy), cost
    return int(ax + best[0]), int(ay + best[1])


def place_wire_labels(sch: Schematic, workdir=None, comp_label_boxes: list | None = None) -> int:
    n = 0
    for w in sch.wires:
        if not w.label:
            continue
        if w.is_point():
            ax, ay = w.x1, w.y1
        else:
            L = abs(w.x2 - w.x1) + abs(w.y2 - w.y1)
            off = max(0, min(w.loff, L))
            t = off / L if L else 0
            ax, ay = int(w.x1 + (w.x2 - w.x1) * t), int(w.y1 + (w.y2 - w.y1) * t)
        lx, ly = best_wire_label_spot(sch, w, ax, ay, workdir, comp_label_boxes)
        if (lx, ly) != (w.lx, w.ly):
            w.lx, w.ly = lx, ly
            n += 1
    return n


def _merge_owner(router: Router, src: int, dst: int):
    for d in (router.point_owner, router.hedge, router.vedge, router.pin_owner):
        for k, v in d.items():
            if src in v:
                v.add(dst)
    for k, v in list(router.conn.point_net.items()):
        if v == src:
            router.conn.point_net[k] = dst


# ============================================================ rip-up/reroute
@dataclass
class Group:
    """A set of pins that are (or must be) wired together, plus labels on that wiring."""
    pin_keys: list            # [(id(component), pin index)]
    labels: list              # [(name, lx, ly, old_x, old_y)]


def _pin_key(sch: Schematic, p) -> tuple:
    if p.comp.startswith("GND@"):
        x, y = p.comp[4:].split(",")
        for c in sch.components:
            if c.type == "GND" and c.x == int(x) and c.y == int(y):
                return (id(c), 1)
    return (id(sch.find(p.comp)), p.index)


def _pins_by_key(sch: Schematic, workdir=None) -> dict:
    out = {}
    for c in sch.components:
        for p in cdb.pins(c, workdir):
            out[(id(c), p.index)] = p
    return out


def collect_groups(sch: Schematic, *, nets: list[str] | None = None, comps: list[str] | None = None,
                   workdir=None) -> tuple[list[Group], set]:
    """Physical wire clusters selected by net name / attached component. Returns (groups, wire idx)."""
    conn = connectivity(sch, workdir)
    wanted = None
    if nets is not None or comps is not None:
        wanted = set()
        for n in nets or []:
            net = conn.net_by_name(n)
            if net is None:
                raise KeyError(f"Unknown net '{n}'. Nets: " + ", ".join(x.name for x in conn.nets))
            wanted.update(net.clusters)
        comp_set = {c.lower() for c in comps or []}
        for cid, plist in conn.cluster_pins.items():
            if any(p.comp.lower() in comp_set for p in plist):
                wanted.add(cid)
    groups, remove = [], set()
    for cid in set(conn.cluster_wires) | set(conn.cluster_pins):
        if wanted is not None and cid not in wanted:
            continue
        widx = conn.cluster_wires.get(cid, [])
        plist = conn.cluster_pins.get(cid, [])
        labels = [(sch.wires[i].label, sch.wires[i].lx, sch.wires[i].ly, sch.wires[i].x1, sch.wires[i].y1)
                  for i in widx if sch.wires[i].label]
        if not widx:
            # pins touching directly (no wires) - nothing to re-create unless they get separated
            if len(plist) >= 2:
                groups.append(Group([_pin_key(sch, p) for p in plist], []))
            continue
        if len(plist) + len(labels) < 2:
            continue
        groups.append(Group([_pin_key(sch, p) for p in plist], labels))
        remove.update(widx)
    return groups, remove


def route_groups(sch: Schematic, groups: list[Group], workdir=None, avoid_boxes=None) -> dict:
    pk = _pins_by_key(sch, workdir)
    groups = sorted(groups, key=lambda g: -len(g.pin_keys))
    failed, routed = [], 0
    for g in groups:
        pins = [pk[k] for k in g.pin_keys if k in pk]
        ok = _route_cluster(sch, pins, g.labels, workdir, avoid_boxes)
        routed += 1 if ok else 0
        if not ok:
            failed.append([p.ref for p in pins])
    return {"routed_groups": routed, "failed_groups": failed}


def reroute(sch: Schematic, nets: list[str] | None = None, comps: list[str] | None = None,
            workdir=None, avoid_boxes=None, mutate=None) -> dict:
    """Rip up and re-create wiring with the router.

    Each group of pins that is currently wired together is reconnected with a
    minimal orthogonal tree; net labels are preserved. ``mutate`` (optional) is
    called after the old wires were removed - e.g. to move components.
    """
    groups, remove = collect_groups(sch, nets=nets, comps=comps, workdir=workdir)
    sch.wires[:] = [w for i, w in enumerate(sch.wires) if i not in remove]
    if mutate:
        mutate()
    res = route_groups(sch, groups, workdir, avoid_boxes)
    res["removed_wires"] = len(remove)
    return res


def _route_cluster(sch, pins, labels, workdir, avoid_boxes) -> bool:
    comp_by_name = {c.name: c for c in sch.components}
    ok = True
    tree_pts: set = set()
    if pins:
        remaining = sorted(pins, key=lambda p: (p.x, p.y))
        first = remaining.pop(0)
        tree_pts = {(first.x, first.y)}
        tree_pins = [first]
        while remaining:
            remaining.sort(key=lambda p: min(abs(p.x - t[0]) + abs(p.y - t[1]) for t in tree_pts))
            p = remaining.pop(0)
            if (p.x, p.y) in tree_pts:
                continue
            router = Router(sch, workdir, avoid_boxes)
            net_id = router.conn.point_net.get((first.x, first.y))
            pnet = router.conn.point_net.get((p.x, p.y))
            if pnet is not None and net_id is not None and pnet != net_id:
                _merge_owner(router, pnet, net_id)
            gdirs = {(tp.x, tp.y): pin_outward(tp, comp_by_name.get(tp.comp), workdir) for tp in tree_pins}
            sd = pin_outward(p, comp_by_name.get(p.comp), workdir)
            path = router.route((p.x, p.y), set(tree_pts), net_id, sd, gdirs)
            if path is None:
                ok = False
                continue
            add_polyline(sch, path)
            tree_pts.update(path)
            tree_pins.append(p)
    # re-attach labels to the nearest new segment of this tree
    for name, lx, ly, ox, oy in labels:
        best, bestd = None, 1e18
        for w in sch.wires:
            if w.label or w.is_point():
                continue
            if w.p1 in tree_pts and w.p2 in tree_pts:
                mx, my = (w.x1 + w.x2) / 2, (w.y1 + w.y2) / 2
                d = abs(mx - ox) + abs(my - oy)
                if d < bestd:
                    best, bestd = w, d
        if best is None:
            if pins:
                p = pins[0]
                sch.wires.append(Wire(p.x, p.y, p.x, p.y, name, p.x + 10, p.y - 30, 0))
            continue
        mx, my = (best.x1 + best.x2) // 2, (best.y1 + best.y2) // 2
        best.label = name
        if abs(lx - mx) + abs(ly - my) > 60:
            lx, ly = mx + 10, my - 30
        best.lx, best.ly, best.loff = lx, ly, 0
    return ok


def move_components(sch: Schematic, moves: list[dict], keep_connections: bool = True, workdir=None,
                    avoid_boxes=None) -> dict:
    """moves: [{name, x?, y?, dx?, dy?, rotation?, mirror?}]"""
    names = []
    targets = []
    for m in moves:
        c = sch.find(m["name"]) if m.get("name") not in (None, "*") else None
        if c is None:
            raise KeyError("each move needs a component 'name'")
        names.append(c.name)
        targets.append((c, m))

    def apply():
        for c, m in targets:
            if "x" in m and m["x"] is not None:
                c.x = int(m["x"])
            if "y" in m and m["y"] is not None:
                c.y = int(m["y"])
            c.x += int(m.get("dx") or 0)
            c.y += int(m.get("dy") or 0)
            if m.get("rotation") is not None:
                c.rotate = (int(m["rotation"]) // 90) % 4
            if m.get("mirror") is not None:
                c.mirror = 1 if m["mirror"] else 0

    if not keep_connections:
        apply()
        return {"moved": names}
    res = reroute(sch, comps=names, workdir=workdir, avoid_boxes=avoid_boxes, mutate=apply)
    res["moved"] = names
    return res


# ==================================================================== tidy
def snap_to_grid(sch: Schematic) -> int:
    n = 0

    def s(v):
        return int(round(v / GRID)) * GRID

    for c in sch.components:
        nx, ny = s(c.x), s(c.y)
        if (nx, ny) != (c.x, c.y):
            c.x, c.y = nx, ny
            n += 1
    for w in sch.wires:
        vals = (s(w.x1), s(w.y1), s(w.x2), s(w.y2))
        if vals != (w.x1, w.y1, w.x2, w.y2):
            w.x1, w.y1, w.x2, w.y2 = vals
            n += 1
    return n


def normalize_wires(sch: Schematic, connect_t_junctions: bool = False, workdir=None) -> dict:
    """Remove zero-length/duplicate wires, merge collinear chains, optionally split at T-junctions."""
    stats = {"removed_zero_length": 0, "merged": 0, "split_t_junctions": 0, "removed_duplicates": 0}
    # zero length without label
    before = len(sch.wires)
    sch.wires[:] = [w for w in sch.wires if not (w.is_point() and not w.label)]
    stats["removed_zero_length"] = before - len(sch.wires)
    # duplicates
    seen, uniq = set(), []
    for w in sch.wires:
        k = tuple(sorted([w.p1, w.p2]))
        if k in seen and not w.label:
            stats["removed_duplicates"] += 1
            continue
        seen.add(k)
        uniq.append(w)
    sch.wires[:] = uniq
    if connect_t_junctions:
        changed = True
        while changed:
            changed = False
            pts = {w.p1 for w in sch.wires} | {w.p2 for w in sch.wires}
            pts |= {(p.x, p.y) for p in all_pins(sch, workdir)}
            for (x, y) in pts:
                if split_wire_at(sch, x, y):
                    stats["split_t_junctions"] += 1
                    changed = True
                    break
    # merge collinear chains through degree-2 points that are not pins
    pin_pts = {(p.x, p.y) for p in all_pins(sch, workdir)}
    changed = True
    while changed:
        changed = False
        ends: dict = {}
        for i, w in enumerate(sch.wires):
            if w.is_point():
                continue
            ends.setdefault(w.p1, []).append(i)
            ends.setdefault(w.p2, []).append(i)
        point_wires = {}
        for i, w in enumerate(sch.wires):
            if w.is_point():
                point_wires.setdefault(w.p1, []).append(i)
        for pt, idxs in ends.items():
            if len(idxs) != 2 or pt in pin_pts or pt in point_wires:
                continue
            i, j = idxs
            a, b = sch.wires[i], sch.wires[j]
            horiz = a.y1 == a.y2 == b.y1 == b.y2
            vert = a.x1 == a.x2 == b.x1 == b.x2
            if not (horiz or vert):
                continue
            if a.label and b.label:
                continue
            other_a = a.p2 if a.p1 == pt else a.p1
            other_b = b.p2 if b.p1 == pt else b.p1
            lab = a if a.label else b
            m = Wire(other_a[0], other_a[1], other_b[0], other_b[1], lab.label, lab.lx, lab.ly, 0, lab.extra)
            for k in sorted((i, j), reverse=True):
                del sch.wires[k]
            sch.wires.append(m)
            stats["merged"] += 1
            changed = True
            break
    return stats


def label_block_size(c: Component) -> tuple[float, float]:
    """Estimated size of a component's text block (name + shown properties)."""
    from .svgtools import text_width_em
    meta = cdb.info(c.type)
    names = [p["name"] for p in meta["props"]] if meta else []
    lines = [c.name]
    for i, (v, d) in enumerate(c.props):
        if str(d) == "1":
            n = names[i] if i < len(names) else ""
            lines.append(f"{n}={v}" if n and not cdb.is_sim_block(c) else str(v))
    fs = 17.5
    w = max(text_width_em(s) for s in lines) * fs
    h = len(lines) * 21
    return w, h


def place_labels(sch: Schematic, measured: dict | None = None, workdir=None) -> int:
    """Move each component's text block to the free side of its symbol."""
    measured = measured or {}
    rects = {c.name: cdb.body_rect(c, workdir) for c in sch.components}
    placed_boxes: list = []
    segs = [w for w in sch.wires if not w.is_point()]
    wire_label_boxes = []
    for w in sch.wires:
        if w.label:
            wire_label_boxes.append((w.lx - 2, w.ly - 2, w.lx + 10 * len(w.label) + 4, w.ly + 22))
    moved = 0
    pin_pts = [(p.x, p.y) for p in all_pins(sch, workdir)]
    for c in sorted(sch.components, key=lambda c: (c.y, c.x)):
        if c.type == "GND" or c.name == "*":
            continue
        r = rects[c.name]
        if c.name in measured:
            mw, mh = measured[c.name]
        else:
            mw, mh = label_block_size(c)
        gap = 6
        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
        if cdb.is_sim_block(c):
            cands = [(r[0], r[3] + 4), (r[2] + gap, r[1])]
        else:
            cands = [
                (r[2] + gap, cy - mh / 2),       # right, centered
                (r[0] - gap - mw, cy - mh / 2),  # left
                (cx - mw / 2, r[1] - gap - mh),  # above
                (cx - mw / 2, r[3] + gap),       # below
                (r[2] + gap, r[1] - mh),         # top-right
                (r[2] + gap, r[3]),              # bottom-right
                (r[0] - gap - mw, r[1] - mh),    # top-left
                (r[0] - gap - mw, r[3]),         # bottom-left
            ]
        best, bestcost = None, 1e18
        for k, (lx, ly) in enumerate(cands):
            box = (lx - 4, ly - 2, lx + mw + 4, ly + mh + 6)  # with clearance
            cost = k * 0.5
            for name, rr in rects.items():
                if name != c.name:
                    cost += rect_area_overlap(box, rr) * 2
            for b in placed_boxes + wire_label_boxes:
                cost += rect_area_overlap(box, b) * 3
            for w in segs:
                cost += seg_rect_length_inside(w.x1, w.y1, w.x2, w.y2, box) * 25
            for (px, py) in pin_pts:
                if box[0] < px < box[2] and box[1] < py < box[3]:
                    cost += 200
            if cost < bestcost:
                best, bestcost = (lx, ly), cost
        ntx, nty = int(round(best[0] - c.x)), int(round(best[1] - c.y))
        if (ntx, nty) != (c.tx, c.ty):
            c.tx, c.ty = ntx, nty
            moved += 1
        placed_boxes.append((best[0], best[1], best[0] + mw, best[1] + mh))
    return moved
