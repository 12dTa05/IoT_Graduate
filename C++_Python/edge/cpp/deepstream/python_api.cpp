/**
 * C API for Python bindings
 * Exports C functions for ctypes
 */

#include "ds_pipeline.h"
#include "ds_probes.h"
#include <iostream>

using namespace deepstream;

// Global pipeline instance
static Pipeline* g_pipeline = nullptr;
static MetadataProbe* g_probe = nullptr;

extern "C" {

/**
 * Create pipeline instance
 */
void* pipeline_create() {
    try {
        g_pipeline = new Pipeline();
        return g_pipeline;
    } catch (const std::exception& e) {
        std::cerr << "Error creating pipeline: " << e.what() << std::endl;
        return nullptr;
    }
}

/**
 * Destroy pipeline
 */
void pipeline_destroy(void* pipeline) {
    if (pipeline) {
        delete static_cast<Pipeline*>(pipeline);
        g_pipeline = nullptr;
    }
    
    if (g_probe) {
        delete g_probe;
        g_probe = nullptr;
    }
}

/**
 * Build pipeline
 */
bool pipeline_build(
    void* pipeline,
    const char* source_uri,
    const char* yolo_config,
    const char* lpr_config,
    const char* tracker_config,
    const char* analytics_config)
{
    if (!pipeline) {
        return false;
    }
    
    try {
        Pipeline* p = static_cast<Pipeline*>(pipeline);
        return p->build(
            source_uri,
            yolo_config,
            lpr_config,
            tracker_config,
            analytics_config
        );
    } catch (const std::exception& e) {
        std::cerr << "Error building pipeline: " << e.what() << std::endl;
        return false;
    }
}

/**
 * Start pipeline
 */
bool pipeline_start(void* pipeline) {
    if (!pipeline) {
        return false;
    }
    
    try {
        Pipeline* p = static_cast<Pipeline*>(pipeline);
        return p->start();
    } catch (const std::exception& e) {
        std::cerr << "Error starting pipeline: " << e.what() << std::endl;
        return false;
    }
}

/**
 * Stop pipeline
 */
void pipeline_stop(void* pipeline) {
    if (pipeline) {
        try {
            Pipeline* p = static_cast<Pipeline*>(pipeline);
            p->stop();
        } catch (const std::exception& e) {
            std::cerr << "Error stopping pipeline: " << e.what() << std::endl;
        }
    }
}

/**
 * Check if running
 */
bool pipeline_is_running(void* pipeline) {
    if (!pipeline) {
        return false;
    }
    
    try {
        Pipeline* p = static_cast<Pipeline*>(pipeline);
        return p->isRunning();
    } catch (const std::exception& e) {
        std::cerr << "Error checking pipeline status: " << e.what() << std::endl;
        return false;
    }
}

/**
 * Set metadata callback
 */
void pipeline_set_callback(void* pipeline, void (*callback)(const char*)) {
    if (!pipeline || !callback) {
        return;
    }
    
    try {
        Pipeline* p = static_cast<Pipeline*>(pipeline);
        p->setMetadataCallback([callback](const char* json) {
            callback(json);
        });
    } catch (const std::exception& e) {
        std::cerr << "Error setting callback: " << e.what() << std::endl;
    }
}

/**
 * Get FPS
 */
float pipeline_get_fps(void* pipeline) {
    if (!pipeline) {
        return 0.0f;
    }
    
    try {
        Pipeline* p = static_cast<Pipeline*>(pipeline);
        return p->getFPS();
    } catch (const std::exception& e) {
        std::cerr << "Error getting FPS: " << e.what() << std::endl;
        return 0.0f;
    }
}

} // extern "C"
