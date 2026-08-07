"""Host regression check for the input-FPS GStreamer callback signature."""

import ast
from pathlib import Path


def test_input_fps_probe_keeps_counter_outside_user_data_slot():
    source = Path(__file__).resolve().parents[1] / "speedflow_python" / "core_pipeline.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    probe = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_input_fps_probe"
    )

    assert [arg.arg for arg in probe.args.args] == [
        "_pad", "_info", "_user_data", "cnt", "cid"
    ]
