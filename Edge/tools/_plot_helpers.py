"""Shared helpers for plot_rmse.py and plot_burst.py."""

from __future__ import annotations

import importlib.util as _ilu
import sys
from pathlib import Path

# -- lazy package import --------------------------------------------------

def _require(pkg: str):
    try:
        return __import__(pkg)
    except ImportError:
        print(f"ERROR: {pkg} required — pip install {pkg}", file=sys.stderr)
        sys.exit(1)

# -- load_model import (no hardware deps) ---------------------------------

_lm_path = Path(__file__).resolve().parents[1] / "speedflow_python" / "load_model.py"
_spec = _ilu.spec_from_file_location("load_model", _lm_path)
_lm = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_lm)

ProactiveModel = _lm.ProactiveModel
CycleSmoother = _lm.CycleSmoother

# -- CSV row → feature/metrics dicts --------------------------------------

def _build_feature_stats(row) -> dict:
    return {"cam_merged": {
        "n_track":             float(row.get("n_track_total",           0.0)),
        "n_plate":             float(row.get("n_plate_total",           0.0)),
        "stationary_fraction": float(row.get("stationary_fraction_mean", 0.0)),
    }}

def _build_metrics(row) -> dict:
    return {
        "gpu_percent": float(row.get("gpu_percent", 0.0)),
        "cpu_percent": float(row.get("cpu_percent", 0.0)),
        "ram_percent": float(row.get("ram_percent", 0.0)),
        "gpu_temp_c":  float(row.get("gpu_temp_c",  0.0)),
    }