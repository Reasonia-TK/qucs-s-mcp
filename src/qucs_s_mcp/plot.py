"""Result plots (matplotlib, headless) and composite report images."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.ticker import EngFormatter  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from .simulate import Plot  # noqa: E402

COLORS = ["#1f6feb", "#d1242f", "#1a7f37", "#bf8700", "#8250df", "#0a7f8f", "#cf222e", "#57606a"]

plt.rcParams.update({
    "font.family": ["Segoe UI", "Yu Gothic UI", "DejaVu Sans"],
    "axes.grid": True, "grid.color": "#d8dee4", "grid.linewidth": 0.7,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.edgecolor": "#57606a", "axes.labelcolor": "#24292f",
    "xtick.color": "#57606a", "ytick.color": "#57606a",
    "legend.frameon": False, "figure.dpi": 110,
})


def match_vector(plot: Plot, name: str) -> str:
    keys = list(plot.vectors)
    low = {k.lower(): k for k in keys}
    n = name.strip().lower()
    for cand in (n, f"v({n})", f"i({n})", f"i(v{n})", n.replace("v(", "").rstrip(")")):
        if cand in low:
            return low[cand]
    raise KeyError(f"Vector '{name}' not in analysis '{plot.name}'. Available: {', '.join(keys)}")


def _unit_of(name: str, plot: Plot) -> str:
    u = plot.units.get(name, "")
    if u.startswith("voltage") or name.lower().startswith("v("):
        return "V"
    if u.startswith("current") or name.lower().startswith("i("):
        return "A"
    if u == "decibel":
        return "dB"
    return ""


def plot_vectors(plot: Plot, vectors: list[str] | None, out_png: Path, *, title: str | None = None,
                 mode: str = "auto", logx: bool | None = None, xlim=None, width_in: float = 8.0) -> dict:
    if plot.kind == "op" or plot.scale is None:
        return _plot_op(plot, vectors, out_png, title)
    xname = plot.scale
    x = np.real(plot.vectors[xname])
    if vectors:
        names = [match_vector(plot, v) for v in vectors]
    else:
        names = [k for k in plot.vectors if k != xname][:6]
    is_ac = plot.kind in ("ac", "noise") or xname == "frequency"
    if logx is None:
        logx = is_ac and x.size > 2 and x.min() > 0 and x.max() / max(x.min(), 1e-30) > 50
    bode = is_ac and mode in ("auto", "bode") and any(np.iscomplexobj(plot.vectors[n]) for n in names)
    if bode:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(width_in, width_in * 0.72), sharex=True,
                                       gridspec_kw={"height_ratios": [3, 2]})
        axes = [ax1, ax2]
    else:
        fig, ax1 = plt.subplots(figsize=(width_in, width_in * 0.5))
        axes = [ax1]
    stats = {}
    for i, n in enumerate(names):
        y = plot.vectors[n]
        col = COLORS[i % len(COLORS)]
        if bode:
            if plot.units.get(n) == "decibel":
                mag = np.real(y)
            else:
                mag = 20 * np.log10(np.maximum(np.abs(y), 1e-30))
            ax1.plot(x, mag, color=col, lw=1.8, label=n)
            if np.iscomplexobj(y):
                ax2.plot(x, np.degrees(np.unwrap(np.angle(y))), color=col, lw=1.4, label=n)
            k = int(np.argmax(mag))
            stats[n] = {"peak_dB": round(float(mag[k]), 3), "at": float(f"{x[k]:.6g}")}
            bw = _f3db(x, mag)
            if bw is not None:
                stats[n]["f_-3dB"] = float(f"{bw:.6g}")
        else:
            yr = np.real(y) if mode != "mag" else np.abs(y)
            ax1.plot(x, yr, color=col, lw=1.8, label=n)
            stats[n] = {"min": float(f"{yr.min():.6g}"), "max": float(f"{yr.max():.6g}"),
                        "final": float(f"{yr[-1]:.6g}")}
    for ax in axes:
        if logx:
            ax.set_xscale("log")
        else:
            ax.xaxis.set_major_formatter(EngFormatter(unit="s" if xname == "time" else ""))
        if xlim:
            ax.set_xlim(*xlim)
    if bode:
        ax1.set_ylabel("Magnitude [dB]")
        ax2.set_ylabel("Phase [deg]")
        ax2.set_xlabel("Frequency [Hz]")
        ax2.xaxis.set_major_formatter(EngFormatter(unit="Hz")) if not logx else None
    else:
        units = {_unit_of(n, plot) for n in names}
        if len(units) == 1 and "" not in units:
            ax1.yaxis.set_major_formatter(EngFormatter(unit=units.pop()))
        ax1.set_xlabel({"time": "Time", "frequency": "Frequency [Hz]"}.get(xname, xname))
    ax1.legend(loc="best", fontsize=9)
    ax1.set_title(title or f"{plot.title} ({plot.name})", fontsize=12, loc="left", color="#24292f")
    fig.tight_layout()
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=110, facecolor="white")
    plt.close(fig)
    return {"path": str(out_png), "stats": stats}


def _f3db(x, mag):
    if mag.size < 3:
        return None
    ref = mag[0]
    below = np.where(mag < ref - 3.0)[0]
    if below.size == 0 or below[0] == 0:
        return None
    i = below[0]
    x0, x1, y0, y1 = x[i - 1], x[i], mag[i - 1], mag[i]
    if y1 == y0:
        return float(x1)
    t = (ref - 3.0 - y0) / (y1 - y0)
    if x0 > 0 and x1 > 0:
        return float(10 ** (np.log10(x0) + t * (np.log10(x1) - np.log10(x0))))
    return float(x0 + t * (x1 - x0))


def _plot_op(plot: Plot, vectors, out_png, title):
    names = [match_vector(plot, v) for v in vectors] if vectors else list(plot.vectors)
    vals = [float(np.real(plot.vectors[n][0])) for n in names]
    fig, ax = plt.subplots(figsize=(7, 0.42 * len(names) + 1.2))
    ax.axis("off")
    eng = EngFormatter()
    rows = [[n, eng.format_eng(v) + ("V" if n.lower().startswith("v(") else
                                     "A" if n.lower().startswith("i(") else "")] for n, v in zip(names, vals)]
    tbl = ax.table(cellText=rows, colLabels=["quantity", "value"], loc="center", cellLoc="left")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(10)
    tbl.scale(1, 1.35)
    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor("#d0d7de")
        if r == 0:
            cell.set_facecolor("#f6f8fa")
            cell.set_text_props(weight="bold")
    ax.set_title(title or "Operating point", loc="left", fontsize=12)
    fig.tight_layout()
    fig.savefig(out_png, dpi=110, facecolor="white")
    plt.close(fig)
    return {"path": str(out_png), "stats": {n: v for n, v in zip(names, vals)}}


def compose_report(images: list[Path], out_png: Path, title: str = "", notes: str = "",
                   max_width: int = 1600) -> str:
    """Stack images vertically with a title bar - handy for progress reports."""
    ims = [Image.open(p).convert("RGB") for p in images]
    width = min(max_width, max(im.width for im in ims))
    scaled = []
    for im in ims:
        if im.width > width:
            im = im.resize((width, int(im.height * width / im.width)), Image.LANCZOS)
        scaled.append(im)
    try:
        font = ImageFont.truetype("seguisb.ttf", 30)
        small = ImageFont.truetype("YuGothM.ttc", 20)
    except OSError:
        font = small = ImageFont.load_default()
    head = 70 if title else 0
    note_lines = notes.splitlines() if notes else []
    note_h = 30 * len(note_lines) + (20 if note_lines else 0)
    H = head + note_h + sum(im.height + 20 for im in scaled) + 10
    canvas = Image.new("RGB", (width + 40, H), "white")
    d = ImageDraw.Draw(canvas)
    y = 10
    if title:
        d.rectangle([0, 0, width + 40, head - 10], fill="#24292f")
        d.text((20, 14), title, fill="white", font=font)
        y = head
    for line in note_lines:
        d.text((20, y), line, fill="#24292f", font=small)
        y += 30
    if note_lines:
        y += 20
    for im in scaled:
        canvas.paste(im, (20 + (width - im.width) // 2, y))
        y += im.height + 20
    out_png = Path(out_png)
    canvas.save(out_png)
    return str(out_png)
