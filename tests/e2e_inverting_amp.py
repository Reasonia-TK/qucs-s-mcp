"""End-to-end smoke test: build, wire, tidy, snapshot and simulate an inverting amplifier.

Run:  uv run python tests/e2e_inverting_amp.py
"""

import json
import shutil
from pathlib import Path

from qucs_s_mcp import server as S

OUT = Path(__file__).resolve().parent.parent / "examples"


def show(title, obj):
    txt = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    print(f"--- {title}: {txt[:1500]}")


def main():
    OUT.mkdir(exist_ok=True)
    shutil.rmtree(OUT / "snapshots", ignore_errors=True)
    S.new_schematic(str(OUT / "inverting_amp.sch"), overwrite=True)
    show("add", S.add_components([
        {"type": "Vac", "name": "V1", "x": 100, "y": 330, "rotation": 90,
         "properties": {"U": "1 V", "f": "1 kHz"}},
        {"type": "GND", "x": 100, "y": 400},
        {"type": "R", "name": "R1", "x": 220, "y": 260, "properties": {"R": "10k"}},
        {"type": "C", "name": "C1", "x": 420, "y": 100, "properties": {"C": "1.5n"}},
        {"type": "R", "name": "R2", "x": 420, "y": 160, "properties": {"R": "47k"}},
        {"type": "OpAmp", "name": "OP1", "x": 420, "y": 280, "mirror": True},
        {"type": "GND", "x": 360, "y": 360},
        {"type": "R", "name": "RL", "x": 560, "y": 340, "rotation": 270, "properties": {"R": "2k"}},
        {"type": "GND", "x": 560, "y": 400},
    ]))
    show("connect", S.connect([
        ["V1.+", "R1.1"],
        ["R1.2", "OP1.IN-"],
        ["V1.-", [100, 400]],
        ["OP1.IN+", [360, 360]],
        ["OP1.OUT", "RL.1"],
        ["RL.2", [560, 400]],
        ["R2.2", "OP1.OUT"],
        ["R2.1", "R1.2"],
        ["C1.1", "R2.1"],
        ["C1.2", "R2.2"],
    ]))
    S.add_net_label("OP1.OUT", "out")
    S.add_net_label("R1.1", "in")
    show("sim ac", S.add_simulation("ac", {"Type": "log", "Start": "10 Hz", "Stop": "10 MHz", "Points": "301"}))
    show("sim tr", S.add_simulation("tran", {"Stop": "3 ms", "Points": "601"}))
    show("eq", S.add_nutmeg_equation("ac", {"gain_db": "dB(v(out)/v(in))"}))

    res = S.snapshot(nets=True, pins=True, issues=True, title="inverting amp - draft")
    show("snapshot draft", res[0])
    show("check", S.check_layout())
    show("tidy", S.tidy_schematic())
    res = S.snapshot(grid=False, rulers=False, title="inverting amp - tidy")
    show("snapshot tidy", res[0])
    sim = S.run_simulation()
    show("sim", sim)
    show("results", S.get_results("ac1", ["v(out)", "gain_db"], at=[100, 1000, 100000]))
    show("plot", S.plot_results("ac1", ["gain_db"])[0])
    show("report", S.create_report("Inverting amplifier (G=-4.7)", notes="AC + transient, ngspice",
                                   plots=[{"analysis": "ac1", "vectors": ["v(out)"]},
                                          {"analysis": "tr1", "vectors": ["v(in)", "v(out)"]}])[0])


if __name__ == "__main__":
    main()
