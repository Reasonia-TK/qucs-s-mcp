"""Locate external tools (Qucs-S, ngspice) and shared paths."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_QUCS_CANDIDATES = [
    r"C:\Program Files\Qucs-S\bin\qucs-s.exe",
    r"C:\Program Files (x86)\Qucs-S\bin\qucs-s.exe",
    "/usr/bin/qucs-s",
    "/usr/local/bin/qucs-s",
    "/Applications/qucs-s.app/Contents/MacOS/qucs-s",
]


@dataclass(frozen=True)
class Tools:
    qucs_s: Path
    ngspice: Path
    qucs_share: Path | None

    @property
    def library_dir(self) -> Path | None:
        return self.qucs_share / "library" if self.qucs_share else None

    @property
    def examples_dir(self) -> Path | None:
        return self.qucs_share / "examples" if self.qucs_share else None


def _first_existing(paths) -> Path | None:
    for p in paths:
        if p and Path(p).is_file():
            return Path(p)
    return None


@lru_cache(maxsize=1)
def tools() -> Tools:
    qucs = _first_existing(
        [os.environ.get("QUCS_S_PATH"), shutil.which("qucs-s"), *_QUCS_CANDIDATES]
    )
    if qucs is None:
        raise RuntimeError(
            "qucs-s executable not found. Set the QUCS_S_PATH environment variable "
            "to the full path of qucs-s(.exe)."
        )
    qbin = qucs.parent
    ngspice = _first_existing(
        [
            os.environ.get("NGSPICE_PATH"),
            shutil.which("ngspice_con"),
            r"C:\Spice64\bin\ngspice_con.exe",
            qbin / "ngspice_con.exe",
            shutil.which("ngspice"),
            qbin / "ngspice",
        ]
    )
    if ngspice is None:
        raise RuntimeError(
            "ngspice executable not found. Set NGSPICE_PATH to ngspice_con.exe "
            "(Windows console build) or ngspice."
        )
    share = None
    for cand in (qbin.parent / "share" / "qucs-s", Path("/usr/share/qucs-s")):
        if cand.is_dir():
            share = cand
            break
    return Tools(qucs_s=qucs, ngspice=ngspice, qucs_share=share)


def workspace_dir() -> Path:
    """Default directory for new schematics / snapshots."""
    d = Path(os.environ.get("QUCS_MCP_WORKDIR", Path.cwd() / "qucs_mcp_work"))
    d.mkdir(parents=True, exist_ok=True)
    return d


DATA_DIR = Path(__file__).parent / "data"
