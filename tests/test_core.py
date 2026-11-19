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


def test_t_junction_is_connected_like_qucs():
    # verified with the qucs-s netlister: a wire END on another wire's middle joins the nets
    s = Schematic.new()
    s.components += [Component.parse('R R1 1 100 100 0 0 0 0 "1k" 1'),
                     Component.parse('R R2 1 180 160 0 0 0 1 "2k" 1')]   # R2 top pin at (180,130)
    s.wires += [Wire(130, 100, 230, 100), Wire(180, 130, 180, 100)]
    conn = layout.connectivity(s)
    assert conn.net_of_pin("R1", 2).id == conn.net_of_pin("R2", 2).id
    issues, _ = layout.lint(s)
    assert not any("junction" in i.code for i in issues)
    # tidy makes the node explicit without changing connectivity
    stats = layout.normalize_wires(s)
    assert stats["split_t_junctions"] == 1 and len(s.wires) == 3


def test_pin_on_wire_middle_is_not_connected():
    s = Schematic.new()
    s.components += [Component.parse('R R1 1 100 100 0 0 0 0 "1k" 1'),
                     Component.parse('R R2 1 180 130 0 0 0 1 "2k" 1')]   # R2 top pin (180,100)
    s.wires += [Wire(130, 100, 230, 100)]
    conn = layout.connectivity(s)
    assert conn.net_of_pin("R1", 2).id != conn.net_of_pin("R2", 2).id
    issues, _ = layout.lint(s)
    assert any(i.code == "pin_on_wire_unconnected" for i in issues)
    layout.normalize_wires(s, connect_pins_on_wires=True)
    conn = layout.connectivity(s)
    assert conn.net_of_pin("R1", 2).id == conn.net_of_pin("R2", 2).id


def test_connect_branches_instead_of_doubling():
    s = Schematic.new()
    s.components += [Component.parse('R R1 1 100 140 0 0 0 0 "1k" 1'),
                     Component.parse('C C1 1 140 200 0 0 0 1 "1n" 1'),
                     Component.parse('R R2 1 220 140 0 0 0 0 "1k" 1')]
    layout.connect(s, "R1.2", "C1.2")
    layout.connect(s, "R1.2", "R2.1")
    issues, _ = layout.lint(s)
    assert not any(i.code == "overlapping_wires" for i in issues)
    conn = layout.connectivity(s)
    assert len({conn.net_of_pin(*r).id for r in (("R1", 2), ("C1", 2), ("R2", 1))}) == 1
    assert layout.connect(s, "R2.1", "C1.2") == []   # already connected -> nothing added


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


def _ac_raw_with_db(tmp_path):
    raw = tmp_path / "ac.raw"
    raw.write_text(
        "Title: t\nDate: d\nPlotname: AC Analysis\nFlags: complex\nNo. Variables: 3\nNo. Points: 2\n"
        "Variables:\n\t0\tfrequency\tfrequency grid=3\n\t1\tv(out)\tvoltage\n\t2\tgain_db\tdecibel\n"
        "Values:\n"
        " 0\t1.0e+01,0.0e+00\n\t-4.7e+00,0.0e+00\n\t1.3442e+01,0.0e+00\n\n"
        " 1\t1.0e+03,0.0e+00\n\t0.0e+00,-1.0e+00\n\t0.0e+00,0.0e+00\n")
    return parse_raw(raw)[0]


def test_decibel_vectors_are_real(tmp_path):
    p = _ac_raw_with_db(tmp_path)
    assert not np.iscomplexobj(p.vectors["gain_db"])
    assert not np.iscomplexobj(p.vectors["frequency"])
    assert np.iscomplexobj(p.vectors["v(out)"])   # genuine phasor stays complex
    assert p.vectors["gain_db"][0] == pytest.approx(13.442)


def test_export_csv_and_json(tmp_path):
    import csv
    import json
    from qucs_s_mcp.simulate import export_plot
    p = _ac_raw_with_db(tmp_path)
    info = export_plot(p, tmp_path / "ac.csv", None, "csv", "db_phase")
    assert info["rows"] == 2
    rows = list(csv.reader(open(tmp_path / "ac.csv", encoding="utf-8")))
    assert rows[0] == ["frequency", "v(out).dB", "v(out).phase_deg", "gain_db"]
    assert float(rows[1][1]) == pytest.approx(20 * np.log10(4.7))
    assert float(rows[2][2]) % 360 == pytest.approx(270.0)  # unwrapped from 180 deg = -90 deg
    assert float(rows[1][3]) == pytest.approx(13.442)
    export_plot(p, tmp_path / "ac.json", ["v(out)"], "json", "real_imag")
    d = json.loads((tmp_path / "ac.json").read_text(encoding="utf-8"))
    assert d["columns"] == ["frequency", "v(out).re", "v(out).im"]
    assert d["data"]["v(out).im"] == [0.0, -1.0]


def test_kind_from_title():
    from qucs_s_mcp.simulate import _kind_from_title as k
    assert k("DC transfer characteristic") == "dc"
    assert k("AC Analysis") == "ac"
    assert k("Transient Analysis") == "tran"
    assert k("Operating Point") == "op"


def test_parse_sweep_steps(tmp_path):
    from qucs_s_mcp.simulate import parse_sweep_steps
    f = tmp_path / "spice4qucs.tr1.cir.res"
    f.write_text("STEP tr1.rx\n0  1000\n1  2000\n2  3000\n")
    assert parse_sweep_steps(f) == [{"rx": 1000.0}, {"rx": 2000.0}, {"rx": 3000.0}]
