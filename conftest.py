"""
Repo-root test isolation guard.

Several test modules stub heavy/native deps (cv2, numpy, gi, pyds, the
speedflow_python.* package, health_agent, profile_collect, etc.) into
sys.modules. Some stubs are installed at import/collection time and never
restored, which leaks across test modules and breaks later suites (e.g. a
leftover `cv2` stub makes speedflow_python.camera_config import fail).

To keep combined invocations deterministic we snapshot sys.modules at session
start (before any test module is collected) and, at the end of every test
module, roll back only the *synthetic* stubs (modules without a real __file__:
plain ModuleType / MagicMock placeholders). Genuinely-imported modules keep
their identity, so per-module caching and side effects survive, while stub
pollution is reliably removed.
"""
import importlib
import sys

import pytest

_BASELINE = None


def pytest_configure(config):
    global _BASELINE
    # Captured before collection imports any test module, so it reflects the
    # pristine interpreter state (real cv2/numpy/etc., or not-yet-imported).
    _BASELINE = dict(sys.modules)


def _is_synthetic(mod) -> bool:
    return getattr(mod, "__file__", None) is None


@pytest.fixture(autouse=True, scope="module")
def _restore_sys_modules_per_module():
    yield
    if _BASELINE is None:
        return
    importlib.invalidate_caches()
    for name, mod in list(sys.modules.items()):
        if name in _BASELINE:
            # Revert only if this module was replaced by a synthetic stub.
            if mod is not _BASELINE[name] and _is_synthetic(mod):
                sys.modules[name] = _BASELINE[name]
        else:
            # Added after baseline: drop only synthetic stubs, keep real imports.
            if _is_synthetic(mod):
                sys.modules.pop(name, None)
