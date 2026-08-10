"""
Edge/speedflow_python/tests/test_trt_dynamic_shape.py

Deterministic mock tests for _TRTEngine dynamic-shape contract in
offload_receiver.py.

Covers:
- TRT10 dynamic success (profile 0 opt shape -> setter ok -> output resolved)
- TRT8 dynamic success (profile 0 opt shape -> setter ok -> output resolved)
- Setter failure (set_input_shape / set_binding_shape returns False)
- Unresolved output / profile invalid (non-positive dims in opt or output)
- infer() input-size mismatch validation

No tensorrt/pycuda required — _try_import_trt is patched with mocks.
"""
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

EDGE = Path(__file__).resolve().parents[2]  # repo Edge/ root
sys.path.insert(0, str(EDGE))

# ---------------------------------------------------------------------------
# Load offload_receiver without importing the real speedflow_python package
# (its __init__ needs gi) and without depending on a real zenoh_session.
# ---------------------------------------------------------------------------

def _load_offload_receiver():
    # Stub the package + its zenoh_session dependency.
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


_offload_receiver = _load_offload_receiver()
_TRTEngine = _offload_receiver._TRTEngine


# ---------------------------------------------------------------------------
# Mock building blocks
# ---------------------------------------------------------------------------

def _make_mock_trt():
    """Mock tensorrt module: Logger/Runtime/TensorIOMode/nptype only."""
    trt = MagicMock()
    trt.Logger.WARNING = 2
    trt.Logger = MagicMock(return_value=MagicMock())
    trt.TensorIOMode = MagicMock()
    trt.TensorIOMode.INPUT = "INPUT"
    trt.TensorIOMode.OUTPUT = "OUTPUT"
    trt.nptype = lambda dt: np.dtype(np.float32)  # all tensors float32
    return trt


def _make_mock_cuda():
    """Mock pycuda.driver: pagelocked_empty/mem_alloc/Stream/copies."""
    cuda = MagicMock()
    cuda.pagelocked_empty = lambda size, dtype: np.zeros(size, dtype=dtype)
    cuda.mem_alloc = lambda nbytes: 0x1000  # fake device address (int)
    cuda.Stream = MagicMock(return_value=MagicMock(handle=123))
    cuda.memcpy_htod_async = MagicMock()
    cuda.memcpy_dtoh_async = MagicMock()
    return cuda


def _mock_engine_trt10(trt, dynamic_input=True, profile_opt=(1, 3, 48, 96),
                       profile_opt_valid=True):
    """TRT10-style mock engine: num_io_tensors, name-based tensor API."""
    engine = MagicMock()
    engine.num_io_tensors = 2

    engine.get_tensor_name = lambda i: "input" if i == 0 else "output"

    def get_tensor_shape(name):
        if name == "input":
            return (-1, 3, 48, 96) if dynamic_input else (1, 3, 48, 96)
        return (-1, 68, 1)
    engine.get_tensor_shape = get_tensor_shape

    engine.get_tensor_dtype = lambda name: MagicMock()
    engine.get_tensor_mode = lambda name: (
        trt.TensorIOMode.INPUT if name == "input" else trt.TensorIOMode.OUTPUT
    )

    if profile_opt_valid:
        engine.get_tensor_profile_shape = lambda name, p: (
            (1, 3, 24, 48), profile_opt, (1, 3, 96, 192)
        )
    else:
        engine.get_tensor_profile_shape = lambda name, p: (
            (1, 3, 24, 48), (1, 3, -1, 96), (1, 3, 96, 192)
        )
    return engine


def _mock_context_trt10(setter_succeeds=True, resolved_output=(1, 68, 1),
                       all_binding_shapes_specified: bool | None = True):
    ctx = MagicMock()
    ctx.set_input_shape = MagicMock(return_value=setter_succeeds)
    ctx.get_tensor_shape = MagicMock(
        side_effect=lambda name: (1, 3, 48, 96) if name == "input" else resolved_output
    )
    ctx.set_tensor_address = MagicMock()
    ctx.execute_async_v3 = MagicMock()
    ctx.all_binding_shapes_specified = all_binding_shapes_specified
    return ctx


def _mock_engine_trt8(trt, dynamic_input=True, profile_opt=(1, 3, 48, 96),
                      profile_opt_valid=True):
    """TRT8-style mock engine: num_bindings, binding-index API.
    Using spec= so it must NOT expose num_io_tensors (MagicMock auto-creates
    attributes, which would falsely select the TRT10 path).
    """
    engine = MagicMock(spec=[
        "num_bindings", "get_binding_shape", "get_binding_dtype",
        "binding_is_input", "get_profile_shape", "create_execution_context",
    ])
    engine.num_bindings = 2

    def get_binding_shape(i):
        if i == 0:
            return (-1, 3, 48, 96) if dynamic_input else (1, 3, 48, 96)
        return (-1, 68, 1)
    engine.get_binding_shape = get_binding_shape
    engine.get_binding_dtype = lambda i: MagicMock()
    engine.binding_is_input = lambda i: i == 0

    if profile_opt_valid:
        engine.get_profile_shape = lambda p, i: (
            (1, 3, 24, 48), profile_opt, (1, 3, 96, 192)
        )
    else:
        engine.get_profile_shape = lambda p, i: (
            (1, 3, 24, 48), (1, 3, -1, 96), (1, 3, 96, 192)
        )
    return engine


def _mock_context_trt8(setter_succeeds=True, resolved_output=(1, 68, 1),
                       all_binding_shapes_specified: bool | None = True):
    ctx = MagicMock()
    ctx.set_binding_shape = MagicMock(return_value=setter_succeeds)
    ctx.get_binding_shape = MagicMock(
        side_effect=lambda i: (1, 3, 48, 96) if i == 0 else resolved_output
    )
    ctx.execute_async_v2 = MagicMock()
    ctx.all_binding_shapes_specified = all_binding_shapes_specified
    return ctx


def _build(trt, cuda, engine, context):
    """Patch _try_import_trt so _TRTEngine.__init__ gets the mocks.
    Also mock open() so no real .engine file needs to exist.
    """
    def fake_open(*args, **kwargs):
        f = MagicMock()
        f.read.return_value = b"fake-engine-bytes"
        f.__enter__.return_value = f
        return f

    runtime = MagicMock()
    runtime.deserialize_cuda_engine = MagicMock(return_value=engine)
    trt.Runtime = MagicMock(return_value=runtime)
    engine.create_execution_context = MagicMock(return_value=context)
    return patch.multiple(
        _offload_receiver,
        _try_import_trt=MagicMock(return_value=(trt, cuda)),
        open=fake_open,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_trt10_dynamic_success():
    """TRT10: dynamic input -> profile 0 opt -> setter ok -> output resolved."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt10(trt, dynamic_input=True)
    context = _mock_context_trt10(setter_succeeds=True)

    with _build(trt, cuda, engine, context):
        eng = _TRTEngine("/fake/path.engine")

    assert eng._input_shapes == [(1, 3, 48, 96)]          # resolved from opt
    assert eng._output_shapes == [(1, 68, 1)]             # resolved from context
    context.set_input_shape.assert_called_once_with("input", (1, 3, 48, 96))
    assert eng._use_trt10_api is True
    print("  PASS  test_trt10_dynamic_success")


def test_trt8_dynamic_success():
    """TRT8: dynamic input -> profile 0 opt -> setter ok -> output resolved."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt8(trt, dynamic_input=True)
    context = _mock_context_trt8(setter_succeeds=True)

    with _build(trt, cuda, engine, context):
        eng = _TRTEngine("/fake/path.engine")

    assert eng._input_shapes == [(1, 3, 48, 96)]
    assert eng._output_shapes == [(1, 68, 1)]
    context.set_binding_shape.assert_called_once_with(0, (1, 3, 48, 96))
    assert eng._use_trt10_api is False
    print("  PASS  test_trt8_dynamic_success")


def test_trt10_setter_failure():
    """TRT10: set_input_shape returns False -> RuntimeError."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt10(trt, dynamic_input=True)
    context = _mock_context_trt10(setter_succeeds=False)

    with _build(trt, cuda, engine, context):
        try:
            _TRTEngine("/fake/path.engine")
            assert False, "Expected RuntimeError"
        except RuntimeError as e:
            assert "set_input_shape failed" in str(e)
            assert "input" in str(e)
    print("  PASS  test_trt10_setter_failure")


def test_trt8_setter_failure():
    """TRT8: set_binding_shape returns False -> RuntimeError."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt8(trt, dynamic_input=True)
    context = _mock_context_trt8(setter_succeeds=False)

    with _build(trt, cuda, engine, context):
        try:
            _TRTEngine("/fake/path.engine")
            assert False, "Expected RuntimeError"
        except RuntimeError as e:
            assert "set_binding_shape failed" in str(e)
            assert "binding 0" in str(e)
    print("  PASS  test_trt8_setter_failure")


def test_trt10_profile_opt_invalid_nonpositive():
    """TRT10: profile 0 opt shape has a non-positive dim -> RuntimeError."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt10(trt, dynamic_input=True, profile_opt_valid=False)
    context = _mock_context_trt10(setter_succeeds=True)

    with _build(trt, cuda, engine, context):
        try:
            _TRTEngine("/fake/path.engine")
            assert False, "Expected RuntimeError"
        except RuntimeError as e:
            assert "non-positive dimension" in str(e)
            assert "opt shape" in str(e)
    print("  PASS  test_trt10_profile_opt_invalid_nonpositive")


def test_trt8_profile_opt_invalid_nonpositive():
    """TRT8: profile 0 opt shape has a non-positive dim -> RuntimeError."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt8(trt, dynamic_input=True, profile_opt_valid=False)
    context = _mock_context_trt8(setter_succeeds=True)

    with _build(trt, cuda, engine, context):
        try:
            _TRTEngine("/fake/path.engine")
            assert False, "Expected RuntimeError"
        except RuntimeError as e:
            assert "non-positive dimension" in str(e)
            assert "opt shape" in str(e)
    print("  PASS  test_trt8_profile_opt_invalid_nonpositive")


def test_trt10_unresolved_output_nonpositive():
    """TRT10: context resolves output with a non-positive dim -> RuntimeError."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt10(trt, dynamic_input=True)
    context = _mock_context_trt10(setter_succeeds=True, resolved_output=(1, -1, 1))

    with _build(trt, cuda, engine, context):
        try:
            _TRTEngine("/fake/path.engine")
            assert False, "Expected RuntimeError"
        except RuntimeError as e:
            assert "non-positive dimension for output" in str(e)
    print("  PASS  test_trt10_unresolved_output_nonpositive")


def test_trt8_unresolved_output_nonpositive():
    """TRT8: context resolves output with a non-positive dim -> RuntimeError."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt8(trt, dynamic_input=True)
    context = _mock_context_trt8(setter_succeeds=True, resolved_output=(1, -1, 1))

    with _build(trt, cuda, engine, context):
        try:
            _TRTEngine("/fake/path.engine")
            assert False, "Expected RuntimeError"
        except RuntimeError as e:
            assert "non-positive dimension for output" in str(e)
    print("  PASS  test_trt8_unresolved_output_nonpositive")


def test_trt10_all_binding_shapes_specified_false():
    """TRT10: context.all_binding_shapes_specified=False -> RuntimeError."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt10(trt, dynamic_input=True)
    context = _mock_context_trt10(setter_succeeds=True, all_binding_shapes_specified=False)

    with _build(trt, cuda, engine, context):
        try:
            _TRTEngine("/fake/path.engine")
            assert False, "Expected RuntimeError"
        except RuntimeError as e:
            assert "all_binding_shapes_specified=False" in str(e)
            assert "Not all required input shapes have been set" in str(e)
    print("  PASS  test_trt10_all_binding_shapes_specified_false")


def test_trt8_all_binding_shapes_specified_false():
    """TRT8: context.all_binding_shapes_specified=False -> RuntimeError."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt8(trt, dynamic_input=True)
    context = _mock_context_trt8(setter_succeeds=True, all_binding_shapes_specified=False)

    with _build(trt, cuda, engine, context):
        try:
            _TRTEngine("/fake/path.engine")
            assert False, "Expected RuntimeError"
        except RuntimeError as e:
            assert "all_binding_shapes_specified=False" in str(e)
            assert "Not all required input shapes have been set" in str(e)
    print("  PASS  test_trt8_all_binding_shapes_specified_false")


def test_trt10_all_binding_shapes_specified_missing_attr():
    """TRT10: context lacks all_binding_shapes_specified -> compatible (no error)."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt10(trt, dynamic_input=True)
    context = _mock_context_trt10(setter_succeeds=True, all_binding_shapes_specified=None)
    # Remove the attribute to simulate older TRT / mock without it
    del context.all_binding_shapes_specified

    with _build(trt, cuda, engine, context):
        eng = _TRTEngine("/fake/path.engine")

    assert eng._input_shapes == [(1, 3, 48, 96)]
    assert eng._output_shapes == [(1, 68, 1)]
    print("  PASS  test_trt10_all_binding_shapes_specified_missing_attr")


def test_trt8_all_binding_shapes_specified_missing_attr():
    """TRT8: context lacks all_binding_shapes_specified -> compatible (no error)."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt8(trt, dynamic_input=True)
    context = _mock_context_trt8(setter_succeeds=True, all_binding_shapes_specified=None)
    # Remove the attribute to simulate older TRT / mock without it
    del context.all_binding_shapes_specified

    with _build(trt, cuda, engine, context):
        eng = _TRTEngine("/fake/path.engine")

    assert eng._input_shapes == [(1, 3, 48, 96)]
    assert eng._output_shapes == [(1, 68, 1)]
    print("  PASS  test_trt8_all_binding_shapes_specified_missing_attr")


def test_infer_input_size_mismatch():
    """infer() rejects an input array with the wrong total element count."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt10(trt, dynamic_input=True)
    context = _mock_context_trt10(setter_succeeds=True)

    with _build(trt, cuda, engine, context):
        eng = _TRTEngine("/fake/path.engine")

    # Engine expects 1*3*48*96 = 13824 elements; give it 1*3*64*64 = 12288.
    wrong = np.zeros((1, 3, 64, 64), dtype=np.float32)
    try:
        eng.infer(wrong)
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "does not match engine input 0 size" in str(e)
        assert "13824" in str(e)
    print("  PASS  test_infer_input_size_mismatch")


def test_infer_input_size_match():
    """infer() accepts a correctly-sized input (mocked CUDA -> zero output)."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt10(trt, dynamic_input=True)
    context = _mock_context_trt10(setter_succeeds=True)

    with _build(trt, cuda, engine, context):
        eng = _TRTEngine("/fake/path.engine")

    right = np.zeros((1, 3, 48, 96), dtype=np.float32)  # exactly 13824
    outputs = eng.infer(right)
    assert len(outputs) == 1
    assert outputs[0].shape == (1, 68, 1)
    print("  PASS  test_infer_input_size_match")


def test_static_shape_still_works():
    """Static (non-dynamic) engines work without profile lookup / setter."""
    trt, cuda = _make_mock_trt(), _make_mock_cuda()
    engine = _mock_engine_trt10(trt, dynamic_input=False)
    context = _mock_context_trt10(setter_succeeds=True)

    with _build(trt, cuda, engine, context):
        eng = _TRTEngine("/fake/path.engine")

    assert eng._input_shapes == [(1, 3, 48, 96)]
    assert eng._output_shapes == [(1, 68, 1)]
    context.set_input_shape.assert_not_called()
    print("  PASS  test_static_shape_still_works")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_trt10_dynamic_success,
        test_trt8_dynamic_success,
        test_trt10_setter_failure,
        test_trt8_setter_failure,
        test_trt10_profile_opt_invalid_nonpositive,
        test_trt8_profile_opt_invalid_nonpositive,
        test_trt10_unresolved_output_nonpositive,
        test_trt8_unresolved_output_nonpositive,
        test_trt10_all_binding_shapes_specified_false,
        test_trt8_all_binding_shapes_specified_false,
        test_trt10_all_binding_shapes_specified_missing_attr,
        test_trt8_all_binding_shapes_specified_missing_attr,
        test_infer_input_size_mismatch,
        test_infer_input_size_match,
        test_static_shape_still_works,
    ]

    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception:
            print(f"  FAIL  {t.__name__}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
