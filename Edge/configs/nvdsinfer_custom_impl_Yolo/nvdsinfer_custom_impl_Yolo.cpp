/*
 * Custom YOLO bbox parser for DeepStream 7.x (nvinfer plugin).
 *
 * Supports two output formats produced by Ultralytics YOLO11/v8 ONNX exports:
 *
 *   Format A — [batch, 4+C, N] (transposed, standard ultralytics):
 *     output0[0][0..3][i] = cx, cy, w, h  (relative to network input, 0..1 range)
 *     output0[0][4..4+C-1][i] = class scores
 *
 *   Format B — [batch, N, 4+C] (row-major):
 *     output0[0][i][0..3] = cx, cy, w, h
 *     output0[0][i][4..4+C-1] = class scores
 *
 * Detection logic auto-detects the format based on tensor dimensions:
 *   - If dim[1] < dim[2]  → Format A (channels < anchors, e.g. 84 < 8400)
 *   - If dim[1] >= dim[2] → Format B (anchors >= channels)
 *
 * Coordinates in the output are in absolute pixels (networkInfo.width/height).
 *
 * NvDsInferYoloCudaEngineGet is provided as a no-op stub (engine is pre-built).
 */

#include <algorithm>
#include <cassert>
#include <cstring>
#include <iostream>
#include <vector>

#include "nvdsinfer_custom_impl.h"

/* ------------------------------------------------------------------ */
/*  Helpers                                                             */
/* ------------------------------------------------------------------ */

static inline float clamp_f(float v, float lo, float hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

/* ------------------------------------------------------------------ */
/*  Format A decoder: output shape [1, 4+C, N]                         */
/* ------------------------------------------------------------------ */
static void decodeFormatA(
    const float *buf,
    int C,      /* number of classes */
    int N,      /* number of anchors */
    float netW, float netH,
    const std::vector<float> &thresh,
    std::vector<NvDsInferParseObjectInfo> &objs)
{
    for (int n = 0; n < N; ++n) {
        /* bbox — relative coords in [0,1] */
        float cx = buf[0 * N + n];
        float cy = buf[1 * N + n];
        float bw = buf[2 * N + n];
        float bh = buf[3 * N + n];

        /* find max class score */
        float maxScore = -1e9f;
        int   maxCls   = 0;
        for (int c = 0; c < C; ++c) {
            float s = buf[(4 + c) * N + n];
            if (s > maxScore) { maxScore = s; maxCls = c; }
        }

        float thr = (maxCls < (int)thresh.size()) ? thresh[maxCls] : 0.25f;
        if (maxScore < thr) continue;

        /* cx,cy,bw,bh are in network-pixel coords (multiplied by netW/netH) */
        float x1 = (cx - bw * 0.5f);
        float y1 = (cy - bh * 0.5f);
        float x2 = (cx + bw * 0.5f);
        float y2 = (cy + bh * 0.5f);

        /* clamp to image */
        x1 = clamp_f(x1, 0.f, netW);
        y1 = clamp_f(y1, 0.f, netH);
        x2 = clamp_f(x2, 0.f, netW);
        y2 = clamp_f(y2, 0.f, netH);

        float w = x2 - x1;
        float h = y2 - y1;
        if (w < 1.f || h < 1.f) continue;

        NvDsInferParseObjectInfo obj;
        obj.classId            = (unsigned int)maxCls;
        obj.detectionConfidence = maxScore;
        obj.left   = x1;
        obj.top    = y1;
        obj.width  = w;
        obj.height = h;
        objs.push_back(obj);
    }
}

/* ------------------------------------------------------------------ */
/*  Format B decoder: output shape [1, N, 4+C]                         */
/* ------------------------------------------------------------------ */
static void decodeFormatB(
    const float *buf,
    int C,
    int N,
    float netW, float netH,
    const std::vector<float> &thresh,
    std::vector<NvDsInferParseObjectInfo> &objs)
{
    int stride = 4 + C;
    for (int n = 0; n < N; ++n) {
        const float *row = buf + n * stride;
        float cx = row[0], cy = row[1], bw = row[2], bh = row[3];

        float maxScore = -1e9f;
        int   maxCls   = 0;
        for (int c = 0; c < C; ++c) {
            float s = row[4 + c];
            if (s > maxScore) { maxScore = s; maxCls = c; }
        }

        float thr = (maxCls < (int)thresh.size()) ? thresh[maxCls] : 0.25f;
        if (maxScore < thr) continue;

        float x1 = clamp_f(cx - bw * 0.5f, 0.f, netW);
        float y1 = clamp_f(cy - bh * 0.5f, 0.f, netH);
        float x2 = clamp_f(cx + bw * 0.5f, 0.f, netW);
        float y2 = clamp_f(cy + bh * 0.5f, 0.f, netH);
        float w = x2 - x1, h = y2 - y1;
        if (w < 1.f || h < 1.f) continue;

        NvDsInferParseObjectInfo obj;
        obj.classId             = (unsigned int)maxCls;
        obj.detectionConfidence = maxScore;
        obj.left   = x1;
        obj.top    = y1;
        obj.width  = w;
        obj.height = h;
        objs.push_back(obj);
    }
}

/* ------------------------------------------------------------------ */
/*  NvDsInferParseYolo — exported symbol                               */
/* ------------------------------------------------------------------ */
extern "C" bool NvDsInferParseYolo(
    std::vector<NvDsInferLayerInfo> const &outputLayersInfo,
    NvDsInferNetworkInfo  const &networkInfo,
    NvDsInferParseDetectionParams const &detectionParams,
    std::vector<NvDsInferParseObjectInfo> &objectList);

extern "C" bool NvDsInferParseYolo(
    std::vector<NvDsInferLayerInfo> const &outputLayersInfo,
    NvDsInferNetworkInfo  const &networkInfo,
    NvDsInferParseDetectionParams const &detectionParams,
    std::vector<NvDsInferParseObjectInfo> &objectList)
{
    if (outputLayersInfo.empty()) {
        std::cerr << "[YoloParser] No output layers found." << std::endl;
        return false;
    }

    const NvDsInferLayerInfo &layer = outputLayersInfo[0];
    const NvDsInferDims &dims = layer.inferDims;

    if (dims.numDims < 2) {
        std::cerr << "[YoloParser] Unexpected tensor rank: " << dims.numDims << std::endl;
        return false;
    }

    const float *buf = static_cast<const float *>(layer.buffer);
    float netW = static_cast<float>(networkInfo.width);
    float netH = static_cast<float>(networkInfo.height);

    const std::vector<float> &thresh = detectionParams.perClassPreclusterThreshold;

    /* For 3-D output [batch, dim1, dim2]:
     *   dim1 = channels (4+C), dim2 = anchors  → Format A
     *   dim1 = anchors, dim2 = channels (4+C)  → Format B
     * We use dims.d[0] and dims.d[1] (batch already stripped by nvinfer).
     */
    int d0 = dims.d[0];   /* first remaining dim */
    int d1 = dims.d[1];   /* second remaining dim */

    if (d0 < d1) {
        /* Format A: [4+C, N] */
        int channels = d0;
        int N        = d1;
        int C = channels - 4;
        if (C <= 0) {
            std::cerr << "[YoloParser] Format A: channels=" << channels << " < 4." << std::endl;
            return false;
        }
        decodeFormatA(buf, C, N, netW, netH, thresh, objectList);
    } else {
        /* Format B: [N, 4+C] */
        int N        = d0;
        int channels = d1;
        int C = channels - 4;
        if (C <= 0) {
            std::cerr << "[YoloParser] Format B: channels=" << channels << " < 4." << std::endl;
            return false;
        }
        decodeFormatB(buf, C, N, netW, netH, thresh, objectList);
    }

    return true;
}

CHECK_CUSTOM_PARSE_FUNC_PROTOTYPE(NvDsInferParseYolo);

/* ------------------------------------------------------------------ */
/*  NvDsInferYoloCudaEngineGet — stub (engine files are pre-built)     */
/* ------------------------------------------------------------------ */
#include "NvInfer.h"
#include "nvdsinfer_context.h"

extern "C" bool NvDsInferYoloCudaEngineGet(
    nvinfer1::IBuilder *const builder,
    nvinfer1::IBuilderConfig *const builderConfig,
    const NvDsInferContextInitParams *const initParams,
    nvinfer1::DataType dataType,
    nvinfer1::ICudaEngine *&cudaEngine);

extern "C" bool NvDsInferYoloCudaEngineGet(
    nvinfer1::IBuilder *const builder,
    nvinfer1::IBuilderConfig *const builderConfig,
    const NvDsInferContextInitParams *const initParams,
    nvinfer1::DataType dataType,
    nvinfer1::ICudaEngine *&cudaEngine)
{
    /* Engine file is pre-built via trtexec.  This function is only called
     * when model-engine-file is missing or stale.  Log and return false so
     * nvinfer falls back to ONNX parsing via NvOnnxParser automatically. */
    std::cerr << "[YoloParser] NvDsInferYoloCudaEngineGet called — "
                 "engine file not found or stale. "
                 "Re-run models/build_engines.sh to rebuild." << std::endl;
    cudaEngine = nullptr;
    return false;
}
