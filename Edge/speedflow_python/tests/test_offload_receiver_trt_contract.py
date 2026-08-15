"""
Edge/speedflow_python/tests/test_offload_receiver_trt_contract.py

Host-side deterministic tests for the TRT runtime contract in
offload_receiver.py (Phase 2 fix).

Covers:
  1. Missing tensorrt/pycuda: _trt_available=False, engines_loaded=True,
     engines remain None, one-time ERROR logged (no per-crop WARNING).
  2. TRT available, engines loaded: _trt_available=True, engine objects set.
  3. _InferenceFailure.fatal flag: fatal=True skips rate-limited WARNING;
     counter still incremented.
  4. _run_lpr/_run_lpd distinguish "TRT absent" vs "engine file missing":
     both return _InferenceFailure but with distinct reasons and fatal bits.
  5. Engine file missing (TRT present): _trt_available=True, engine=None,
     fatal=False so rate-limited WARNING fires.
  6. trt_available property: None before probe, False after absent-TRT load.
  7. No double-load: _load_engines_once is idempotent.
  8. _engines_loaded=True after TRT-absent path (no retry on next crop).

No real tensorrt/pycuda required — _try_import_trt is patched.

Run:
    conda run -n DoAn python3 speedflow_python/tests/test_offload_receiver_trt_contract.py
    conda run -n DoAn python3 -m pytest speedflow_python/tests/test_offload_receiver_trt_contract.py -q
"""

import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

EDGE = Path(__file__).resolve().parents[2]  # repo Edge/ root
sys.path.insert(0, str(EDGE))


# ---------------------------------------------------------------------------
# Module loader (same stub pattern as test_trt_dynamic_shape.py)
# ---------------------------------------------------------------------------

def _load_offload_receiver():
    pkg = sys.modules.get("speedflow_python")
    if pkg is None:
        pkg = types.ModuleType("speedflow_python")
        pkg.__path__ = [str(EDGE / "speedflow_python")]
        sys.modules["speedflow_python"] = pkg

    zs = sys.modules.get("speedflow_python.zenoh_session")
    if zs is None:
        zs = types.ModuleType("speedflow_python.zenoh_session")
        zs.make_session = lambda: None
        sys.modules["speedflow_python.zenoh_session"] = zs

    from importlib.util import spec_from_file_location, module_from_spec

    mod_name = "speedflow_python.offload_receiver"
    path = EDGE / "speedflow_python/offload_receiver.py"
    spec = spec_from_file_location(mod_name, path)
    mod = module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_or = _load_offload_receiver()
OffloadReceiver = _or.OffloadReceiver
_InferenceFailure = _or._InferenceFailure


# ---------------------------------------------------------------------------
# Minimal stub OffloadReceiver (no Zenoh session needed for unit tests)
# ---------------------------------------------------------------------------

def _make_receiver(**kwargs):
    """Build a bare OffloadReceiver without a real Zenoh session."""
    defaults = dict(
        node_id="test_node",
        session=MagicMock(),
        lpr_engine_path="/fake/lpr.engine",
        lpd_engine_path="/fake/lpd.engine",
        labels_path="/fake/labels_lpr.txt",
    )
    defaults.update(kwargs)
    return OffloadReceiver(**defaults)


# ---------------------------------------------------------------------------
# 1. Missing TRT: state after _load_engines_once with absent deps
# ---------------------------------------------------------------------------

def test_missing_trt_sets_trt_available_false():
    """When tensorrt/pycuda absent, _trt_available must be False (not None)."""
    rcv = _make_receiver()
    assert rcv.trt_available is None, "Should be None before first probe"
    with patch.object(_or, "_try_import_trt", return_value=(None, None)):
        rcv._load_engines_once()
    assert rcv.trt_available is False


def test_missing_trt_engines_remain_none():
    """Engines must stay None; _engines_loaded must be True (no retry)."""
    rcv = _make_receiver()
    with patch.object(_or, "_try_import_trt", return_value=(None, None)):
        rcv._load_engines_once()
    assert rcv._lpr_engine is None
    assert rcv._lpd_engine is None
    assert rcv._engines_loaded is True


def test_missing_trt_logs_error_once(caplog):
    """A single actionable ERROR must be emitted — never per-crop WARNINGs."""
    rcv = _make_receiver()
    with caplog.at_level(logging.ERROR, logger="speedflow_python.offload_receiver"):
        with patch.object(_or, "_try_import_trt", return_value=(None, None)):
            rcv._load_engines_once()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    msg = errors[0].message
    assert "tensorrt" in msg.lower() or "pycuda" in msg.lower(), (
        f"ERROR should mention tensorrt/pycuda; got: {msg!r}"
    )
    assert "install" in msg.lower(), (
        f"ERROR should include install guidance; got: {msg!r}"
    )


def test_missing_trt_no_repeated_warnings(caplog):
    """fatal=True path: _record_inference_error must NOT emit rate-limited WARNINGs."""
    rcv = _make_receiver()
    with patch.object(_or, "_try_import_trt", return_value=(None, None)):
        rcv._load_engines_once()

    # Simulate 10 crops arriving — should produce 0 per-crop WARNINGs
    with caplog.at_level(logging.WARNING, logger="speedflow_python.offload_receiver"):
        caplog.clear()
        for _ in range(10):
            rcv._record_inference_error("LPR", "tensorrt/pycuda not installed", fatal=True)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 0, (
        f"fatal=True must suppress per-crop WARNINGs; got {len(warnings)} warnings"
    )


def test_missing_trt_inference_errors_still_counted():
    """Even with fatal=True, the counter must increment for health telemetry."""
    rcv = _make_receiver()
    with patch.object(_or, "_try_import_trt", return_value=(None, None)):
        rcv._load_engines_once()
    for _ in range(5):
        rcv._record_inference_error("LPR", "tensorrt/pycuda not installed", fatal=True)
    assert rcv.offload_inference_errors_count == 5


# ---------------------------------------------------------------------------
# 2. TRT present, engine files absent: trt_available=True, engines None
# ---------------------------------------------------------------------------

def _make_mock_trt_cuda():
    trt = MagicMock()
    cuda = MagicMock()
    return trt, cuda


def test_trt_present_missing_engine_files():
    """TRT installed but engine files absent: _trt_available=True, engines None."""
    rcv = _make_receiver(
        lpr_engine_path="/nonexistent/lpr.engine",
        lpd_engine_path="/nonexistent/lpd.engine",
    )
    trt, cuda = _make_mock_trt_cuda()
    with patch.object(_or, "_try_import_trt", return_value=(trt, cuda)):
        rcv._load_engines_once()
    assert rcv.trt_available is True
    assert rcv._lpr_engine is None
    assert rcv._lpd_engine is None


def test_trt_present_missing_engine_reason_is_not_fatal():
    """Engine-file-missing path: _InferenceFailure.fatal must be False."""
    fail = _InferenceFailure("LPR engine not loaded (file missing or failed to parse)")
    assert fail.fatal is False


# ---------------------------------------------------------------------------
# 3. _run_lpr/_run_lpd: TRT absent returns fatal InferenceFailure
# ---------------------------------------------------------------------------

def test_run_lpr_trt_absent_returns_fatal_failure():
    """_run_lpr returns _InferenceFailure(fatal=True) when TRT not installed."""
    rcv = _make_receiver()
    with patch.object(_or, "_try_import_trt", return_value=(None, None)):
        rcv._load_engines_once()
    dummy_crop = np.zeros((48, 96, 3), dtype=np.uint8)
    result = rcv._run_lpr(dummy_crop)
    assert isinstance(result, _InferenceFailure)
    assert result.fatal is True
    assert "tensorrt" in result.reason.lower() or "pycuda" in result.reason.lower()


def test_run_lpd_trt_absent_returns_fatal_failure():
    """_run_lpd returns _InferenceFailure(fatal=True) when TRT not installed."""
    rcv = _make_receiver()
    with patch.object(_or, "_try_import_trt", return_value=(None, None)):
        rcv._load_engines_once()
    dummy_crop = np.zeros((100, 100, 3), dtype=np.uint8)
    result = rcv._run_lpd(dummy_crop)
    assert isinstance(result, _InferenceFailure)
    assert result.fatal is True


def test_run_lpr_engine_missing_returns_nonfatal_failure():
    """Engine file absent (TRT present): _InferenceFailure.fatal is False."""
    rcv = _make_receiver(lpr_engine_path="/nonexistent/lpr.engine")
    trt, cuda = _make_mock_trt_cuda()
    with patch.object(_or, "_try_import_trt", return_value=(trt, cuda)):
        rcv._load_engines_once()
    dummy_crop = np.zeros((48, 96, 3), dtype=np.uint8)
    result = rcv._run_lpr(dummy_crop)
    assert isinstance(result, _InferenceFailure)
    assert result.fatal is False
    # Reason must be distinct from the TRT-absent message
    assert "engine not loaded" in result.reason or "missing" in result.reason


# ---------------------------------------------------------------------------
# 4. _InferenceFailure.fatal attribute contract
# ---------------------------------------------------------------------------

def test_inference_failure_default_not_fatal():
    f = _InferenceFailure("some error")
    assert f.fatal is False


def test_inference_failure_fatal_kwarg():
    f = _InferenceFailure("trt missing", fatal=True)
    assert f.fatal is True
    assert f.reason == "trt missing"


# ---------------------------------------------------------------------------
# 5. trt_available property: None before probe
# ---------------------------------------------------------------------------

def test_trt_available_none_before_load():
    """Property must return None until _load_engines_once is called."""
    rcv = _make_receiver()
    assert rcv.trt_available is None


# ---------------------------------------------------------------------------
# 6. Idempotency: _load_engines_once does not run twice
# ---------------------------------------------------------------------------

def test_load_engines_once_idempotent():
    """Second call to _load_engines_once must be a no-op."""
    rcv = _make_receiver()
    call_count = 0

    original = _or._try_import_trt

    def counting_import():
        nonlocal call_count
        call_count += 1
        return None, None

    with patch.object(_or, "_try_import_trt", side_effect=counting_import):
        rcv._load_engines_once()
        rcv._load_engines_once()  # second call — must not re-enter
        rcv._load_engines_once()  # third call

    assert call_count == 1, (
        f"_try_import_trt should be called exactly once; called {call_count} times"
    )


# ---------------------------------------------------------------------------
# 7. Non-fatal warning fires once per interval (existing behavior preserved)
# ---------------------------------------------------------------------------

def test_nonfatal_inference_error_warning_fires(caplog):
    """Non-fatal errors (engine file missing) must still emit rate-limited WARNINGs."""
    rcv = _make_receiver()
    # Force last log time to -inf so the interval gate is open
    rcv._last_inference_error_log = float("-inf")
    with caplog.at_level(logging.WARNING, logger="speedflow_python.offload_receiver"):
        rcv._record_inference_error(
            "LPR", "LPR engine not loaded (file missing or failed to parse)", fatal=False
        )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "LPR" in warnings[0].message


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    tests = [
        test_missing_trt_sets_trt_available_false,
        test_missing_trt_engines_remain_none,
        test_missing_trt_no_repeated_warnings,
        test_missing_trt_inference_errors_still_counted,
        test_trt_present_missing_engine_files,
        test_trt_present_missing_engine_reason_is_not_fatal,
        test_run_lpr_trt_absent_returns_fatal_failure,
        test_run_lpd_trt_absent_returns_fatal_failure,
        test_run_lpr_engine_missing_returns_nonfatal_failure,
        test_inference_failure_default_not_fatal,
        test_inference_failure_fatal_kwarg,
        test_trt_available_none_before_load,
        test_load_engines_once_idempotent,
        test_nonfatal_inference_error_warning_fires,
    ]
    # caplog tests need pytest; run them via pytest only
    caplog_tests = {
        test_missing_trt_logs_error_once,
        test_missing_trt_no_repeated_warnings,
        test_nonfatal_inference_error_warning_fires,
    }

    passed = failed = skipped = 0
    for t in tests:
        if t in caplog_tests:
            print(f"  SKIP  {t.__name__}  (needs pytest caplog fixture)")
            skipped += 1
            continue
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception:
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped (run with pytest for caplog tests)")
    import sys as _sys
    _sys.exit(0 if failed == 0 else 1)
