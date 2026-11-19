"""Run ngspice on a Qucs-S generated netlist and parse the results."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import qucs_cli
from .config import tools

_NO_WINDOW = 0x08000000 if hasattr(subprocess, "STARTUPINFO") else 0


@dataclass
class Plot:
    name: str                 # e.g. "ac1" (from file name) or plot title
    kind: str                 # ac | tran | dc | op | noise | sweep | other
    title: str
    scale: str | None
    vectors: dict[str, np.ndarray] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    source: str = ""

    def summary(self, max_vectors: int = 60) -> dict:
        out = {"analysis": self.name, "kind": self.kind, "title": self.title, "scale": self.scale,
               "points": int(len(next(iter(self.vectors.values())))) if self.vectors else 0,
               "vectors": {}}
        for k, v in list(self.vectors.items())[:max_vectors]:
            out["vectors"][k] = describe_vector(v, self.units.get(k, ""))
        return out


@dataclass
class SimResult:
    ok: bool
    netlist_path: str
    log: str
    plots: list[Plot]
    errors: list[str]
    warnings: list[str]
    elapsed_s: float
    workdir: str

    def find(self, analysis: str | None) -> Plot:
        if not self.plots:
            raise KeyError("No simulation results available")
        if analysis is None:
            return self.plots[0]
        a = analysis.lower()
        for p in self.plots:
            if p.name.lower() == a:
                return p
        for p in self.plots:
            if p.kind == a or p.name.lower().startswith(a):
                return p
        raise KeyError(f"No analysis '{analysis}'. Available: " + ", ".join(p.name for p in self.plots))


def describe_vector(v: np.ndarray, unit: str = "") -> dict:
    if v.size == 0:
        return {"n": 0}
    if np.iscomplexobj(v) and np.any(np.abs(v.imag) > 0):
        mag = np.abs(v)
        return {"n": int(v.size), "unit": unit, "complex": True,
                "mag_min": _f(mag.min()), "mag_max": _f(mag.max()),
                "first": [_f(v[0].real), _f(v[0].imag)], "last": [_f(v[-1].real), _f(v[-1].imag)]}
    r = np.real(v)
    if r.size == 1:
        return {"value": _f(r[0]), "unit": unit}
    return {"n": int(r.size), "unit": unit, "min": _f(r.min()), "max": _f(r.max()),
            "first": _f(r[0]), "last": _f(r[-1]), "mean": _f(r.mean())}


def _f(x) -> float:
    x = float(x)
    return float(f"{x:.6g}")


# ------------------------------------------------------------------ parsers
def parse_raw(path: Path) -> list[Plot]:
    data = path.read_bytes()
    plots: list[Plot] = []
    pos = 0
    while pos < len(data):
        # header
        hdr: dict[str, str] = {}
        variables: list[tuple[str, str]] = []
        while pos < len(data):
            nl = data.find(b"\n", pos)
            if nl < 0:
                nl = len(data)
            line = data[pos:nl].decode("latin-1").rstrip("\r")
            pos = nl + 1
            if not line.strip():
                continue
            if line.startswith("Variables:"):
                nvars = int(hdr.get("No. Variables", "0"))
                for _ in range(nvars):
                    nl = data.find(b"\n", pos)
                    vline = data[pos:nl].decode("latin-1").strip()
                    pos = nl + 1
                    parts = vline.split()
                    variables.append((parts[1], parts[2] if len(parts) > 2 else ""))
                continue
            if line.startswith(("Values:", "Binary:")):
                hdr["_format"] = line.rstrip(":")
                break
            if ":" in line:
                k, v = line.split(":", 1)
                hdr[k.strip()] = v.strip()
        if "_format" not in hdr:
            break
        complex_ = "complex" in hdr.get("Flags", "").lower()
        npts = int(hdr.get("No. Points", "0"))
        nvars = len(variables)
        if hdr["_format"] == "Binary":
            width = 16 if complex_ else 8
            nbytes = npts * nvars * width
            arr = np.frombuffer(data[pos:pos + nbytes], dtype="<f8")
            pos += nbytes
            if complex_:
                arr = arr.reshape(npts, nvars, 2)
                mat = arr[..., 0] + 1j * arr[..., 1]
            else:
                mat = arr.reshape(npts, nvars)
        else:
            ends = [e for e in (data.find(b"\nTitle:", pos), data.find(b"\nPlotname:", pos)) if e >= 0]
            end = min(ends) if ends else len(data)
            toks = data[pos:end].decode("latin-1").split()
            pos = end + 1
            mat = np.zeros((npts, nvars), dtype=complex if complex_ else float)
            k = 0
            for i in range(npts):
                k += 1  # point index
                for j in range(nvars):
                    if k >= len(toks):
                        break
                    s = toks[k]
                    k += 1
                    if complex_:
                        re_, _, im_ = s.partition(",")
                        mat[i, j] = complex(float(re_), float(im_ or 0))
                    else:
                        mat[i, j] = float(s)
        title = hdr.get("Plotname", "")
        p = Plot(name=path.name, kind=_kind_from_title(title), title=title,
                 scale=variables[0][0] if variables else None, source=str(path))
        for j, (vn, unit) in enumerate(variables):
            col = mat[:, j]
            if complex_ and (j == 0 or unit.split()[0].lower() in REAL_UNITS) \
                    and not np.any(np.abs(col.imag) > 1e-12 * (np.abs(col.real).max() + 1e-300)):
                # scale and real-valued post-processing results (e.g. dB(), ph()) are stored
                # as complex numbers with zero imaginary part in a complex plot
                col = col.real.copy()
            key = vn
            n = 2
            while key in p.vectors:
                key = f"{vn}#{n}"
                n += 1
            p.vectors[key] = col
            p.units[key] = unit
        plots.append(p)
    return plots


REAL_UNITS = {"frequency", "time", "decibel", "degree", "degrees", "radian", "radians", "hertz"}


def _kind_from_title(title: str) -> str:
    t = title.lower()
    for k, v in (("ac", "ac"), ("transient", "tran"), ("dc transfer", "dc"), ("operating", "op"),
                 ("noise", "noise"), ("fourier", "fourier"), ("distortion", "disto"), ("pole", "pz")):
        if k in t:
            return v
    return "other"


def parse_print(path: Path) -> Plot:
    """Parse `print a b > file` output of ngspice (operating point values)."""
    vecs = {}
    for line in path.read_text(encoding="latin-1").splitlines():
        m = re.match(r"\s*(\S+)\s*=\s*([-+0-9.eE]+)(?:\s*,\s*([-+0-9.eE]+))?", line)
        if m:
            val = float(m.group(2)) if m.group(3) is None else complex(float(m.group(2)), float(m.group(3)))
            vecs[m.group(1)] = np.array([val])
    return Plot(name=path.name, kind="op", title="Operating point", scale=None, vectors=vecs,
                source=str(path))


def _short_name(fname: str) -> str:
    # spice4qucs.ac1.plot -> ac1 ; spice4qucs.dc1.ngspice.dc.print -> dc1
    parts = fname.split(".")
    if len(parts) >= 3 and parts[0] == "spice4qucs":
        return parts[1]
    return fname


def _output_files(netlist: str) -> list[str]:
    files = []
    for line in netlist.splitlines():
        m = re.match(r"\s*(?:write|wrdata)\s+(\S+)", line, re.I)
        if m:
            files.append(m.group(1))
        m = re.search(r">\s*(\S+)\s*$", line)
        if m and line.strip().lower().startswith("print"):
            files.append(m.group(1))
    return files


# --------------------------------------------------------------------- run
def run_netlist(netlist_path: Path, workdir: Path, timeout: float = 300) -> SimResult:
    netlist = netlist_path.read_text(encoding="utf-8", errors="replace")
    expected = _output_files(netlist)
    for f in expected:
        (workdir / f).unlink(missing_ok=True)
    t0 = time.time()
    try:
        proc = subprocess.run([str(tools().ngspice), "-b", netlist_path.name], cwd=str(workdir),
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL)
        log = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    except subprocess.TimeoutExpired as e:
        log = f"ngspice timed out after {timeout}s\n" + str(e.stdout or "")
    elapsed = time.time() - t0
    (workdir / "ngspice.log").write_text(log, encoding="utf-8")
    errors, warnings = [], []
    for line in log.splitlines():
        ll = line.lower()
        if re.search(r"\berror\b|fatal|singular matrix|timestep too small|no such vector|"
                     r"unknown model|could not find|aborted|is not converging", ll):
            if line.strip() not in errors:
                errors.append(line.strip())
        elif "warning" in ll and line.strip() not in warnings:
            warnings.append(line.strip())
    plots: list[Plot] = []
    for f in expected:
        p = workdir / f
        if not p.exists():
            errors.append(f"expected output '{f}' was not produced")
            continue
        try:
            if f.endswith(".print"):
                pl = parse_print(p)
                pl.name = _short_name(f)
                plots.append(pl)
            else:
                sub = parse_raw(p)
                for k, pl in enumerate(sub):
                    pl.name = _short_name(f) + (f"_{k + 1}" if len(sub) > 1 else "")
                    plots.append(pl)
        except Exception as e:  # noqa: BLE001
            errors.append(f"failed to parse {f}: {e}")
    ok = bool(plots) and not any("expected output" in e for e in errors)
    return SimResult(ok=ok, netlist_path=str(netlist_path), log=log[-6000:], plots=plots,
                     errors=errors[:30], warnings=warnings[:30], elapsed_s=round(elapsed, 3),
                     workdir=str(workdir))


def simulate(sch_path: Path, timeout: float = 300) -> SimResult:
    sch_path = Path(sch_path).resolve()
    workdir = sch_path.parent / f"{sch_path.stem}_sim"
    workdir.mkdir(exist_ok=True)
    net = workdir / f"{sch_path.stem}.cir"
    qucs_cli.netlist_file(sch_path, net)
    text = net.read_text(encoding="utf-8", errors="replace")
    if ".control" not in text.lower() or not _output_files(text):
        raise RuntimeError("The schematic contains no simulation (add a .TR/.AC/.DC/... block "
                           "with add_simulation).")
    return run_netlist(net, workdir, timeout)


# ------------------------------------------------------------------- export
COMPLEX_FORMATS = ("real_imag", "mag_phase", "db_phase")


def export_plot(p: Plot, path: Path, vectors: list[str] | None = None, fmt: str = "csv",
                complex_format: str = "real_imag") -> dict:
    """Write every point of an analysis to CSV or JSON. Returns columns / row count."""
    import csv
    import json

    if complex_format not in COMPLEX_FORMATS:
        raise ValueError(f"complex_format must be one of {COMPLEX_FORMATS}")
    names = vectors or [k for k in p.vectors if k != p.scale]
    cols: list[str] = []
    data: list[np.ndarray] = []
    if p.scale is not None:
        cols.append(p.scale)
        data.append(np.real(p.vectors[p.scale]))
    for n in names:
        v = p.vectors[n]
        if np.iscomplexobj(v):
            if complex_format == "real_imag":
                cols += [f"{n}.re", f"{n}.im"]
                data += [v.real, v.imag]
            elif complex_format == "mag_phase":
                cols += [f"{n}.mag", f"{n}.phase_deg"]
                data += [np.abs(v), np.degrees(np.unwrap(np.angle(v)))]
            else:
                cols += [f"{n}.dB", f"{n}.phase_deg"]
                data += [20 * np.log10(np.maximum(np.abs(v), 1e-300)), np.degrees(np.unwrap(np.angle(v)))]
        else:
            cols.append(n)
            data.append(np.real(v))
    mat = np.column_stack(data) if data else np.zeros((0, 0))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for row in mat:
                w.writerow([repr(float(x)) for x in row])
    elif fmt == "json":
        path.write_text(json.dumps({
            "analysis": p.name, "kind": p.kind, "title": p.title, "scale": p.scale,
            "complex_format": complex_format, "columns": cols,
            "units": {n: p.units.get(n, "") for n in ([p.scale] if p.scale else []) + names},
            "data": {c: [float(x) for x in mat[:, i]] for i, c in enumerate(cols)},
        }, ensure_ascii=False), encoding="utf-8")
    else:
        raise ValueError("fmt must be 'csv' or 'json'")
    return {"analysis": p.name, "path": str(path), "columns": cols, "rows": int(mat.shape[0])}
