"""Host regression checks for the FPS measurement contract.

The old design counted input FPS at the source-pad BUFFER probe and output
FPS at the OSD sink-pad probe, giving different windows / burst values for
file sources.  The contract now: output FPS is derived from the OSD callback
counter (_fps_frame_count), bounded by the PTS-measured source rate.
_input_fps is the PTS-derived native source rate (buf_pts deltas), falling
back to the bounded output FPS when PTS is unavailable — no independent
source probe, no fabricated values.
"""

import ast
import sys
from pathlib import Path


def test_no_source_pad_input_probe_in_core_pipeline():
    """The independent source-pad input probe must be gone."""
    source = Path(__file__).resolve().parents[1] / "speedflow_python" / "core_pipeline.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    probes = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "input_fps_probe" in node.name
    ]
    assert probes == [], f"stale source-pad input probe(s) still present: {probes}"

    # No _InputCounter class definition either.
    classes = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    ]
    assert "_InputCounter" not in classes


def test_writer_payload_derives_input_fps_from_same_counter():
    """_input_fps is PTS-derived source rate (falling back to bounded OSD fps)
    — it must be present in the writer payload and must not come from an
    independent _input_counter drain."""
    source = Path(__file__).resolve().parents[1] / "speedflow_python" / "probes.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    writer = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_fps_writer_loop"
    )

    writer_src = ast.get_source_segment(source.read_text(encoding="utf-8"), writer)
    assert writer_src is not None

    # Both must come from the same frame_count snapshot, never an _input_counter.
    assert "_input_counter" not in writer_src
    assert "out[\"_input_fps\"]" in writer_src


def test_tick_fps_still_increments_osd_counter():
    """_tick_fps remains the single per-frame counter for FPS."""
    source = Path(__file__).resolve().parents[1] / "speedflow_python" / "probes.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    tick = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_tick_fps"
    )

    tick_src = ast.get_source_segment(source.read_text(encoding="utf-8"), tick)
    assert tick_src is not None
    assert "_fps_frame_count[camera_id] += 1" in tick_src


if __name__ == "__main__":
    _tests = [
        test_no_source_pad_input_probe_in_core_pipeline,
        test_writer_payload_derives_input_fps_from_same_counter,
        test_tick_fps_still_increments_osd_counter,
    ]
    failed = []
    for t in _tests:
        try:
            t()
        except Exception as exc:
            import traceback
            failed.append(t.__name__)
            print(f"  FAIL  {t.__name__}: {exc}")
            traceback.print_exc()
    if failed:
        print(f"\n{len(failed)} test(s) FAILED: {failed}")
        sys.exit(1)
    print(f"\nAll {len(_tests)} tests passed.")
