from pathlib import Path

import numpy as np
import pytest

from qucs_s_mcp import components as cdb
from qucs_s_mcp import layout
from qucs_s_mcp.schematic import Component, Schematic, Wire, transform_point
from qucs_s_mcp.simulate import parse_print, parse_raw

QUCS = Path(r"C:\Program Files\Qucs-S\bin\qucs-s.exe")


def test_component_roundtrip():
    line = 'R R1 1 660 460 16 -18 0 1 "1k" 1 "26.85" 0 "0.0" 0 "0.0" 0 "26.85" 0 "european" 0'
    c = Component.parse(line)
    assert (c.type, c.name, c.x, c.y, c.rotate) == ("R", "R1", 660, 460, 1)
    assert c.props[0] == ["1k", 1]
    assert c.serialize() == f"<{line}>"


def test_schematic_roundtrip_keeps_sections():
    s = Schematic.new()
    s.components.append(Component.parse('GND * 1 0 0 0 0 0 0'))
    s.wires.append(Wire(0, 0, 0, -30, "out", 10, -20, 0))
    s.raw_sections["Diagrams"] = ["  <Rect 1 2 3>", "  </Rect>"]
    t = s.serialize()
    s2 = Schematic.parse(t)
    assert s2.serialize() == t
    assert s2.wires[0].label == "out"


def test_transform_matches_qucs():
    # verified empirically against qucs-s netlisting
    assert transform_point(-30, 0, 0, 1) == (0, 30)
    assert transform_point(0, -30, 1, 0) == (0, 30)
    assert transform_point(0, -30, 1, 1) == (30, 0)


def test_pins_and_connectivity():
    s = Schematic.new()
    r = Component.parse('R R1 1 100 100 0 0 0 0 "1k" 1')
    g = Component.parse('GND * 1 130 160 0 0 0 0')
    s.components += [r, g]
    pins = cdb.pins(r)
    assert [(p.x, p.y) for p in pins] == [(70, 100), (130, 100)]
    s.wires.append(Wire(130, 100, 130, 160))
    conn = layout.connectivity(s)
    assert conn.net_of_pin("R1", 2).name == "gnd"


def test_lint_detects_unconnected_t_junction():
    s = Schematic.new()
    s.wires += [Wire(0, 0, 100, 0), Wire(50, 0, 50, 50)]
    issues, _ = layout.lint(s)
    assert any(i.code == "t_junction_unconnected" for i in issues)
    layout.normalize_wires(s, connect_t_junctions=True)
    issues, _ = layout.lint(s)
    assert not any(i.code == "t_junction_unconnected" for i in issues)


def test_merge_collinear():
    s = Schematic.new()
    s.wires += [Wire(0, 0, 50, 0), Wire(50, 0, 100, 0)]
    layout.normalize_wires(s)
    assert len(s.wires) == 1 and (s.wires[0].x1, s.wires[0].x2) in ((0, 100), (100, 0))


def test_router_avoids_bodies():
    s = Schematic.new()
    s.components.append(Component.parse('R R1 1 100 100 0 0 0 0 "1k" 1'))
    s.components.append(Component.parse('R R2 1 300 100 0 0 0 0 "1k" 1'))
    s.components.append(Component.parse('C C1 1 200 100 0 0 0 1 "1n" 1'))  # vertical cap in the way
    wires = layout.connect(s, "R1.2", "R2.1")
    body = cdb.body_rect(s.find("C1"))
    for w in wires:
        assert not layout.seg_intersects_rect(w.x1, w.y1, w.x2, w.y2, body)
        assert w.x1 == w.x2 or w.y1 == w.y2


def test_parse_ascii_raw(tmp_path):
    raw = tmp_path / "a.raw"
    raw.write_text(
        "Title: t\nDate: d\nPlotname: AC Analysis\nFlags: complex\nNo. Variables: 2\nNo. Points: 2\n"
        "Variables:\n\t0\tfrequency\tfrequency grid=3\n\t1\tv(out)\tvoltage\nValues:\n"
        " 0\t1.0e+00,0.0e+00\n\t2.0e+00,1.0e+00\n\n 1\t1.0e+01,0.0e+00\n\t1.0e+00,-1.0e+00\n")
    p = parse_raw(raw)[0]
    assert p.kind == "ac"
    assert np.allclose(p.vectors["frequency"], [1, 10])
    assert np.allclose(p.vectors["v(out)"], [2 + 1j, 1 - 1j])


def test_parse_print(tmp_path):
    f = tmp_path / "x.print"
    f.write_text("v(out) = 1.250000e+00\ni(vpr1) = -2.0e-03\n")
    p = parse_print(f)
    assert p.vectors["v(out)"][0] == pytest.approx(1.25)
