/*
 * Custom LPR (License Plate Recognition) classifier parser for DeepStream 7.x.
 *
 * Model: LPRNet — character-level CTC-style plate recognizer.
 *
 * Engine outputs (confirmed by inspecting lpr.engine):
 *   tf_op_layer_ArgMax : int32  [batch, 24]  — argmax char index per timestep
 *   tf_op_layer_Max    : float  [batch, 24]  — max probability per timestep
 *
 * Labels file (labels_lpr.txt) contains one character per line:
 *   0-9, A,B,C,D,E,F,G,H,K,L,M,N,P,S,T,U,V,X,Y,Z, -, .
 *   (32 entries, 0-indexed)
 *
 * Decoding:
 *   - Read argmax sequence for the first image in the batch.
 *   - Collapse consecutive repeated characters (CTC blank-merge heuristic).
 *   - Skip index == num_labels (blank / padding token) if model uses it.
 *   - Compute average confidence over accepted characters.
 *   - Return as a single NvDsInferAttribute with the plate string.
 *
 * parse-classifier-func-name=NvDsInferParseCustomNVPlate
 */

#include <cassert>
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "nvdsinfer_custom_impl.h"

/* ------------------------------------------------------------------ */
/*  Label loader                                                        */
/* ------------------------------------------------------------------ */

static std::vector<std::string> g_labels;
static bool                     g_labels_loaded = false;

/* Look for labels_lpr.txt next to this .so, or in common DS paths. */
static bool loadLabels()
{
    if (g_labels_loaded) return !g_labels.empty();
    g_labels_loaded = true;

    /* Candidate paths — resolved at runtime relative to config dirs. */
    static const char *CANDIDATES[] = {
        /* Relative to the directory that contains this .so */
        "../labels_lpr.txt",
        "labels_lpr.txt",
        /* Absolute fallback */
        "/home/mta/Documents/IoT_Graduate/Edge/configs/labels_lpr.txt",
        nullptr
    };

    for (int i = 0; CANDIDATES[i]; ++i) {
        std::ifstream f(CANDIDATES[i]);
        if (!f.is_open()) continue;
        std::string line;
        while (std::getline(f, line)) {
            /* Strip trailing whitespace */
            while (!line.empty() && (line.back() == '\r' || line.back() == '\n' || line.back() == ' '))
                line.pop_back();
            if (!line.empty())
                g_labels.push_back(line);
        }
        if (!g_labels.empty()) {
            std::cerr << "[LPRParser] Loaded " << g_labels.size()
                      << " labels from " << CANDIDATES[i] << std::endl;
            return true;
        }
    }

    std::cerr << "[LPRParser] WARNING: Could not find labels_lpr.txt. "
                 "Will use raw char indices in output." << std::endl;
    return false;
}

/* ------------------------------------------------------------------ */
/*  NvDsInferParseCustomNVPlate                                        */
/* ------------------------------------------------------------------ */

extern "C" bool NvDsInferParseCustomNVPlate(
    std::vector<NvDsInferLayerInfo> const &outputLayersInfo,
    NvDsInferNetworkInfo  const &networkInfo,
    float classifierThreshold,
    std::vector<NvDsInferAttribute> &attrList,
    std::string &descString);

extern "C" bool NvDsInferParseCustomNVPlate(
    std::vector<NvDsInferLayerInfo> const &outputLayersInfo,
    NvDsInferNetworkInfo  const &networkInfo,
    float classifierThreshold,
    std::vector<NvDsInferAttribute> &attrList,
    std::string &descString)
{
    loadLabels();

    /* Locate the two output blobs by name */
    const int32_t *argmaxBuf = nullptr;
    const float   *maxBuf    = nullptr;
    int            seqLen    = 0;

    for (const auto &layer : outputLayersInfo) {
        if (layer.isInput) continue;
        const std::string name(layer.layerName ? layer.layerName : "");

        if (name.find("ArgMax") != std::string::npos ||
            name == "tf_op_layer_ArgMax") {
            argmaxBuf = static_cast<const int32_t *>(layer.buffer);
            /* seqLen from last dim */
            seqLen = layer.inferDims.d[layer.inferDims.numDims - 1];
        } else if (name.find("Max") != std::string::npos ||
                   name == "tf_op_layer_Max") {
            maxBuf = static_cast<const float *>(layer.buffer);
        }
    }

    /* If we couldn't match by name, fall back to positional order */
    if (!argmaxBuf && outputLayersInfo.size() >= 1) {
        const auto &l0 = outputLayersInfo[0];
        if (!l0.isInput) {
            argmaxBuf = static_cast<const int32_t *>(l0.buffer);
            seqLen    = l0.inferDims.d[l0.inferDims.numDims - 1];
        }
    }
    if (!maxBuf && outputLayersInfo.size() >= 2) {
        const auto &l1 = outputLayersInfo[1];
        if (!l1.isInput)
            maxBuf = static_cast<const float *>(l1.buffer);
    }

    if (!argmaxBuf || seqLen <= 0) {
        std::cerr << "[LPRParser] Could not find ArgMax output layer." << std::endl;
        return false;
    }

    int numLabels = (int)g_labels.size();
    /* Blank token index = numLabels (one past the last real char) */
    int blankIdx = numLabels;

    /* ---- CTC greedy decode (collapse repeats, skip blank) ---- */
    std::string plate;
    float       totalConf = 0.f;
    int         confCount = 0;
    int         prevIdx   = -1;

    for (int t = 0; t < seqLen; ++t) {
        int idx = argmaxBuf[t];

        /* Skip blank */
        if (idx == blankIdx || idx < 0) {
            prevIdx = idx;
            continue;
        }

        /* Collapse consecutive repeats */
        if (idx == prevIdx) continue;
        prevIdx = idx;

        float conf = maxBuf ? maxBuf[t] : 1.f;

        /* Below per-char threshold → treat as blank */
        if (conf < classifierThreshold) continue;

        if (idx < numLabels) {
            plate += g_labels[idx];
        } else {
            /* Unknown index — append raw digit */
            plate += std::to_string(idx);
        }
        totalConf += conf;
        ++confCount;
    }

    if (plate.empty()) {
        /* Nothing decoded above threshold */
        return true;   /* not an error — just no plate in this crop */
    }

    float avgConf = (confCount > 0) ? (totalConf / confCount) : 0.f;

    /* ---- Fill NvDsInferAttribute ---- */
    NvDsInferAttribute attr;
    attr.attributeIndex      = 0;
    attr.attributeValue      = 0;
    attr.attributeConfidence = avgConf;
    /* strdup — memory managed by DeepStream metadata system */
    attr.attributeLabel = strdup(plate.c_str());

    attrList.push_back(attr);
    descString = plate;

    return true;
}

CHECK_CUSTOM_CLASSIFIER_PARSE_FUNC_PROTOTYPE(NvDsInferParseCustomNVPlate);
