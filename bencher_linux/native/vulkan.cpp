// Observe accepted game and LSFG presents without modifying Vulkan submissions.
#include <vulkan/vk_layer.h>
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <fcntl.h>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unistd.h>

namespace {
using Clock = std::chrono::steady_clock;
struct PresentScope {
    VkSwapchainKHR swapchain;
    unsigned forwarded = 0;
};
thread_local PresentScope* current_present = nullptr;

bool selected_process() {
    static const bool selected = [] {
        const char* target = std::getenv("BENCHER_FRAME_TARGET");
        if (!target || !std::getenv("BENCHER_FRAME_DIR")) return false;
        std::ifstream process("/proc/self/cmdline");
        std::string command;
        std::getline(process, command, '\0');
        const auto basename = command.substr(command.find_last_of("/\\") + 1);
        return strcasecmp(basename.c_str(), target) == 0;
    }();
    return selected;
}

bool accepted(VkResult result) {
    return result == VK_SUCCESS || result == VK_SUBOPTIMAL_KHR;
}

void* key(const void* handle) {
    return *static_cast<void* const*>(handle);
}

template<typename Node, typename Info>
Node* link_info(const Info* info, VkStructureType type) {
    auto* node = const_cast<VkBaseOutStructure*>(static_cast<const VkBaseOutStructure*>(info->pNext));
    for (; node; node = node->pNext) {
        if (node->sType == type) {
            auto* entry = reinterpret_cast<Node*>(node);
            if (entry->function == VK_LAYER_LINK_INFO) return entry;
        }
    }
    return nullptr;
}

struct InstanceDispatch {
    PFN_vkGetInstanceProcAddr proc = nullptr;
    PFN_GetPhysicalDeviceProcAddr physical_proc = nullptr;
    VkInstance handle = VK_NULL_HANDLE;
};
struct DeviceDispatch {
    VkDevice handle = VK_NULL_HANDLE;
    PFN_vkGetDeviceProcAddr proc = nullptr;
    PFN_vkQueuePresentKHR present = nullptr;
#ifdef VK_NV_low_latency2
    PFN_vkGetLatencyTimingsNV latency = nullptr;
#endif
};

struct TimingBatch {
    uint64_t after;
    uint64_t newest = after;
    std::array<uint64_t, 64> ids{};
    unsigned count = 0;
    double sum = 0;

    void add(uint64_t id, uint64_t from, uint64_t to) {
        if (!from || to <= from || to <= after || to - from >= 1'000'000) return;
        if (std::find(ids.begin(), ids.begin() + count, id) != ids.begin() + count) return;
        ids[count++] = id;
        sum += to - from;
        newest = std::max(newest, to);
    }

    void publish(FILE* out, const char* key, bool fresh) const {
        if (fresh && count) std::fprintf(out, ",\"%s\":%.3f", key, sum / count / 1000);
    }
};

struct Counters {
    std::mutex mutex;
    uint64_t rendered = 0;
    uint64_t generated = 0;
    uint64_t last_rendered = 0;
    uint64_t last_generated = 0;
    VkSwapchainKHR latency_swapchain = VK_NULL_HANDLE;
    uint64_t last_cpu_end = 0;
    uint64_t last_gpu_end = 0;
    uint64_t last_present_end = 0;
    Clock::time_point published = Clock::now();

    void simulation(uint64_t id) {
        std::lock_guard lock(mutex);
        if (last_simulation == id) return;
        last_simulation = id;
        static const int fd = [] {
            const auto path = std::string(std::getenv("BENCHER_FRAME_DIR")) + "/simulation-" +
                              std::to_string(getpid()) + ".bin";
            return open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
        }();
        if (fd < 0) return;
        struct { uint64_t stamp; double value; } event{
            static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
                Clock::now().time_since_epoch()).count()), 0};
        (void)write(fd, &event, sizeof(event));
    }
    uint64_t last_simulation = UINT64_MAX;

    void marker(VkSwapchainKHR swapchain) {
        std::lock_guard lock(mutex);
        if (latency_swapchain != swapchain) {
            latency_swapchain = swapchain;
            last_cpu_end = last_gpu_end = last_present_end = 0;
        }
    }

    void record(const DeviceDispatch& device, const PresentScope& scope) {
        static const int stream = [] {
            const auto path = std::string(std::getenv("BENCHER_FRAME_DIR")) + "/real-" +
                              std::to_string(getpid()) + ".bin";
            return open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
        }();
        std::lock_guard lock(mutex);
        ++rendered;
        if (scope.forwarded > 1) generated += scope.forwarded - 1;
        const auto now = Clock::now();
        if (stream >= 0) {
            struct { uint64_t stamp; double generated; } sample{
                static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
                    now.time_since_epoch()).count()),
                static_cast<double>(scope.forwarded > 1 ? scope.forwarded - 1 : 0)};
            static_assert(sizeof(sample) == 16);
            (void)write(stream, &sample, sizeof(sample));
        }
        const double elapsed = std::chrono::duration<double>(now - published).count();
        if (elapsed < 1) return;

        TimingBatch cpu{last_cpu_end}, gpu{last_gpu_end}, latency{last_present_end};
#ifdef VK_NV_low_latency2
        if (device.latency && scope.swapchain == latency_swapchain) {
            VkLatencyTimingsFrameReportNV reports[64]{};
            for (auto& report : reports) report.sType = VK_STRUCTURE_TYPE_LATENCY_TIMINGS_FRAME_REPORT_NV;
            VkGetLatencyMarkerInfoNV info{VK_STRUCTURE_TYPE_GET_LATENCY_MARKER_INFO_NV, nullptr, 64, reports};
            device.latency(device.handle, scope.swapchain, &info);
            for (const auto* row = reports; row < reports + std::min(info.timingCount, 64u); ++row) {
                cpu.add(row->presentID, row->renderSubmitStartTimeUs, row->renderSubmitEndTimeUs);
                gpu.add(row->presentID, row->gpuRenderStartTimeUs, row->gpuRenderEndTimeUs);
                latency.add(row->presentID, row->inputSampleTimeUs, row->presentEndTimeUs);
            }
            last_cpu_end = cpu.newest;
            last_gpu_end = gpu.newest;
            last_present_end = latency.newest;
        }
#endif
        const auto filename = std::string(std::getenv("BENCHER_FRAME_DIR")) + "/submit-" +
                              std::to_string(getpid()) + ".json";
        const auto temporary = filename + ".tmp";
        if (auto* out = std::fopen(temporary.c_str(), "w")) {
            std::fprintf(out, "{\"rendered_frames\":%llu,\"rendered_fps\":%.3f",
                         static_cast<unsigned long long>(rendered), (rendered - last_rendered) / elapsed);
            if (scope.forwarded) {
                std::fprintf(out, ",\"generated_frames\":%llu,\"generated_fps\":%.3f",
                             static_cast<unsigned long long>(generated), (generated - last_generated) / elapsed);
            }
            cpu.publish(out, "cpu_render_ms", elapsed <= 3);
            gpu.publish(out, "gpu_render_ms", elapsed <= 3);
            latency.publish(out, "input_present_ms", elapsed <= 3);
            std::fputs("}\n", out);
            if (std::fclose(out) == 0) std::rename(temporary.c_str(), filename.c_str());
        }
        published = now;
        last_rendered = rendered;
        last_generated = generated;
    }
} counters;

template<bool Output>
struct Probe {
    static inline std::mutex mutex;
    static inline std::unordered_map<void*, InstanceDispatch> instances;
    static inline std::unordered_map<void*, DeviceDispatch> devices;

    static InstanceDispatch instance(const void* handle) {
        std::lock_guard lock(mutex);
        const auto entry = instances.find(key(handle));
        return entry == instances.end() ? InstanceDispatch{} : entry->second;
    }

    static DeviceDispatch device(const void* handle) {
        std::lock_guard lock(mutex);
        const auto entry = devices.find(key(handle));
        return entry == devices.end() ? DeviceDispatch{} : entry->second;
    }

    static VKAPI_ATTR VkResult VKAPI_CALL create_instance(
        const VkInstanceCreateInfo* info, const VkAllocationCallbacks* allocator, VkInstance* handle
    ) {
        auto* layer = link_info<VkLayerInstanceCreateInfo>(info, VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO);
        if (!layer) return VK_ERROR_INITIALIZATION_FAILED;
        auto dispatch = InstanceDispatch{layer->u.pLayerInfo->pfnNextGetInstanceProcAddr,
                                         layer->u.pLayerInfo->pfnNextGetPhysicalDeviceProcAddr};
        const auto next = reinterpret_cast<PFN_vkCreateInstance>(dispatch.proc(nullptr, "vkCreateInstance"));
        layer->u.pLayerInfo = layer->u.pLayerInfo->pNext;
        const auto result = next(info, allocator, handle);
        if (result == VK_SUCCESS) {
            std::lock_guard lock(mutex);
            dispatch.handle = *handle;
            instances[key(*handle)] = dispatch;
        }
        return result;
    }

    static VKAPI_ATTR VkResult VKAPI_CALL create_device(
        VkPhysicalDevice physical, const VkDeviceCreateInfo* info, const VkAllocationCallbacks* allocator, VkDevice* handle
    ) {
        auto* layer = link_info<VkLayerDeviceCreateInfo>(info, VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO);
        if (!layer) return VK_ERROR_INITIALIZATION_FAILED;
        const auto instance_proc = layer->u.pLayerInfo->pfnNextGetInstanceProcAddr;
        const auto device_proc = layer->u.pLayerInfo->pfnNextGetDeviceProcAddr;
        // The loader and layers use the physical device's instance dispatch key.
        const auto instance_handle = instance(physical).handle;
        const auto next = reinterpret_cast<PFN_vkCreateDevice>(instance_proc(instance_handle, "vkCreateDevice"));
        if (!next) return VK_ERROR_INITIALIZATION_FAILED;
        layer->u.pLayerInfo = layer->u.pLayerInfo->pNext;
        const auto result = next(physical, info, allocator, handle);
        if (result == VK_SUCCESS) {
            DeviceDispatch dispatch{*handle, device_proc,
                reinterpret_cast<PFN_vkQueuePresentKHR>(device_proc(*handle, "vkQueuePresentKHR"))};
#ifdef VK_NV_low_latency2
            if constexpr (!Output) {
                for (uint32_t i = 0; i < info->enabledExtensionCount; ++i) {
                    if (!std::strcmp(info->ppEnabledExtensionNames[i], VK_NV_LOW_LATENCY_2_EXTENSION_NAME)) {
                        dispatch.latency = reinterpret_cast<PFN_vkGetLatencyTimingsNV>(
                            device_proc(*handle, "vkGetLatencyTimingsNV"));
                        break;
                    }
                }
            }
#endif
            std::lock_guard lock(mutex);
            devices[key(*handle)] = dispatch;
        }
        return result;
    }

    static VKAPI_ATTR void VKAPI_CALL destroy_instance(VkInstance handle, const VkAllocationCallbacks* allocator) {
        auto dispatch = instance(handle);
        {
            std::lock_guard lock(mutex);
            instances.erase(key(handle));
        }
        reinterpret_cast<PFN_vkDestroyInstance>(dispatch.proc(handle, "vkDestroyInstance"))(handle, allocator);
    }

    static VKAPI_ATTR void VKAPI_CALL destroy_device(VkDevice handle, const VkAllocationCallbacks* allocator) {
        auto dispatch = device(handle);
        {
            std::lock_guard lock(mutex);
            devices.erase(key(handle));
        }
        reinterpret_cast<PFN_vkDestroyDevice>(dispatch.proc(handle, "vkDestroyDevice"))(handle, allocator);
    }

#ifdef VK_NV_low_latency2
    static VKAPI_ATTR void VKAPI_CALL marker(VkDevice handle, VkSwapchainKHR swapchain, const VkSetLatencyMarkerInfoNV* info) {
        auto dispatch = device(handle);
        auto next = reinterpret_cast<PFN_vkSetLatencyMarkerNV>(dispatch.proc(handle, "vkSetLatencyMarkerNV"));
        next(handle, swapchain, info);
        if (selected_process()) {
            counters.marker(swapchain);
            if (info->marker == VK_LATENCY_MARKER_SIMULATION_END_NV)
                counters.simulation(info->presentID);
        }
    }

#endif
    static VKAPI_ATTR VkResult VKAPI_CALL present(VkQueue queue, const VkPresentInfoKHR* info) {
        auto dispatch = device(queue);
        if (!dispatch.present) return VK_ERROR_INITIALIZATION_FAILED;
        if constexpr (Output) {
            auto result = dispatch.present(queue, info);
            if (current_present && info->swapchainCount == 1 &&
                info->pSwapchains[0] == current_present->swapchain && accepted(result)) {
                ++current_present->forwarded;
            }
            return result;
        } else {
            if (!selected_process() || info->swapchainCount != 1) return dispatch.present(queue, info);
            PresentScope scope{info->pSwapchains[0]};
            auto* previous = current_present;
            current_present = &scope;
            auto result = dispatch.present(queue, info);
            current_present = previous;
            if (accepted(result)) counters.record(dispatch, scope);
            return result;
        }
    }

    static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL device_proc(VkDevice handle, const char* name) {
        if (!std::strcmp(name, "vkGetDeviceProcAddr")) return reinterpret_cast<PFN_vkVoidFunction>(device_proc);
        if (!handle) return nullptr;
        auto dispatch = device(handle);
        if (!dispatch.proc) return nullptr;
        auto next = dispatch.proc(handle, name);
        if (!next) return nullptr;
        if (!std::strcmp(name, "vkDestroyDevice")) return reinterpret_cast<PFN_vkVoidFunction>(destroy_device);
        if (!std::strcmp(name, "vkQueuePresentKHR")) return reinterpret_cast<PFN_vkVoidFunction>(present);
#ifdef VK_NV_low_latency2
        if constexpr (!Output) {
            if (!std::strcmp(name, "vkSetLatencyMarkerNV")) return reinterpret_cast<PFN_vkVoidFunction>(marker);
        }
#endif
        return next;
    }

    static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL instance_proc(VkInstance handle, const char* name) {
        if (!std::strcmp(name, "vkCreateInstance")) return reinterpret_cast<PFN_vkVoidFunction>(create_instance);
        if (!std::strcmp(name, "vkGetInstanceProcAddr")) return reinterpret_cast<PFN_vkVoidFunction>(instance_proc);
        if (!handle) return nullptr;
        if (!std::strcmp(name, "vkCreateDevice")) return reinterpret_cast<PFN_vkVoidFunction>(create_device);
        if (!std::strcmp(name, "vkDestroyInstance")) return reinterpret_cast<PFN_vkVoidFunction>(destroy_instance);
        if (!std::strcmp(name, "vkGetDeviceProcAddr")) return reinterpret_cast<PFN_vkVoidFunction>(device_proc);
        if (!std::strcmp(name, "vkQueuePresentKHR")) return reinterpret_cast<PFN_vkVoidFunction>(present);
        if (!std::strcmp(name, "vkDestroyDevice")) return reinterpret_cast<PFN_vkVoidFunction>(destroy_device);
#ifdef VK_NV_low_latency2
        if constexpr (!Output) {
            if (!std::strcmp(name, "vkSetLatencyMarkerNV")) return reinterpret_cast<PFN_vkVoidFunction>(marker);
        }
#endif
        auto dispatch = instance(handle);
        return dispatch.proc ? dispatch.proc(handle, name) : nullptr;
    }

    static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL physical_proc(VkInstance handle, const char* name) {
        if (!handle) return nullptr;
        auto dispatch = instance(handle);
        if (dispatch.physical_proc) return dispatch.physical_proc(handle, name);
        return dispatch.proc ? dispatch.proc(handle, name) : nullptr;
    }

    static VkResult negotiate(VkNegotiateLayerInterface* interface) {
        if (interface->loaderLayerInterfaceVersion < 2) return VK_ERROR_INITIALIZATION_FAILED;
        interface->loaderLayerInterfaceVersion = 2;
        interface->pfnGetInstanceProcAddr = instance_proc;
        interface->pfnGetDeviceProcAddr = device_proc;
        interface->pfnGetPhysicalDeviceProcAddr = physical_proc;
        return VK_SUCCESS;
    }
};
}

extern "C" __attribute__((visibility("default")))
VKAPI_ATTR VkResult VKAPI_CALL bencherGameProbe(VkNegotiateLayerInterface* interface) {
    return Probe<false>::negotiate(interface);
}

extern "C" __attribute__((visibility("default")))
VKAPI_ATTR VkResult VKAPI_CALL bencherOutputProbe(VkNegotiateLayerInterface* interface) {
    return Probe<true>::negotiate(interface);
}
