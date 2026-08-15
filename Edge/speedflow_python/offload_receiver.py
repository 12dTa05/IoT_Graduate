"""
speedflow_python/offload_receiver.py

Receives offloaded crops from peer nodes and runs lightweight
TensorRT inference directly (no DeepStream pipeline required on the
receiver side).

Subscribes to:
  offload/plates/*/{my_node_id}   — Level 3: run LPR, return plate text
  offload/vehicles/*/{my_node_id} — Level 2: run LPD then LPR, return plate text
  offload/results/*/{my_node_id}  — results returned when this node is sender

Results are published back to the sender:
  offload/results/{my_node_id}/{sender_node_id}
    payload: {stid, camera_id, frame_no, plate_text, confidence,
              inference_ok, ts}

inference_ok is True for every published payload: only successful inference
runs publish.  An inference failure — engine unavailable (not loaded) or an
inference exception (LPR or LPD) — suppresses the result entirely and
increments the offload_inference_errors counter, with a concise reason carried
to the rate-limited WARNING log.  A valid empty observation (LPD found no
plate bbox, or LPR decoded no text) is *not* an error — it still publishes
with inference_ok=True and an empty plate_text.

The inference engines are loaded lazily on the first request (avoids startup
cost when offload is not active).  Both engines are loaded in a dedicated
worker thread so the Zenoh callback thread is never blocked.

TensorRT Python API (tensorrt / pycuda) is required.  If unavailable the
receiver falls back to a no-op stub that logs a warning — the sender will
simply not receive results for that frame.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import msgpack
import numpy as np

from .zenoh_session import make_session

logger = logging.getLogger(__name__)

# BUG-7 fix: replace the unsafe function-attribute label cache with a
# module-level dict protected by a lock.  The old approach was not
# thread-safe: two worker threads calling _decode_lpr_output concurrently
# before the first completed the file read could each partially initialise
# _decode_lpr_output._labels, leaving a corrupt list.
_lpr_labels_cache: dict = {}          # labels_path → List[str]
_lpr_labels_lock  = threading.Lock()

# Rate limit for inference-exception warning logs: at most one warning per
# this many seconds.  A crashed TRT engine can fail on every crop, so without
# a limit the log would drown in repetitive stack traces.
_inference_error_log_interval = 5.0


class _InferenceFailure:
    """Internal outcome distinguishing inference *failure* from a valid empty
    observation.  Returned by _run_lpr/_run_lpd when the engine is unavailable
    (not loaded) or raises.  Carries a concise, safe reason for the
    rate-limited WARNING log.  A None (LPD) or ("", 0.0) (LPR) return remains a
    *valid* empty result that still publishes.

    fatal=True marks a permanent, unrecoverable condition (e.g. TRT not
    installed) so callers can distinguish it from a transient engine failure
    and suppress per-crop repeats after the one-time ERROR has been logged.
    """

    __slots__ = ("reason", "fatal")

    def __init__(self, reason: str, *, fatal: bool = False) -> None:
        self.reason = reason
        self.fatal  = fatal


def _safe_infer_reason(exc: BaseException, limit: int = 120) -> str:
    """One-line exception summary for logs: type + message, truncated.
    Never includes a traceback — no stack spam when a crashed engine fails
    on every crop."""
    text = f"{type(exc).__name__}: {exc}".strip()
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text

# ---------------------------------------------------------------------------
# TensorRT engine loader (lazy, cached)
# ---------------------------------------------------------------------------

def _try_import_trt():
    """Return (trt, cuda) or (None, None) if not installed."""
    try:
        import tensorrt as trt
        import pycuda.driver as cuda
        import pycuda.autoinit  # noqa: F401 — initialises CUDA context
        return trt, cuda
    except ImportError:
        return None, None


class _TRTEngine:
    """
    Minimal TensorRT engine wrapper for single-batch inference.
    Loads a serialised .engine file produced by trtexec / DeepStream.
    """

    def __init__(self, engine_path: str) -> None:
        trt, cuda = _try_import_trt()
        if trt is None:
            raise RuntimeError("tensorrt / pycuda not installed")

        logger.info("[TRTEngine] Loading %s ...", engine_path)
        TRT_LOGGER = trt.Logger(trt.Logger.WARNING)
        runtime    = trt.Runtime(TRT_LOGGER)

        with open(engine_path, "rb") as f:
            engine_data = f.read()
        self._engine  = runtime.deserialize_cuda_engine(engine_data)
        self._context = self._engine.create_execution_context()
        self._trt     = trt
        self._cuda    = cuda

        # Allocate pinned host + device buffers.  JetPack 5 / TensorRT 8 uses
        # the legacy binding-index API; JetPack 6 / TensorRT 10 uses the
        # name-based tensor API.  Support both so Jetson deployments can move
        # between JetPack releases without breaking crop offload inference.
        self._bindings = []
        self._tensor_names: list = []
        self._use_trt10_api = hasattr(self._engine, "num_io_tensors")
        self._host_inputs:  list = []
        self._host_outputs: list = []
        self._dev_inputs:   list = []
        self._dev_outputs:  list = []
        self._input_shapes: list = []
        self._output_shapes: list = []

        if self._use_trt10_api:
            self._allocate_trt10(trt, cuda)
        else:
            self._allocate_trt8(trt, cuda)

        if not self._host_inputs:
            raise RuntimeError(f"TensorRT engine has no input bindings/tensors: {engine_path}")

        self._stream = cuda.Stream()
        logger.info("[TRTEngine] Loaded %s — %d I/O tensor(s)", engine_path, len(self._bindings))

    def _allocate_trt10(self, trt, cuda) -> None:
        """TensorRT 10 name-based tensor API with dynamic profile 0 support."""
        n_io = int(self._engine.num_io_tensors)
        # First pass: collect tensor info and resolve dynamic INPUT shapes via profile 0
        tensor_infos: list[dict] = []
        for i in range(n_io):
            name = self._engine.get_tensor_name(i)
            tensor_infos.append({
                "name":  name,
                "shape": tuple(self._engine.get_tensor_shape(name)),
                "dtype": trt.nptype(self._engine.get_tensor_dtype(name)),
                "mode":  self._engine.get_tensor_mode(name),
            })

        # Resolve dynamic input shapes using profile 0 (MIN shape for batch-1 crops)
        for info in tensor_infos:
            if info["mode"] != trt.TensorIOMode.INPUT:
                continue
            shape = info["shape"]
            # Check if this input has dynamic dimensions
            if any(dim < 0 for dim in shape):
                name = info["name"]
                min_shape, opt_shape, max_shape = self._engine.get_tensor_profile_shape(name, 0)
                logger.info("[TRTEngine] Dynamic input '%s': min=%s opt=%s max=%s",
                            name, min_shape, opt_shape, max_shape)
                # Use MIN shape (index 0) for batch=1 crop preprocessing
                if any(dim <= 0 for dim in min_shape):
                    raise RuntimeError(
                        f"TensorRT profile 0 min shape for input '{name}' contains "
                        f"non-positive dimension(s): {min_shape}"
                    )
                info["shape"] = tuple(min_shape)
                # Require the context to accept the concrete input shape.
                if not self._context.set_input_shape(name, info["shape"]):
                    raise RuntimeError(
                        f"TensorRT set_input_shape failed for input '{name}' "
                        f"with shape {info['shape']} (profile 0 min)"
                    )
            # else: static input, shape already concrete

        # After all input shapes are set, validate that the context considers
        # all binding shapes specified (TRT 10+ provides all_binding_shapes_specified).
        # Explicit False -> RuntimeError. Missing attribute -> compatible (skip check).
        all_specified = getattr(self._context, "all_binding_shapes_specified", None)
        if all_specified is False:
            raise RuntimeError(
                "TensorRT context reports all_binding_shapes_specified=False. "
                "Not all required input shapes have been set."
            )

        # Second pass: now all input shapes are set, resolve output shapes from context
        for info in tensor_infos:
            name  = info["name"]
            shape = info["shape"]
            dtype = info["dtype"]
            host_mem = None
            dev_mem = None
            if info["mode"] == trt.TensorIOMode.INPUT:
                # Input shape already resolved above
                host_mem = cuda.pagelocked_empty(int(np.prod(shape)), dtype)
                dev_mem = cuda.mem_alloc(host_mem.nbytes)
                self._host_inputs.append(host_mem)
                self._dev_inputs.append(dev_mem)
                self._input_shapes.append(shape)
            else:
                # Output: get concrete shape from context after input shapes are set
                resolved_shape = tuple(self._context.get_tensor_shape(name))
                if any(dim <= 0 for dim in resolved_shape):
                    raise RuntimeError(
                        f"TensorRT context resolved non-positive dimension for output "
                        f"'{name}': {resolved_shape}. Ensure all dynamic inputs have "
                        f"valid shapes set before allocation."
                    )
                shape = resolved_shape
                host_mem = cuda.pagelocked_empty(int(np.prod(shape)), dtype)
                dev_mem = cuda.mem_alloc(host_mem.nbytes)
                self._host_outputs.append(host_mem)
                self._dev_outputs.append(dev_mem)
                self._output_shapes.append(shape)

            self._bindings.append(int(dev_mem))
            self._tensor_names.append(name)
            self._context.set_tensor_address(name, int(dev_mem))

    def _allocate_trt8(self, trt, cuda) -> None:
        """TensorRT 8 binding-index API with dynamic profile 0 support."""
        binding_count = int(self._engine.num_bindings)

        # First pass: collect binding info
        binding_infos: list[dict] = []
        for i in range(binding_count):
            binding_infos.append({
                "index":   i,
                "shape":   tuple(self._engine.get_binding_shape(i)),
                "dtype":   trt.nptype(self._engine.get_binding_dtype(i)),
                "is_input": self._engine.binding_is_input(i),
            })

        # Resolve dynamic input shapes using profile 0 (MIN shape for batch=1 crops)
        for info in binding_infos:
            if not info["is_input"]:
                continue
            shape = info["shape"]
            if any(dim < 0 for dim in shape):
                i = info["index"]
                min_shape, opt_shape, max_shape = self._engine.get_profile_shape(0, i)
                logger.info("[TRTEngine] Dynamic binding %d: min=%s opt=%s max=%s",
                            i, min_shape, opt_shape, max_shape)
                # Use MIN shape (index 0) for batch=1 crop preprocessing
                if any(dim <= 0 for dim in min_shape):
                    raise RuntimeError(
                        f"TensorRT profile 0 min shape for binding {i} contains "
                        f"non-positive dimension(s): {min_shape}"
                    )
                info["shape"] = tuple(min_shape)
                # Require the context to accept the concrete input shape.
                if not self._context.set_binding_shape(i, info["shape"]):
                    raise RuntimeError(
                        f"TensorRT set_binding_shape failed for binding {i} "
                        f"with shape {info['shape']} (profile 0 min)"
                    )
            # else: static input, shape already concrete

        # After all input shapes are set, validate that the context considers
        # all binding shapes specified (TRT 10+ provides all_binding_shapes_specified).
        # Explicit False -> RuntimeError. Missing attribute -> compatible (skip check).
        all_specified = getattr(self._context, "all_binding_shapes_specified", None)
        if all_specified is False:
            raise RuntimeError(
                "TensorRT context reports all_binding_shapes_specified=False. "
                "Not all required input shapes have been set."
            )

        # Second pass: allocate with resolved shapes
        for info in binding_infos:
            i         = info["index"]
            shape     = info["shape"]
            dtype     = info["dtype"]
            host_mem = None
            dev_mem  = None
            if info["is_input"]:
                # Input shape already resolved above
                host_mem = cuda.pagelocked_empty(int(np.prod(shape)), dtype)
                dev_mem = cuda.mem_alloc(host_mem.nbytes)
                self._host_inputs.append(host_mem)
                self._dev_inputs.append(dev_mem)
                self._input_shapes.append(shape)
            else:
                # Output: get concrete shape from context
                resolved_shape = tuple(self._context.get_binding_shape(i))
                if any(dim <= 0 for dim in resolved_shape):
                    raise RuntimeError(
                        f"TensorRT context resolved non-positive dimension for output "
                        f"binding {i}: {resolved_shape}. Ensure all dynamic inputs have "
                        f"valid shapes set before allocation."
                    )
                shape = resolved_shape
                host_mem = cuda.pagelocked_empty(int(np.prod(shape)), dtype)
                dev_mem = cuda.mem_alloc(host_mem.nbytes)
                self._host_outputs.append(host_mem)
                self._dev_outputs.append(dev_mem)
                self._output_shapes.append(shape)

            self._bindings.append(int(dev_mem))

    def infer(self, input_array: np.ndarray) -> list:
        """Run one inference pass. Returns list of output ndarrays."""
        expected_size = int(self._host_inputs[0].size)
        actual_size = int(input_array.size)
        if actual_size != expected_size:
            raise ValueError(
                f"Input array size {actual_size} does not match engine input 0 "
                f"size {expected_size} (shape {self._input_shapes[0] if self._input_shapes else '?'}); "
                f"preprocess crop to the engine's expected shape"
            )
        np.copyto(self._host_inputs[0], input_array.ravel())
        self._cuda.memcpy_htod_async(self._dev_inputs[0], self._host_inputs[0], self._stream)
        if self._use_trt10_api:
            self._context.execute_async_v3(stream_handle=self._stream.handle)
        else:
            self._context.execute_async_v2(self._bindings, self._stream.handle, None)
        for host, dev in zip(self._host_outputs, self._dev_outputs):
            self._cuda.memcpy_dtoh_async(host, dev, self._stream)
        self._stream.synchronize()
        return [
            np.array(host).reshape(shape)
            for host, shape in zip(self._host_outputs, self._output_shapes)
        ]


# ---------------------------------------------------------------------------
# Preprocessing helpers
# ---------------------------------------------------------------------------

def _preprocess_lpr(crop_bgr: np.ndarray, target_h: int = 48, target_w: int = 96) -> np.ndarray:
    """
    Resize + normalise a plate crop to the LPR model input format.
    Matches the preprocessing used during LPRNet training:
      - resize to (W, H)
      - convert to float32 in [0, 1]
      - channel-first layout: (1, 3, H, W)
    """
    resized = cv2.resize(crop_bgr, (target_w, target_h))
    img = resized.astype(np.float32) / 255.0
    img = np.transpose(img, (2, 0, 1))          # HWC → CHW
    return np.expand_dims(img, axis=0)           # (1, 3, H, W)


def _preprocess_lpd(crop_bgr: np.ndarray, target_size: int = 640) -> np.ndarray:
    """
    Resize a vehicle crop for the LPD detector input.
    Returns float32 CHW normalised array, shape (1, 3, target_size, target_size).
    """
    resized = cv2.resize(crop_bgr, (target_size, target_size))
    img = resized.astype(np.float32) / 255.0
    img = np.transpose(img, (2, 0, 1))
    return np.expand_dims(img, axis=0)


def _decode_lpr_output(outputs: list, labels_path: str) -> Tuple[str, float]:
    """
    CTC greedy decode: collapse repeats, skip blank.
    Mirrors the C++ NvDsInferParseCustomNVPlate logic in nvdsinfer_custom_impl_lpr.cpp.

    Returns (plate_text, avg_confidence).

    BUG-7 fix: labels are cached in a module-level dict protected by a lock
    so concurrent worker threads never see a partially-initialised label list.
    """
    # Thread-safe label loading
    with _lpr_labels_lock:
        if labels_path not in _lpr_labels_cache:
            try:
                with open(labels_path, "r", encoding="utf-8") as f:
                    _lpr_labels_cache[labels_path] = [
                        l.rstrip() for l in f if l.strip()
                    ]
            except Exception:
                _lpr_labels_cache[labels_path] = []
        labels = _lpr_labels_cache[labels_path]

    blank  = len(labels)

    # outputs[0]: argmax sequence  (seqLen,) int32
    # outputs[1]: max probs        (seqLen,) float32
    argmax_seq = outputs[0].ravel().astype(int)
    max_probs  = outputs[1].ravel().astype(float) if len(outputs) > 1 else None

    plate     = ""
    total_c   = 0.0
    count     = 0
    prev_idx  = -1

    for t, idx in enumerate(argmax_seq):
        if idx == blank or idx < 0:
            prev_idx = idx
            continue
        if idx == prev_idx:
            continue
        prev_idx = idx

        conf = float(max_probs[t]) if max_probs is not None else 1.0
        if conf < 0.3:
            continue
        if idx < len(labels):
            plate += labels[idx]
        total_c += conf
        count   += 1

    avg_conf = total_c / count if count > 0 else 0.0
    return plate, avg_conf


def _decode_lpd_output(outputs: list, orig_w: int, orig_h: int,
                        conf_thresh: float = 0.5) -> Optional[Tuple[int, int, int, int]]:
    """
    Parse LPD bounding box from the first detected plate (highest confidence).
    Returns (x, y, w, h) in original image coordinates or None if no detection.

    Assumes the LPD model outputs in YOLO/SSD-style format:
    [batch, num_boxes, 6] where columns are [x1, y1, x2, y2, conf, class].
    """
    if not outputs:
        return None

    boxes = outputs[0].reshape(-1, 6) if outputs[0].ndim > 2 else outputs[0]
    best  = None
    best_conf = conf_thresh

    for row in boxes:
        conf = float(row[4]) if len(row) > 4 else 1.0
        if conf < best_conf:
            continue
        best_conf = conf
        best      = row

    if best is None:
        return None

    x1 = int(best[0] * orig_w)
    y1 = int(best[1] * orig_h)
    x2 = int(best[2] * orig_w)
    y2 = int(best[3] * orig_h)
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(orig_w, x2), min(orig_h, y2)

    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2 - x1, y2 - y1


# ---------------------------------------------------------------------------
# OffloadReceiver
# ---------------------------------------------------------------------------

class OffloadReceiver:
    """
    Receives plate/vehicle crops from peer nodes, runs TRT inference,
    and publishes results back on offload/results/{my_node}/{sender_node}.

    Engines are loaded lazily in the worker thread.

    Args:
        node_id:        this node's ID
        session:        shared Zenoh session (from PeerOrchestrator)
        lpr_engine_path: path to lpr.engine (Level 2 + 3)
        lpd_engine_path: path to lpd.engine (Level 2 only)
        labels_path:    path to labels_lpr.txt
    """

    def __init__(
        self,
        node_id: str,
        session,
        lpr_engine_path: str,
        lpd_engine_path: str,
        labels_path: str,
    ) -> None:
        self._node_id         = node_id
        self._session         = session
        self._lpr_engine_path = lpr_engine_path
        self._lpd_engine_path = lpd_engine_path
        self._labels_path     = labels_path

        self._work_q: queue.Queue[Optional[dict]] = queue.Queue(maxsize=32)
        self._thread: Optional[threading.Thread]  = None
        self._running = False

        # Lazy-loaded engines (None until first use)
        self._lpr_engine: Optional[_TRTEngine] = None
        self._lpd_engine: Optional[_TRTEngine] = None
        self._engines_loaded = False
        # Three-valued: None = not yet checked; True = TRT available;
        # False = tensorrt/pycuda absent (permanent — logged once at ERROR).
        # ponytail: None sentinel avoids importing TRT at construction time,
        # keeping the receiver cheap to instantiate on host/CI.
        self._trt_available: Optional[bool] = None

        # Result publisher cache
        self._result_pubs: Dict[str, Any] = {}

        # Thread-safe lifetime E2E counters (int reads are atomic in CPython)
        self._received       = 0
        self._queue_dropped  = 0
        self._processed      = 0
        self._errors         = 0
        self._results_sent   = 0

        # Cumulative inference-execution errors (distinct from worker-loop
        # errors above).  Incremented only when an inference run itself raises
        # inside _run_lpr/_run_lpd — NOT for valid empty results such as
        # "no plate decoded".  Rate-limited warning logging.
        self._inference_errors = 0
        self._last_inference_error_log = float("-inf")

        # Result subscription used when this node sends crops to a peer.
        self._result_handler: Optional[Any] = None
        self._result_sub_declared = False

    @property
    def offload_processed_count(self) -> int:
        """Monotonic count of successfully handled crop items. Thread-safe (int)."""
        return self._processed

    @property
    def offload_received_count(self) -> int:
        """Crops successfully decoded from the wire. Thread-safe (int)."""
        return self._received

    @property
    def offload_queue_dropped_count(self) -> int:
        """Crops dropped because the work queue was full. Thread-safe (int)."""
        return self._queue_dropped

    @property
    def offload_errors_count(self) -> int:
        """Worker-loop handle errors. Thread-safe (int)."""
        return self._errors

    @property
    def offload_results_sent_count(self) -> int:
        """Results published back to senders. Thread-safe (int)."""
        return self._results_sent

    @property
    def offload_inference_errors_count(self) -> int:
        """Cumulative inference-execution errors (distinct from worker-loop
        errors).  Incremented only when inference itself raises, not for
        valid empty results. Thread-safe (int)."""
        return self._inference_errors

    @property
    def trt_available(self) -> Optional[bool]:
        """Three-valued TRT dependency state.

        None  — not yet probed (engine load hasn't been triggered yet).
        True  — tensorrt/pycuda imported successfully on first use.
        False — tensorrt/pycuda absent; a one-time ERROR was logged at
                _load_engines_once time; all crops are suppressed.

        Downstream (health_agent, dashboard) can surface this to distinguish
        "receiver running but TRT missing" from "receiver idle".
        """
        return self._trt_available

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running = True
        # BUG-18 fix: Zenoh uses '*' for single-level wildcards, not '+'.
        # '+' is MQTT syntax and is silently ignored by Zenoh, causing these
        # subscribers to never match any published key expression.
        self._session.declare_subscriber(
            f"offload/plates/*/{self._node_id}",
            self._on_plate_sample,
        )
        self._session.declare_subscriber(
            f"offload/vehicles/*/{self._node_id}",
            self._on_vehicle_sample,
        )
        self._thread = threading.Thread(
            target=self._worker_loop,
            name=f"OffloadReceiver-{self._node_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "[OffloadReceiver] Started. Listening on offload/*/*/%s", self._node_id
        )

    def stop(self) -> None:
        self._running = False
        self._work_q.put(None)
        if self._thread:
            self._thread.join(timeout=5)
        logger.info(
            "[OffloadReceiver] Stopped. received=%d processed=%d queue_dropped=%d errors=%d results_sent=%d",
            self._received, self._processed, self._queue_dropped,
            self._errors, self._results_sent,
        )

    def set_result_handler(self, handler) -> None:
        """
        Register a callback to receive offload results when this node is the
        SENDER.  The handler receives the decoded result dict:
          {src, dst, camera_id, stid, frame_no, plate_text, confidence, ts}

        Declares a Zenoh subscriber on offload/results/*/{my_node_id} once,
        and forwards every decoded message to handler(payload).
        Called during probe setup before the pipeline starts PLAYING.
        """
        self._result_handler = handler
        if not self._result_sub_declared:
            self._result_sub_declared = True
            self._session.declare_subscriber(
                f"offload/results/*/{self._node_id}",
                self._on_result_sample,
            )
            logger.info(
                "[OffloadReceiver] Result subscriber declared: offload/results/*/%s",
                self._node_id,
            )

    def _on_result_sample(self, sample) -> None:
        """Callback for offload/results/*/{my_node_id} — dispatch to handler."""
        try:
            payload = msgpack.unpackb(sample.payload.to_bytes(), raw=False)
            if self._result_handler is not None:
                self._result_handler(payload)
        except Exception as exc:
            logger.warning("[OffloadReceiver] Result dispatch error: %s", exc)

    # ------------------------------------------------------------------
    # Zenoh subscriber callbacks — called on the Zenoh recv thread
    # Must NOT block.
    # ------------------------------------------------------------------

    def _on_plate_sample(self, sample) -> None:
        self._enqueue_sample(sample, crop_type="plate")

    def _on_vehicle_sample(self, sample) -> None:
        self._enqueue_sample(sample, crop_type="vehicle")

    def _enqueue_sample(self, sample, crop_type: str) -> None:
        try:
            payload = msgpack.unpackb(sample.payload.to_bytes(), raw=False)
            payload["_crop_type"] = crop_type
            # Received is credited as soon as the wire payload decodes — even
            # if the work queue is full and the crop is then dropped.
            self._received += 1
            try:
                self._work_q.put_nowait(payload)
            except queue.Full:
                self._queue_dropped += 1
                logger.debug("[OffloadReceiver] Work queue full — dropping")
        except Exception as exc:
            logger.warning("[OffloadReceiver] Decode error: %s", exc)

    # ------------------------------------------------------------------
    # Worker loop — runs in dedicated thread
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        while self._running:
            try:
                item = self._work_q.get(timeout=2.0)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._handle_item(item)
                self._processed += 1
            except Exception as exc:
                self._errors += 1
                logger.warning("[OffloadReceiver] Handle error: %s", exc)

    def _load_engines_once(self) -> None:
        if self._engines_loaded:
            return

        # --- TRT dependency check (once, actionable ERROR if absent) ----------
        trt, cuda = _try_import_trt()
        if trt is None:
            self._trt_available = False
            self._engines_loaded = True  # don't retry — dependency is permanent
            logger.error(
                "[OffloadReceiver] tensorrt / pycuda are NOT installed on this node. "
                "All offloaded crops will be silently suppressed until TRT is available. "
                "Install JetPack TensorRT bindings (pip install tensorrt pycuda) and "
                "restart the service to enable crop-offload inference."
            )
            return
        self._trt_available = True

        # --- Engine file loading ----------------------------------------------
        # Mark loaded BEFORE the attempts so a repeated first-crop race in the
        # worker thread doesn't trigger a second load.  Partial failure (one
        # engine missing / corrupt) is logged once at ERROR here; per-crop
        # _run_lpr/_run_lpd will still produce _InferenceFailure with a clear
        # reason distinguishing "engine not loaded" from "inference exception".
        self._engines_loaded = True
        try:
            if Path(self._lpr_engine_path).exists():
                self._lpr_engine = _TRTEngine(self._lpr_engine_path)
                logger.info("[OffloadReceiver] LPR engine ready: %s", self._lpr_engine_path)
            else:
                logger.error(
                    "[OffloadReceiver] LPR engine file not found: %s — "
                    "rebuild with trtexec and set lpr_engine_path in config.",
                    self._lpr_engine_path,
                )
        except Exception as exc:
            logger.error(
                "[OffloadReceiver] LPR engine load failed (%s) — "
                "LPR inference will be suppressed until engine is rebuilt.",
                exc,
            )
        try:
            if Path(self._lpd_engine_path).exists():
                self._lpd_engine = _TRTEngine(self._lpd_engine_path)
                logger.info("[OffloadReceiver] LPD engine ready: %s", self._lpd_engine_path)
            else:
                logger.error(
                    "[OffloadReceiver] LPD engine file not found: %s — "
                    "rebuild with trtexec and set lpd_engine_path in config.",
                    self._lpd_engine_path,
                )
        except Exception as exc:
            logger.error(
                "[OffloadReceiver] LPD engine load failed (%s) — "
                "vehicle-level (L2) offload inference will be suppressed until engine is rebuilt.",
                exc,
            )

    def _handle_item(self, item: dict) -> None:
        self._load_engines_once()

        crop_type = item.get("_crop_type", "plate")
        src_node  = item.get("src", "")
        camera_id = item.get("camera_id", "")
        stid      = tuple(item.get("stid", [0, 0]))
        frame_no  = item.get("frame_no", 0)
        jpeg_data = item.get("jpeg", b"")

        # Decode JPEG crop
        arr = np.frombuffer(jpeg_data, dtype=np.uint8)
        crop_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if crop_bgr is None:
            return

        plate_text = ""
        confidence = 0.0
        inference_ok = True

        if crop_type == "plate":
            result = self._run_lpr(crop_bgr)
            if isinstance(result, _InferenceFailure):
                self._record_inference_error("LPR", result.reason, fatal=result.fatal)
                return
            plate_text, confidence = result

        elif crop_type == "vehicle":
            # Level 2: LPD first, then LPR on the plate sub-crop
            plate_bbox = self._run_lpd(crop_bgr)
            if isinstance(plate_bbox, _InferenceFailure):
                self._record_inference_error("LPD", plate_bbox.reason, fatal=plate_bbox.fatal)
                return
            if plate_bbox is not None:
                px, py, pw, ph = plate_bbox
                plate_crop = crop_bgr[py:py+ph, px:px+pw]
                if plate_crop.size > 0:
                    lpr_result = self._run_lpr(plate_crop)
                    if isinstance(lpr_result, _InferenceFailure):
                        self._record_inference_error("LPR", lpr_result.reason, fatal=lpr_result.fatal)
                        return
                    plate_text, confidence = lpr_result
            # else: no plate bbox — valid empty observation, still publish.

        # Publish result back to sender (only reached on successful inference)
        self._publish_result(
            dst_node  = src_node,
            camera_id = camera_id,
            stid      = stid,
            frame_no  = frame_no,
            plate_text= plate_text,
            confidence= confidence,
            inference_ok = inference_ok,
        )

    def _record_inference_error(self, stage: str, reason: str, *,
                                fatal: bool = False) -> None:
        """
        Count an inference failure (engine unavailable or exception) and log it
        at WARNING rate-limited to one message per
        _inference_error_log_interval seconds.  Never debug-only, never silent.
        The reason is a concise one-liner already produced by
        _safe_infer_reason or a static "engine not loaded" string — never a
        traceback.

        fatal=True (TRT not installed): skips the per-crop rate-limited
        WARNING because the actionable one-time ERROR was already emitted in
        _load_engines_once.  Still increments the counter so the health
        snapshot surfaces the suppressed crop count.
        """
        self._inference_errors += 1
        if fatal:
            # One-time ERROR already logged at engine-load time; per-crop
            # WARNING would be misleading noise.  Counter is still incremented.
            return
        now = time.monotonic()
        if now - self._last_inference_error_log >= _inference_error_log_interval:
            self._last_inference_error_log = now
            logger.warning(
                "[OffloadReceiver] %s inference error #%d: %s; result suppressed",
                stage, self._inference_errors, reason,
            )

    def _run_lpr(self, plate_bgr: np.ndarray) -> Any:
        if self._trt_available is False:
            # Fatal permanent condition — TRT not installed.  Don't spam the
            # rate-limited warning on every crop; the one-time ERROR in
            # _load_engines_once is the actionable signal.
            return _InferenceFailure("tensorrt/pycuda not installed", fatal=True)
        if self._lpr_engine is None:
            return _InferenceFailure("LPR engine not loaded (file missing or failed to parse)")
        inp = _preprocess_lpr(plate_bgr)
        try:
            outputs = self._lpr_engine.infer(inp)
            return _decode_lpr_output(outputs, self._labels_path)
        except Exception as exc:
            return _InferenceFailure(_safe_infer_reason(exc))

    def _run_lpd(self, vehicle_bgr: np.ndarray) -> Any:
        if self._trt_available is False:
            return _InferenceFailure("tensorrt/pycuda not installed", fatal=True)
        if self._lpd_engine is None:
            return _InferenceFailure("LPD engine not loaded (file missing or failed to parse)")
        h, w = vehicle_bgr.shape[:2]
        inp  = _preprocess_lpd(vehicle_bgr)
        try:
            outputs = self._lpd_engine.infer(inp)
            return _decode_lpd_output(outputs, w, h)
        except Exception as exc:
            return _InferenceFailure(_safe_infer_reason(exc))

    def _publish_result(
        self, dst_node: str, camera_id: str, stid: tuple,
        frame_no: int, plate_text: str, confidence: float,
        inference_ok: bool = True,
    ) -> None:
        key = f"offload/results/{self._node_id}/{dst_node}"
        pub = self._result_pubs.get(key)
        if pub is None:
            pub = self._session.declare_publisher(key)
            self._result_pubs[key] = pub

        payload = {
            "src":         self._node_id,
            "dst":         dst_node,
            "camera_id":   camera_id,
            "stid":        list(stid),
            "frame_no":    frame_no,
            "plate_text":  plate_text,
            "confidence":  float(confidence),
            "inference_ok": bool(inference_ok),
            "ts":          time.time(),
        }
        pub.put(msgpack.packb(payload, use_bin_type=True))
        self._results_sent += 1
