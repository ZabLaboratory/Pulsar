// Pulsar frontend stub.
//
// Replaces the upstream OBS Studio Qt frontend (frontend/widgets/) with
// a minimal implementation of the obs_frontend_callbacks vtable. Without
// this, every obs_frontend_* call from a plugin (notably obs-websocket's
// EventHandler) hits a "no callbacks" guard and returns null/false,
// leaving WebSocket events frozen and getters useless.
//
// This is a static library, not an OBS plugin. pulsar-headless's main()
// owns the lifecycle:
//
//   pulsar_frontend_init()             -- BEFORE obs_load_all_modules.
//                                         Installs the vtable + UI task
//                                         handler only. Plugins registering
//                                         frontend callbacks during
//                                         obs_module_load find a populated
//                                         table.
//   obs_load_all_modules
//   obs_post_load_modules
//   pulsar_frontend_finished_loading() -- AFTER plugins are loaded.
//                                         Runs setup() (scene, fade
//                                         transition, x264 + aac encoders,
//                                         outputs with encoders attached,
//                                         window_capture source, rtmp
//                                         service, record directory) and
//                                         then emits FINISHED_LOADING.
//                                         Splitting init from setup is the
//                                         only way to call factories like
//                                         obs_video_encoder_create("obs_x264",
//                                         ...) -- the IDs are owned by
//                                         plugins that aren't registered
//                                         until after load_all_modules.
//   ... idle loop ...
//   pulsar_frontend_shutdown()         -- emits EXIT, gracefully stops
//                                         active outputs, then hands the
//                                         object back to obs-frontend-api
//                                         (which deletes it via the
//                                         destructor running teardown()).
//
// The dual-lane path uses the libobs-owned main view/video as the stable
// Program surface and one auxiliary view/video as Preview.  A Cut swaps their
// channel-0 roots at the graphics boundary.  The legacy single-lane fallback
// continues to bind channel 0 with obs_set_output_source().
//
// Event sources mix two patterns: explicit emission for state mutations
// (SCENE_CHANGED on set_current_scene, STUDIO_MODE_* on
// set_preview_program_mode, etc.) and signal-bridged for output state
// (STREAMING/RECORDING/REPLAY/VIRTUALCAM start/stop). STARTING and STOPPING
// are emitted manually around obs_output_start / obs_output_stop, STARTED
// and STOPPED come from the output's own signal handler -- this matches
// the ordering OBSBasic produces upstream so v5 clients see the same
// timeline.

#include <obs.h>
#include <obs.hpp>
#include <obs-frontend-internal.hpp>
#include <util/config-file.h>
#include <util/darray.h>
#include <util/source-profiler.h>
#include <util/util.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <ctime>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <memory>
#include <map>
#include <mutex>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <util/platform.h>
#include <nlohmann/json.hpp>

#ifdef _WIN32
#include <windows.h>
#endif

#ifdef _WIN32
#include <bcrypt.h>
#endif

#include "pulsar-frontend-stub.h"
#include "pulsar-dual-lane-config.h"
#include "pulsar-transition-controller.h"
#include "pulsar-dual-lane-control.h"
#include "pulsar-runtime-telemetry.h"
#include "pulsar-program-audio.h"
#include "pulsar-stream-egress.h"
#include "obs-websocket-api.h"

namespace {

using json = nlohmann::json;

// Boot-time feature switches are deliberately process-local and fail closed.
// `PULSAR_DISABLE_DUAL_LANE` is retained as the backwards-compatible probe
// switch used by the single-canvas reference campaign.  The positive flag is
// useful for operators that want an explicit capability declaration; when it
// is absent, the approved dual-lane path remains enabled.  Neither value is
// read after setup(), and no WebSocket/leaf request can mutate it.
enum class EnvBool : uint8_t { Unset, Enabled, Disabled, Invalid };

EnvBool parse_env_bool(const char *value)
{
    if (!value)
        return EnvBool::Unset;
    // An explicitly present empty value is configuration, not absence.  It
    // must fail closed just like every other malformed boot switch; treating
    // it as Unset would silently select the default dual-lane topology.
    if (!*value)
        return EnvBool::Invalid;
    std::string lower;
    for (const char *p = value; *p; ++p)
        lower.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(*p))));
    if (lower == "1" || lower == "true" || lower == "on" || lower == "yes")
        return EnvBool::Enabled;
    if (lower == "0" || lower == "false" || lower == "off" || lower == "no")
        return EnvBool::Disabled;
    return EnvBool::Invalid;
}

struct DualLaneActivation {
    bool enabled = true;
    const char *reason = "default";
};

DualLaneActivation resolve_dual_lane_activation()
{
    const EnvBool positive = parse_env_bool(std::getenv("PULSAR_DUAL_LANE_ENABLED"));
    const EnvBool legacyDisable = parse_env_bool(std::getenv("PULSAR_DISABLE_DUAL_LANE"));

    // A malformed explicit capability is never treated as an enable.  This
    // prevents a typo in a deployment environment from silently activating a
    // topology that was meant to stay in the compatibility path.
    if (positive == EnvBool::Invalid)
        return {false, "invalid-PULSAR_DUAL_LANE_ENABLED"};
    if (legacyDisable == EnvBool::Invalid)
        return {false, "invalid-PULSAR_DISABLE_DUAL_LANE"};
    if (legacyDisable == EnvBool::Enabled)
        return {false, "PULSAR_DISABLE_DUAL_LANE"};
    if (positive == EnvBool::Disabled)
        return {false, "PULSAR_DUAL_LANE_ENABLED=0"};
    if (positive == EnvBool::Enabled)
        return {true, "PULSAR_DUAL_LANE_ENABLED=1"};
    return {true, "default"};
}

struct DualLaneRollbackSetting {
    uint64_t takes = 0;
    bool valid = true;
    const char *reason = "unset";
};

DualLaneRollbackSetting resolve_dual_lane_rollback_after_takes()
{
    const char *value = std::getenv("PULSAR_DUAL_LANE_ROLLBACK_AFTER_TAKES");
    const auto parsed = pulsar_dual_lane_config::parse_rollback_after_takes(value);
    if (!parsed.valid) {
        blog(LOG_WARNING,
             "[pulsar-dual-lane] PULSAR_DUAL_LANE_ROLLBACK_AFTER_TAKES rejected; "
             "expected an integer in 1..100000");
        return {0, false, "invalid-PULSAR_DUAL_LANE_ROLLBACK_AFTER_TAKES"};
    }
    if (!parsed.present)
        return {0, true, "unset"};
    return {parsed.takes, true, "valid"};
}

bool resolve_dual_lane_transitions()
{
    const EnvBool value = parse_env_bool(std::getenv("PULSAR_DUAL_LANE_TRANSITIONS"));
    if (value == EnvBool::Enabled)
        return true;
    // Unset and every explicit false value preserve the validated atomic Cut.
    // Malformed configuration is fail-closed and therefore also remains Cut.
    return false;
}

class PulsarFrontendAPI;
PulsarFrontendAPI *g_api = nullptr;
class PulsarSceneSwitchVendor;
std::atomic<PulsarSceneSwitchVendor *> g_sceneSwitchVendor{nullptr};

template <typename T> struct StubCallback {
    T cb;
    void *priv;
    StubCallback(T c, void *p) : cb(c), priv(p) {}
};

// The websocket module is a DLL while this frontend implementation is part
// of pulsar-headless.exe.  Keep the mutation fence in a process-lifetime
// object and expose only the two small operations through libobs's global
// proc handler.  In particular, the callbacks never carry a
// PulsarFrontendAPI pointer: a proc handler can outlive the frontend during
// module/OBS teardown, so such a pointer would turn a late request into a
// use-after-free.
class PulsarDualLaneControlBridge {
public:
    void install()
    {
        lifecycle_.store(Lifecycle::Disabled, std::memory_order_release);
        pending_.store(false, std::memory_order_release);

        proc_handler_t *global = obs_get_proc_handler();
        if (!global)
            return;

        proc_handler_add(global,
                         "void pulsar_dual_lane_mutation_enter(in bool mutating, out bool available, "
                         "out bool allowed, out bool held, out bool frozen)",
                         &MutationEnter, this);
        proc_handler_add(global, "void pulsar_dual_lane_mutation_leave()", &MutationLeave, this);
    }

    void activate()
    {
        pending_.store(false, std::memory_order_release);
        lifecycle_.store(Lifecycle::Active, std::memory_order_release);
    }

    // Frame-boundary callbacks use this non-blocking transition.  It closes
    // admission immediately and lets any mutation which was already holding
    // dispatchMutex_ finish after the callback releases dualLaneMutex.  The
    // draining wait belongs to deactivate(), which is called from teardown,
    // never from the graphics thread.
    void freeze()
    {
        lifecycle_.store(Lifecycle::ShuttingDown, std::memory_order_release);
        pending_.store(true, std::memory_order_release);
    }

    // This is the single effective rollback/teardown latch. Readers such as
    // the scene-switch GetState adapter must observe the same atomic bit as
    // websocket mutation admission; a separately updated vendor boolean
    // would leave a window in which GetState says "ready" after admission was
    // already closed.
    bool frozen() const
    {
        return lifecycle_.load(std::memory_order_acquire) == Lifecycle::ShuttingDown;
    }

    // Stop admitting new mutations, then wait for the one already inside the
    // supported websocket dispatch path to finish.  The graphics callback
    // only changes the atomic pending bit and never waits on this mutex, so a
    // Cut cannot deadlock teardown or vice versa.
    void deactivate()
    {
        freeze();
        std::unique_lock<std::mutex> lock(dispatchMutex_);
        pending_.store(false, std::memory_order_release);
        // Keep ShuttingDown published until the next install().  A late
        // websocket proc call must remain fail-closed while the frontend
        // emits EXIT and libobs releases its objects; reporting Disabled
        // here would make the websocket adapter fall back to legacy writes.
    }

    void set_pending(bool pending) { pending_.store(pending, std::memory_order_release); }

private:
    enum class Lifecycle : uint8_t { Disabled, Active, ShuttingDown };

    static void MutationEnter(void *param, calldata_t *cd)
    {
        auto *self = static_cast<PulsarDualLaneControlBridge *>(param);
        if (!self)
            return;

        const bool mutating = calldata_bool(cd, "mutating");
        const Lifecycle state = self->lifecycle_.load(std::memory_order_acquire);
        const bool available = state != Lifecycle::Disabled;
        bool allowed = true;
        bool held = false;
        const bool frozen = state == Lifecycle::ShuttingDown;

        if (mutating && available) {
            // Lock before checking pending.  This makes a mutation which was
            // already in progress complete before TakeAccepted can publish
            // the pending bit, and makes every later mutation re-check the
            // bit after it obtains the same lock.
            self->dispatchMutex_.lock();
            const Lifecycle lockedState = self->lifecycle_.load(std::memory_order_acquire);
            if (lockedState != Lifecycle::Active || self->pending_.load(std::memory_order_acquire)) {
                self->dispatchMutex_.unlock();
                allowed = false;
            } else {
                held = true;
            }
        }

        calldata_set_bool(cd, "available", available);
        calldata_set_bool(cd, "allowed", allowed);
        calldata_set_bool(cd, "held", held);
        calldata_set_bool(cd, "frozen", frozen);
    }

    static void MutationLeave(void *param, calldata_t *)
    {
        auto *self = static_cast<PulsarDualLaneControlBridge *>(param);
        if (self)
            self->dispatchMutex_.unlock();
    }

    std::mutex dispatchMutex_;
    std::atomic<Lifecycle> lifecycle_{Lifecycle::Disabled};
    std::atomic<bool> pending_{false};
};

// Deliberately never destroyed while the global proc handler can still call
// it.  OBS owns/replaces the proc handler as part of its process lifecycle;
// the callback data itself remains valid across that boundary.
PulsarDualLaneControlBridge g_dualLaneControlBridge;

// #246 runtime evidence producer.  This object intentionally has process
// lifetime: libobs's procedure handler has no remove operation, and the
// websocket/virtual-camera modules can still issue a late lookup while OBS is
// tearing down its frontend callbacks.  `shutdown()` only disables evidence;
// it never leaves a dangling callback payload behind.
class PulsarRuntimeTelemetry {
    struct TakeContext {
        bool valid = false;
        std::string commandId;
        std::string intentId;
        std::string runtimeInstanceId;
        std::string takeCommandId;
        std::string targetLaneId;
        std::string targetSceneId;
        std::string payloadSha256;
        uint64_t freezeUntilNs = 0;
        uint64_t acceptedAtNs = 0;
        uint64_t programRevision = 0;
        uint64_t previewRevision = 0;
        uint64_t roleMapRevision = 0;
        int onAirLane = 0;
        int previewLane = 1;
        uint64_t frameId = 0;
        uint64_t ptsNs = 0;
    };

public:
    ~PulsarRuntimeTelemetry()
    {
        stopResourceSampler();
        stopTraceWriter();
    }

    void install()
    {
        proc_handler_t *global = obs_get_proc_handler();
        if (!global)
            return;
        proc_handler_add(global,
                         "void pulsar_runtime_telemetry_begin_take(in string command_id, in string intent_id, "
                         "in string runtime_instance_id, in string take_command_id, in string target_lane_id, "
                         "in string target_scene_id, in int freeze_until_monotonic_ns, in string payload_sha256, "
                         "out bool available, out bool accepted)",
                         &BeginTake, this);
        proc_handler_add(global, "void pulsar_runtime_telemetry_cancel_take()", &CancelTake, this);
        proc_handler_add(global,
                         "void pulsar_runtime_telemetry_snapshot_frame(out bool valid, out int server_seq, "
                         "out int frame_id, out int pts_ns, out int program_revision, out int preview_revision, "
                         "out int role_map_revision, out string runtime_instance_id, out string command_id, "
                         "out string intent_id, out string take_command_id)",
                         &SnapshotFrame, this);
    }

    void initialize(const char *encoderFamily, const obs_video_info &video, bool wgcWorkload,
                    bool cefWorkload, bool wgcSourceBound, obs_encoder_t *videoEncoder,
                    obs_output_t *streamOutput, obs_output_t *programReturnOutput,
                    obs_output_t *previewReturnOutput, video_t *programVideo, video_t *previewVideo,
                    obs_source_t *programRoot, obs_source_t *previewRoot)
    {
        stopResourceSampler();
        stopTraceWriter();

        // Retire any previous runtime before validating the next trace
        // configuration.  This keeps a failed reinitialization fail-closed.
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            enabled_ = false;
            degraded_ = false;
            pending_ = {};
            reserved_ = {};
            accepted_ = {};
            committed_ = {};
            lastRawTake_.clear();
            lastPacketTake_.clear();
            serverSeq_ = 0;
            programRevision_ = 0;
            previewRevision_ = 0;
            roleMapRevision_ = 0;
            rawFrameCount_.store(0, std::memory_order_relaxed);
            packetFrameCount_.store(0, std::memory_order_relaxed);
            encodeTimeNsTotal_.store(0, std::memory_order_relaxed);
            encodeTimeSampleCount_.store(0, std::memory_order_relaxed);
            resourceMode_.clear();
            wgcWorkload_ = false;
            cefWorkload_ = false;
            buildRevision_.clear();
            traceHost_.clear();
            traceGpu_.clear();
            producerTopology_.clear();
            producerCount_ = 0;
            encoderFamily_.clear();
            videoEncoder_ = nullptr;
            streamOutput_ = nullptr;
            programReturnOutput_ = nullptr;
            previewReturnOutput_ = nullptr;
            programVideo_ = nullptr;
            previewVideo_ = nullptr;
            programRoot_ = nullptr;
            previewRoot_ = nullptr;
            videoWidth_ = 0;
            videoHeight_ = 0;
            videoFpsNum_ = 0;
            videoFpsDen_ = 0;
        }

        const char *tracePath = std::getenv("PULSAR_TRACE_PATH");
        const char *runtimeId = std::getenv("PULSAR_RUNTIME_INSTANCE_ID");
        const char *buildRevision = std::getenv("PULSAR_BUILD_REVISION");
        const char *traceHost = std::getenv("PULSAR_TRACE_HOST");
        const char *traceGpu = std::getenv("PULSAR_TRACE_GPU");
        const char *producerTopology = std::getenv("PULSAR_TRACE_PRODUCER_TOPOLOGY");
        const char *producerCount = std::getenv("PULSAR_TRACE_PRODUCER_COUNT");
        {
            // Even non-traced rollback runs use the same validated process
            // identity in their operational marker.  Do not fall back to a
            // caller-controlled or stale value after a failed trace setup.
            std::lock_guard<std::mutex> lock(stateMutex_);
            runtimeInstanceId_ = validIdentifier(runtimeId) ? runtimeId : "pulsar-runtime";
        }
        char *producerCountEnd = nullptr;
        const unsigned long parsedProducerCount = producerCount
                                                         ? std::strtoul(producerCount, &producerCountEnd, 10)
                                                         : 0;
        const bool producerCountValid = producerCount && producerCountEnd && *producerCountEnd == '\0' &&
                                        parsedProducerCount >= 1 && parsedProducerCount <= 2;
        const bool producerTopologyValid = producerTopology &&
                                           (std::strcmp(producerTopology, "single_lane_reference") == 0 ||
                                            std::strcmp(producerTopology, "dual_lane_ab") == 0);
        if (!tracePath || !*tracePath || !validIdentifier(runtimeId) || !validBuildRevision(buildRevision) ||
            !validHardwareLabel(traceHost) || !validHardwareLabel(traceGpu) || !producerTopologyValid ||
            !producerCountValid ||
            ((std::strcmp(producerTopology, "single_lane_reference") == 0 && parsedProducerCount != 1) ||
             (std::strcmp(producerTopology, "dual_lane_ab") == 0 && parsedProducerCount != 2))) {
            blog(LOG_WARNING,
                 "[pulsar-runtime-telemetry] trace disabled: exact build, host/GPU, and producer topology metadata are required");
            return;
        }

        std::string session;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            runtimeInstanceId_ = runtimeId;
            tracePath_ = tracePath;
            buildRevision_ = buildRevision;
            traceHost_ = traceHost;
            traceGpu_ = traceGpu;
            producerTopology_ = producerTopology;
            producerCount_ = static_cast<uint64_t>(parsedProducerCount);
            sessionId_ = environmentOr("PULSAR_TRACE_SESSION_ID", runtimeInstanceId_ + "-" + std::to_string(nowNs()));
            if (!validIdentifier(sessionId_.c_str()))
                sessionId_ = runtimeInstanceId_ + "-session";

            const char *resourceMode = std::getenv("PULSAR_TRACE_RESOURCE_MODE");
            if (resourceMode && (std::strcmp(resourceMode, "reference") == 0 ||
                                 std::strcmp(resourceMode, "dual_lane") == 0)) {
                resourceMode_ = resourceMode;
            }
            wgcWorkload_ = wgcWorkload;
            cefWorkload_ = cefWorkload;
            encoderFamily_ = encoderFamily && *encoderFamily ? encoderFamily : "unknown";
            videoEncoder_ = videoEncoder;
            // Non-owning: setup owns streamOutput and teardown joins this
            // sampler before stopping/releasing the output.
            streamOutput_ = streamOutput;
            programReturnOutput_ = programReturnOutput;
            previewReturnOutput_ = previewReturnOutput;
            programVideo_ = programVideo;
            previewVideo_ = previewVideo;
            programRoot_ = programRoot;
            previewRoot_ = previewRoot;
            videoWidth_ = video.output_width;
            videoHeight_ = video.output_height;
            videoFpsNum_ = video.fps_num;
            videoFpsDen_ = video.fps_den;
            session = sessionJson(encoderFamily, video, wgcWorkload, cefWorkload, wgcSourceBound);
        }

#ifdef _WIN32
        const std::string mutexName = "Local\\Pulsar." + runtimeInstanceId_ + ".Trace";
        traceMutex_ = CreateMutexA(nullptr, FALSE, mutexName.c_str());
#endif

        const bool append = truthy(std::getenv("PULSAR_TRACE_APPEND"));
        bool hasExisting = false;
        if (append) {
            std::ifstream existing(tracePath_, std::ios::binary | std::ios::ate);
            hasExisting = existing.good() && existing.tellg() > 0;
        }
        if (!hasExisting) {
            {
                std::lock_guard<std::mutex> fileLock(fileMutex_);
#ifdef _WIN32
                if (traceMutex_)
                    WaitForSingleObject(traceMutex_, INFINITE);
#endif
                std::ofstream truncate(tracePath_, std::ios::binary | std::ios::trunc);
#ifdef _WIN32
                if (traceMutex_)
                    ReleaseMutex(traceMutex_);
#endif
            }
            // The state/lane locks are intentionally not held while opening
            // or appending JSONL.  The writer queue is asynchronous: this
            // enqueue is the only work the control path performs for the
            // session record, and the worker performs the actual file I/O.
        }

        if (!startTraceWriter() || (!hasExisting && !enqueueLine(session))) {
            stopTraceWriter();
            blog(LOG_WARNING, "[pulsar-runtime-telemetry] trace disabled: FIFO writer unavailable");
            return;
        }

        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            enabled_ = true;
        }
        blog(LOG_INFO, "[pulsar-runtime-telemetry] trace enabled path=%s runtime_instance_id=%s session_id=%s",
             tracePath_.c_str(), runtimeInstanceId_.c_str(), sessionId_.c_str());
        bool hasResourceMode = false;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            hasResourceMode = !resourceMode_.empty();
        }
        if (hasResourceMode)
            source_profiler_enable(true);
        if (hasResourceMode)
            source_profiler_gpu_enable(true);
        if (hasResourceMode)
            startResourceSampler();
    }

    void shutdown()
    {
        stopResourceSampler();
        stopTraceWriter();
        std::lock_guard<std::mutex> lock(stateMutex_);
        enabled_ = false;
        degraded_ = false;
        pending_ = {};
        reserved_ = {};
        accepted_ = {};
        committed_ = {};
        lastRawTake_.clear();
        lastPacketTake_.clear();
        encoderFamily_.clear();
        videoEncoder_ = nullptr;
        streamOutput_ = nullptr;
        programReturnOutput_ = nullptr;
        previewReturnOutput_ = nullptr;
        programVideo_ = nullptr;
        previewVideo_ = nullptr;
        programRoot_ = nullptr;
        previewRoot_ = nullptr;
    }

    bool enabled()
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        return enabled_;
    }

    void updateMixRoots(obs_source_t *programRoot, obs_source_t *previewRoot)
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        programRoot_ = programRoot;
        previewRoot_ = previewRoot;
    }

    // This is the runtime identity accepted by the trace initializer.  The
    // rollback marker uses the same value so an operational observation
    // cannot be attributed to a caller-controlled or stale process ID.
    std::string runtimeInstanceId()
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        return runtimeInstanceId_.empty() ? std::string("pulsar-runtime") : runtimeInstanceId_;
    }

    // A post-swap telemetry integrity fault is fail-stop: the physical role
    // map is already changed, so accepting another Take would make the trace
    // and the live route diverge further.  The caller uses this read-only
    // guard while holding dualLaneMutex before it queues any new swap.
    bool integrityFaulted()
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        return degraded_;
    }

    bool environmentTruthy(const char *value) const { return truthy(value); }

    static bool resourceReferenceRequested()
    {
        const char *tracePath = std::getenv("PULSAR_TRACE_PATH");
        const char *mode = std::getenv("PULSAR_TRACE_RESOURCE_MODE");
        return tracePath && *tracePath && mode && std::strcmp(mode, "reference") == 0;
    }

    void previewRevisionChanged()
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        if (enabled_)
            ++previewRevision_;
    }

    void cancelPending()
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        pending_.valid = false;
    }

    void rejectReserved(const char *reason)
    {
        TakeContext context;
        uint64_t lastCommittedFrameId = 0;
        uint64_t lastCommittedPtsNs = 0;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            if (!reserved_.valid)
                return;
            context = reserved_;
            if (committed_.valid) {
                lastCommittedFrameId = committed_.frameId;
                lastCommittedPtsNs = committed_.ptsNs;
            }
            reserved_.valid = false;
            accepted_.valid = false;
        }

        // A queue rejection is terminal without a preceding TakeAccepted: the
        // physical frame-boundary operation never admitted the candidate.  The
        // event is emitted after releasing stateMutex_ and, in particular,
        // never while the frontend's dualLaneMutex is held.  Retain the last
        // committed frame/PTS so the rejection cannot be mistaken for a
        // producer reset.
        const uint64_t seq = nextServerSeq();
        const std::string revisions = revisionJson(context.programRevision, context.previewRevision,
                                                    context.roleMapRevision);
        const std::string roleMap = roleMapJson(context.onAirLane, context.previewLane);
        std::ostringstream event;
        event << "{\"record_type\":\"event\",\"event\":{";
        event << commonEventFields("TakeAborted", context, seq, "ready", nowNs(), revisions, roleMap);
        event << ",\"take_command_id\":\"" << escape(context.takeCommandId)
              << "\",\"reason\":\"queue_rejected\""
              << ",\"last_committed_frame_id\":" << lastCommittedFrameId
              << ",\"last_committed_pts_ns\":" << lastCommittedPtsNs << "}}";
        writeLine(event.str());
        blog(LOG_ERROR, "[pulsar-runtime-telemetry] Take reservation rejected reason=%s take_command_id=%s",
             reason ? reason : "unspecified", context.takeCommandId.c_str());
    }

    // Called while the lane mutex is held, before the libobs atomic swap is
    // queued.  This only reserves state and consumes the ingress envelope; it
    // deliberately emits no event and performs no trace-file operation.  The
    // reservation is promoted to accepted only after the queue primitive
    // returns success.
    bool reserve(obs_source_t *scene, int onAirLane, int previewLane, uint64_t *admissionFloorNs)
    {
        if (admissionFloorNs)
            *admissionFloorNs = 0;
        TakeContext context;
        const char *rejectReason = nullptr;
        std::string diagnosticCommandId;
        uint64_t diagnosticFreezeUntilNs = 0;
        uint64_t diagnosticNowNs = 0;
        bool diagnosticHasDeadline = false;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            if (!enabled_) {
                rejectReason = "producer_disabled";
            } else if (degraded_) {
                rejectReason = "telemetry_degraded";
            } else if (!pending_.valid) {
                rejectReason = "no_pending_ingress";
            } else if (reserved_.valid || accepted_.valid) {
                rejectReason = "take_already_reserved";
            } else {
                // Consume the ingress envelope on every queue outcome.  A
                // failed or non-Take request must never leave metadata
                // available for a later scene mutation.
                TakeContext candidate = pending_;
                pending_.valid = false;
                diagnosticCommandId = candidate.commandId;
                diagnosticFreezeUntilNs = candidate.freezeUntilNs;
                diagnosticNowNs = nowNs();
                diagnosticHasDeadline = true;
                if (candidate.targetLaneId != laneId(previewLane)) {
                    rejectReason = "target_lane_mismatch";
                } else {
                    const char *name = scene ? obs_source_get_name(scene) : nullptr;
                    const char *uuid = scene ? obs_source_get_uuid(scene) : nullptr;
                    if ((!name || candidate.targetSceneId != name) &&
                        (!uuid || candidate.targetSceneId != uuid)) {
                        rejectReason = "target_scene_mismatch";
                    } else {
                        if (candidate.freezeUntilNs <= diagnosticNowNs) {
                            rejectReason = "freeze_deadline_expired";
                        } else {
                            context = candidate;
                            context.acceptedAtNs = diagnosticNowNs;
                            if (admissionFloorNs)
                                *admissionFloorNs = context.acceptedAtNs;
                            context.programRevision = programRevision_;
                            context.previewRevision = previewRevision_;
                            context.roleMapRevision = roleMapRevision_;
                            context.onAirLane = onAirLane;
                            context.previewLane = previewLane;
                            reserved_ = context;
                            reserved_.valid = true;
                            // committed_ still names the previous Take until
                            // the frame-boundary callback runs.  Keep its raw
                            // and packet latches armed; clearing them here (or
                            // in markAccepted/rejection) would attribute
                            // pre-commit frames for this reservation to the
                            // previous committed transaction a second time.
                        }
                    }
                }
            }
        }

        if (rejectReason) {
            if (diagnosticHasDeadline) {
                const std::string delta = deadlineDelta(diagnosticFreezeUntilNs, diagnosticNowNs);
                blog(LOG_ERROR,
                     "[pulsar-runtime-telemetry] accept rejected reason=%s command_id=%s "
                     "freeze_until_monotonic_ns=%llu reserve_now_monotonic_ns=%llu deadline_delta_ns=%s",
                     rejectReason, diagnosticCommandId.c_str(),
                     static_cast<unsigned long long>(diagnosticFreezeUntilNs),
                     static_cast<unsigned long long>(diagnosticNowNs), delta.c_str());
            } else {
                blog(LOG_ERROR, "[pulsar-runtime-telemetry] accept rejected reason=%s", rejectReason);
            }
            return false;
        }

        if (diagnosticHasDeadline) {
            const std::string delta = deadlineDelta(diagnosticFreezeUntilNs, diagnosticNowNs);
            blog(LOG_INFO,
                 "[pulsar-runtime-telemetry] accept admitted command_id=%s "
                 "freeze_until_monotonic_ns=%llu reserve_now_monotonic_ns=%llu deadline_delta_ns=%s",
                 diagnosticCommandId.c_str(), static_cast<unsigned long long>(diagnosticFreezeUntilNs),
                 static_cast<unsigned long long>(diagnosticNowNs), delta.c_str());
        }

        return context.valid;
    }

    // Promote a successfully queued reservation while dualLaneMutex is still
    // held.  The state transition and FIFO enqueue are deliberately free of
    // filesystem I/O.  Because the acceptance line enters the FIFO before
    // the queue callback can publish TakeCommitted, the worker preserves the
    // Accepted-before-Commit order without charging disk latency to the
    // TakeAccepted timestamp.
    bool markAccepted()
    {
        TakeContext context;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            if (!enabled_ || !reserved_.valid || accepted_.valid)
                return false;
            // reserve() captured the logical admission instant before the
            // frame-boundary queue operation.  Preserve that causal timestamp
            // even when the callback reaches markAccepted later; replacing it
            // here would move TakeAccepted after the actual admission.
            context = reserved_;
            accepted_ = context;
            accepted_.valid = true;
            reserved_ = {};
        }

        const uint64_t seq = nextServerSeq();
        const std::string revisions = revisionJson(context.programRevision, context.previewRevision,
                                                    context.roleMapRevision);
        const std::string roleMap = roleMapJson(context.onAirLane, context.previewLane);
        std::ostringstream event;
        event << "{\"record_type\":\"event\",\"event\":{";
        event << commonEventFields("TakeAccepted", context, seq, "take_accepted", context.acceptedAtNs,
                                   revisions, roleMap);
        event << ",\"take_command_id\":\"" << escape(context.takeCommandId)
              << "\",\"target_lane_id\":\"" << escape(context.targetLaneId)
              << "\",\"target_scene_id\":\"" << escape(context.targetSceneId)
              << "\",\"freeze_until_monotonic_ns\":" << context.freezeUntilNs;
        event << "}}";
        if (enqueueLine(event.str()))
            return true;

        // The FIFO can only reject an enqueue during teardown or failed
        // initialization.  Retire the accepted state so a missing Accepted
        // line can never be followed by a correlated Commit observation.
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            if (accepted_.valid && accepted_.takeCommandId == context.takeCommandId)
                accepted_.valid = false;
        }
        blog(LOG_ERROR, "[pulsar-runtime-telemetry] TakeAccepted FIFO enqueue failed take_command_id=%s",
             context.takeCommandId.c_str());
        return false;
    }

    void commit(uint64_t frameId, uint64_t ptsNs, int onAirLane, int previewLane)
    {
        TakeContext context;
        uint64_t previousProgram = 0;
        uint64_t previousPreview = 0;
        uint64_t previousRoleMap = 0;
        uint64_t previousCommittedFrame = 0;
        uint64_t previousCommittedPts = 0;
        int previousOnAirLane = 0;
        int previousPreviewLane = 1;
        bool frameOrPtsRegression = false;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            if (!enabled_ || !accepted_.valid) {
                // The accepted context is terminal after either a successful
                // Commit or a post-swap integrity fault.  A duplicate
                // frame-boundary callback therefore has a defined no-op
                // outcome and cannot emit a second terminal event.
                blog(LOG_DEBUG,
                     "[pulsar-runtime-telemetry] commit ignored: no accepted telemetry take");
                return;
            }
            previousProgram = accepted_.programRevision;
            previousPreview = accepted_.previewRevision;
            previousRoleMap = accepted_.roleMapRevision;
            previousOnAirLane = accepted_.onAirLane;
            previousPreviewLane = accepted_.previewLane;
            if (committed_.valid &&
                (frameId < committed_.frameId || ptsNs < committed_.ptsNs)) {
                // Compare against the last committed physical frame, not the
                // zero-initialised fields of a newly accepted context.  Clear
                // the accepted context while holding stateMutex_ so a racing
                // duplicate callback cannot create another terminal event.
                context = accepted_;
                context.frameId = frameId;
                context.ptsNs = ptsNs;
                previousCommittedFrame = committed_.frameId;
                previousCommittedPts = committed_.ptsNs;
                // The callback is invoked after obs_view_queue_atomic_swap has
                // applied the physical role change.  Reconcile telemetry to
                // that actual post-swap map and latch fail-stop.  The exact
                // regressed values are retained in TakeCommitted so the
                // evidence cannot imply a fabricated monotone boundary.
                ++programRevision_;
                ++roleMapRevision_;
                context.programRevision = programRevision_;
                context.previewRevision = previewRevision_;
                context.roleMapRevision = roleMapRevision_;
                context.onAirLane = onAirLane;
                context.previewLane = previewLane;
                committed_ = context;
                committed_.valid = true;
                accepted_.valid = false;
                pending_.valid = false;
                lastRawTake_.clear();
                lastPacketTake_.clear();
                degraded_ = true;
                frameOrPtsRegression = true;
            } else {
                ++programRevision_;
                ++roleMapRevision_;
                context = accepted_;
                context.frameId = frameId;
                context.ptsNs = ptsNs;
                context.onAirLane = onAirLane;
                context.previewLane = previewLane;
                context.programRevision = programRevision_;
                context.previewRevision = previewRevision_;
                context.roleMapRevision = roleMapRevision_;
                committed_ = context;
                committed_.valid = true;
                accepted_.valid = false;
                pending_.valid = false;
                lastRawTake_.clear();
                lastPacketTake_.clear();
            }
        }

        const uint64_t seq = nextServerSeq();
        const std::string revisions = revisionJson(context.programRevision, context.previewRevision,
                                                    context.roleMapRevision);
        const std::string previousRevisions = revisionJson(previousProgram, previousPreview, previousRoleMap);
        const std::string roleMap = roleMapJson(context.onAirLane, context.previewLane);
        const std::string previousRoleMapJson = roleMapJson(previousOnAirLane, previousPreviewLane);
        std::ostringstream event;
        event << "{\"record_type\":\"event\",\"event\":{";
        event << commonEventFields("TakeCommitted", context, seq, "ready", nowNs(), revisions, roleMap,
                                   previousRevisions, previousRoleMapJson);
        event << ",\"take_command_id\":\"" << escape(context.takeCommandId)
              << "\",\"target_lane_id\":\"" << escape(context.targetLaneId)
              << "\",\"target_scene_id\":\"" << escape(context.targetSceneId)
              << "\",\"source_lane_id\":\"" << laneId(context.onAirLane)
              << "\",\"frame_id\":" << frameId << ",\"pts_ns\":" << ptsNs
              << ",\"program_lane_id\":\"" << laneId(context.onAirLane)
              << "\",\"preview_lane_id\":\"" << laneId(context.previewLane) << "\"";
        event << "}}";
        writeLine(event.str());

        if (frameOrPtsRegression) {
            // This is intentionally not a scene-switch v1 event.  The role
            // swap already happened, so a TakeAborted event would falsely
            // describe the physical route.  The parser rejects this record
            // type and therefore cannot accept the campaign as valid.
            std::ostringstream fault;
            fault << "{\"record_type\":\"integrity_fault\","
                  << "\"fault_type\":\"frame_or_pts_regression\","
                  << "\"runtime_instance_id\":\"" << escape(context.runtimeInstanceId)
                  << "\",\"command_id\":\"" << escape(context.commandId)
                  << "\",\"intent_id\":\"" << escape(context.intentId)
                  << "\",\"take_command_id\":\"" << escape(context.takeCommandId)
                  << "\",\"observed_frame_id\":" << frameId
                  << ",\"observed_pts_ns\":" << ptsNs
                  << ",\"last_committed_frame_id\":" << previousCommittedFrame
                  << ",\"last_committed_pts_ns\":" << previousCommittedPts
                  << ",\"physical_swap_committed\":true"
                  << ",\"fail_stop\":true"
                  << ",\"observed_at_monotonic_ns\":" << nowNs()
                  << ",\"revisions\":" << revisions
                  << ",\"role_map\":" << roleMap
                  << ",\"message\":\"telemetry frame/PTS regressed after physical swap;"
                  << " exact values reconciled and future Takes fail-stopped\"}";
            writeLine(fault.str());
            blog(LOG_ERROR,
                 "[pulsar-runtime-telemetry] integrity fault: frame/PTS regression after physical swap; "
                 "take_command_id=%s candidate_frame=%llu candidate_pts=%llu "
                 "last_committed_frame=%llu last_committed_pts=%llu; no rollback; fail-stop enabled",
                 context.takeCommandId.c_str(), static_cast<unsigned long long>(frameId),
                 static_cast<unsigned long long>(ptsNs),
                 static_cast<unsigned long long>(previousCommittedFrame),
                 static_cast<unsigned long long>(previousCommittedPts));
        }
    }

    void rawFrame(struct video_data *frame)
    {
        if (!frame)
            return;
        rawFrameCount_.fetch_add(1, std::memory_order_relaxed);
        TakeContext context;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            if (!enabled_ || !committed_.valid || frame->timestamp < committed_.ptsNs ||
                lastRawTake_ == committed_.takeCommandId)
                return;
            context = committed_;
            lastRawTake_ = committed_.takeCommandId;
        }

        std::ostringstream observation;
        observation << "{\"record_type\":\"observation\",\"boundary\":\"encoder_input_raw\","
                    << "\"clock_domain\":\"monotonic_ns\",\"runtime_instance_id\":\""
                    << escape(context.runtimeInstanceId) << "\",\"command_id\":\""
                    << escape(context.commandId) << "\",\"intent_id\":\"" << escape(context.intentId)
                    << "\",\"take_command_id\":\"" << escape(context.takeCommandId)
                    << "\",\"revisions\":"
                    << revisionJson(context.programRevision, context.previewRevision, context.roleMapRevision)
                    << ",\"frame_id\":" << context.frameId << ",\"pts_ns\":" << frame->timestamp
                    << ",\"observed_at_monotonic_ns\":" << nowNs()
                    << ",\"valid\":true,\"program_frame\":true,\"surface\":\"ProgramView\","
                    << "\"consumer\":\"encoder_input\"}";
        writeLine(observation.str());
    }

    void packet(obs_output_t *, struct encoder_packet *packet, struct encoder_packet_time *packetTime)
    {
        if (!packet || packet->type != OBS_ENCODER_VIDEO)
            return;
        if (packetTime && packetTime->fer > 0 && packetTime->ferc >= packetTime->fer) {
            encodeTimeNsTotal_.fetch_add(packetTime->ferc - packetTime->fer,
                                        std::memory_order_relaxed);
            encodeTimeSampleCount_.fetch_add(1, std::memory_order_relaxed);
        }
        // Monotone video-packet sequence on the native streamOutput callback.
        // The external RTMP receiver has its own sequence and is correlated
        // by rational PTS/timebase, never by log order alone.
        const uint64_t packetIndex = packetFrameCount_.fetch_add(1, std::memory_order_relaxed);
        TakeContext context;
        const uint64_t callbackAt = nowNs();
        uint64_t observed = callbackAt;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            if (!enabled_ || !committed_.valid || lastPacketTake_ == committed_.takeCommandId)
                return;
            if (packetTime) {
                if (packetTime->cts < committed_.ptsNs)
                    return;
                if (packetTime->pir)
                    observed = packetTime->pir;
            }
            context = committed_;
            lastPacketTake_ = committed_.takeCommandId;
        }

        std::ostringstream observation;
        observation << "{\"record_type\":\"observation\",\"boundary\":\"encoded_first_packet\","
                    << "\"clock_domain\":\"monotonic_ns\",\"runtime_instance_id\":\""
                    << escape(context.runtimeInstanceId) << "\",\"command_id\":\""
                    << escape(context.commandId) << "\",\"intent_id\":\"" << escape(context.intentId)
                    << "\",\"take_command_id\":\"" << escape(context.takeCommandId)
                    << "\",\"revisions\":"
                    << revisionJson(context.programRevision, context.previewRevision, context.roleMapRevision)
                    << ",\"frame_id\":" << context.frameId << ",\"pts_ns\":" << context.ptsNs
                    << ",\"observed_at_monotonic_ns\":" << observed
                    << ",\"valid\":true,\"packet_index\":" << packetIndex
                    << ",\"packet_pts\":" << packet->pts
                    << ",\"packet_dts\":" << packet->dts
                    << ",\"packet_timebase_num\":" << packet->timebase_num
                    << ",\"packet_timebase_den\":" << packet->timebase_den;
        if (packetTime) {
            observation << ",\"packet_cts_monotonic_ns\":" << packetTime->cts
                        << ",\"packet_fer_monotonic_ns\":" << packetTime->fer
                        << ",\"packet_ferc_monotonic_ns\":" << packetTime->ferc
                        << ",\"packet_pir_monotonic_ns\":" << packetTime->pir
                        << ",\"packet_callback_monotonic_ns\":" << callbackAt;
        }
        observation
                    << ",\"surface\":\"EncoderOutput\",\"consumer\":\"encoder_callback\"}";
        writeLine(observation.str());
    }

    void snapshot(calldata_t *cd)
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        const bool countersFit = fitsCalldataInt(serverSeq_) && fitsCalldataInt(committed_.frameId) &&
                                 fitsCalldataInt(committed_.ptsNs) && fitsCalldataInt(committed_.programRevision) &&
                                 fitsCalldataInt(committed_.previewRevision) && fitsCalldataInt(committed_.roleMapRevision);
        const bool valid = enabled_ && committed_.valid && countersFit;
        calldata_set_bool(cd, "valid", valid);
        if (!valid) {
            if (enabled_ && committed_.valid && !countersFit)
                blog(LOG_ERROR, "[pulsar-runtime-telemetry] snapshot refused uint64 counter outside signed calldata range");
            return;
        }
        calldata_set_int(cd, "server_seq", static_cast<long long>(serverSeq_));
        calldata_set_int(cd, "frame_id", static_cast<long long>(committed_.frameId));
        calldata_set_int(cd, "pts_ns", static_cast<long long>(committed_.ptsNs));
        calldata_set_int(cd, "program_revision", static_cast<long long>(committed_.programRevision));
        calldata_set_int(cd, "preview_revision", static_cast<long long>(committed_.previewRevision));
        calldata_set_int(cd, "role_map_revision", static_cast<long long>(committed_.roleMapRevision));
        calldata_set_string(cd, "runtime_instance_id", committed_.runtimeInstanceId.c_str());
        calldata_set_string(cd, "command_id", committed_.commandId.c_str());
        calldata_set_string(cd, "intent_id", committed_.intentId.c_str());
        calldata_set_string(cd, "take_command_id", committed_.takeCommandId.c_str());
    }

private:
    static uint64_t nowNs() { return os_gettime_ns(); }

    static std::string deadlineDelta(uint64_t deadline, uint64_t now)
    {
        if (deadline >= now)
            return std::string("+") + std::to_string(deadline - now);
        return std::string("-") + std::to_string(now - deadline);
    }

    struct GpuMetrics {
        double utilization = 0.0;
        double encoderUtilization = 0.0;
        uint64_t memoryBytes = 0;
    };

    static bool queryGpuMetrics(GpuMetrics &metrics)
    {
        // nvidia-smi is the driver's supported process-independent telemetry
        // boundary for this runtime.  Keep the command constant (there is no
        // user-controlled shell fragment) and fail closed when the driver or
        // one of the requested counters is unavailable.  A resource record
        // with invented zeroes would make a missing GPU look healthy and would
        // incorrectly satisfy the strict #246 resource gate.
        const char *command =
            "nvidia-smi --query-gpu=utilization.gpu,utilization.encoder,memory.used "
            "--format=csv,noheader,nounits";
#ifdef _WIN32
        FILE *pipe = _popen(command, "r");
#else
        FILE *pipe = popen(command, "r");
#endif
        if (!pipe)
            return false;

        char line[256] = {};
        const bool read = std::fgets(line, sizeof(line), pipe) != nullptr;
#ifdef _WIN32
        _pclose(pipe);
#else
        pclose(pipe);
#endif
        if (!read)
            return false;

        char *cursor = line;
        char *end = nullptr;
        const double gpu = std::strtod(cursor, &end);
        if (end == cursor)
            return false;
        cursor = end;
        while (*cursor == ' ' || *cursor == '\t' || *cursor == ',')
            ++cursor;
        const double encoder = std::strtod(cursor, &end);
        if (end == cursor)
            return false;
        cursor = end;
        while (*cursor == ' ' || *cursor == '\t' || *cursor == ',')
            ++cursor;
        const double memoryMb = std::strtod(cursor, &end);
        if (end == cursor || !std::isfinite(gpu) || !std::isfinite(encoder) || !std::isfinite(memoryMb) ||
            gpu < 0.0 || encoder < 0.0 || memoryMb < 0.0)
            return false;

        metrics.utilization = gpu;
        metrics.encoderUtilization = encoder;
        metrics.memoryBytes = static_cast<uint64_t>(memoryMb * 1024.0 * 1024.0);
        return true;
    }

    static uint32_t resourceIntervalMs()
    {
        uint32_t interval = 500;
        if (const char *value = std::getenv("PULSAR_TRACE_RESOURCE_INTERVAL_MS")) {
            char *end = nullptr;
            const unsigned long parsed = std::strtoul(value, &end, 10);
            if (end && *end == '\0' && parsed >= 100 && parsed <= 10000)
                interval = static_cast<uint32_t>(parsed);
        }
        return interval;
    }

    static double intervalAverageMs(uint64_t currentTotal, uint64_t previousTotal,
                                    uint64_t currentSamples, uint64_t previousSamples)
    {
        if (currentTotal < previousTotal || currentSamples <= previousSamples)
            return 0.0;
        return static_cast<double>(currentTotal - previousTotal) /
               static_cast<double>(currentSamples - previousSamples) / 1000000.0;
    }

    static double renderResidualMs(const obs_video_mix_pipeline_stats &current,
                                   const obs_video_mix_pipeline_stats &previous)
    {
        const double render = intervalAverageMs(current.render_submit_ns, previous.render_submit_ns,
                                                current.sample_count, previous.sample_count);
        const double attributed =
            intervalAverageMs(current.render_setup_ns, previous.render_setup_ns, current.sample_count,
                              previous.sample_count) +
            intervalAverageMs(current.render_main_ns, previous.render_main_ns, current.sample_count,
                              previous.sample_count) +
            intervalAverageMs(current.render_scale_ns, previous.render_scale_ns, current.sample_count,
                              previous.sample_count) +
            intervalAverageMs(current.render_convert_ns, previous.render_convert_ns,
                              current.sample_count, previous.sample_count) +
            intervalAverageMs(current.gpu_flush_ns, previous.gpu_flush_ns, current.sample_count,
                              previous.sample_count) +
            intervalAverageMs(current.gpu_encode_submit_ns, previous.gpu_encode_submit_ns,
                              current.sample_count, previous.sample_count) +
            intervalAverageMs(current.raw_stage_ns, previous.raw_stage_ns, current.sample_count,
                              previous.sample_count) +
            intervalAverageMs(current.render_teardown_ns, previous.render_teardown_ns,
                              current.sample_count, previous.sample_count);
        return render - attributed;
    }

    static double frameResidualMs(const obs_video_mix_pipeline_stats &current,
                                  const obs_video_mix_pipeline_stats &previous)
    {
        const double frame = intervalAverageMs(current.frame_total_ns, previous.frame_total_ns,
                                               current.sample_count, previous.sample_count);
        const double attributed =
            intervalAverageMs(current.render_submit_ns, previous.render_submit_ns,
                              current.sample_count, previous.sample_count) +
            intervalAverageMs(current.download_ns, previous.download_ns, current.sample_count,
                              previous.sample_count) +
            intervalAverageMs(current.flush_ns, previous.flush_ns, current.sample_count,
                              previous.sample_count) +
            intervalAverageMs(current.borrowed_schedule_ns, previous.borrowed_schedule_ns,
                              current.sample_count, previous.sample_count) +
            intervalAverageMs(current.output_copy_ns, previous.output_copy_ns, current.sample_count,
                              previous.sample_count);
        return frame - attributed;
    }

    void startResourceSampler()
    {
        resourceStop_.store(false, std::memory_order_release);
        resourceThread_ = std::thread([this] { resourceLoop(); });
    }

    void stopResourceSampler()
    {
        resourceStop_.store(true, std::memory_order_release);
        if (resourceThread_.joinable())
            resourceThread_.join();
        if (resourceCpuInfo_) {
            os_cpu_usage_info_destroy(resourceCpuInfo_);
            resourceCpuInfo_ = nullptr;
        }
    }

    void resourceLoop()
    {
        const uint32_t interval = resourceIntervalMs();
        obs_graphics_pipeline_stats previousGraphics = {};
        obs_video_mix_pipeline_stats previousProgram = {};
        obs_video_mix_pipeline_stats previousPreview = {};
        obs_raw_output_pipeline_stats previousProgramReturn = {};
        obs_raw_output_pipeline_stats previousPreviewReturn = {};
        bool havePipelineBaseline = false;
        resourceCpuInfo_ = os_cpu_usage_info_start();
        if (!resourceCpuInfo_)
            blog(LOG_WARNING, "[pulsar-runtime-telemetry] process CPU sampler unavailable");

        bool gpuWarningLogged = false;
        while (!resourceStop_.load(std::memory_order_acquire)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(interval));
            if (resourceStop_.load(std::memory_order_acquire))
                break;

            GpuMetrics gpu;
            if (!queryGpuMetrics(gpu)) {
                if (!gpuWarningLogged) {
                    blog(LOG_WARNING,
                         "[pulsar-runtime-telemetry] resource sample skipped: nvidia-smi GPU counters unavailable");
                    gpuWarningLogged = true;
                }
                continue;
            }

            const double cpu = resourceCpuInfo_ ? os_cpu_usage_info_query(resourceCpuInfo_) : -1.0;
            if (cpu < 0.0 || !std::isfinite(cpu))
                continue;
            const double frameRenderMs = static_cast<double>(obs_get_average_frame_time_ns()) / 1000000.0;
            if (!std::isfinite(frameRenderMs) || frameRenderMs < 0.0)
                continue;

            const uint64_t rawFrames = rawFrameCount_.load(std::memory_order_relaxed);
            const uint64_t encodedFrames = packetFrameCount_.load(std::memory_order_relaxed);
            const uint64_t queueDepth = rawFrames > encodedFrames ? rawFrames - encodedFrames : 0;
            std::string mode;
            std::string runtime;
            std::string buildRevision;
            std::string host;
            std::string gpuName;
            std::string producerTopology;
            std::string encoderFamily;
            uint64_t producerCount = 0;
            obs_encoder_t *videoEncoder = nullptr;
            obs_output_t *streamOutput = nullptr;
            obs_output_t *programReturnOutput = nullptr;
            obs_output_t *previewReturnOutput = nullptr;
            video_t *programVideo = nullptr;
            video_t *previewVideo = nullptr;
            obs_source_t *programRoot = nullptr;
            obs_source_t *previewRoot = nullptr;
            {
                std::lock_guard<std::mutex> lock(stateMutex_);
                if (!enabled_ || resourceMode_.empty())
                    continue;
                mode = resourceMode_;
                runtime = runtimeInstanceId_;
                buildRevision = buildRevision_;
                host = traceHost_;
                gpuName = traceGpu_;
                producerTopology = producerTopology_;
                encoderFamily = encoderFamily_;
                producerCount = producerCount_;
                videoEncoder = videoEncoder_;
                streamOutput = streamOutput_;
                programReturnOutput = programReturnOutput_;
                previewReturnOutput = previewReturnOutput_;
                programVideo = programVideo_;
                previewVideo = previewVideo_;
                programRoot = programRoot_;
                previewRoot = previewRoot_;
            }

            obs_graphics_pipeline_stats graphics = {};
            obs_video_mix_pipeline_stats program = {};
            obs_video_mix_pipeline_stats preview = {};
            obs_raw_output_pipeline_stats programReturn = {};
            obs_raw_output_pipeline_stats previewReturn = {};
            if (!obs_get_graphics_pipeline_stats(&graphics) ||
                !obs_video_get_mix_pipeline_stats(programVideo, &program) ||
                (producerCount == 2 && !obs_video_get_mix_pipeline_stats(previewVideo, &preview)) ||
                (programReturnOutput &&
                 !obs_output_get_raw_pipeline_stats(programReturnOutput, &programReturn)) ||
                (producerCount == 2 && previewReturnOutput &&
                 !obs_output_get_raw_pipeline_stats(previewReturnOutput, &previewReturn)))
                continue;
            if (!havePipelineBaseline) {
                previousGraphics = graphics;
                previousProgram = program;
                previousPreview = preview;
                previousProgramReturn = programReturn;
                previousPreviewReturn = previewReturn;
                havePipelineBaseline = true;
                continue;
            }

            profiler_result_t programProfile = {};
            profiler_result_t previewProfile = {};
            const bool programProfileValid = programRoot &&
                                             source_profiler_fill_result(programRoot, &programProfile);
            const bool previewProfileValid = producerCount == 2 && previewRoot &&
                                             source_profiler_fill_result(previewRoot, &previewProfile);
            const bool encoderActive = videoEncoder && obs_encoder_active(videoEncoder);
            const bool rtmpLoadActive = streamOutput && obs_output_active(streamOutput);
            const int outputDropped = rtmpLoadActive ? obs_output_get_frames_dropped(streamOutput) : 0;
            const uint64_t droppedFrames =
                outputDropped > 0 ? static_cast<uint64_t>(outputDropped) : 0;
            const uint64_t missedFrames = static_cast<uint64_t>(obs_get_lagged_frames());
            const uint64_t encodeSamples =
                encodeTimeSampleCount_.load(std::memory_order_relaxed);
            const uint64_t encodeTimeNs = encodeTimeNsTotal_.load(std::memory_order_relaxed);
            const double encodeTimeMs = encodeSamples > 0
                ? static_cast<double>(encodeTimeNs) /
                      static_cast<double>(encodeSamples) / 1000000.0
                : 0.0;

            std::ostringstream sample;
            sample << "{\"record_type\":\"resource_sample\",\"sample_mode\":\"" << mode
                   << "\",\"clock_domain\":\"monotonic_ns\",\"runtime_instance_id\":\""
                   << escape(runtime) << "\",\"observed_at_monotonic_ns\":" << nowNs()
                   << ",\"frame_render_ms\":" << std::setprecision(9) << frameRenderMs
                   << ",\"resident_bytes\":" << os_get_proc_resident_size()
                   << ",\"process_cpu_percent\":" << std::setprecision(6) << cpu
                   << ",\"host_gpu_percent\":" << gpu.utilization
                   << ",\"callback_backlog_estimate\":" << queueDepth
                   << ",\"dropped_frames\":" << droppedFrames
                   << ",\"missed_frames\":" << missedFrames
                   << ",\"encode_time_ms\":" << std::setprecision(9) << encodeTimeMs
                   << ",\"encode_time_samples\":" << encodeSamples
                   << ",\"pipeline\":{"
                   << "\"tick_sources_ms\":" << intervalAverageMs(graphics.tick_sources_ns, previousGraphics.tick_sources_ns, graphics.sample_count, previousGraphics.sample_count)
                   << ",\"output_frames_ms\":" << intervalAverageMs(graphics.output_frames_ns, previousGraphics.output_frames_ns, graphics.sample_count, previousGraphics.sample_count)
                   << ",\"render_displays_ms\":" << intervalAverageMs(graphics.render_displays_ns, previousGraphics.render_displays_ns, graphics.sample_count, previousGraphics.sample_count)
                   << ",\"graphics_tasks_ms\":" << intervalAverageMs(graphics.graphics_tasks_ns, previousGraphics.graphics_tasks_ns, graphics.sample_count, previousGraphics.sample_count)
                   << ",\"frame_total_ms\":" << intervalAverageMs(graphics.frame_total_ns, previousGraphics.frame_total_ns, graphics.sample_count, previousGraphics.sample_count)
                   << "},\"program_mix\":{"
                   << "\"width\":" << videoWidth_ << ",\"height\":" << videoHeight_
                   << ",\"fps_num\":" << videoFpsNum_ << ",\"fps_den\":" << videoFpsDen_
                   << ","
                   << "\"render_submit_ms\":" << intervalAverageMs(program.render_submit_ns, previousProgram.render_submit_ns, program.sample_count, previousProgram.sample_count)
                   << ",\"render_setup_ms\":" << intervalAverageMs(program.render_setup_ns, previousProgram.render_setup_ns, program.sample_count, previousProgram.sample_count)
                   << ",\"render_main_ms\":" << intervalAverageMs(program.render_main_ns, previousProgram.render_main_ns, program.sample_count, previousProgram.sample_count)
                   << ",\"render_scale_ms\":" << intervalAverageMs(program.render_scale_ns, previousProgram.render_scale_ns, program.sample_count, previousProgram.sample_count)
                   << ",\"render_convert_ms\":" << intervalAverageMs(program.render_convert_ns, previousProgram.render_convert_ns, program.sample_count, previousProgram.sample_count)
                   << ",\"gpu_flush_ms\":" << intervalAverageMs(program.gpu_flush_ns, previousProgram.gpu_flush_ns, program.sample_count, previousProgram.sample_count)
                   << ",\"gpu_encode_submit_ms\":" << intervalAverageMs(program.gpu_encode_submit_ns, previousProgram.gpu_encode_submit_ns, program.sample_count, previousProgram.sample_count)
                   << ",\"raw_stage_ms\":" << intervalAverageMs(program.raw_stage_ns, previousProgram.raw_stage_ns, program.sample_count, previousProgram.sample_count)
                   << ",\"render_teardown_ms\":" << intervalAverageMs(program.render_teardown_ns, previousProgram.render_teardown_ns, program.sample_count, previousProgram.sample_count)
                   << ",\"render_unattributed_ms\":" << renderResidualMs(program, previousProgram)
                   << ",\"download_ms\":" << intervalAverageMs(program.download_ns, previousProgram.download_ns, program.sample_count, previousProgram.sample_count)
                   << ",\"flush_ms\":" << intervalAverageMs(program.flush_ns, previousProgram.flush_ns, program.sample_count, previousProgram.sample_count)
                   << ",\"output_copy_ms\":" << intervalAverageMs(program.output_copy_ns, previousProgram.output_copy_ns, program.sample_count, previousProgram.sample_count)
                   << ",\"borrowed_schedule_ms\":" << intervalAverageMs(program.borrowed_schedule_ns, previousProgram.borrowed_schedule_ns, program.sample_count, previousProgram.sample_count)
                   << ",\"borrowed_publish_ms\":" << intervalAverageMs(program.borrowed_publish_ns, previousProgram.borrowed_publish_ns, program.borrowed_publish_sample_count, previousProgram.borrowed_publish_sample_count)
                   << ",\"borrowed_wait_ms\":" << intervalAverageMs(program.borrowed_wait_ns, previousProgram.borrowed_wait_ns, program.sample_count, previousProgram.sample_count)
                   << ",\"return_output_callback_ms\":" << intervalAverageMs(programReturn.callback_ns, previousProgramReturn.callback_ns, programReturn.sample_count, previousProgramReturn.sample_count)
                   << ",\"frame_unattributed_ms\":" << frameResidualMs(program, previousProgram)
                   << ",\"frame_total_ms\":" << intervalAverageMs(program.frame_total_ns, previousProgram.frame_total_ns, program.sample_count, previousProgram.sample_count)
                   << "},\"preview_mix\":{"
                   << "\"active\":" << (producerCount == 2 ? "true" : "false")
                   << ",\"width\":" << videoWidth_ << ",\"height\":" << videoHeight_
                   << ",\"fps_num\":" << videoFpsNum_ << ",\"fps_den\":" << videoFpsDen_
                   << ",\"render_submit_ms\":" << intervalAverageMs(preview.render_submit_ns, previousPreview.render_submit_ns, preview.sample_count, previousPreview.sample_count)
                   << ",\"render_setup_ms\":" << intervalAverageMs(preview.render_setup_ns, previousPreview.render_setup_ns, preview.sample_count, previousPreview.sample_count)
                   << ",\"render_main_ms\":" << intervalAverageMs(preview.render_main_ns, previousPreview.render_main_ns, preview.sample_count, previousPreview.sample_count)
                   << ",\"render_scale_ms\":" << intervalAverageMs(preview.render_scale_ns, previousPreview.render_scale_ns, preview.sample_count, previousPreview.sample_count)
                   << ",\"render_convert_ms\":" << intervalAverageMs(preview.render_convert_ns, previousPreview.render_convert_ns, preview.sample_count, previousPreview.sample_count)
                   << ",\"gpu_flush_ms\":" << intervalAverageMs(preview.gpu_flush_ns, previousPreview.gpu_flush_ns, preview.sample_count, previousPreview.sample_count)
                   << ",\"gpu_encode_submit_ms\":" << intervalAverageMs(preview.gpu_encode_submit_ns, previousPreview.gpu_encode_submit_ns, preview.sample_count, previousPreview.sample_count)
                   << ",\"raw_stage_ms\":" << intervalAverageMs(preview.raw_stage_ns, previousPreview.raw_stage_ns, preview.sample_count, previousPreview.sample_count)
                   << ",\"render_teardown_ms\":" << intervalAverageMs(preview.render_teardown_ns, previousPreview.render_teardown_ns, preview.sample_count, previousPreview.sample_count)
                   << ",\"render_unattributed_ms\":" << renderResidualMs(preview, previousPreview)
                   << ",\"download_ms\":" << intervalAverageMs(preview.download_ns, previousPreview.download_ns, preview.sample_count, previousPreview.sample_count)
                   << ",\"flush_ms\":" << intervalAverageMs(preview.flush_ns, previousPreview.flush_ns, preview.sample_count, previousPreview.sample_count)
                   << ",\"output_copy_ms\":" << intervalAverageMs(preview.output_copy_ns, previousPreview.output_copy_ns, preview.sample_count, previousPreview.sample_count)
                   << ",\"borrowed_schedule_ms\":" << intervalAverageMs(preview.borrowed_schedule_ns, previousPreview.borrowed_schedule_ns, preview.sample_count, previousPreview.sample_count)
                   << ",\"borrowed_publish_ms\":" << intervalAverageMs(preview.borrowed_publish_ns, previousPreview.borrowed_publish_ns, preview.borrowed_publish_sample_count, previousPreview.borrowed_publish_sample_count)
                   << ",\"borrowed_wait_ms\":" << intervalAverageMs(preview.borrowed_wait_ns, previousPreview.borrowed_wait_ns, preview.sample_count, previousPreview.sample_count)
                   << ",\"return_output_callback_ms\":" << intervalAverageMs(previewReturn.callback_ns, previousPreviewReturn.callback_ns, previewReturn.sample_count, previousPreviewReturn.sample_count)
                   << ",\"frame_unattributed_ms\":" << frameResidualMs(preview, previousPreview)
                   << ",\"frame_total_ms\":" << intervalAverageMs(preview.frame_total_ns, previousPreview.frame_total_ns, preview.sample_count, previousPreview.sample_count)
                   << "},\"source_profile\":{"
                   << "\"program_valid\":" << (programProfileValid ? "true" : "false")
                   << ",\"program_tick_cpu_ms\":" << static_cast<double>(programProfile.tick_avg) / 1000000.0
                   << ",\"program_render_cpu_ms\":" << static_cast<double>(programProfile.render_sum) / 1000000.0
                   << ",\"program_render_gpu_ms\":" << static_cast<double>(programProfile.render_gpu_sum) / 1000000.0
                   << ",\"preview_valid\":" << (previewProfileValid ? "true" : "false")
                   << ",\"preview_tick_cpu_ms\":" << static_cast<double>(previewProfile.tick_avg) / 1000000.0
                   << ",\"preview_render_cpu_ms\":" << static_cast<double>(previewProfile.render_sum) / 1000000.0
                   << ",\"preview_render_gpu_ms\":" << static_cast<double>(previewProfile.render_gpu_sum) / 1000000.0
                   << "}"
                   << ",\"encoder_utilization_percent\":" << gpu.encoderUtilization
                   << ",\"encoder_active\":" << (encoderActive ? "true" : "false")
                   << ",\"encoder_family\":\"" << escape(encoderFamily) << "\""
                   << ",\"rtmp_load_active\":" << (rtmpLoadActive ? "true" : "false")
                   << ",\"gpu_memory_bytes\":" << gpu.memoryBytes
                   << ",\"measurement_phase\":\"" << mode
                   << "\",\"build_revision\":\"" << escape(buildRevision)
                   << "\",\"hardware\":{\"host\":\"" << escape(host)
                   << "\",\"gpu\":\"" << escape(gpuName)
                   << "\"},\"producer_topology\":\"" << escape(producerTopology)
                   << "\",\"producer_count\":" << producerCount
                   << ",\"notes\":\"frame time is OBS average; dropped_frames is the active RTMP output counter; missed_frames is OBS render lag; encode_time_ms is cumulative mean FERC-FER and is qualified by encode_time_samples; process CPU is this runtime; host GPU and encoder utilization are nvidia-smi device counters; callback backlog is a producer/packet counter estimate\"}";
            writeLine(sample.str());
            previousGraphics = graphics;
            previousProgram = program;
            previousPreview = preview;
            previousProgramReturn = programReturn;
            previousPreviewReturn = previewReturn;
        }
    }

    static bool truthy(const char *value)
    {
        if (!value)
            return false;
        std::string lower;
        for (const char *p = value; *p; ++p)
            lower.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(*p))));
        return lower == "1" || lower == "true" || lower == "on" || lower == "yes";
    }

    static bool validIdentifier(const char *value)
    {
        if (!value || !*value || std::strlen(value) > 128)
            return false;
        if (!(std::isalnum(static_cast<unsigned char>(*value))))
            return false;
        for (const char *p = value; *p; ++p) {
            const unsigned char c = static_cast<unsigned char>(*p);
            if (!(std::isalnum(c) || c == '.' || c == '_' || c == ':' || c == '-'))
                return false;
        }
        return true;
    }

    static bool validHardwareLabel(const char *value)
    {
        if (!value || !*value || std::strlen(value) > 128)
            return false;
        // Host/GPU labels are diagnostic metadata, not identifiers.  Permit
        // spaces and driver punctuation, but reject controls/newlines and the
        // placeholders that would make a resource comparison unverifiable.
        if (std::strcmp(value, "unknown-host") == 0 || std::strcmp(value, "unknown-gpu") == 0)
            return false;
        for (const unsigned char *p = reinterpret_cast<const unsigned char *>(value); *p; ++p) {
            if (*p < 0x20 || *p == 0x7f)
                return false;
        }
        return true;
    }

    static bool fitsCalldataInt(uint64_t value)
    {
        return value <= static_cast<uint64_t>(INT64_MAX);
    }

    static bool validSceneId(const char *value)
    {
        if (!value || !*value || std::strlen(value) > 256)
            return false;
        for (const unsigned char *p = reinterpret_cast<const unsigned char *>(value); *p; ++p) {
            if (*p < 0x20)
                return false;
        }
        return true;
    }

    static std::string environmentOr(const char *name, const std::string &fallback)
    {
        const char *value = std::getenv(name);
        return value && *value ? value : fallback;
    }

    static bool validBuildRevision(const char *value)
    {
        if (!value || std::strlen(value) != 40)
            return false;
        for (const unsigned char *p = reinterpret_cast<const unsigned char *>(value); *p; ++p) {
            if (!(std::isdigit(*p) || (*p >= 'a' && *p <= 'f')))
                return false;
        }
        return true;
    }

    static std::string escape(const std::string &value)
    {
        std::string out;
        out.reserve(value.size() + 8);
        for (unsigned char c : value) {
            switch (c) {
            case '\\': out += "\\\\"; break;
            case '"': out += "\\\""; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    std::ostringstream hex;
                    hex << "\\u00" << std::hex << static_cast<unsigned int>(c);
                    out += hex.str();
                } else {
                    out.push_back(static_cast<char>(c));
                }
            }
        }
        return out;
    }

    static std::string quoted(const std::string &value)
    {
        return std::string("\"") + escape(value) + "\"";
    }

    static const char *laneId(int lane) { return lane == 0 ? "A" : "B"; }

    static std::string revisionJson(uint64_t program, uint64_t preview, uint64_t roleMap)
    {
        std::ostringstream out;
        out << "{\"program\":" << program << ",\"preview\":" << preview << ",\"role_map\":" << roleMap
            << "}";
        return out.str();
    }

    static std::string roleMapJson(int onAirLane, int previewLane)
    {
        std::ostringstream out;
        out << "{\"on_air\":\"" << laneId(onAirLane) << "\",\"preview\":\"" << laneId(previewLane)
            << "\"}";
        return out.str();
    }

    static std::string commonEventFields(const char *eventType, const TakeContext &context, uint64_t serverSeq,
                                         const char *state, uint64_t observed, const std::string &revisions,
                                         const std::string &roleMap, const std::string &previousRevisions = {},
                                         const std::string &previousRoleMap = {})
    {
        std::ostringstream out;
        out << "\"contract\":\"pulsar.scene-switch.v1\",\"schema_version\":1,\"message_type\":\"event\","
            << "\"event_type\":\"" << eventType << "\",\"command_id\":\"" << escape(context.commandId)
            << "\",\"intent_id\":\"" << escape(context.intentId) << "\",\"runtime_instance_id\":\""
            << escape(context.runtimeInstanceId) << "\",\"server_seq\":" << serverSeq << ",\"state\":\""
            << state << "\",\"previous_revisions\":"
            << (previousRevisions.empty() ? revisions : previousRevisions) << ",\"revisions\":" << revisions
            << ",\"role_map\":" << roleMap;
        if (!previousRoleMap.empty())
            out << ",\"previous_role_map\":" << previousRoleMap;
        out << ",\"observed_at_monotonic_ns\":" << observed << ",\"payload_sha256\":\""
            << escape(context.payloadSha256) << "\"";
        return out.str();
    }

    uint64_t nextServerSeq()
    {
        std::lock_guard<std::mutex> lock(stateMutex_);
        return ++serverSeq_;
    }

    std::string sessionJson(const char *encoderFamily, const obs_video_info &video, bool wgcWorkload,
                            bool cefWorkload, bool wgcSourceBound) const
    {
        const bool nvenc = encoderFamily && std::strcmp(encoderFamily, "nvenc") == 0;
        const std::string commandLine = environmentOr("PULSAR_TRACE_COMMAND", "pulsar-headless runtime producer");
        unsigned long long warmup = 100;
        if (const char *value = std::getenv("PULSAR_TRACE_WARMUP_TAKES")) {
            char *end = nullptr;
            const unsigned long long parsed = std::strtoull(value, &end, 10);
            if (end && *end == '\0' && parsed <= 1000000)
                warmup = parsed;
        }
        std::ostringstream out;
        out << "{\"record_type\":" << quoted("session") << ",\"schema\":"
            << quoted("pulsar.take-latency.v1")
            << ",\"runtime_instance_id\":" << quoted(runtimeInstanceId_) << ",\"session_id\":"
            << quoted(sessionId_) << ",\"codec\":" << quoted(nvenc ? "nvenc" : "x264")
            << ",\"warmup_takes\":" << warmup << ",\"video\":{"
            << "\"width\":" << video.base_width << ",\"height\":" << video.base_height
            << ",\"fps_num\":" << video.fps_num << ",\"fps_den\":" << video.fps_den << "},"
            << "\"workload\":{"
            << "\"wgc\":" << (wgcWorkload ? "true" : "false") << ",\"cef\":" << (cefWorkload ? "true" : "false")
            << ",\"nvenc\":" << (nvenc ? "true" : "false") << "},"
            << "\"capture_paths\":[\"encoder_input_raw\",\"directshow_return\",\"encoded_first_packet\","
            << "\"decoded_first_frame\",\"antenna_first_frame\"],"
            << "\"source_types\":[";
        bool firstSourceType = true;
        if (wgcSourceBound) {
            out << quoted("window_capture");
            firstSourceType = false;
        }
        if (cefWorkload) {
            if (!firstSourceType)
                out << ",";
            out << quoted("browser_source");
        }
        out << "],"
            << "\"resource_reference\":{"
            << "\"extra_frame_render_ms\":0.091,\"extra_resident_bytes\":3130000},"
            << "\"build_revision\":" << quoted(buildRevision_) << ",\"command_line\":"
            << quoted(commandLine) << ",\"hardware\":{"
            << "\"host\":" << quoted(traceHost_) << ",\"gpu\":" << quoted(traceGpu_) << "},"
            << "\"producer_topology\":" << quoted(producerTopology_)
            << ",\"producer_count\":" << producerCount_ << ","
            << "\"evidence_kind\":" << quoted("runtime") << "}";
        return out.str();
    }

    bool startTraceWriter()
    {
        std::lock_guard<std::mutex> lock(writerMutex_);
        if (writerThread_.joinable())
            return false;
        writerStopping_ = false;
        writerAccepting_ = true;
        writerThread_ = std::thread([this] { traceWriterLoop(); });
        return true;
    }

    void stopTraceWriter()
    {
        {
            std::lock_guard<std::mutex> lock(writerMutex_);
            writerAccepting_ = false;
            writerStopping_ = true;
        }
        writerCv_.notify_all();
        if (writerThread_.joinable())
            writerThread_.join();
        {
            std::lock_guard<std::mutex> lock(writerMutex_);
            writerQueue_.clear();
            writerStopping_ = false;
        }
#ifdef _WIN32
        if (traceMutex_) {
            CloseHandle(traceMutex_);
            traceMutex_ = nullptr;
        }
#endif
    }

    void traceWriterLoop()
    {
        for (;;) {
            std::string line;
            {
                std::unique_lock<std::mutex> lock(writerMutex_);
                writerCv_.wait(lock, [this] { return writerStopping_ || !writerQueue_.empty(); });
                if (writerQueue_.empty() && writerStopping_)
                    return;
                line = std::move(writerQueue_.front());
                writerQueue_.pop_front();
            }
            writeLineFile(line);
        }
    }

    bool enqueueLine(const std::string &line)
    {
        if (line.empty())
            return true;
        {
            std::lock_guard<std::mutex> lock(writerMutex_);
            if (!writerAccepting_)
                return false;
            writerQueue_.push_back(line);
        }
        writerCv_.notify_one();
        return true;
    }

    void writeLine(const std::string &line)
    {
        (void)enqueueLine(line);
    }

    void writeLineFile(const std::string &line)
    {
        if (line.empty() || tracePath_.empty())
            return;
        std::lock_guard<std::mutex> lock(fileMutex_);
#ifdef _WIN32
        if (traceMutex_)
            WaitForSingleObject(traceMutex_, INFINITE);
#endif
        std::ofstream output(tracePath_, std::ios::binary | std::ios::app);
        if (output.good())
            output << line << '\n';
        output.close();
#ifdef _WIN32
        if (traceMutex_)
            ReleaseMutex(traceMutex_);
#endif
    }

    static void BeginTake(void *param, calldata_t *cd)
    {
        auto *self = static_cast<PulsarRuntimeTelemetry *>(param);
        if (self)
            self->begin(cd);
    }

    static void SnapshotFrame(void *param, calldata_t *cd)
    {
        auto *self = static_cast<PulsarRuntimeTelemetry *>(param);
        if (self)
            self->snapshot(cd);
    }

    static void CancelTake(void *param, calldata_t *)
    {
        auto *self = static_cast<PulsarRuntimeTelemetry *>(param);
        if (self)
            self->cancelPending();
    }

    void begin(calldata_t *cd)
    {
        const char *command = calldata_string(cd, "command_id");
        const char *intent = calldata_string(cd, "intent_id");
        const char *runtime = calldata_string(cd, "runtime_instance_id");
        const char *take = calldata_string(cd, "take_command_id");
        const char *lane = calldata_string(cd, "target_lane_id");
        const char *scene = calldata_string(cd, "target_scene_id");
        const char *digest = calldata_string(cd, "payload_sha256");
        const long long freeze = calldata_int(cd, "freeze_until_monotonic_ns");
        const uint64_t ingressNowNs = nowNs();
        bool available = false;
        bool accepted = false;
        {
            std::lock_guard<std::mutex> lock(stateMutex_);
            available = enabled_;
            pending_ = {};
            if (available && validIdentifier(command) && validIdentifier(intent) && validIdentifier(runtime) &&
                validIdentifier(take) && validSceneId(scene) && validIdentifier(digest) &&
                (std::strcmp(lane ? lane : "", "A") == 0 || std::strcmp(lane ? lane : "", "B") == 0) &&
                runtime == runtimeInstanceId_ && freeze > 0 && std::strlen(digest) == 64 && isLowerHex(digest)) {
                pending_.valid = true;
                pending_.commandId = command;
                pending_.intentId = intent;
                pending_.runtimeInstanceId = runtime;
                pending_.takeCommandId = take;
                pending_.targetLaneId = lane;
                pending_.targetSceneId = scene;
                pending_.payloadSha256 = digest;
                pending_.freezeUntilNs = static_cast<uint64_t>(freeze);
                accepted = true;
            }
        }
        calldata_set_bool(cd, "available", available);
        calldata_set_bool(cd, "accepted", accepted);
        const std::string delta = freeze > 0
                                      ? deadlineDelta(static_cast<uint64_t>(freeze), ingressNowNs)
                                      : std::string("invalid");
        blog(accepted ? LOG_INFO : LOG_ERROR,
             "[pulsar-runtime-telemetry] begin command_id=%s "
             "freeze_until_monotonic_ns=%lld ingress_now_monotonic_ns=%llu deadline_delta_ns=%s "
             "available=%d accepted=%d",
             command ? command : "", freeze, static_cast<unsigned long long>(ingressNowNs), delta.c_str(),
             available, accepted);
    }

    static bool isLowerHex(const char *value)
    {
        if (!value)
            return false;
        for (size_t i = 0; value[i]; ++i) {
            const unsigned char c = static_cast<unsigned char>(value[i]);
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
                return false;
        }
        return true;
    }

    std::mutex stateMutex_;
    std::mutex fileMutex_;
    bool enabled_ = false;
    bool degraded_ = false;
    std::string tracePath_;
    std::string runtimeInstanceId_ = "pulsar-runtime";
    std::string sessionId_;
    std::string buildRevision_;
    std::string traceHost_;
    std::string traceGpu_;
    std::string producerTopology_;
    uint64_t producerCount_ = 0;
    std::string resourceMode_;
    std::string encoderFamily_;
    // Non-owning: setup owns the encoder; teardown stops and joins the
    // resource sampler before releasing it.
    obs_encoder_t *videoEncoder_ = nullptr;
    // Non-owning; resourceLoop is joined before setup releases streamOutput.
    obs_output_t *streamOutput_ = nullptr;
    // Non-owning stable raw return outputs; sampled before teardown joins the
    // resource thread and stops/releases either output.
    obs_output_t *programReturnOutput_ = nullptr;
    obs_output_t *previewReturnOutput_ = nullptr;
    // Non-owning stable media/root handles. The sampler is joined before
    // frontend teardown releases any of them.
    video_t *programVideo_ = nullptr;
    video_t *previewVideo_ = nullptr;
    obs_source_t *programRoot_ = nullptr;
    obs_source_t *previewRoot_ = nullptr;
    uint32_t videoWidth_ = 0;
    uint32_t videoHeight_ = 0;
    uint32_t videoFpsNum_ = 0;
    uint32_t videoFpsDen_ = 0;
    bool wgcWorkload_ = false;
    bool cefWorkload_ = false;
    uint64_t serverSeq_ = 0;
    uint64_t programRevision_ = 0;
    uint64_t previewRevision_ = 0;
    uint64_t roleMapRevision_ = 0;
    TakeContext pending_;
    TakeContext reserved_;
    TakeContext accepted_;
    TakeContext committed_;
    std::string lastRawTake_;
    std::string lastPacketTake_;
    std::atomic<uint64_t> rawFrameCount_{0};
    std::atomic<uint64_t> packetFrameCount_{0};
    std::atomic<uint64_t> encodeTimeNsTotal_{0};
    std::atomic<uint64_t> encodeTimeSampleCount_{0};
    std::atomic<bool> resourceStop_{true};
    std::thread resourceThread_;
    os_cpu_usage_info_t *resourceCpuInfo_ = nullptr;
    std::mutex writerMutex_;
    std::condition_variable writerCv_;
    std::deque<std::string> writerQueue_;
    std::thread writerThread_;
    bool writerAccepting_ = false;
    bool writerStopping_ = false;
#ifdef _WIN32
    HANDLE traceMutex_ = nullptr;
#endif
};

PulsarRuntimeTelemetry g_runtimeTelemetry;

// The rollback marker is operational evidence, not a scene-switch command.
// Keep its filesystem work away from the graphics callback: the callback only
// transfers an immutable path/payload to this process-lifetime worker and can
// return without directory creation, file open, or JSON serialization I/O.
class PulsarRollbackMarkerWriter {
public:
    ~PulsarRollbackMarkerWriter() { stop(); }

    void start()
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (worker_.joinable())
            return;
        stopping_ = false;
        worker_ = std::thread([this] { run(); });
    }

    void enqueue(std::filesystem::path path, std::string payload)
    {
        start();
        {
            std::lock_guard<std::mutex> lock(mutex_);
            pendingPath_ = std::move(path);
            pendingPayload_ = std::move(payload);
            pending_ = true;
        }
        condition_.notify_one();
    }

private:
    void run()
    {
        for (;;) {
            std::filesystem::path path;
            std::string payload;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                condition_.wait(lock, [this] { return pending_ || stopping_; });
                if (!pending_ && stopping_)
                    return;
                path = std::move(pendingPath_);
                payload = std::move(pendingPayload_);
                pending_ = false;
            }

            std::error_code ec;
            std::filesystem::create_directories(path.parent_path(), ec);
            if (ec)
                continue;
            std::ofstream out(path, std::ios::binary | std::ios::trunc);
            if (out)
                out << payload;
        }
    }

    void stop()
    {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopping_ = true;
        }
        condition_.notify_one();
        if (worker_.joinable())
            worker_.join();
    }

    std::mutex mutex_;
    std::condition_variable condition_;
    std::filesystem::path pendingPath_;
    std::string pendingPayload_;
    bool pending_ = false;
    bool stopping_ = false;
    std::thread worker_;
};

PulsarRollbackMarkerWriter g_rollbackMarkerWriter;
std::atomic<bool> g_frontend_cleanup_succeeded{true};

static void pulsar_runtime_raw_video_callback(void *, struct video_data *frame)
{
    g_runtimeTelemetry.rawFrame(frame);
}

static void pulsar_runtime_packet_callback(obs_output_t *output, struct encoder_packet *packet,
                                           struct encoder_packet_time *packetTime, void *)
{
    g_runtimeTelemetry.packet(output, packet, packetTime);
}

class PulsarFrontendAPI : public obs_frontend_callbacks {
public:
    PulsarFrontendAPI() = default;
    ~PulsarFrontendAPI() override { teardown(); }

    bool setup();
    void emit(obs_frontend_event event);
    bool sceneSwitchPrepare(const std::string &commandId, char laneId, const std::string &sceneId);
    bool sceneSwitchTake(const std::string &takeCommandId);
    bool sceneSwitchAbort(const std::string &takeCommandId);
    void sceneSwitchClearPrepared(const std::string &commandId);
    void dualLaneTransitionTick();
    static void OnSceneSwitchPreviewVideoFrame(void *param, struct video_data *frame);
    static void OnDualLaneTick(void *param, float seconds);
    static void OnDualLaneTransitionStarted(void *param, uint64_t frameId, uint64_t ptsNs);
    static void OnDualLaneTransitionAbortCommitted(void *param, uint64_t frameId, uint64_t ptsNs);

    // ---------- main window / system tray (no GUI) ----------
    void *obs_frontend_get_main_window(void) override { return nullptr; }
    void *obs_frontend_get_main_window_handle(void) override { return nullptr; }
    void *obs_frontend_get_system_tray(void) override { return nullptr; }

    // ---------- scenes ----------
    void obs_frontend_get_scenes(struct obs_frontend_source_list *sources) override;
    obs_source_t *obs_frontend_get_current_scene(void) override;
    void obs_frontend_set_current_scene(obs_source_t *scene) override;

    // ---------- transitions ----------
    void obs_frontend_get_transitions(struct obs_frontend_source_list *sources) override;
    obs_source_t *obs_frontend_get_current_transition(void) override;
    void obs_frontend_set_current_transition(obs_source_t *transition) override;
    int obs_frontend_get_transition_duration(void) override { return transitionDuration; }
    void obs_frontend_set_transition_duration(int duration) override
    {
        transitionDuration = duration;
        // Updating a Stinger recreates its private media source. Do this when
        // Solar configures the transition, never in the on-air Take path, so
        // the decoder can become hot before the transition is admitted.
        if (dualLaneStingerTransition) {
            OBSDataAutoRelease settings = obs_source_get_settings(dualLaneStingerTransition);
            const uint64_t transitionPointMs = stinger_transition_point_ms(duration);
            obs_data_set_int(settings, "transition_point",
                             static_cast<long long>(transitionPointMs));
            obs_data_set_int(settings, "tp_type", 0);
            obs_source_update(dualLaneStingerTransition, settings);
            blog(LOG_INFO,
                 "[pulsar-dual-lane] stinger timing duration_ms=%d transition_point_ms=%llu",
                 duration, static_cast<unsigned long long>(transitionPointMs));
        }
        emit(OBS_FRONTEND_EVENT_TRANSITION_DURATION_CHANGED);
    }
    void obs_frontend_release_tbar(void) override { emit(OBS_FRONTEND_EVENT_TBAR_VALUE_CHANGED); }
    int obs_frontend_get_tbar_position(void) override { return tbarPosition; }
    void obs_frontend_set_tbar_position(int position) override
    {
        tbarPosition = position;
        emit(OBS_FRONTEND_EVENT_TBAR_VALUE_CHANGED);
    }

    // ---------- scene collections ----------
    void obs_frontend_get_scene_collections(std::vector<std::string> &strings) override
    {
        strings.assign({"Default"});
    }
    char *obs_frontend_get_current_scene_collection(void) override { return bstrdup_or_null("Default"); }
    void obs_frontend_set_current_scene_collection(const char *) override {}
    bool obs_frontend_add_scene_collection(const char *) override { return false; }

    // ---------- profiles ----------
    void obs_frontend_get_profiles(std::vector<std::string> &strings) override { strings.assign({"Default"}); }
    char *obs_frontend_get_current_profile(void) override { return bstrdup_or_null("Default"); }
    char *obs_frontend_get_current_profile_path(void) override { return bstrdup_or_null(""); }
    void obs_frontend_set_current_profile(const char *) override {}
    void obs_frontend_create_profile(const char *) override {}
    void obs_frontend_duplicate_profile(const char *) override {}
    void obs_frontend_delete_profile(const char *) override {}

    // ---------- streaming ----------
    void obs_frontend_streaming_start(void) override;
    void obs_frontend_streaming_stop(void) override;
    bool obs_frontend_streaming_active(void) override
    {
        return streamOutput && obs_output_active(streamOutput);
    }

    // ---------- recording ----------
    void obs_frontend_recording_start(void) override;
    void obs_frontend_recording_stop(void) override;
    bool obs_frontend_recording_active(void) override
    {
        return recordOutput && obs_output_active(recordOutput);
    }
    void obs_frontend_recording_pause(bool pause) override;
    bool obs_frontend_recording_paused(void) override { return recordingPaused.load(); }
    bool obs_frontend_recording_split_file(void) override;
    bool obs_frontend_recording_add_chapter(const char *name) override;

    // ---------- replay buffer ----------
    void obs_frontend_replay_buffer_start(void) override;
    void obs_frontend_replay_buffer_save(void) override;
    void obs_frontend_replay_buffer_stop(void) override;
    bool obs_frontend_replay_buffer_active(void) override
    {
        return replayOutput && obs_output_active(replayOutput);
    }

    // ---------- tools menu / docks (no GUI) ----------
    void *obs_frontend_add_tools_menu_qaction(const char *) override { return nullptr; }
    void obs_frontend_add_tools_menu_item(const char *, obs_frontend_cb, void *) override {}
    bool obs_frontend_add_dock_by_id(const char *, const char *, void *) override { return false; }
    void obs_frontend_remove_dock(const char *) override {}
    bool obs_frontend_add_custom_qdock(const char *, void *) override { return false; }

    // ---------- event callbacks ----------
    void obs_frontend_add_event_callback(obs_frontend_event_cb cb, void *p) override
    {
        std::lock_guard<std::mutex> lk(callbacksMutex);
        eventCallbacks.emplace_back(cb, p);
    }
    void obs_frontend_remove_event_callback(obs_frontend_event_cb cb, void *p) override
    {
        std::lock_guard<std::mutex> lk(callbacksMutex);
        for (auto it = eventCallbacks.begin(); it != eventCallbacks.end(); ++it) {
            if (it->cb == cb && it->priv == p) {
                eventCallbacks.erase(it);
                return;
            }
        }
    }

    // ---------- output handles ----------
    obs_output_t *obs_frontend_get_streaming_output(void) override
    {
        return streamOutput ? obs_output_get_ref(streamOutput) : nullptr;
    }
    obs_output_t *obs_frontend_get_recording_output(void) override
    {
        return recordOutput ? obs_output_get_ref(recordOutput) : nullptr;
    }
    obs_output_t *obs_frontend_get_replay_buffer_output(void) override
    {
        return replayOutput ? obs_output_get_ref(replayOutput) : nullptr;
    }

    // ---------- config (always returns NULL; obs-websocket guards on it) ----------
    config_t *obs_frontend_get_profile_config(void) override { return nullptr; }
    config_t *obs_frontend_get_app_config(void) override { return nullptr; }
    config_t *obs_frontend_get_user_config(void) override { return nullptr; }

    // ---------- save callbacks ----------
    void obs_frontend_open_projector(const char *, int, const char *, const char *) override {}
    void obs_frontend_save(void) override {}
    void obs_frontend_defer_save_begin(void) override {}
    void obs_frontend_defer_save_end(void) override {}
    void obs_frontend_add_save_callback(obs_frontend_save_cb cb, void *p) override
    {
        std::lock_guard<std::mutex> lk(callbacksMutex);
        saveCallbacks.emplace_back(cb, p);
    }
    void obs_frontend_remove_save_callback(obs_frontend_save_cb cb, void *p) override
    {
        std::lock_guard<std::mutex> lk(callbacksMutex);
        for (auto it = saveCallbacks.begin(); it != saveCallbacks.end(); ++it) {
            if (it->cb == cb && it->priv == p) {
                saveCallbacks.erase(it);
                return;
            }
        }
    }
    void obs_frontend_add_preload_callback(obs_frontend_save_cb cb, void *p) override
    {
        std::lock_guard<std::mutex> lk(callbacksMutex);
        preloadCallbacks.emplace_back(cb, p);
    }
    void obs_frontend_remove_preload_callback(obs_frontend_save_cb cb, void *p) override
    {
        std::lock_guard<std::mutex> lk(callbacksMutex);
        for (auto it = preloadCallbacks.begin(); it != preloadCallbacks.end(); ++it) {
            if (it->cb == cb && it->priv == p) {
                preloadCallbacks.erase(it);
                return;
            }
        }
    }

    // ---------- translation (no-op) ----------
    void obs_frontend_push_ui_translation(obs_frontend_translate_ui_cb) override {}
    void obs_frontend_pop_ui_translation(void) override {}

    // ---------- streaming service ----------
    obs_service_t *obs_frontend_get_streaming_service(void) override
    {
        return streamService ? obs_service_get_ref(streamService) : nullptr;
    }
    void obs_frontend_set_streaming_service(obs_service_t *service) override
    {
        if (streamService)
            obs_service_release(streamService);
        streamService = service ? obs_service_get_ref(service) : nullptr;
    }
    void obs_frontend_save_streaming_service(void) override {}

    // ---------- studio mode ----------
    bool obs_frontend_preview_program_mode_active(void) override { return studioMode; }
    void obs_frontend_set_preview_program_mode(bool enable) override
    {
        if (studioMode == enable)
            return;
        studioMode = enable;
        emit(enable ? OBS_FRONTEND_EVENT_STUDIO_MODE_ENABLED : OBS_FRONTEND_EVENT_STUDIO_MODE_DISABLED);
    }
    // Deliberately empty, and NOT dead weight we can delete: the base
    // obs_frontend_callbacks declares it pure virtual
    // (upstream frontend/api/obs-frontend-internal.hpp:97), so the override is
    // mandatory to keep PulsarFrontendAPI instantiable.
    //
    // No obs-websocket v5 request routes here. TriggerStudioModeTransition
    // performs the preview->program swap itself, via
    // obs_frontend_set_current_scene() on the current preview scene
    // (plugins/pulsar-websocket/src/requesthandler/RequestHandler_Transitions.cpp:277-287),
    // which this stub implements for real. Studio mode therefore works; only
    // this vtable slot is unused. Do not read the empty body as "the fork
    // cannot transition" -- that misreading already produced a wrong
    // conclusion in ADR 003 (see ADR Prism 026 3.4, issue #118).
    void obs_frontend_preview_program_trigger_transition(void) override {}
    bool obs_frontend_preview_enabled(void) override { return previewEnabled; }
    void obs_frontend_set_preview_enabled(bool enable) override { previewEnabled = enable; }
    obs_source_t *obs_frontend_get_current_preview_scene(void) override
    {
        if (!studioMode)
            return nullptr;
        std::lock_guard<std::mutex> lk(dualLaneMutex);
        obs_source_t *s = dualLaneReady ? previewSelection : (previewScene ? previewScene : currentScene);
        return s ? obs_source_get_ref(s) : nullptr;
    }
    void obs_frontend_set_current_preview_scene(obs_source_t *scene) override
    {
        if (!scene)
            return;
        bool changed = false;
        {
            std::lock_guard<std::mutex> lk(dualLaneMutex);
            if (dualLaneReady && !dualLaneOperational) {
                blog(LOG_WARNING,
                     "[pulsar-dual-lane] Preview mutation rejected: rollback freeze is active");
                return;
            }
            if (dualLaneCutPending.load()) {
                blog(LOG_WARNING, "[pulsar-dual-lane] Preview mutation rejected while Take is pending");
                return;
            }
            if (dualLaneReady && (scene == programSelection || scene == currentScene)) {
                // Sharing the selected source or physical root between ProgramView
                // and PreviewView would invalidate the lane isolation invariant.
                blog(LOG_WARNING, "[pulsar-dual-lane] Preview mutation rejected: scene aliases OnAir");
                return;
            }
            if (dualLaneReady) {
                if (!replaceLaneCompositionLocked(previewLane, scene))
                    return;
                if (previewSelection)
                    obs_source_release(previewSelection);
                previewSelection = obs_source_get_ref(scene);
                dualLaneInvariantLocked("set-preview");
                changed = true;
            } else {
                if (previewScene)
                    obs_source_release(previewScene);
                previewScene = obs_source_get_ref(scene);
                changed = true;
            }
        }
        if (changed)
            g_runtimeTelemetry.previewRevisionChanged();
        emit(OBS_FRONTEND_EVENT_PREVIEW_SCENE_CHANGED);
    }

    // ---------- screenshots ----------
    void obs_frontend_take_screenshot(void) override {}
    void obs_frontend_take_source_screenshot(obs_source_t *) override {}

    // ---------- virtualcam ----------
    obs_output_t *obs_frontend_get_virtualcam_output(void) override
    {
        return virtualcamOutput ? obs_output_get_ref(virtualcamOutput) : nullptr;
    }
    void obs_frontend_start_virtualcam(void) override;
    void obs_frontend_stop_virtualcam(void) override;
    bool obs_frontend_virtualcam_active(void) override
    {
        return virtualcamOutput && obs_output_active(virtualcamOutput);
    }

    // ---------- video reset (libobs already exposes obs_reset_video) ----------
    void obs_frontend_reset_video(void) override {}

    // ---------- source-properties windows (no GUI) ----------
    void obs_frontend_open_source_properties(obs_source_t *) override {}
    void obs_frontend_open_source_filters(obs_source_t *) override {}
    void obs_frontend_open_source_interaction(obs_source_t *) override {}
    void obs_frontend_open_sceneitem_edit_transform(obs_sceneitem_t *) override {}

    // ---------- assorted strings ----------
    char *obs_frontend_get_current_record_output_path(void) override
    {
        return bstrdup_or_null(recordDirectory.c_str());
    }
    const char *obs_frontend_get_locale_string(const char *string) override { return string; }
    bool obs_frontend_is_theme_dark(void) override { return true; }
    char *obs_frontend_get_last_recording(void) override { return bstrdup_or_null(lastRecording.c_str()); }
    char *obs_frontend_get_last_screenshot(void) override { return bstrdup_or_null(""); }
    char *obs_frontend_get_last_replay(void) override { return bstrdup_or_null(lastReplay.c_str()); }

    // ---------- undo/redo (no-op) ----------
    void obs_frontend_add_undo_redo_action(const char *, const undo_redo_cb, const undo_redo_cb, const char *,
                                           const char *, bool) override {}

    // ---------- canvases (single default canvas list, mutations not supported) ----------
    void obs_frontend_get_canvases(obs_frontend_canvas_list *) override {}
    obs_canvas_t *obs_frontend_add_canvas(const char *, obs_video_info *, int) override { return nullptr; }
    bool obs_frontend_remove_canvas(obs_canvas_t *) override { return false; }

    // ---------- on_event / on_save / on_load (called from obs_frontend_api.cpp) ----------
    void on_load(obs_data_t *settings) override
    {
        std::lock_guard<std::mutex> lk(callbacksMutex);
        for (size_t i = saveCallbacks.size(); i > 0; --i) {
            auto &c = saveCallbacks[i - 1];
            c.cb(settings, false, c.priv);
        }
    }
    void on_preload(obs_data_t *settings) override
    {
        std::lock_guard<std::mutex> lk(callbacksMutex);
        for (size_t i = preloadCallbacks.size(); i > 0; --i) {
            auto &c = preloadCallbacks[i - 1];
            c.cb(settings, false, c.priv);
        }
    }
    void on_save(obs_data_t *settings) override
    {
        std::lock_guard<std::mutex> lk(callbacksMutex);
        for (size_t i = saveCallbacks.size(); i > 0; --i) {
            auto &c = saveCallbacks[i - 1];
            c.cb(settings, true, c.priv);
        }
    }
    void on_event(obs_frontend_event event) override
    {
        std::vector<StubCallback<obs_frontend_event_cb>> snapshot;
        {
            std::lock_guard<std::mutex> lk(callbacksMutex);
            snapshot = eventCallbacks;
        }
        for (size_t i = snapshot.size(); i > 0; --i) {
            auto &c = snapshot[i - 1];
            c.cb(event, c.priv);
        }
    }

private:
    // helpers
    static char *bstrdup_or_null(const char *s) { return s ? bstrdup(s) : nullptr; }

    void teardown();
    void clear_libobs_scene_data();
    void verify_libobs_scene_data_drained();

    // Write a compact machine-readable rollback marker beside the recording
    // output.  This is intentionally not a scene-switch-v1 event: the
    // rollback freezes an already committed route and does not represent a
    // new scene command.  Keeping it as a separate operational record avoids
    // widening the shared command contract while giving the runbook a durable
    // assertion to verify alongside the structured log line.
    struct RollbackObservation {
        bool laneRootBindingValid = false;
        bool programViewStable = false;
        bool programVideoStable = false;
        bool previewViewStable = false;
        bool currentProgramPreserved = false;
        bool activeVideoTRebound = true;
        bool newTakesEnabled = true;
        bool frozen = false;
    };

    void writeDualLaneRollbackStatus(uint64_t frameId, uint64_t ptsNs,
                                     int committedOnAirLane, int committedPreviewLane,
                                     const RollbackObservation &observation)
    {
        if (recordDirectory.empty()) {
            blog(LOG_WARNING,
                 "[pulsar-dual-lane] rollback status marker unavailable: recording directory is unset");
            return;
        }
        const std::filesystem::path marker =
            std::filesystem::path(recordDirectory) / "pulsar-dual-lane-rollback.json";
        const json status = {
            {"schema", "pulsar.dual-lane-rollback.v1"},
            {"runtime_instance_id", g_runtimeTelemetry.runtimeInstanceId()},
            {"state", observation.frozen ? "frozen" : "active"},
            {"frame_id", frameId},
            {"pts_ns", ptsNs},
            {"onair_lane", committedOnAirLane},
            {"preview_lane", committedPreviewLane},
            {"lane_root_binding_valid", observation.laneRootBindingValid},
            {"current_program_preserved", observation.currentProgramPreserved},
            {"active_video_t_rebound", observation.activeVideoTRebound},
            {"new_takes_enabled", observation.newTakesEnabled},
            {"program_view_stable", observation.programViewStable},
            {"program_video_stable", observation.programVideoStable},
            {"preview_view_stable", observation.previewViewStable},
        };
        // Queue only: directory creation and file I/O happen on the
        // process-lifetime marker worker, after the frame callback returns.
        g_rollbackMarkerWriter.enqueue(marker, status.dump() + "\n");
        blog(LOG_INFO, "[pulsar-dual-lane] rollback status marker queued=%s", marker.string().c_str());
    }

    // Dual-lane video topology (ADR-PULSAR-DUAL-LANE-001).  The two view
    // contexts and their video_t objects are created once; a Take only queues
    // a pair swap for the libobs graphics boundary.
    bool setupDualLane(obs_scene_t *templateScene);
    bool queueDualLaneCut(obs_source_t *scene);
    bool replaceLaneCompositionLocked(int lane, obs_source_t *scene);
    bool dualLaneInvariantLocked(const char *where) const;
    static void OnDualLaneCutCommitted(void *param, uint64_t frameId, uint64_t ptsNs);

    // ADR-005 §3.4 / #182: pulsar:OutputFailed. This binary is a static lib
    // linked into pulsar-headless.exe -- it owns no obs-websocket vendor of
    // its own, so it reaches pulsar-multi-stream's already-registered
    // "pulsar" vendor through libobs's global proc handler, the same
    // mechanism obs-websocket-api.h itself uses to find obs-websocket
    // (see plugins/pulsar-multi-stream/src/plugin-main.cpp's
    // emit_output_failed_proc comment for the full rationale). The callee
    // no-ops on OBS_OUTPUT_SUCCESS (a requested/graceful stop, RC7) and on a
    // missing proc/vendor (obs-websocket or pulsar-multi-stream absent), so
    // this call is unconditional and safe on every "stop" signal.
    static void EmitOutputFailedViaGlobalProc(const char *output_name, bool is_local, calldata_t *stopData)
    {
        long long code = 0;
        calldata_get_int(stopData, "code", &code);
        const char *last_error = nullptr;
        calldata_get_string(stopData, "last_error", &last_error);

        proc_handler_t *ph = obs_get_proc_handler();
        if (!ph)
            return;
        calldata_t cd = {};
        calldata_set_string(&cd, "output", output_name);
        calldata_set_string(&cd, "phase", "active");
        calldata_set_bool(&cd, "is_local_output", is_local);
        calldata_set_int(&cd, "code", code);
        calldata_set_string(&cd, "last_error", last_error ? last_error : "");
        proc_handler_call(ph, "pulsar_multi_stream_emit_output_failed", &cd);
        calldata_free(&cd);
    }

    // ADR-005 §3.5 / #186: same global-proc bridge as
    // EmitOutputFailedViaGlobalProc above, for pulsar:OutputAttemptSettled.
    // `live` selects "live" (reason_class omitted) vs "failed"
    // (reason_class from #182's closed set, computed on the receiving side
    // by pulsar-multi-stream's classify_output_failure -- this binary does
    // not link that header, see plugin-main.cpp's classify_output_failure
    // callers for why it must stay the single source of that mapping).
    static void EmitOutputAttemptSettledViaGlobalProc(const char *output_name, const char *destination_id,
                                                        long long attempt, bool live, bool is_local, int code,
                                                        const char *last_error, long long duration_ms)
    {
        proc_handler_t *ph = obs_get_proc_handler();
        if (!ph)
            return;
        calldata_t cd = {};
        calldata_set_string(&cd, "output", output_name);
        calldata_set_string(&cd, "destination", destination_id);
        calldata_set_int(&cd, "attempt", attempt);
        calldata_set_bool(&cd, "live", live);
        calldata_set_bool(&cd, "is_local_output", is_local);
        calldata_set_int(&cd, "code", code);
        calldata_set_string(&cd, "last_error", last_error ? last_error : "");
        calldata_set_int(&cd, "duration_ms", duration_ms);
        proc_handler_call(ph, "pulsar_multi_stream_emit_output_attempt_settled", &cd);
        calldata_free(&cd);
    }

    // Milliseconds elapsed since a std::atomic<long long>-stored
    // steady_clock::now().time_since_epoch().count() reading was taken.
    static long long ElapsedMsSince(long long startNs)
    {
        auto start = std::chrono::steady_clock::time_point(std::chrono::nanoseconds(startNs));
        return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - start)
            .count();
    }

    static void OnStreamStart(void *param, calldata_t *)
    {
        auto *self = static_cast<PulsarFrontendAPI *>(param);
        self->emit(OBS_FRONTEND_EVENT_STREAMING_STARTED);
        // ADR-005 §3.5 / #186: this IS the attempt reaching "live" -- settle
        // it here, once, before any later "stop" can be mistaken for the
        // same attempt failing.
        self->streamAttemptWentActive.store(true);
        EmitOutputAttemptSettledViaGlobalProc("stream", "stream", self->streamAttempt.load(), /*live=*/true,
                                               /*is_local=*/false, /*code=*/0, /*last_error=*/nullptr,
                                               ElapsedMsSince(self->streamAttemptStartNs.load()));
    }
    static void OnStreamStop(void *param, calldata_t *data)
    {
        auto *self = static_cast<PulsarFrontendAPI *>(param);
        self->emit(OBS_FRONTEND_EVENT_STREAMING_STOPPED);
        long long code = 0;
        calldata_get_int(data, "code", &code);
        const char *last_error = nullptr;
        calldata_get_string(data, "last_error", &last_error);
        // ADR-005 §3.5 / #186 authority split: went active already -> this is
        // a mid-diffusion failure, #182's emit_output_failed stays the sole
        // authority (RC7's code==0 no-op is unaffected, decided downstream by
        // classify_output_failure). Never went active -> this stop settles
        // the SAME attempt that OnStreamStart never got to fire for; that
        // verdict belongs to attempt-settled exclusively, not to
        // emit_output_failed, per the same split. code==0 and never active is
        // a client cancelling a still-connecting attempt: no class exists for
        // it (classify_output_failure returns nullptr for code 0), so no
        // verdict is built at all rather than forcing one.
        if (self->streamAttemptWentActive.exchange(false)) {
            EmitOutputFailedViaGlobalProc("stream", /*is_local_output=*/false, data);
        } else if (code != 0) {
            EmitOutputAttemptSettledViaGlobalProc("stream", "stream", self->streamAttempt.load(), /*live=*/false,
                                                   /*is_local=*/false, static_cast<int>(code), last_error,
                                                   ElapsedMsSince(self->streamAttemptStartNs.load()));
        }
    }
    static void OnRecordStart(void *param, calldata_t *)
    {
        static_cast<PulsarFrontendAPI *>(param)->emit(OBS_FRONTEND_EVENT_RECORDING_STARTED);
    }
    static void OnRecordStop(void *param, calldata_t *data)
    {
        auto *self = static_cast<PulsarFrontendAPI *>(param);
        const char *last = nullptr;
        calldata_get_string(data, "last_file", &last);
        if (last)
            self->lastRecording = last;
        self->recordingPaused.store(false);
        self->emit(OBS_FRONTEND_EVENT_RECORDING_STOPPED);
    }
    static void OnRecordPause(void *param, calldata_t *)
    {
        auto *self = static_cast<PulsarFrontendAPI *>(param);
        self->recordingPaused.store(true);
        self->emit(OBS_FRONTEND_EVENT_RECORDING_PAUSED);
    }
    static void OnRecordUnpause(void *param, calldata_t *)
    {
        auto *self = static_cast<PulsarFrontendAPI *>(param);
        self->recordingPaused.store(false);
        self->emit(OBS_FRONTEND_EVENT_RECORDING_UNPAUSED);
    }
    static void OnReplayStart(void *param, calldata_t *)
    {
        static_cast<PulsarFrontendAPI *>(param)->emit(OBS_FRONTEND_EVENT_REPLAY_BUFFER_STARTED);
    }
    static void OnReplayStop(void *param, calldata_t *)
    {
        static_cast<PulsarFrontendAPI *>(param)->emit(OBS_FRONTEND_EVENT_REPLAY_BUFFER_STOPPED);
    }
    static void OnReplaySaved(void *param, calldata_t *)
    {
        // The replay-buffer output exposes the saved path through its proc
        // handler "get_last_replay" (obs-ffmpeg-mux.c:944). That proc only
        // yields a path once muxing has finished -- upstream clears the
        // `muxing` flag immediately BEFORE emitting "saved"
        // (obs-ffmpeg-mux.c:1129-1134), so reading it from this handler is
        // exactly the ordering OBSBasic relies on. Without this,
        // GetLastReplayBufferReplay would keep returning "" forever.
        auto *self = static_cast<PulsarFrontendAPI *>(param);
        if (self->replayOutput) {
            proc_handler_t *ph = obs_output_get_proc_handler(self->replayOutput);
            if (ph) {
                calldata_t cd = {};
                if (proc_handler_call(ph, "get_last_replay", &cd)) {
                    const char *path = nullptr;
                    if (calldata_get_string(&cd, "path", &path) && path && *path) {
                        self->lastReplay = path;
                        blog(LOG_INFO, "[pulsar-frontend-stub] replay saved -> %s", path);
                    } else {
                        blog(LOG_WARNING, "[pulsar-frontend-stub] replay saved but "
                             "get_last_replay yielded no path");
                    }
                }
                calldata_free(&cd);
            }
        }
        self->emit(OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED);
    }
    // Issue #129 (mirror of #119): libobs DELEGATES scene-item cleanup to the
    // frontend. obs_source_remove() only flags the source and fires the global
    // "source_remove" signal (obs-source.c:927-944); the scene items that hold
    // the last refs are only dropped by obs_scene_prune_sources(), which libobs
    // itself calls exclusively from scene_video_render (obs-scene.c:1071) --
    // i.e. never for a scene that is not being rendered. obs-studio closes the
    // loop in OBSBasic (InitOBSCallbacks -> SourceRemoved, and
    // RemoveSceneAndReleaseNested in OBSBasic_Scenes.cpp:322-331). The stub had
    // no such handler, so RemoveInput answered success and removed nothing: the
    // input stayed in GetInputList and its item in GetSceneItemList forever.
    static void OnSourceRemove(void *, calldata_t *)
    {
        // Two phases ON PURPOSE. obs_enum_scenes walks libobs's source list
        // holding obs->data.sources_mutex, while obs_scene_prune_sources takes
        // the scene's video lock (obs-scene.c:4130-4143). Pruning from inside
        // the enumeration callback would nest those two locks; collecting refs
        // first and pruning after the enumeration returned keeps them disjoint.
        std::vector<obs_source_t *> sceneRefs;
        obs_enum_scenes(
            [](void *param, obs_source_t *scene) {
                auto *out = static_cast<std::vector<obs_source_t *> *>(param);
                if (obs_source_is_group(scene))
                    return true; // groups are pruned with their owning scene
                if (obs_source_t *ref = obs_source_get_ref(scene))
                    out->push_back(ref);
                return true;
            },
            &sceneRefs);

        for (obs_source_t *s : sceneRefs) {
            if (obs_scene_t *sc = obs_scene_from_source(s))
                obs_scene_prune_sources(sc);
            obs_source_release(s);
        }
    }

    static void OnVCamStart(void *param, calldata_t *)
    {
        auto *self = static_cast<PulsarFrontendAPI *>(param);
        self->emit(OBS_FRONTEND_EVENT_VIRTUALCAM_STARTED);
        self->vcamAttemptWentActive.store(true);
        EmitOutputAttemptSettledViaGlobalProc("virtualcam", "virtualcam", self->vcamAttempt.load(), /*live=*/true,
                                               /*is_local=*/true, /*code=*/0, /*last_error=*/nullptr,
                                               ElapsedMsSince(self->vcamAttemptStartNs.load()));
    }
    static void OnVCamStop(void *param, calldata_t *data)
    {
        auto *self = static_cast<PulsarFrontendAPI *>(param);
        self->emit(OBS_FRONTEND_EVENT_VIRTUALCAM_STOPPED);
        long long code = 0;
        calldata_get_int(data, "code", &code);
        const char *last_error = nullptr;
        calldata_get_string(data, "last_error", &last_error);
        // Same §3.5 authority split as OnStreamStop, is_local=true throughout
        // (virtualcam has no ingest/auth surface -- classify_output_failure
        // always answers "disconnected_local" for it).
        if (self->vcamAttemptWentActive.exchange(false)) {
            EmitOutputFailedViaGlobalProc("virtualcam", /*is_local_output=*/true, data);
        } else if (code != 0) {
            EmitOutputAttemptSettledViaGlobalProc("virtualcam", "virtualcam", self->vcamAttempt.load(),
                                                   /*live=*/false, /*is_local=*/true, static_cast<int>(code),
                                                   last_error, ElapsedMsSince(self->vcamAttemptStartNs.load()));
        }
    }

    void hookOutputSignals(obs_output_t *out, void (*onStart)(void *, calldata_t *),
                           void (*onStop)(void *, calldata_t *))
    {
        if (!out)
            return;
        signal_handler_t *sh = obs_output_get_signal_handler(out);
        if (!sh)
            return;
        signal_handler_connect(sh, "start", onStart, this);
        signal_handler_connect(sh, "stop", onStop, this);
    }

    // Releasing an output while it is still active is undefined: the
    // muxer / encoder threads keep writing through pointers that the
    // ref-drop is about to free. Issue obs_output_stop, poll for
    // obs_output_active to flip, and only then return so teardown can
    // safely release the handle. ~1 s budget; fall back to force_stop
    // if the muxer's writeback is wedged (loses the trailing fragment
    // but still better than a use-after-free at exit).
    static void stop_output_and_wait(obs_output_t *out, const char *name)
    {
        if (!out || !obs_output_active(out))
            return;
        obs_output_stop(out);
        for (int i = 0; i < 50 && obs_output_active(out); ++i)
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        if (obs_output_active(out)) {
            blog(LOG_WARNING, "[pulsar-frontend-stub] %s still active after 1s, force-stopping", name);
            obs_output_force_stop(out);
        }
    }

    // ---------- transition compositing (M10 Gap B' fix, ADR 003 §3.3) ----------
    // Resolve the demo stinger asset to a LOCAL path. The path is NEVER taken
    // from a leaf / network value (ADR 003 Amendment 2 §A2.1, R7 / C-PATH):
    // it is an operator-pinned env override, else a default packaged location.
    // The fork registers the stinger source with THIS path; obs-websocket's
    // SetCurrentSceneTransitionSettings only ever drives transition_point /
    // duration, never a remote path.
    static std::string resolve_stinger_asset_path()
    {
        if (const char *e = std::getenv("PULSAR_STINGER_ASSET"); e && *e)
            return e;
        // Default: <cwd>/../../data/pulsar/stinger-demo.webm. pulsar.exe runs
        // with cwd=bin/64bit (PRISM-EMBEDDING.md), so ../../data is the bundle
        // data root. Absent asset => the stinger simply decodes nothing; the
        // fade fallback still composites and the encoder is never blanked.
        std::error_code ec;
        std::filesystem::path p =
            std::filesystem::current_path(ec) / ".." / ".." / "data" / "pulsar" / "stinger-demo.webm";
        return std::filesystem::weakly_canonical(p, ec).string();
    }

    // obs_transition_start can report success before the media decoder has
    // opened its file. Validate the operator-pinned local asset before
    // creating the transition so a missing, unreadable, or obviously corrupt
    // container cannot produce transition_started followed by a black seam.
    struct StingerAssetValidation {
        bool usable = false;
        const char *reason = "asset_unreadable";
    };

    static StingerAssetValidation validate_stinger_asset(const std::string &assetPath)
    {
        std::error_code ec;
        const std::filesystem::path path(assetPath);
        if (!std::filesystem::is_regular_file(path, ec))
            return {false, "asset_missing"};
        const auto size = std::filesystem::file_size(path, ec);
        if (ec || size < 16)
            return {false, "asset_unreadable"};

        std::array<unsigned char, 12> header{};
        std::ifstream stream(path, std::ios::binary);
        stream.read(reinterpret_cast<char *>(header.data()), static_cast<std::streamsize>(header.size()));
        if (stream.gcount() < 4)
            return {false, "asset_unreadable"};

        std::string extension = path.extension().string();
        std::transform(extension.begin(), extension.end(), extension.begin(), [](unsigned char c) {
            return static_cast<char>(std::tolower(c));
        });
        const bool ebml = header[0] == 0x1a && header[1] == 0x45 &&
                          header[2] == 0xdf && header[3] == 0xa3;
        const bool isoBmff = header[4] == 'f' && header[5] == 't' &&
                             header[6] == 'y' && header[7] == 'p';
        if ((extension == ".webm" || extension == ".mkv") && !ebml)
            return {false, "asset_invalid_container"};
        if ((extension == ".mp4" || extension == ".m4v" || extension == ".mov") && !isoBmff)
            return {false, "asset_invalid_container"};
        return {true, nullptr};
    }

    // FinalQueued intentionally remains visible for one complete graphics
    // tick and the atomic swap callback is observed on a following frame.
    // Start that control-plane tail before the requested deadline so
    // transitionDuration remains an end-to-end Program contract rather than
    // animation time plus hidden finalization frames.
    static uint64_t transition_finalization_lead_ms(uint64_t frameCount)
    {
        struct obs_video_info video = {};
        if (!obs_get_video_info(&video) || video.fps_num == 0 || video.fps_den == 0)
            return frameCount * 17; // conservative 60 fps fallback
        const uint64_t numerator = frameCount * 1000ULL * static_cast<uint64_t>(video.fps_den);
        return (numerator + static_cast<uint64_t>(video.fps_num) - 1) /
               static_cast<uint64_t>(video.fps_num);
    }

    static uint64_t stinger_transition_point_ms(int durationMs)
    {
        const uint64_t duration = static_cast<uint64_t>((std::max)(1, durationMs));
        return (std::min)(uint64_t{300}, (std::max)(uint64_t{1}, duration / 2));
    }

    // M10 PIVOT (ADR 003 Amendment 4 §A4.3 / §A4.7 #69, issue #73): the OBS-native
    // stinger compositing built in #67 is DORMANT by default. The M10 transition is
    // rendered by Solar/CEF as an overlay; OBS only ever performs a hard cut. This
    // flag guards the #67 native path so it survives in `main` for a future
    // capability without ever running in the M10 chain.
    //
    // SECURITY INVARIANT (Bastion #76 / ADR §A4.5 R1′·R7): the flag is resolved
    // EXCLUSIVELY from the process environment at boot -- it is operator/env-
    // controlled and NEVER derived from, or reachable by, a leaf / obs-websocket /
    // network value. There is no obs-ws request, no leaf field, and no scene/
    // transition setting that can flip it; the only input is std::getenv below.
    // Default OFF means an unset env => dormant => no native transition, no media
    // decode. Any value other than the explicit truthy set ("1"/"true"/"on"/"yes",
    // case-insensitive) keeps it OFF.
    static bool resolve_native_stinger_flag()
    {
        const char *e = std::getenv("PULSAR_NATIVE_STINGER");
        if (!e || !*e)
            return false; // default OFF: dormant
        std::string v;
        for (const char *p = e; *p; ++p)
            v.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(*p))));
        return v == "1" || v == "true" || v == "on" || v == "yes";
    }

    // Bind `transition` as output source 0, seeded to hold `scene` so the
    // encoder always sees frames (passthrough when idle, blend mid-switch).
    // Mirrors upstream OBSBasic::SetTransition + InitTransition.
    void bindTransitionOutput(obs_source_t *transition, obs_source_t *scene)
    {
        if (!transition)
            return;
        // Size the transition to the program canvas so a fixed-size stinger
        // media composites at full output resolution (obs-scene.c:4072-4074).
        obs_video_info ovi;
        if (obs_get_video_info(&ovi)) {
            obs_transition_set_size(transition, ovi.base_width, ovi.base_height);
            obs_transition_set_alignment(transition, OBS_ALIGN_CENTER);
            obs_transition_set_scale_type(transition, OBS_TRANSITION_SCALE_ASPECT);
        }
        if (scene)
            obs_transition_set(transition, scene); // hold the current scene
        obs_set_output_source(0, transition);      // transition feeds the encoder
    }

    void prepareDualLaneTransition(obs_source_t *transition, obs_source_t *scene)
    {
        if (!transition)
            return;
        obs_video_info ovi;
        if (obs_get_video_info(&ovi)) {
            obs_transition_set_size(transition, ovi.base_width, ovi.base_height);
            obs_transition_set_alignment(transition, OBS_ALIGN_CENTER);
            obs_transition_set_scale_type(transition, OBS_TRANSITION_SCALE_ASPECT);
        }
        if (scene)
            obs_transition_set(transition, scene);
    }

    // state
    std::mutex dualLaneMutex;
    // Resolved exactly once during setup().  `dualLaneReady` describes the
    // physical topology; `dualLaneOperational` is separately cleared by the
    // frame-boundary rollback drill so the current Program route remains live
    // while new scene mutations fail closed.
    bool dualLaneEnabled = true;
    bool dualLaneReady = false;
    bool dualLaneOperational = false;
    std::atomic<bool> dualLaneCutPending{false};
    std::string sceneSwitchPreparedCommandId;
    std::string sceneSwitchPendingTakeId;
    std::atomic<uint64_t> lastCutFrameId{0};
    std::atomic<uint64_t> lastCutPtsNs{0};
    uint64_t cutCount = 0;
    uint64_t rollbackAfterTakes = 0;
    int onAirLane = 0;
    int previewLane = 1;
    obs_source_t *laneSources[2] = {};
    obs_sceneitem_t *laneItems[2] = {};

    // Physical role roots remain stable for the lifetime of the frontend.
    // These references point at the roots bound to ProgramView/PreviewView;
    // the selected public scene sources are tracked separately and remain live
    // children of the roots (rather than private snapshots).
    obs_source_t *currentScene = nullptr;
    obs_source_t *previewScene = nullptr;
    obs_source_t *programSelection = nullptr;
    obs_source_t *previewSelection = nullptr;
    obs_source_t *currentTransition = nullptr;
    // Reference OWNER only -- never an enumeration source of truth (ADR Prism
    // 026 §3.1, issue #119). It holds the ref on the boot "Default" scene so it
    // outlives setup(); obs_frontend_get_scenes enumerates libobs, not this.
    std::vector<obs_source_t *> scenes;
    std::vector<obs_source_t *> transitions; // fade + stinger, owned.

    obs_output_t *streamOutput = nullptr;
    obs_output_t *recordOutput = nullptr;
    obs_output_t *replayOutput = nullptr;
    obs_output_t *virtualcamOutput = nullptr;
    obs_output_t *programReturnOutput = nullptr;
    obs_output_t *previewReturnOutput = nullptr;

    // Stable downstream surfaces.  The encoder and the two return outputs
    // remain attached to these video_t objects for the lifetime of Pulsar.
    obs_view_t *programView = nullptr;
    video_t *programVideo = nullptr;
    obs_view_t *previewView = nullptr;
    video_t *previewVideo = nullptr;
    video_t *runtimeTelemetryVideo = nullptr;
    bool runtimeTelemetryRawConnected = false;

    // The r2 audio graph is deliberately independent of the two video lanes.
    // Keep the process' libobs audio_t captured once at setup and reuse this
    // exact pointer for every encoded Program surface.  A Cut only swaps
    // video roots; it must never select or recreate this route.  The raw
    // Program/Preview return outputs are video-only and do not consume audio.
    audio_t *programAudio = nullptr;

    // ADR-005 §3.5 / #186: attempt lifecycle for pulsar:OutputAttemptSettled,
    // one pair per output this binary owns directly (multi-stream's own
    // destinations track the same thing on Destination itself, see
    // plugin-main.cpp). Atomics: the "start"/"stop" signal callbacks run on
    // libobs's own thread, concurrently with the API-thread call that opens
    // an attempt. StartNs stores steady_clock::now().time_since_epoch().count()
    // rather than a non-atomic-friendly time_point.
    std::atomic<long long> streamAttempt{0};
    std::atomic<long long> streamAttemptStartNs{0};
    std::atomic<bool> streamAttemptWentActive{false};
    std::atomic<long long> vcamAttempt{0};
    std::atomic<long long> vcamAttemptStartNs{0};
    std::atomic<bool> vcamAttemptWentActive{false};
    // Source-mode virtual cam (Zab): when a "ZabVirtualCamSource" scene exists,
    // the vcam carries THAT scene (the source the operator chose in settings)
    // through a dedicated view instead of the program. Created lazily on the
    // first source-mode start, torn down with the output.
    obs_view_t *vcamView = nullptr;
    video_t *vcamVideo = nullptr;
    obs_service_t *streamService = nullptr;

    obs_encoder_t *videoEncoder = nullptr;
    // One audio encoder per libobs mixer slot in use (issue #168, ADR Prism 028
    // §3.5). audioEncoders[i] encodes mix i -- track i+1 as a client numbers
    // them -- and stays null when that track carries no encoder. The boot
    // default creates exactly ONE, on track 1, bound at slot 0 of the three
    // outputs: the pre-#168 wiring, unchanged.
    obs_encoder_t *audioEncoders[MAX_AUDIO_MIXES] = {};
    obs_source_t *captureSource = nullptr;
    obs_sceneitem_t *captureItem = nullptr;
    obs_source_t *cefSource = nullptr;
    obs_sceneitem_t *cefItem = nullptr;

    // Audio sources bound to libobs main mixer channels 1-3 (the AAC encoder
    // mixes channels 0..5 into mixer index 0). Source IDs come from
    // upstream's win-wasapi plugin which is loaded by obs_load_all_modules
    // on Windows. These are NOT scene items -- audio sources live on
    // libobs's audio routing graph, not the visual scene.
    obs_source_t *desktopAudioSource = nullptr; // channel 1
    obs_source_t *processAudioSource = nullptr; // channel 2 (optional)
    obs_source_t *micAudioSource = nullptr;     // channel 3

    std::string recordDirectory; // resolved at setup() from env or default

    // Recording container, resolved ONCE at setup() from PULSAR_RECORD_CONTAINER
    // (issue #166). "mp4" or "mkv"; applied to both extension sites in
    // obs_frontend_recording_start() and seeded into recordOutput's settings at
    // setup() so GetCapabilities (pulsar-multi-stream) can read it before any
    // recording has ever started.
    std::string recordContainer = "mp4";

    // Replay buffer sizing (ADR Prism 024 §3.1). Resolved ONCE at setup() from
    // env, like every other PULSAR_* knob, and pushed into replayOutput's
    // settings -- the replay buffer holds already-encoded packets in RAM, so
    // max_time_sec x (video+audio bitrate) is the memory bill, capped by
    // max_size_mb. Replays land in recordDirectory alongside recordings.
    int replayMaxTimeSec = 30;
    int replayMaxSizeMb = 512;

    int transitionDuration = 300;
    int tbarPosition = 0;
    bool studioMode = false;
    bool previewEnabled = true;
    // M10 dormant native-stinger flag (ADR 003 §A4.3, issue #73). Resolved ONCE
    // at setup() from PULSAR_NATIVE_STINGER (env-only, never leaf-reachable).
    // Default false => OBS performs a raw hard cut, the #67 stinger compositing
    // is inert and the stinger source is never registered.
    bool nativeStingerEnabled = false;
    bool dualLaneTransitionsEnabled = false;
    pulsar_transition::Controller dualLaneTransition;
    obs_source_t *dualLaneStingerTransition = nullptr;
    const char *dualLaneStingerAssetFailure = nullptr;
    bool dualLaneTransitionFinalPending = false;
    bool dualLaneTransitionAbortPending = false;
    uint64_t dualLaneTransitionStartNs = 0;
    std::atomic<bool> recordingPaused{false};
    std::string lastRecording;
    std::string lastReplay;

    std::mutex callbacksMutex;
    std::vector<StubCallback<obs_frontend_event_cb>> eventCallbacks;
    std::vector<StubCallback<obs_frontend_save_cb>> saveCallbacks;
    std::vector<StubCallback<obs_frontend_save_cb>> preloadCallbacks;
};

// Runtime transport for the versioned scene-switch contract.  It deliberately
// lives with the frontend rather than in the websocket DLL: libobs keeps
// global procedure registrations for process lifetime, so callback payloads
// into an unloadable module would be unsafe during shutdown.
class PulsarSceneSwitchVendor {
public:
    static constexpr const char *kVendorName = "pulsar-scene-switch";

    bool start()
    {
        if (vendor_)
            return true;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            operational_ = true;
            state_ = "ready";
        }
        vendor_ = obs_websocket_register_vendor(kVendorName);
        if (!vendor_ || !obs_websocket_vendor_register_request(vendor_, "Prepare", &Prepare, this) ||
            !obs_websocket_vendor_register_request(vendor_, "Take", &Take, this) ||
            !obs_websocket_vendor_register_request(vendor_, "Abort", &Abort, this) ||
            !obs_websocket_vendor_register_request(vendor_, "Dispatch", &Dispatch, this) ||
            !obs_websocket_vendor_register_request(vendor_, "GetState", &GetState, this)) {
            blog(LOG_ERROR, "[pulsar-scene-switch] vendor registration failed");
            // The websocket API has no unregister operation for a partially
            // registered vendor.  This object has static lifetime, so those
            // callbacks remain safe, but it is never published to graphics
            // callbacks or marked running after a partial registration.
            vendor_ = nullptr;
            return false;
        }
        obs_add_tick_callback(&Tick, this);
        running_ = true;
        blog(LOG_INFO, "[pulsar-scene-switch] vendor registered name=%s", kVendorName);
        return true;
    }

    void stop()
    {
        if (!running_)
            return;
        obs_remove_tick_callback(&Tick, this);
        running_ = false;
        std::unique_lock<std::mutex> lock(mutex_);
        const std::string pendingTake = pendingTake_ ? pendingTake_->commandId : "";
        lock.unlock();
        const bool cancelled = !pendingTake.empty() && g_api && g_api->sceneSwitchAbort(pendingTake);
        lock.lock();
        if (cancelled && pendingTake_ && pendingTake_->commandId == pendingTake) {
            Pending take = *pendingTake_;
            pendingTake_.reset();
            pendingPrepare_.reset();
            state_ = "ready";
            emit(eventFor("TakeAborted", take, {{"take_command_id", take.commandId}, {"reason", "shutdown"}}, "ready"));
        }
        pendingPrepare_.reset();
        pendingTake_.reset();
    }

    void previewRendered(const std::string &commandId, uint64_t frameId, uint64_t ptsNs)
    {
        std::unique_lock<std::mutex> lock(mutex_);
        if (!pendingPrepare_ || pendingPrepare_->commandId != commandId || pendingPrepare_->ready || state_ != "preparing")
            return;
        pendingPrepare_->ready = true;
        state_ = "preview_ready";
        json event = eventFor("PreviewReady", *pendingPrepare_, {{"target_lane_id", pendingPrepare_->lane},
            {"target_scene_id", pendingPrepare_->scene}, {"first_frame_id", frameId}, {"first_pts_ns", ptsNs}}, "preview_ready");
        pendingPrepare_->readyEvent = event;
        emit(event);
    }

    void takeCommitted(const std::string &takeId, uint64_t frameId, uint64_t ptsNs)
    {
        takeCommitted(takeId, frameId, ptsNs, false);
    }

    void freezeAfterFrontendRollback()
    {
        std::lock_guard<std::mutex> lock(mutex_);
        // The rollback drill may be armed through the legacy
        // TriggerStudioModeTransition path rather than a v1 vendor Take.  In
        // that case there is no pending vendor command to complete, but
        // GetState must still expose the same operational freeze.
        operational_ = false;
        state_ = "frozen";
        pendingPrepare_.reset();
        pendingTake_.reset();
    }

    void takeCommitted(const std::string &takeId, uint64_t frameId, uint64_t ptsNs,
                       bool freezeAfterCommit)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!pendingTake_ || pendingTake_->commandId != takeId)
            return;
        const Pending take = *pendingTake_;
        const json previous = revisions_;
        const json previousRoles = roleMap_;
        // doTake admits a frame-boundary Cut only after both counters pass
        // this same bound check. Keep the callback defensive: a corrupted
        // internal state must not silently wrap a protocol revision.
        if (!revisionCanAdvance("program") || !revisionCanAdvance("role_map")) {
            blog(LOG_ERROR, "[pulsar-scene-switch] TakeCommitted revision overflow guard tripped");
            return;
        }
        advanceRevision("program");
        advanceRevision("role_map");
        std::swap(roleMap_["on_air"], roleMap_["preview"]);
        // The v1 terminal event keeps lifecycle `state="ready"` (the schema
        // deliberately models the completed Take).  GetState exposes the
        // separate operational status below: after this commit it is
        // `state="frozen", operational=false`.  Keeping these two views
        // explicit avoids publishing a non-schema event state while still
        // making the rollback freeze machine-readable.
        state_ = freezeAfterCommit ? "frozen" : "ready";
        operational_ = !freezeAfterCommit;
        pendingTake_.reset();
        pendingPrepare_.reset();
        json extra = {{"take_command_id", take.commandId}, {"target_lane_id", take.lane},
            {"target_scene_id", take.scene}, {"source_lane_id", previousRoles["on_air"]},
            {"frame_id", frameId}, {"pts_ns", ptsNs}, {"program_lane_id", roleMap_["on_air"]},
            {"preview_lane_id", roleMap_["preview"]}, {"previous_role_map", previousRoles}};
        json event = eventFor("TakeCommitted", take, extra, "ready", previous);
        // Keep the original Dispatch(Take) response (TakeAccepted) in the
        // idempotency map. The terminal commit is a one-shot VendorEvent, not
        // a replacement response for an exact retry of the command.
        emit(event);
    }

private:
    struct Pending {
        std::string key, commandId, intentId, runtimeId, digest, lane, scene;
        uint64_t deadlineNs = 0;
        bool ready = false;
        json readyEvent;
    };
    static bool validId(const json &v)
    {
        if (!v.is_string() || v.get_ref<const std::string &>().empty() || v.get_ref<const std::string &>().size() > 128)
            return false;
        const std::string &s = v.get_ref<const std::string &>();
        if (!std::isalnum(static_cast<unsigned char>(s.front()))) return false;
        for (unsigned char c : s)
            if (!(std::isalnum(c) || c == '.' || c == '_' || c == ':' || c == '-')) return false;
        return true;
    }
    static bool integer(const json &v, uint64_t &out)
    {
        if (v.is_number_unsigned()) { out = v.get<uint64_t>(); return true; }
        if (v.is_number_integer() && v.get<int64_t>() >= 0) { out = static_cast<uint64_t>(v.get<int64_t>()); return true; }
        if (v.is_number_float()) { double n = v.get<double>(); if (std::isfinite(n) && n >= 0 && n < 18446744073709551616.0 && std::floor(n) == n) { out = static_cast<uint64_t>(n); return true; } }
        return false;
    }
    bool revisionCanAdvance(const char *key) const
    {
        const auto it = revisions_.find(key);
        if (it == revisions_.end() || (!it->is_number_integer() && !it->is_number_unsigned()))
            return false;
        if (it->is_number_integer() && it->get<int64_t>() < 0)
            return false;
        return it->get<uint64_t>() < (std::numeric_limits<uint64_t>::max)();
    }
    bool advanceRevision(const char *key)
    {
        if (!revisionCanAdvance(key))
            return false;
        const uint64_t current = revisions_.at(key).get<uint64_t>();
        revisions_[key] = current + 1;
        return true;
    }
    static uint64_t nowNs() { return os_gettime_ns(); }
    static std::string sha256(const std::string &text)
    {
        // BCrypt is the Windows system SHA-256 provider; no mutable or
        // process-global crypto state is retained by the request path.
#ifdef _WIN32
        BCRYPT_ALG_HANDLE alg = nullptr; BCRYPT_HASH_HANDLE hash = nullptr;
        DWORD objectLen = 0, bytes = 0; std::vector<unsigned char> object, digest(32);
        if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, nullptr, 0) != 0)
            return {};
        if (BCryptGetProperty(alg, BCRYPT_OBJECT_LENGTH, reinterpret_cast<PUCHAR>(&objectLen), sizeof(objectLen), &bytes, 0) != 0) {
            BCryptCloseAlgorithmProvider(alg, 0);
            return {};
        }
        object.resize(objectLen);
        const bool ok = BCryptCreateHash(alg, &hash, object.data(), objectLen, nullptr, 0, 0) == 0 &&
            BCryptHashData(hash, reinterpret_cast<PUCHAR>(const_cast<char *>(text.data())), static_cast<ULONG>(text.size()), 0) == 0 &&
            BCryptFinishHash(hash, digest.data(), static_cast<ULONG>(digest.size()), 0) == 0;
        if (hash) BCryptDestroyHash(hash); if (alg) BCryptCloseAlgorithmProvider(alg, 0);
        if (!ok) return {};
        static const char hex[] = "0123456789abcdef"; std::string result; result.reserve(64);
        for (unsigned char b : digest) { result.push_back(hex[b >> 4]); result.push_back(hex[b & 15]); }
        return result;
#else
        return {};
#endif
    }
    bool normalize(const json &in, json &out, std::string &error)
    {
        if (!in.is_object()) { error = "requestData must be an object"; return false; }
        const std::set<std::string> common = {"contract","schema_version","message_type","command_type","command_id","intent_id","runtime_instance_id","expected_revisions","expected_server_seq"};
        const char *required[] = {"contract","schema_version","message_type","command_type","command_id","intent_id","runtime_instance_id","expected_revisions"};
        for (auto key : required) if (!in.contains(key)) { error = "command is missing required fields"; return false; }
        if (in["contract"] != "pulsar.scene-switch.v1" || in["message_type"] != "command" || !in["command_type"].is_string()) { error = "command contract is not v1"; return false; }
        uint64_t n = 0; if (!integer(in["schema_version"], n) || n != 1 || !validId(in["command_id"]) || !validId(in["intent_id"]) || !validId(in["runtime_instance_id"])) { error = "command identifiers or schema version are invalid"; return false; }
        if (!in["expected_revisions"].is_object() || in["expected_revisions"].size() != 3) { error = "expected_revisions is invalid"; return false; }
        for (auto key : {"program","preview","role_map"}) if (!in["expected_revisions"].contains(key) || !integer(in["expected_revisions"][key], n)) { error = "expected_revisions is invalid"; return false; }
        if (in.contains("expected_server_seq") && !integer(in["expected_server_seq"], n)) { error = "expected_server_seq is invalid"; return false; }
        const std::string type = in["command_type"];
        std::set<std::string> allowed = common;
        if (type == "Prepare") {
            allowed.insert("target"); allowed.insert("timeout_ms");
            for (const auto &it : in.items()) if (!allowed.count(it.key())) { error = "Prepare command contains unknown or cross-type fields"; return false; }
            // Materialize once before taking iterators: begin/end from two
            // get<std::string>() temporaries are unrelated and invalid.
            std::string sceneId;
            if (in.contains("target") && in["target"].is_object() && in["target"].contains("scene_id") && in["target"]["scene_id"].is_string())
                sceneId = in["target"]["scene_id"].get<std::string>();
            const bool sceneHasControl = std::any_of(sceneId.begin(), sceneId.end(), [](unsigned char c) { return c < 0x20 || c == 0x7F; });
            if (!in.contains("target") || !in.contains("timeout_ms") || !in["target"].is_object() || in["target"].size() != 2 || !in["target"].contains("lane_id") || !in["target"].contains("scene_id") || !in["target"]["lane_id"].is_string() || (in["target"]["lane_id"] != "A" && in["target"]["lane_id"] != "B") || !in["target"]["scene_id"].is_string() || in["target"]["scene_id"].get<std::string>().empty() || in["target"]["scene_id"].get<std::string>().size() > 256 || sceneHasControl || !integer(in["timeout_ms"], n) || n < 1 || n > 60000) { error = "Prepare payload is invalid"; return false; }
        } else if (type == "Take") {
            allowed.insert("prepared_command_id"); allowed.insert("timeout_ms");
            for (const auto &it : in.items()) if (!allowed.count(it.key())) { error = "Take command contains unknown or cross-type fields"; return false; }
            if (!in.contains("prepared_command_id") || !in.contains("timeout_ms") || !validId(in["prepared_command_id"]) || !integer(in["timeout_ms"], n) || n < 1 || n > 60000) { error = "Take payload is invalid"; return false; }
        } else if (type == "Abort") {
            allowed.insert("take_command_id"); allowed.insert("reason"); allowed.insert("last_committed_frame_id"); allowed.insert("last_committed_pts_ns");
            for (const auto &it : in.items()) if (!allowed.count(it.key())) { error = "Abort command contains unknown or cross-type fields"; return false; }
            const bool hasLastFrame = in.contains("last_committed_frame_id"); const bool hasLastPts = in.contains("last_committed_pts_ns");
            if (!in.contains("take_command_id") || !in.contains("reason") || !validId(in["take_command_id"]) || !in["reason"].is_string() || (in["reason"] != "operator" && in["reason"] != "timeout" && in["reason"] != "shutdown" && in["reason"] != "superseded" && in["reason"] != "queue_rejected") || (hasLastFrame && !integer(in["last_committed_frame_id"], n)) || (hasLastPts && !integer(in["last_committed_pts_ns"], n)) || (in["reason"] == "queue_rejected" && (!hasLastFrame || !hasLastPts))) { error = "Abort payload is invalid"; return false; }
        } else { error = "command_type is not supported by v1"; return false; }
        out = in;
        out["schema_version"] = 1;
        for (auto key : {"program","preview","role_map"}) { integer(in["expected_revisions"][key], n); out["expected_revisions"][key] = n; }
        if (in.contains("expected_server_seq")) { integer(in["expected_server_seq"], n); out["expected_server_seq"] = n; }
        if (type == "Prepare" || type == "Take") { integer(in["timeout_ms"], n); out["timeout_ms"] = n; }
        if (type == "Abort") {
            if (in.contains("last_committed_frame_id")) { integer(in["last_committed_frame_id"], n); out["last_committed_frame_id"] = n; }
            if (in.contains("last_committed_pts_ns")) { integer(in["last_committed_pts_ns"], n); out["last_committed_pts_ns"] = n; }
        }
        return true;
    }
    json eventFor(const char *type, const Pending &p, json extra = json::object(), const char *state = nullptr, json previous = json())
    {
        const json before = previous.is_null() ? revisions_ : previous;
        json event = {{"contract","pulsar.scene-switch.v1"},{"schema_version",1},{"message_type","event"},{"event_type",type},{"command_id",p.commandId},{"intent_id",p.intentId},{"runtime_instance_id",p.runtimeId},{"server_seq",++serverSeq_},{"state",state ? state : "ready"},{"previous_revisions",before},{"revisions",revisions_},{"role_map",roleMap_},{"observed_at_monotonic_ns",nowNs()},{"payload_sha256",p.digest}};
        for (const auto &it : extra.items()) event[it.key()] = it.value();
        return event;
    }
    json reject(const Pending &p, const std::string &code, const std::string &message, json details = json::object())
    {
        json event = eventFor("CommandRejected", p, {{"error_code",code},{"error_message",message},{"error_details",details}});
        return event;
    }
    void emit(const json &event)
    {
        if (!vendor_) return;
        obs_data_t *data = obs_data_create_from_json(event.dump().c_str());
        if (data) { obs_websocket_vendor_emit_event(vendor_, event["event_type"].get<std::string>().c_str(), data); obs_data_release(data); }
    }
    void respond(obs_data_t *response, const json &event)
    {
        if (!response) {
            blog(LOG_ERROR, "[pulsar-scene-switch] vendor response object is null");
            return;
        }
        const std::string serialized = event.dump();
        obs_data_t *temporary = obs_data_create_from_json(serialized.c_str());
        if (!temporary) {
            // Leave the vendor response empty rather than emitting a partial
            // or non-canonical object when libobs rejects our serialization.
            blog(LOG_ERROR, "[pulsar-scene-switch] failed to serialize vendor response");
            return;
        }
        obs_data_apply(response, temporary);
        obs_data_release(temporary);
    }
    void respondFrozen(const Pending &p, obs_data_t *response)
    {
        // This direct-vendor guard uses the canonical v1 CommandRejected
        // envelope.  The websocket gateway normally rejects the request
        // before it reaches this adapter; a direct vendor caller therefore
        // receives the same typed reason without a schema fork.  The
        // rejection may advance the protocol's server sequence, as any
        // canonical rejection does, but it never changes route/revision state.
        const json event = reject(p, "PREVIEW_FROZEN", "dual-lane rollback freeze is active");
        // The frozen path is reachable before the normal capacity guard. Do
        // not let an unbounded stream of new command IDs grow the idempotency
        // map after it has reached its fixed limit; known entries were
        // replayed above, while new frozen requests remain fail-closed without
        // an insertion.
        if (outcomes_.size() < kMaxOutcomes)
            outcomes_[p.key] = {p.digest, event};
        emit(event);
        respond(response, event);
    }
    static void Dispatch(obs_data_t *request, obs_data_t *response, void *priv) { static_cast<PulsarSceneSwitchVendor *>(priv)->dispatch(request, response, nullptr); }
    static void Prepare(obs_data_t *request, obs_data_t *response, void *priv) { static_cast<PulsarSceneSwitchVendor *>(priv)->dispatch(request, response, "Prepare"); }
    static void Take(obs_data_t *request, obs_data_t *response, void *priv) { static_cast<PulsarSceneSwitchVendor *>(priv)->dispatch(request, response, "Take"); }
    static void Abort(obs_data_t *request, obs_data_t *response, void *priv) { static_cast<PulsarSceneSwitchVendor *>(priv)->dispatch(request, response, "Abort"); }
    static void GetState(obs_data_t *, obs_data_t *response, void *priv) { static_cast<PulsarSceneSwitchVendor *>(priv)->state(response); }
    static void Tick(void *priv, float) { static_cast<PulsarSceneSwitchVendor *>(priv)->expire(); }
    void dispatch(obs_data_t *request, obs_data_t *response, const char *requestType)
    {
        json raw; try { raw = json::parse(obs_data_get_json(request)); } catch (...) { raw = json::object(); }
        json command; std::string error;
        std::unique_lock<std::mutex> lock(mutex_);
        if (requestType && raw.is_object() && (!raw.contains("command_type") || raw["command_type"] != requestType))
            error = "vendor request type and required command_type disagree";
        if (!error.empty() || !normalize(raw, command, error)) { std::string digest=sha256(raw.dump()); if (digest.empty()) digest.assign(64, '0'); Pending invalid{"invalid","invalid-command","invalid-intent",runtimeId_,digest, "", ""}; json event = reject(invalid, "SCHEMA_INVALID", error); emit(event); respond(response,event); return; }
        const std::string key = command["runtime_instance_id"].get<std::string>() + "\n" + command["command_id"].get<std::string>();
        const std::string digest = sha256(command.dump());
        auto prior = outcomes_.find(key);
        if (prior != outcomes_.end()) { if (prior->second.first == digest) { respond(response, prior->second.second); return; } Pending p{key,command["command_id"],command["intent_id"],command["runtime_instance_id"],digest,"",""}; json event=reject(p,"IDEMPOTENCY_CONFLICT","command_id was already used with a different payload",{{"original_payload_sha256",prior->second.first},{"received_payload_sha256",digest}}); emit(event); respond(response,event); return; }
        Pending p{key,command["command_id"],command["intent_id"],command["runtime_instance_id"],digest,"",""};
        // A foreign runtime can never mutate this process.  Do not retain its
        // caller-controlled key: doing so would turn RUNTIME_MISMATCH into an
        // unbounded remote-memory allocation surface.
        if (p.runtimeId != runtimeId_) { json event=reject(p,"RUNTIME_MISMATCH","command runtime_instance_id does not belong to this runtime"); emit(event); respond(response,event); return; }
        // A post-rollback direct vendor call must not be confused with a
        // stale lane or missing preparation.  The gateway rejects this path
        // first; retain the same explicit, mutation-free guard here for
        // callers that reach the registered vendor directly.
        const std::string type = command["command_type"];
        // The frontend publishes the bridge latch first at the frame
        // boundary. Consult it directly so a direct vendor caller cannot
        // observe or mutate through the short interval before the adapter's
        // own bookkeeping lock is updated.
        if ((g_dualLaneControlBridge.frozen() || !operational_) && type != "Abort") {
            respondFrozen(p, response);
            return;
        }
        // Process-lifetime idempotency history is intentionally bounded. Once
        // full, every new command fails closed before any lane mutation; known
        // command IDs remain replayable exactly and are never evicted.
        if (outcomes_.size() >= kMaxOutcomes) { json event=reject(p,"SCHEMA_INVALID","idempotency history capacity reached; restart runtime before a new command",{{"max_entries",kMaxOutcomes}}); emit(event); respond(response,event); return; }
        if (command["expected_revisions"] != revisions_) { json event=reject(p,"REVISION_STALE","expected revisions do not match the current runtime revisions",{{"expected_revisions",command["expected_revisions"]},{"actual_revisions",revisions_}}); outcomes_[key]={digest,event}; emit(event); respond(response,event); return; }
        if (command.contains("expected_server_seq") && command["expected_server_seq"] != serverSeq_) { json event=reject(p,"SERVER_SEQ_STALE","expected server sequence does not match the current runtime sequence"); outcomes_[key]={digest,event}; emit(event); respond(response,event); return; }
        if (type == "Prepare") { doPrepare(command,p,response); return; }
        if (type == "Take") { doTake(command,p,response); return; }
        doAbort(command,p,response,lock);
    }
    void doPrepare(const json &c, Pending p, obs_data_t *response)
    {
        if (pendingTake_) { json e=reject(p,"PREVIEW_FROZEN","Preview is frozen while Take is pending"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e); return; }
        if (!revisionCanAdvance("preview")) { json e=reject(p,"SCHEMA_INVALID","Preview revision capacity has been reached"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e); return; }
        p.lane=c["target"]["lane_id"]; p.scene=c["target"]["scene_id"]; p.deadlineNs=nowNs()+c["timeout_ms"].get<uint64_t>()*1000000ULL;
        if (!g_api || !g_api->sceneSwitchPrepare(p.commandId,p.lane[0],p.scene)) { json e=reject(p,"PREVIEW_LANE_MISMATCH","target does not name the live Preview lane or scene"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e); return; }
        const json previous = revisions_;
        if (!advanceRevision("preview")) { g_api->sceneSwitchClearPrepared(p.commandId); json e=reject(p,"SCHEMA_INVALID","Preview revision capacity has been reached"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e); return; }
        state_="preparing"; pendingPrepare_=p; json e=eventFor("PrepareAccepted",p,{{"target_lane_id",p.lane},{"target_scene_id",p.scene},{"deadline_monotonic_ns",p.deadlineNs}},"preparing",previous); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e);
    }
    void doTake(const json &c, Pending p, obs_data_t *response)
    {
        if (pendingTake_ || state_ == "take_accepted") { json e=reject(p,"PREVIEW_FROZEN","another Take is already pending"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e); return; }
        if (!pendingPrepare_ || pendingPrepare_->commandId != c["prepared_command_id"].get<std::string>()) { json e=reject(p,"PREPARE_NOT_FOUND","prepared_command_id has no current preparation"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e); return; }
        if (!pendingPrepare_->ready) { json e=reject(p,"PREVIEW_NOT_READY","Preview has not rendered its first frame"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e); return; }
        if (pendingPrepare_->intentId != p.intentId) { json e=reject(p,"TAKE_INTENT_CONFLICT","Take intent_id differs from Prepare"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e); return; }
        if (!revisionCanAdvance("program") || !revisionCanAdvance("role_map")) { json e=reject(p,"SCHEMA_INVALID","Program revision capacity has been reached"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e); return; }
        p.lane=pendingPrepare_->lane; p.scene=pendingPrepare_->scene; p.deadlineNs=nowNs()+c["timeout_ms"].get<uint64_t>()*1000000ULL;
        pendingTake_=p; state_="take_accepted";
        if (!g_api || !g_api->sceneSwitchTake(p.commandId)) { pendingTake_.reset(); state_="preview_ready"; json e=reject(p,"TAKE_NOT_PENDING","atomic frame-boundary swap was not admitted"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e); return; }
        json e=eventFor("TakeAccepted",p,{{"take_command_id",p.commandId},{"target_lane_id",p.lane},{"target_scene_id",p.scene},{"freeze_until_monotonic_ns",p.deadlineNs}},"take_accepted"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e);
    }
    void doAbort(const json &c, Pending p, obs_data_t *response, std::unique_lock<std::mutex> &lock)
    {
        const std::string requested=c["take_command_id"];
        if (!pendingTake_ || pendingTake_->commandId != requested) { json e=reject(p,"TAKE_NOT_PENDING","take_command_id has no cancellable pending Take"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e); return; }
        if (pendingTake_->intentId != p.intentId) { json e=reject(p,"TAKE_INTENT_CONFLICT","Abort intent_id differs from Take"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e); return; }
        // Cancellation can synchronously drain a graphics callback.  Do not
        // hold the protocol mutex across it: the callback emits the terminal
        // TakeCommitted through this same state machine.
        lock.unlock();
        const bool cancelled = g_api && g_api->sceneSwitchAbort(requested);
        lock.lock();
        if (!cancelled || !pendingTake_ || pendingTake_->commandId != requested) { json e=reject(p,"TAKE_NOT_PENDING","take_command_id has no cancellable pending Take"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e); return; }
        pendingTake_.reset(); pendingPrepare_.reset(); state_="ready"; json e=eventFor("TakeAborted",p,{{"take_command_id",requested},{"reason",c["reason"]}},"ready"); outcomes_[p.key]={p.digest,e}; emit(e); respond(response,e);
    }
    void expire()
    {
        std::unique_lock<std::mutex> lock(mutex_); const uint64_t now=nowNs();
        if (pendingPrepare_ && !pendingTake_ && now >= pendingPrepare_->deadlineNs) { Pending p=*pendingPrepare_; pendingPrepare_.reset(); state_="ready"; if (g_api) g_api->sceneSwitchClearPrepared(p.commandId); json e=reject(p,"TIMEOUT","Preview did not render before deadline"); emit(e); }
        if (!pendingTake_ || now < pendingTake_->deadlineNs) return;
        const std::string id=pendingTake_->commandId; lock.unlock(); const bool cancelled=g_api && g_api->sceneSwitchAbort(id); lock.lock();
        if (cancelled && pendingTake_ && pendingTake_->commandId==id) { Pending p=*pendingTake_; pendingTake_.reset(); pendingPrepare_.reset(); state_="ready"; json e=eventFor("TakeAborted",p,{{"take_command_id",p.commandId},{"reason","timeout"}},"ready"); emit(e); }
    }
    void state(obs_data_t *response)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        const bool effectiveFrozen = g_dualLaneControlBridge.frozen() || !operational_;
        const std::string effectiveState = effectiveFrozen ? "frozen" : state_;
        json out = { {"contract", "pulsar.scene-switch.v1"},
            {"schema_version", 1},
            {"runtime_instance_id", runtimeId_},
            {"state", effectiveState},
            {"operational", !effectiveFrozen},
            {"frozen", effectiveFrozen},
            {"server_seq", serverSeq_},
            {"revisions", revisions_},
            {"role_map", roleMap_},
            {"idempotency_cache_entries",outcomes_.size()},
            {"idempotency_cache_capacity",kMaxOutcomes} };
        respond(response, out);
    }
    std::mutex mutex_; obs_websocket_vendor vendor_ = nullptr; bool running_ = false;
    std::string runtimeId_ = [] { const char *v=std::getenv("PULSAR_RUNTIME_INSTANCE_ID"); return (v && *v) ? std::string(v) : std::string("pulsar-runtime"); }();
    uint64_t serverSeq_ = 0; json revisions_={{"program",0},{"preview",0},{"role_map",0}}; json roleMap_={{"on_air","A"},{"preview","B"}}; std::string state_="ready"; bool operational_ = true;
    static constexpr size_t kMaxOutcomes = 4096;
    std::optional<Pending> pendingPrepare_, pendingTake_; std::map<std::string,std::pair<std::string,json>> outcomes_;
};

PulsarSceneSwitchVendor g_sceneSwitchVendorStorage;

namespace {

// ADR 004 §3.1-3.2 -- boot-time video encoder selection with typed x264
// fallback. The family names below are the ONLY strings accepted from env;
// each resolves against the LIVE obs_enum_encoder_types() set through a pinned
// preference list (never a blind env string to libobs) to absorb OBS-version
// id drift (ADR R4). v0 scope is H.264 only.

bool encoderIdAvailable(const char *id)
{
    const char *enumerated = nullptr;
    for (size_t i = 0; obs_enum_encoder_types(i, &enumerated); ++i) {
        if (enumerated && std::strcmp(enumerated, id) == 0)
            return true;
    }
    return false;
}

// Concrete obs encoder id for a family, or nullptr if no id from that family's
// preference list is present on this machine.
const char *resolveEncoderId(const std::string &family)
{
    static const char *const kNvenc[] = {"jim_nvenc", "obs_nvenc_h264_tex", "ffmpeg_nvenc", nullptr};
    static const char *const kQsv[]   = {"obs_qsv11_v2", "obs_qsv11", nullptr};
    static const char *const kAmf[]   = {"h264_texture_amf", nullptr};

    auto firstAvailable = [](const char *const *ids) -> const char * {
        for (size_t i = 0; ids[i]; ++i)
            if (encoderIdAvailable(ids[i]))
                return ids[i];
        return nullptr;
    };

    if (family == "x264")  return encoderIdAvailable("obs_x264") ? "obs_x264" : nullptr;
    if (family == "nvenc") return firstAvailable(kNvenc);
    if (family == "qsv")   return firstAvailable(kQsv);
    if (family == "amf")   return firstAvailable(kAmf);
    if (family == "auto") {
        if (const char *id = firstAvailable(kNvenc)) return id;
        if (const char *id = firstAvailable(kQsv))   return id;
        if (const char *id = firstAvailable(kAmf))   return id;
        return encoderIdAvailable("obs_x264") ? "obs_x264" : nullptr;
    }
    return nullptr; // unknown family (already whitelisted upstream, defensive)
}

// Concrete obs id -> reported family short name (ADR §3.3 mapping).
const char *encoderFamilyForId(const char *id)
{
    if (std::strcmp(id, "jim_nvenc") == 0 || std::strcmp(id, "obs_nvenc_h264_tex") == 0 ||
        std::strcmp(id, "ffmpeg_nvenc") == 0)
        return "nvenc";
    if (std::strcmp(id, "obs_qsv11_v2") == 0 || std::strcmp(id, "obs_qsv11") == 0)
        return "qsv";
    if (std::strcmp(id, "h264_texture_amf") == 0)
        return "amf";
    return "x264";
}

// Audio track list read off an env var, as the 1-based track numbers a client
// uses ("1,3"). Values outside 1..trackCount and duplicates are dropped with a
// named warning; an unset, empty or entirely invalid list yields {1} -- the
// single-track wiring every pre-#168 spawn had, so the default path is
// unchanged. Order matters: it is the order the tracks land on the output's
// slots.
std::vector<int> parseAudioTrackList(const char *envName, int trackCount)
{
    std::vector<int> tracks;
    const char *raw = std::getenv(envName);
    if (raw && *raw) {
        const std::string value(raw);
        size_t pos = 0;
        while (pos <= value.size()) {
            const size_t comma = value.find(',', pos);
            std::string token = value.substr(
                pos, comma == std::string::npos ? std::string::npos : comma - pos);
            pos = (comma == std::string::npos) ? value.size() + 1 : comma + 1;

            const auto notSpace = [](unsigned char c) { return !std::isspace(c); };
            token.erase(token.begin(),
                        std::find_if(token.begin(), token.end(), notSpace));
            token.erase(std::find_if(token.rbegin(), token.rend(), notSpace).base(),
                        token.end());
            if (token.empty())
                continue;

            const int track = std::atoi(token.c_str());
            if (track < 1 || track > trackCount) {
                blog(LOG_WARNING, "[pulsar-frontend-stub] %s: track '%s' rejected "
                     "(1..%d -- the number of encoders actually created)",
                     envName, token.c_str(), trackCount);
                continue;
            }
            if (std::find(tracks.begin(), tracks.end(), track) != tracks.end()) {
                blog(LOG_WARNING, "[pulsar-frontend-stub] %s: track %d listed twice, "
                     "kept once", envName, track);
                continue;
            }
            tracks.push_back(track);
        }
    }
    if (tracks.empty())
        tracks.push_back(1);
    return tracks;
}

// Per-family whitelisted preset set + default + the libobs PROPERTY NAME that
// family spells the knob with. An env preset outside the set is normalised to
// the default (logged), never passed raw to create (ADR R5).
//
// The property name is NOT uniformly "preset": obs-qsv11 registers no "preset"
// key at all -- its knob is "target_usage"
// (upstream/plugins/obs-qsv11/obs-qsv11.c:390), values TU1..TU7
// (QSV_Encoder.h:89), default TU4 (obs-qsv11.c:165). Writing "preset" for a QSV
// spawn was a silent no-op: every QSV encoder ran at its own TU4 default
// whatever PULSAR_VIDEO_PRESET said. The QSV set below is the encoder's own
// seven levels, so it is exactly what capabilities.encoder_families publishes
// from that same property list (#148) -- the boot whitelist and the manifest
// cannot disagree.
//
// NVENC makes the same point one level down: the property name is not even
// uniform WITHIN a family, so a per-family name is not enough (see
// presetPropForId below).
struct PresetSet {
    const char *const *values;
    const char *dflt;
    const char *prop;
};

PresetSet presetsForFamily(const std::string &family)
{
    static const char *const kX264[]  = {"ultrafast", "superfast", "veryfast", "faster", "fast",
                                         "medium", "slow", "slower", "veryslow", nullptr};
    static const char *const kNvenc[] = {"p1", "p2", "p3", "p4", "p5", "p6", "p7", nullptr};
    // Named apart from the kQsv encoder-ID list above: these are preset values.
    static const char *const kQsvPresets[] = {"TU1", "TU2", "TU3", "TU4",
                                              "TU5", "TU6", "TU7", nullptr};
    static const char *const kAmf[]   = {"speed", "balanced", "quality", nullptr};
    if (family == "nvenc") return {kNvenc, "p5", "preset"};
    if (family == "qsv")   return {kQsvPresets, "TU4", "target_usage"};
    if (family == "amf")   return {kAmf, "balanced", "preset"};
    return {kX264, "veryfast", "preset"};
}

// The preset property name of a CONCRETE encoder id, which for NVENC is not the
// family's name. resolveEncoderId's nvenc preference list spans two different
// spellings of the same knob:
//
//   obs_nvenc_h264_tex  -- the 31.0+ encoder, knob "preset"
//                          (upstream/plugins/obs-nvenc/nvenc-properties.c:142,
//                          default "p5" at :50)
//   jim_nvenc,          -- pre-31.0 COMPAT shims, knob "preset2"
//   ffmpeg_nvenc           (upstream/plugins/obs-nvenc/nvenc-compat.c:183,
//                          default "p5" at :111; ffmpeg_nvenc is the same
//                          compat object re-registered under the old id at
//                          nvenc-compat.c:397)
//
// The values (p1..p7) and the default (p5) are identical on both sides, so only
// the NAME differs -- which is exactly what made this silent. Writing "preset"
// to a compat shim is worse than a no-op: migrate_settings() (nvenc-compat.c:20)
// OVERWRITES "preset" with the value of "preset2" before rerouting, so the
// encoder ran at preset2's own default p5 whatever PULSAR_VIDEO_PRESET said --
// the QSV bug (#150) a second time, on the path resolveEncoderId tries FIRST.
//
// This is deliberately keyed on the id rather than writing both names: only one
// of the two is real for a given encoder, and writing both would put two preset
// keys in one settings object, which is precisely the assumption the read side
// relies on to pick a name (plugin-main.cpp:kPresetPropNames, "first match wins").
const char *presetPropForId(const char *id, const char *familyProp)
{
    if (std::strcmp(id, "jim_nvenc") == 0 || std::strcmp(id, "ffmpeg_nvenc") == 0)
        return "preset2";
    return familyProp;
}

std::string toLower(const char *s)
{
    std::string out = s ? s : "";
    std::transform(out.begin(), out.end(), out.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    return out;
}

std::string toUpper(const char *s)
{
    std::string out = s ? s : "";
    std::transform(out.begin(), out.end(), out.begin(),
                   [](unsigned char c) { return (char)std::toupper(c); });
    return out;
}

// Case-insensitive membership returning the SET's OWN spelling. libobs compares
// these values case-insensitively, but the value we write is also the value
// GetVideoSettings reports and the manifest advertises (QSV spells them
// "TU1".."TU7"), so a lowercased echo of the env var would read back as a value
// the published set does not contain. nullptr = not a member.
const char *canonicalInSet(const char *const *set, const char *v)
{
    const std::string needle = toLower(v);
    for (size_t i = 0; set[i]; ++i)
        if (needle == toLower(set[i]))
            return set[i];
    return nullptr;
}

// Today's exact x264 parameter set (ADR RC2 / §3.2(3) byte-for-byte fallback).
void applyX264Defaults(obs_data_t *s, int bitrate)
{
    obs_data_set_int(s, "bitrate", bitrate);
    obs_data_set_string(s, "rate_control", "CBR");
    obs_data_set_int(s, "keyint_sec", 2);
    obs_data_set_string(s, "preset", "veryfast");
    obs_data_set_string(s, "profile", "high");
    obs_data_set_string(s, "tune", "zerolatency");
}

} // namespace

bool PulsarFrontendAPI::dualLaneInvariantLocked(const char *where) const
{
    if (!dualLaneReady)
        return true;

    const bool valid = onAirLane >= 0 && onAirLane < 2 && previewLane >= 0 && previewLane < 2 &&
                       onAirLane != previewLane && laneSources[onAirLane] == currentScene &&
                       laneSources[previewLane] == previewScene && laneItems[0] && laneItems[1] &&
                       programSelection && previewSelection && programSelection != previewSelection &&
                       programView == obs_get_main_view() &&
                       programVideo == obs_get_video() && previewView && programVideo && previewVideo;
    if (!valid) {
        blog(LOG_ERROR,
             "[pulsar-dual-lane] invariant failed at %s: onair_lane=%d preview_lane=%d "
             "LaneA=%p LaneB=%p current=%p preview=%p program_selection=%p preview_selection=%p",
             where ? where : "unknown", onAirLane, previewLane, (void *)laneSources[0],
             (void *)laneSources[1], (void *)currentScene, (void *)previewScene,
             (void *)programSelection, (void *)previewSelection);
    }
    return valid;
}

bool PulsarFrontendAPI::replaceLaneCompositionLocked(int lane, obs_source_t *scene)
{
    if (lane < 0 || lane >= 2 || !scene || !laneSources[lane])
        return false;
    if (!obs_scene_from_source(scene))
        return false;
    if (scene == laneSources[lane] || scene == laneSources[lane == 0 ? 1 : 0]) {
        blog(LOG_WARNING, "[pulsar-dual-lane] refusing to nest a physical lane root as composition");
        return false;
    }

    obs_scene_t *laneScene = obs_scene_from_source(laneSources[lane]);
    if (!laneScene)
        return false;

    // Keep the exact selected scene source as the single wrapper child.  This
    // is deliberately a live reference: mutations made through the public
    // scene API after binding are rendered by this lane without recreating the
    // physical root, view, video_t, output, or encoder binding.
    obs_sceneitem_t *newItem = obs_scene_add(laneScene, scene);
    if (!newItem) {
        blog(LOG_WARNING, "[pulsar-dual-lane] failed to install composition in lane %d", lane);
        return false;
    }
    obs_sceneitem_t *oldItem = laneItems[lane];
    laneItems[lane] = newItem;
    if (oldItem)
        obs_sceneitem_remove(oldItem);
    return true;
}

bool PulsarFrontendAPI::setupDualLane(obs_scene_t *templateScene)
{
    if (!templateScene)
        return false;

    // The lane roots are private wrappers, not aliases for the user's scene.
    // Their single child is a live scene source.  Program starts on the
    // selected public template; Preview starts on a distinct private
    // bootstrap until a second public scene is selected.  This keeps each
    // physical root stable while allowing later scene mutations to reach the
    // lane without rebinding either downstream view.
    obs_scene_t *laneAScene = obs_scene_create_private("PulsarLaneA");
    obs_scene_t *laneBScene = obs_scene_create_private("PulsarLaneB");
    obs_scene_t *previewBootstrap = obs_scene_create_private("PulsarPreviewBootstrap");
    if (!laneAScene || !laneBScene || !previewBootstrap) {
        if (laneAScene)
            obs_scene_release(laneAScene);
        if (laneBScene)
            obs_scene_release(laneBScene);
        if (previewBootstrap)
            obs_scene_release(previewBootstrap);
        blog(LOG_ERROR, "[pulsar-dual-lane] failed to create fixed roots and Preview bootstrap");
        return false;
    }

    obs_sceneitem_t *laneAItem = obs_scene_add(laneAScene, obs_scene_get_source(templateScene));
    obs_sceneitem_t *laneBItem = obs_scene_add(laneBScene, obs_scene_get_source(previewBootstrap));
    obs_source_t *laneA = obs_source_get_ref(obs_scene_get_source(laneAScene));
    obs_source_t *laneB = obs_source_get_ref(obs_scene_get_source(laneBScene));
    obs_source_t *bootstrapSource = obs_source_get_ref(obs_scene_get_source(previewBootstrap));
    obs_scene_release(previewBootstrap);
    obs_scene_release(laneAScene);
    obs_scene_release(laneBScene);
    if (!laneAItem || !laneBItem || !laneA || !laneB || !bootstrapSource) {
        if (laneA)
            obs_source_release(laneA);
        if (laneB)
            obs_source_release(laneB);
        if (bootstrapSource)
            obs_source_release(bootstrapSource);
        blog(LOG_ERROR, "[pulsar-dual-lane] failed to install live compositions in fixed roots");
        return false;
    }

    laneSources[0] = laneA;
    laneSources[1] = laneB;
    laneItems[0] = laneAItem;
    laneItems[1] = laneBItem;

    // Role refs point only to the fixed roots.  Public scene identity is
    // tracked separately so the frontend API still reports the selected scene.
    if (currentScene)
        obs_source_release(currentScene);
    if (previewScene)
        obs_source_release(previewScene);
    currentScene = obs_source_get_ref(laneSources[0]);
    previewScene = obs_source_get_ref(laneSources[1]);
    programSelection = obs_source_get_ref(templateScene ? obs_scene_get_source(templateScene) : nullptr);
    previewSelection = bootstrapSource;
    onAirLane = 0;
    previewLane = 1;

    // Program is the libobs-owned main view.  It is already part of the main
    // render loop and its video_t is the object returned by obs_get_video(),
    // which preserves legacy stats/outputs and gives the encoder one stable
    // binding.  Only Preview needs an auxiliary hot view.
    programView = obs_get_main_view();
    previewView = obs_view_create();
    if (!programView || !previewView) {
        blog(LOG_ERROR, "[pulsar-dual-lane] failed to create ProgramView/PreviewView");
        // programView aliases libobs's main canvas and is never destroyed by
        // the frontend.  Only the auxiliary Preview view is owned here.
        if (previewView) {
            obs_view_destroy(previewView);
            previewView = nullptr;
        }
        programView = nullptr;
        if (currentScene) {
            obs_source_release(currentScene);
            currentScene = nullptr;
        }
        if (previewScene) {
            obs_source_release(previewScene);
            previewScene = nullptr;
        }
        if (programSelection) {
            obs_source_release(programSelection);
            programSelection = nullptr;
        }
        if (previewSelection) {
            obs_source_release(previewSelection);
            previewSelection = nullptr;
        }
        obs_source_release(laneSources[0]);
        obs_source_release(laneSources[1]);
        laneSources[0] = nullptr;
        laneSources[1] = nullptr;
        laneItems[0] = nullptr;
        laneItems[1] = nullptr;
        return false;
    }

    programVideo = obs_get_video();
    struct obs_video_info previewVideoInfo = {};
    if (!obs_get_video_info(&previewVideoInfo)) {
        blog(LOG_ERROR, "[pulsar-dual-lane] failed to read Program video settings for PreviewView");
    } else {
        previewVideo = obs_view_add3(previewView, &previewVideoInfo, 3);
    }
    if (!programVideo || !previewVideo) {
        blog(LOG_ERROR, "[pulsar-dual-lane] failed to add stable view mixes");
        if (previewView) {
            obs_view_remove(previewView);
            obs_view_destroy(previewView);
        }
        programView = nullptr;
        previewView = nullptr;
        programVideo = nullptr;
        previewVideo = nullptr;
        if (currentScene) {
            obs_source_release(currentScene);
            currentScene = nullptr;
        }
        if (previewScene) {
            obs_source_release(previewScene);
            previewScene = nullptr;
        }
        if (programSelection) {
            obs_source_release(programSelection);
            programSelection = nullptr;
        }
        if (previewSelection) {
            obs_source_release(previewSelection);
            previewSelection = nullptr;
        }
        obs_source_release(laneSources[0]);
        obs_source_release(laneSources[1]);
        laneSources[0] = nullptr;
        laneSources[1] = nullptr;
        laneItems[0] = nullptr;
        laneItems[1] = nullptr;
        return false;
    }

    obs_view_set_source(programView, 0, currentScene);
    obs_view_set_source(previewView, 0, previewScene);
    // `previewVideo` is the dedicated PreviewView video_t.  Connecting here
    // observes an output frame produced by that mix, unlike a main-render
    // callback which only proves Program rendering.
    if (!obs_video_add_borrowed_callback(previewVideo, OnSceneSwitchPreviewVideoFrame, this)) {
        blog(LOG_ERROR, "[pulsar-scene-switch] failed to observe PreviewView frames");
        return false;
    }

    // These are the only media bindings for the two stable raw surfaces.  In
    // particular, no output media is rebound by a Cut.
    if (!programAudio)
        programAudio = obs_get_audio();
    if (!programAudio) {
        blog(LOG_ERROR, "[pulsar-program-audio] common Program route unavailable: libobs audio is null");
        return false;
    }
    if (programReturnOutput)
        // The return filter is a video-only output; keeping its audio media
        // slot null makes the lack of a second Preview/return audio route
        // explicit at the libobs boundary.
        obs_output_set_media(programReturnOutput, programVideo, nullptr);
    if (previewReturnOutput) {
        // Preview has a distinct video surface, but r2 intentionally has no
        // independent Preview audio/AFV route.  These return outputs are
        // video-only; the audio argument is ignored by libobs for them.  The
        // actual audio consumers below all bind the same common Program bus.
        obs_output_set_media(previewReturnOutput, previewVideo, nullptr);
        // Keep the auxiliary Preview mix hot independently of any external
        // DirectShow consumer.  Without an active output no previewVideo
        // frame is produced, so the vendor could accept Prepare but never
        // prove PreviewReady.  Its callback still observes previewVideo
        // itself, never a Program frame or synthetic readiness signal.
        if (!obs_output_start(previewReturnOutput)) {
            blog(LOG_ERROR, "[pulsar-scene-switch] failed to start PreviewView producer: %s",
                 obs_output_get_last_error(previewReturnOutput));
            return false;
        }
        blog(LOG_INFO, "[pulsar-scene-switch] PreviewView producer started for frame-backed readiness");
    } else {
        blog(LOG_ERROR, "[pulsar-scene-switch] PreviewView producer is unavailable");
        return false;
    }

    // ProgramView aliases the main canvas, so channel 0 is already the legacy
    // output source and obs_get_video()/GetStats observe programVideo directly.
    // Do not create a third mix or rebind the main route after setup.

    dualLaneReady = true;
    dualLaneOperational = true;
    dualLaneTransitionsEnabled = resolve_dual_lane_transitions();
    if (dualLaneTransitionsEnabled) {
        std::string stingerPath = resolve_stinger_asset_path();
        const StingerAssetValidation asset = validate_stinger_asset(stingerPath);
        OBSDataAutoRelease stingerSettings = obs_data_create();
        obs_data_set_string(stingerSettings, "path", stingerPath.c_str());
        obs_data_set_int(stingerSettings, "transition_point", 300);
        obs_data_set_int(stingerSettings, "tp_type", 0);
        obs_data_set_bool(stingerSettings, "hw_decode", false);
        obs_source_t *stingerRegistration =
            obs_source_create_private("obs_stinger_transition", "DualLaneStinger", stingerSettings);
        if (stingerRegistration)
            transitions.push_back(stingerRegistration);
        if (asset.usable && stingerRegistration) {
            dualLaneStingerTransition = stingerRegistration;
            blog(LOG_INFO, "[pulsar-dual-lane] transitions enabled; stinger path=%s", stingerPath.c_str());
        } else if (!asset.usable) {
            dualLaneStingerAssetFailure = asset.reason;
            blog(LOG_WARNING,
                 "[pulsar-dual-lane] stinger asset rejected; fallback=cut fallback_to_cut=1 reason=%s path=%s",
                 asset.reason, stingerPath.c_str());
        } else {
            dualLaneStingerAssetFailure = "transition_unavailable";
            blog(LOG_WARNING,
                 "[pulsar-dual-lane] stinger unavailable; Stinger requests will fall back to Cut");
        }
        obs_add_tick_callback(&OnDualLaneTick, this);
    }
    if (!dualLaneInvariantLocked("setup")) {
        dualLaneOperational = false;
        dualLaneReady = false;
        blog(LOG_ERROR, "[pulsar-dual-lane] setup invariant failed");
        return false;
    }
    g_dualLaneControlBridge.activate();
    const bool lane_root_binding_valid = laneSources[onAirLane] == currentScene &&
                                         laneSources[previewLane] == previewScene;
    const bool program_main_view_valid = programView == obs_get_main_view();
    const bool program_main_video_valid = programVideo == obs_get_video();
    const bool preview_distinct_valid = programView != previewView && programVideo != previewVideo &&
                                        currentScene != previewScene;
    blog(LOG_INFO, "[pulsar-dual-lane] ready LaneA=lane-a LaneB=lane-b "
         "lane_root_binding_valid=%d program_main_view_valid=%d program_main_video_valid=%d "
         "preview_distinct_valid=%d ProgramAudioRoute=%s ProgramAudioBound=%d "
         "PreviewAudioPolicy=common",
         lane_root_binding_valid, program_main_view_valid, program_main_video_valid,
         preview_distinct_valid, pulsar_program_audio::kRouteId, programAudio != nullptr);
    return true;
}

bool PulsarFrontendAPI::sceneSwitchPrepare(const std::string &commandId, char laneId, const std::string &sceneId)
{
    obs_source_t *scene = obs_get_source_by_name(sceneId.c_str());
    if (!scene)
        scene = obs_get_source_by_uuid(sceneId.c_str());
    if (!scene)
        return false;
    bool prepared = false;
    {
        std::lock_guard<std::mutex> lock(dualLaneMutex);
        if (dualLaneReady && !dualLaneOperational) {
            blog(LOG_WARNING,
                 "[pulsar-dual-lane] Prepare rejected: rollback freeze is active");
        }
        const char expectedLane = previewLane == 0 ? 'A' : 'B';
        if (dualLaneReady && dualLaneOperational && !dualLaneCutPending.load() && laneId == expectedLane &&
            scene != programSelection && scene != currentScene && obs_scene_from_source(scene)) {
            // Hold the previous public selection before the physical child is
            // replaced.  A postcondition failure must be able to restore both
            // the lane composition and the selection without observable state
            // change, including when external ownership is otherwise absent.
            obs_source_t *oldSelection = obs_source_get_ref(previewSelection);
            if (oldSelection && replaceLaneCompositionLocked(previewLane, scene)) {
                obs_source_t *newSelection = obs_source_get_ref(scene);
                if (!newSelection) {
                    replaceLaneCompositionLocked(previewLane, oldSelection);
                    blog(LOG_ERROR, "[pulsar-scene-switch] Prepare failed to retain new Preview selection");
                } else {
                    obs_source_release(previewSelection);
                    previewSelection = newSelection;
                    sceneSwitchPreparedCommandId = commandId;
                    prepared = dualLaneInvariantLocked("scene-switch-prepare");
                }
                if (!prepared && newSelection) {
                    // `replaceLaneCompositionLocked` is the only physical
                    // mutation in Prepare. Restore the old child and marker
                    // before returning a rejection so it is externally inert.
                    const bool restored = replaceLaneCompositionLocked(previewLane, oldSelection);
                    obs_source_release(previewSelection);
                    previewSelection = oldSelection;
                    oldSelection = nullptr;
                    sceneSwitchPreparedCommandId.clear();
                    if (!restored || !dualLaneInvariantLocked("scene-switch-prepare-rollback"))
                        blog(LOG_ERROR, "[pulsar-scene-switch] Prepare rollback invariant failed");
                }
            }
            if (oldSelection)
                obs_source_release(oldSelection);
        }
    }
    obs_source_release(scene);
    if (prepared) {
        // PreviewView is a distinct active libobs mix; its video-output
        // callback, not Program's main-render callback, supplies readiness.
        g_runtimeTelemetry.previewRevisionChanged();
        emit(OBS_FRONTEND_EVENT_PREVIEW_SCENE_CHANGED);
    }
    return prepared;
}

bool PulsarFrontendAPI::sceneSwitchTake(const std::string &takeCommandId)
{
    obs_source_t *preview = nullptr;
    {
        std::lock_guard<std::mutex> lock(dualLaneMutex);
        if (!dualLaneReady || !dualLaneOperational || dualLaneCutPending.load() ||
            sceneSwitchPreparedCommandId.empty())
            return false;
        sceneSwitchPendingTakeId = takeCommandId;
        preview = obs_source_get_ref(previewSelection);
    }
    const bool queued = queueDualLaneCut(preview);
    if (preview)
        obs_source_release(preview);
    if (!queued) {
        std::lock_guard<std::mutex> lock(dualLaneMutex);
        if (sceneSwitchPendingTakeId == takeCommandId)
            sceneSwitchPendingTakeId.clear();
    }
    return queued;
}

bool PulsarFrontendAPI::sceneSwitchAbort(const std::string &takeCommandId)
{
    // libobs cancels only a still-pending atomic request and drains a racing
    // graphics callback.  Once that callback owns the request it clears the
    // frontend pending marker, so this post-cancel check never reports a
    // fabricated pre-boundary abort.
    obs_view_cancel_atomic_swap();
    std::lock_guard<std::mutex> lock(dualLaneMutex);
    if (sceneSwitchPendingTakeId != takeCommandId || !dualLaneCutPending.load())
        return false;
    if (dualLaneTransition.phase() == pulsar_transition::Phase::Queued) {
        // The initial transition queue may still own the next graphics
        // boundary.  Cancel it first, then queue the unchanged role pair so
        // the abort is observed at a real frame boundary.  Do not emit a
        // synthetic frame/PTS or clear telemetry before that callback.
        dualLaneTransitionAbortPending = true;
        if (obs_view_queue_atomic_swap_with_floor(
                programView, 0, currentScene, previewView, 0, previewScene,
                os_gettime_ns(), OnDualLaneTransitionAbortCommitted, this)) {
            sceneSwitchPendingTakeId.clear();
            return true;
        }
        dualLaneTransitionAbortPending = false;
        dualLaneTransition.abort("operator");
        dualLaneCutPending.store(false);
        g_dualLaneControlBridge.set_pending(false);
        g_runtimeTelemetry.cancelPending();
        sceneSwitchPendingTakeId.clear();
        return false;
    }
    if (dualLaneTransition.phase() == pulsar_transition::Phase::Running ||
        dualLaneTransition.phase() == pulsar_transition::Phase::FinalQueued) {
        // An already-started transition is returned to the existing Program
        // root through the same atomic two-view primitive.  This preserves
        // the old role map and keeps interruption at a frame boundary.
        dualLaneTransitionFinalPending = false;
        dualLaneTransitionAbortPending = true;
        if (obs_view_queue_atomic_swap_with_floor(
                programView, 0, currentScene, previewView, 0, previewScene,
                os_gettime_ns(), OnDualLaneTransitionAbortCommitted, this)) {
            sceneSwitchPendingTakeId.clear();
            return true;
        }
        dualLaneTransitionAbortPending = false;
        return false;
    }
    dualLaneCutPending.store(false);
    g_dualLaneControlBridge.set_pending(false);
    g_runtimeTelemetry.cancelPending();
    sceneSwitchPendingTakeId.clear();
    return true;
}

void PulsarFrontendAPI::sceneSwitchClearPrepared(const std::string &commandId)
{
    std::lock_guard<std::mutex> lock(dualLaneMutex);
    if (sceneSwitchPreparedCommandId == commandId)
        sceneSwitchPreparedCommandId.clear();
}

void PulsarFrontendAPI::OnSceneSwitchPreviewVideoFrame(void *param, struct video_data *frame)
{
    auto *self = static_cast<PulsarFrontendAPI *>(param);
    auto *vendor = g_sceneSwitchVendor.load(std::memory_order_acquire);
    if (!self || !vendor)
        return;
    std::string prepared;
    {
        std::lock_guard<std::mutex> lock(self->dualLaneMutex);
        prepared = self->sceneSwitchPreparedCommandId;
    }
    if (!prepared.empty() && frame)
        vendor->previewRendered(prepared, video_output_get_total_frames(self->previewVideo), frame->timestamp);
}

void PulsarFrontendAPI::OnDualLaneTick(void *param, float)
{
    auto *self = static_cast<PulsarFrontendAPI *>(param);
    if (self)
        self->dualLaneTransitionTick();
}

void PulsarFrontendAPI::OnDualLaneTransitionStarted(void *param, uint64_t frameId, uint64_t ptsNs)
{
    auto *self = static_cast<PulsarFrontendAPI *>(param);
    if (!self)
        return;
    std::lock_guard<std::mutex> lock(self->dualLaneMutex);
    if (!self->dualLaneTransition.started(frameId, ptsNs))
        return;
    self->dualLaneTransitionStartNs = os_gettime_ns();
    self->dualLaneTransition.set_start_monotonic_ns(self->dualLaneTransitionStartNs);
    const auto &metrics = self->dualLaneTransition.metrics();
    blog(LOG_INFO,
         "[pulsar-dual-lane] transition_started kind=%s frame_id=%llu pts_ns=%llu duration_ms=%llu",
         pulsar_transition::kind_name(metrics.kind), static_cast<unsigned long long>(frameId),
         static_cast<unsigned long long>(ptsNs),
         static_cast<unsigned long long>(metrics.requested_duration_ms));
}

void PulsarFrontendAPI::OnDualLaneTransitionAbortCommitted(void *param, uint64_t frameId, uint64_t ptsNs)
{
    auto *self = static_cast<PulsarFrontendAPI *>(param);
    if (!self)
        return;
    std::lock_guard<std::mutex> lock(self->dualLaneMutex);
    if (!self->dualLaneTransitionAbortPending)
        return;
    const bool roleMapPreserved = self->onAirLane >= 0 && self->onAirLane < 2 &&
                                  self->previewLane >= 0 && self->previewLane < 2 &&
                                  self->laneSources[self->onAirLane] == self->currentScene &&
                                  self->laneSources[self->previewLane] == self->previewScene;
    const bool surfacesStable = self->programView && self->previewView && self->programVideo &&
                                self->previewVideo && self->programView == obs_get_main_view() &&
                                self->programView != self->previewView &&
                                self->programVideo != self->previewVideo;
    const bool videoTStable = self->programVideo && self->previewVideo &&
                              self->programVideo == obs_get_video() &&
                              self->programVideo != self->previewVideo;
    const bool invariantValid = self->dualLaneInvariantLocked("transition-abort");
    self->dualLaneTransition.abort("operator");
    self->dualLaneTransitionAbortPending = false;
    self->dualLaneTransitionFinalPending = false;
    self->dualLaneTransitionStartNs = 0;
    self->dualLaneCutPending.store(false);
    g_dualLaneControlBridge.set_pending(false);
    g_runtimeTelemetry.cancelPending();
    blog(LOG_INFO,
         "[pulsar-dual-lane] transition_aborted fallback=cut fallback_to_cut=1 frame_id=%llu pts_ns=%llu "
         "reason=operator role_map_preserved=%d surfaces_stable=%d video_t_stable=%d invariant_valid=%d",
         static_cast<unsigned long long>(frameId), static_cast<unsigned long long>(ptsNs),
         roleMapPreserved, surfacesStable, videoTStable, invariantValid);
}

void PulsarFrontendAPI::dualLaneTransitionTick()
{
    std::lock_guard<std::mutex> lock(dualLaneMutex);
    if (!dualLaneTransition.active() || dualLaneTransitionFinalPending)
        return;

    // Publish FinalQueued for one complete tick before admitting the terminal
    // atomic swap.  The vendor's Abort path can therefore cancel the pending
    // transition deterministically after observing transition_final_queued;
    // the final callback remains the only place that publishes TakeCommitted.
    // This is a control-plane grace window, not a second video lane or a
    // callback-side wait.
    if (dualLaneTransition.phase() == pulsar_transition::Phase::Running) {
        if (!dualLaneTransition.deadline_reached(os_gettime_ns()) ||
            !dualLaneTransition.final_queued())
            return;
        blog(LOG_INFO, "[pulsar-dual-lane] transition_final_queued kind=%s",
             pulsar_transition::kind_name(dualLaneTransition.metrics().kind));
        return;
    }
    if (dualLaneTransition.phase() != pulsar_transition::Phase::FinalQueued)
        return;

    // The final operation is the same two-view atomic Cut used by the core:
    // Program becomes the prepared Preview lane and Preview becomes the old
    // OnAir lane.  The transition source is released by the view at this
    // boundary; no view/video_t/output/encoder is rebound.
    const bool queued = obs_view_queue_atomic_swap_with_floor(
        programView, 0, previewScene, previewView, 0, currentScene,
        dualLaneTransitionStartNs, OnDualLaneCutCommitted, this);
    if (queued) {
        dualLaneTransitionFinalPending = true;
        blog(LOG_INFO, "[pulsar-dual-lane] transition_final_commit_queued kind=%s",
             pulsar_transition::kind_name(dualLaneTransition.metrics().kind));
    } else {
        dualLaneTransition.final_queue_failed();
    }
}

bool PulsarFrontendAPI::queueDualLaneCut(obs_source_t *scene)
{
    obs_source_t *queuedPreview = nullptr;
    obs_source_t *queuedOnAir = nullptr;
    obs_view_t *queuedProgramView = nullptr;
    obs_view_t *queuedPreviewView = nullptr;
    int queuedOnAirLane = -1;
    int queuedPreviewLane = -1;
    uint64_t queuedAdmissionFloorNs = 0;
    bool queued = false;
    bool telemetryReserved = false;
    bool telemetryAccepted = false;
    pulsar_transition::Kind transitionKind = pulsar_transition::Kind::Cut;
    obs_source_t *transitionSource = nullptr;
    {
        std::lock_guard<std::mutex> lk(dualLaneMutex);
        if (!dualLaneReady || !dualLaneOperational || !scene || !dualLaneInvariantLocked("queue-before")) {
            g_runtimeTelemetry.cancelPending();
            if (dualLaneReady && !dualLaneOperational)
                blog(LOG_WARNING,
                     "[pulsar-dual-lane] Take rejected: rollback freeze is active");
            return false;
        }
        if (dualLaneCutPending.load()) {
            g_runtimeTelemetry.cancelPending();
            blog(LOG_WARNING, "[pulsar-dual-lane] Take rejected: another Cut is pending");
            return false;
        }
        if (scene != previewSelection) {
            g_runtimeTelemetry.cancelPending();
            blog(LOG_WARNING, "[pulsar-dual-lane] Take rejected: scene is not the frozen Preview selection");
            return false;
        }
        if (g_runtimeTelemetry.integrityFaulted()) {
            g_runtimeTelemetry.cancelPending();
            blog(LOG_ERROR,
                 "[pulsar-dual-lane] Take rejected: runtime telemetry integrity fail-stop is degraded");
            return false;
        }

        // Reserve the role pair under the lane mutex, but do not perform any
        // trace-file I/O while it is held.  The reservation blocks public
        // mutations through the bridge and remains valid until the queue
        // primitive is called below.
        dualLaneCutPending.store(true);
        g_dualLaneControlBridge.set_pending(true);
        queuedPreview = previewScene;
        queuedOnAir = currentScene;
        queuedProgramView = programView;
        queuedPreviewView = previewView;
        queuedOnAirLane = onAirLane;
        queuedPreviewLane = previewLane;

        if (dualLaneTransitionsEnabled && currentTransition) {
            const char *transitionId = obs_source_get_id(currentTransition);
            const char *transitionName = obs_source_get_name(currentTransition);
            if ((transitionId && std::strcmp(transitionId, "obs_stinger_transition") == 0) ||
                (transitionName && std::strcmp(transitionName, "DualLaneStinger") == 0) ||
                (transitionName && std::strcmp(transitionName, "Stinger") == 0)) {
                transitionKind = pulsar_transition::Kind::Stinger;
                transitionSource = dualLaneStingerTransition;
            } else if ((transitionId && std::strcmp(transitionId, "fade_transition") == 0) ||
                       (transitionName && std::strcmp(transitionName, "Fade") == 0)) {
                transitionKind = pulsar_transition::Kind::Fade;
                transitionSource = currentTransition;
            }
        }

        const bool transitionAvailable = transitionSource != nullptr;
        // Both transition sources are hot before Take and reach the atomic
        // boundary on the tick following FinalQueued.
        const uint64_t finalizationFrames =
            transitionKind == pulsar_transition::Kind::Stinger ||
            transitionKind == pulsar_transition::Kind::Fade ? 1 : 0;
        const uint64_t finalizationLeadMs =
            transition_finalization_lead_ms(finalizationFrames);
        const bool transitionStarted =
            dualLaneTransition.begin(transitionKind, static_cast<uint64_t>((std::max)(0, transitionDuration)),
                                     transitionKind == pulsar_transition::Kind::Cut || transitionAvailable,
                                     finalizationLeadMs);
        const bool animate = transitionStarted && transitionKind != pulsar_transition::Kind::Cut;
        if (transitionKind != pulsar_transition::Kind::Cut && !animate) {
            if (transitionKind == pulsar_transition::Kind::Stinger && dualLaneStingerAssetFailure)
                dualLaneTransition.set_fallback_reason(dualLaneStingerAssetFailure);
            const auto &fallback = dualLaneTransition.metrics();
            blog(LOG_WARNING,
                 "[pulsar-dual-lane] transition_fallback kind=%s fallback=cut fallback_to_cut=1 reason=%s",
                 pulsar_transition::kind_name(fallback.kind),
                 fallback.fallback_reason ? fallback.fallback_reason : "transition_unavailable");
        }
        if (animate) {
            prepareDualLaneTransition(transitionSource, queuedOnAir);
            if (!obs_transition_start(transitionSource, OBS_TRANSITION_MODE_AUTO,
                                      static_cast<uint32_t>(transitionDuration), queuedPreview)) {
                dualLaneTransition.abort("transition_start_failed");
                blog(LOG_WARNING,
                     "[pulsar-dual-lane] transition_fallback kind=%s fallback=cut fallback_to_cut=1 reason=transition_start_failed",
                     pulsar_transition::kind_name(transitionKind));
                transitionKind = pulsar_transition::Kind::Cut;
                transitionSource = nullptr;
            }
        }

        // Reserve the metadata while the same role mutex protects the pair.
        // This consumes state only; no event is timestamped or written until
        // libobs accepts the frame-boundary queue operation.
        telemetryReserved = g_runtimeTelemetry.reserve(scene, queuedOnAirLane, queuedPreviewLane,
                                                        &queuedAdmissionFloorNs);
        const bool reservationStillOwned =
            dualLaneReady && dualLaneCutPending.load() && currentScene == queuedOnAir &&
            previewScene == queuedPreview && onAirLane == queuedOnAirLane &&
            previewLane == queuedPreviewLane && programView == queuedProgramView &&
            previewView == queuedPreviewView;
        if (reservationStillOwned) {
            queued = obs_view_queue_atomic_swap_with_floor(
                queuedProgramView, 0, animate ? transitionSource : queuedPreview,
                queuedPreviewView, 0, queuedOnAir, queuedAdmissionFloorNs,
                animate ? OnDualLaneTransitionStarted : OnDualLaneCutCommitted, this);
            // Default Cut terminal callback: queuedAdmissionFloorNs, OnDualLaneCutCommitted.
        }
        if (queued) {
            // The primitive has admitted the pair.  Mark acceptance and put
            // its event in the asynchronous FIFO before releasing the lane
            // mutex; this operation performs no filesystem I/O.
            telemetryAccepted = telemetryReserved && g_runtimeTelemetry.markAccepted();
        }
        if (!queued) {
            if (animate)
                dualLaneTransition.abort("atomic_swap_rejected");
            dualLaneCutPending.store(false);
            g_dualLaneControlBridge.set_pending(false);
        }
    }

    if (!queued) {
        // No TakeAccepted is emitted: the atomic queue did not admit the
        // candidate.  A reserved trace gets a correlated terminal rejection
        // after the lane mutex is released, with the last committed frame/PTS.
        if (telemetryReserved)
            g_runtimeTelemetry.rejectReserved("atomic_swap_rejected");
        g_runtimeTelemetry.cancelPending();
        blog(LOG_WARNING, "[pulsar-dual-lane] Take rejected: atomic frame-boundary slot is busy");
        return false;
    }

    if (telemetryReserved && !telemetryAccepted) {
        // The physical queue succeeded but the opt-in trace FIFO was already
        // unavailable (normally teardown).  Do not let the following commit
        // masquerade as a correlated runtime measurement.
        blog(LOG_ERROR,
             "[pulsar-runtime-telemetry] atomic Cut queued without TakeAccepted FIFO admission; runtime evidence is invalid");
    }

    const bool lane_root_binding_valid = laneSources[onAirLane] == currentScene &&
                                         laneSources[previewLane] == previewScene;
    const bool program_main_view_valid = programView == obs_get_main_view();
    const bool program_main_video_valid = programVideo == obs_get_video();
    const bool preview_distinct_valid = programView != previewView && programVideo != previewVideo &&
                                        currentScene != previewScene;
    blog(LOG_INFO, "[pulsar-dual-lane] TakeAccepted preview_lane=%d onair_lane=%d "
         "lane_root_binding_valid=%d program_main_view_valid=%d program_main_video_valid=%d "
         "preview_distinct_valid=%d",
         previewLane, onAirLane, lane_root_binding_valid, program_main_view_valid,
         program_main_video_valid, preview_distinct_valid);
    return true;
}

void PulsarFrontendAPI::OnDualLaneCutCommitted(void *param, uint64_t frameId, uint64_t ptsNs)
{
    auto *self = static_cast<PulsarFrontendAPI *>(param);
    if (!self)
        return;

    uint64_t committedCount = 0;
    int committedOnAirLane = -1;
    int committedPreviewLane = -1;
    bool laneRootBindingValid = false;
    bool programMainViewValid = false;
    bool programMainVideoValid = false;
    bool previewDistinctValid = false;
    bool rollbackNow = false;
    bool transitionCommitted = false;
    pulsar_transition::Metrics transitionMetrics;
    pulsar_transition::AggregateSummary transitionAggregate;
    PulsarFrontendAPI::RollbackObservation rollbackObservation;
    std::string sceneSwitchTakeId;
    {
        std::lock_guard<std::mutex> lk(self->dualLaneMutex);
        if (!self->dualLaneCutPending.load())
            return;

        // The pair was already replaced in libobs at this frame boundary.  A
        // role swap is now only metadata/ref interpretation; the two view and
        // video_t identities do not change.
        std::swap(self->currentScene, self->previewScene);
        std::swap(self->onAirLane, self->previewLane);
        std::swap(self->programSelection, self->previewSelection);
        self->lastCutFrameId.store(frameId);
        self->lastCutPtsNs.store(ptsNs);
        ++self->cutCount;
        committedCount = self->cutCount;
        committedOnAirLane = self->onAirLane;
        committedPreviewLane = self->previewLane;
        laneRootBindingValid = self->laneSources[self->onAirLane] == self->currentScene &&
                               self->laneSources[self->previewLane] == self->previewScene;
        programMainViewValid = self->programView == obs_get_main_view();
        programMainVideoValid = self->programVideo == obs_get_video();
        previewDistinctValid = self->programView != self->previewView &&
                               self->programVideo != self->previewVideo &&
                               self->currentScene != self->previewScene;
        sceneSwitchTakeId = self->sceneSwitchPendingTakeId;
        self->sceneSwitchPendingTakeId.clear();
        self->sceneSwitchPreparedCommandId.clear();
        if (self->dualLaneTransitionFinalPending) {
            transitionCommitted = self->dualLaneTransition.committed(frameId, ptsNs, os_gettime_ns());
            transitionMetrics = self->dualLaneTransition.metrics();
            if (transitionCommitted)
                transitionAggregate = self->dualLaneTransition.aggregate(transitionMetrics.kind);
            self->dualLaneTransitionFinalPending = false;
            self->dualLaneTransitionStartNs = 0;
        }
        if (!self->dualLaneInvariantLocked("commit"))
            blog(LOG_ERROR, "[pulsar-dual-lane] commit invariant failed");
        if (self->rollbackAfterTakes > 0 && self->cutCount >= self->rollbackAfterTakes &&
            self->dualLaneOperational) {
            // The atomic swap has completed and the current Program root is
            // already producing the committed frame.  Rollback therefore
            // closes the mutation gate before publishing the pending=false
            // boundary.  It never calls output-source setters or the encoder
            // binding and cannot rebind the active video_t.
            //
            // A websocket mutation may already hold dispatchMutex_ and be
            // waiting for this lane mutex.  Setting the bridge pending bit is
            // therefore the non-blocking admission close; deactivate() drains
            // the bridge only after this lane lock is released, avoiding an
            // inverse-lock deadlock while preventing a post-freeze admission.
            g_dualLaneControlBridge.freeze();
            self->dualLaneOperational = false;
            rollbackObservation.laneRootBindingValid = laneRootBindingValid;
            rollbackObservation.programViewStable = programMainViewValid;
            rollbackObservation.programVideoStable = self->programVideo == obs_get_video();
            rollbackObservation.previewViewStable = self->previewView && self->previewVideo &&
                                                    self->programView != self->previewView &&
                                                    self->programVideo != self->previewVideo;
            rollbackObservation.currentProgramPreserved =
                rollbackObservation.laneRootBindingValid && rollbackObservation.programViewStable &&
                rollbackObservation.programVideoStable;
            rollbackObservation.activeVideoTRebound = !rollbackObservation.programVideoStable;
            rollbackObservation.newTakesEnabled = self->dualLaneOperational;
            rollbackObservation.frozen = !self->dualLaneOperational;
            rollbackNow = true;
        }
        self->dualLaneCutPending.store(false);
        if (!rollbackNow)
            g_dualLaneControlBridge.set_pending(false);
    }

    g_runtimeTelemetry.updateMixRoots(self->currentScene, self->previewScene);
    g_runtimeTelemetry.commit(frameId, ptsNs, committedOnAirLane, committedPreviewLane);
    if (rollbackNow) {
        self->writeDualLaneRollbackStatus(frameId, ptsNs, committedOnAirLane, committedPreviewLane,
                                           rollbackObservation);
        blog(LOG_WARNING,
             "[pulsar-dual-lane] rollback committed at frame_id=%llu pts_ns=%llu "
             "onair_lane=%d preview_lane=%d current_program_preserved=%d "
             "active_video_t_rebound=%d new_takes_enabled=%d lane_root_binding_valid=%d "
             "program_view_stable=%d program_video_stable=%d preview_view_stable=%d frozen=%d",
             static_cast<unsigned long long>(frameId), static_cast<unsigned long long>(ptsNs),
             committedOnAirLane, committedPreviewLane, rollbackObservation.currentProgramPreserved,
             rollbackObservation.activeVideoTRebound, rollbackObservation.newTakesEnabled,
             rollbackObservation.laneRootBindingValid, rollbackObservation.programViewStable,
             rollbackObservation.programVideoStable, rollbackObservation.previewViewStable,
             rollbackObservation.frozen);
    }
    if (auto *vendor = g_sceneSwitchVendor.load(std::memory_order_acquire); vendor) {
        if (!sceneSwitchTakeId.empty()) {
            if (rollbackNow)
                vendor->takeCommitted(sceneSwitchTakeId, frameId, ptsNs, true);
            else
                vendor->takeCommitted(sceneSwitchTakeId, frameId, ptsNs);
        }
        if (rollbackNow)
            vendor->freezeAfterFrontendRollback();
    }

    blog(LOG_INFO, "[pulsar-dual-lane] TakeCommitted count=%llu frame_id=%llu pts_ns=%llu "
         "onair_lane=%d preview_lane=%d lane_root_binding_valid=%d "
         "program_main_view_valid=%d program_main_video_valid=%d preview_distinct_valid=%d "
         "ProgramAudioRoute=%s ProgramAudioBound=%d",
         static_cast<unsigned long long>(committedCount), static_cast<unsigned long long>(frameId),
         static_cast<unsigned long long>(ptsNs), committedOnAirLane, committedPreviewLane,
         laneRootBindingValid, programMainViewValid, programMainVideoValid, previewDistinctValid,
         pulsar_program_audio::kRouteId, self->programAudio != nullptr);
    if (transitionCommitted) {
        blog(LOG_INFO,
             "[pulsar-dual-lane] transition_committed kind=%s requested_duration_ms=%llu "
             "actual_duration_ms=%llu start_frame_id=%llu start_pts_ns=%llu "
             "end_frame_id=%llu end_pts_ns=%llu fallback_to_cut=%d "
             "aggregate_count=%llu duration_p50_ms=%llu duration_p95_ms=%llu duration_p99_ms=%llu "
             "frames_p50=%llu frames_p95=%llu frames_p99=%llu",
             pulsar_transition::kind_name(transitionMetrics.kind),
             static_cast<unsigned long long>(transitionMetrics.requested_duration_ms),
             static_cast<unsigned long long>(transitionMetrics.actual_duration_ms),
             static_cast<unsigned long long>(transitionMetrics.start_frame_id),
             static_cast<unsigned long long>(transitionMetrics.start_pts_ns),
             static_cast<unsigned long long>(transitionMetrics.end_frame_id),
             static_cast<unsigned long long>(transitionMetrics.end_pts_ns),
             transitionMetrics.fallback_to_cut,
             static_cast<unsigned long long>(transitionAggregate.count),
             static_cast<unsigned long long>(transitionAggregate.duration_p50_ms),
             static_cast<unsigned long long>(transitionAggregate.duration_p95_ms),
             static_cast<unsigned long long>(transitionAggregate.duration_p99_ms),
             static_cast<unsigned long long>(transitionAggregate.frames_p50),
             static_cast<unsigned long long>(transitionAggregate.frames_p95),
             static_cast<unsigned long long>(transitionAggregate.frames_p99));
    }
    self->emit(OBS_FRONTEND_EVENT_SCENE_CHANGED);
    self->emit(OBS_FRONTEND_EVENT_PREVIEW_SCENE_CHANGED);
}

bool PulsarFrontendAPI::setup()
{
    // Capture the process-wide libobs audio bus once.  Every r2 output and
    // audio encoder below must use this exact identity; it is intentionally
    // not derived from the current Program/Preview video lane.
    programAudio = obs_get_audio();
    if (!programAudio) {
        blog(LOG_ERROR, "[pulsar-program-audio] setup failed: libobs audio is unavailable");
        return false;
    }
    blog(LOG_INFO, "[pulsar-program-audio] ProgramAudioRoute=%s ProgramAudio=%p "
         "cut_policy=%s preview_audio_supported=false afv_supported=false",
         pulsar_program_audio::kRouteId,
         (void *)programAudio, pulsar_program_audio::kCutPolicy);

    // Issue #129: close the loop libobs expects the FRONTEND to close on
    // source removal (see OnSourceRemove). Connected before anything else is
    // created so no removal can slip through, disconnected in teardown().
    if (signal_handler_t *globalSh = obs_get_signal_handler())
        signal_handler_connect(globalSh, "source_remove", OnSourceRemove, this);
    else
        blog(LOG_WARNING, "[pulsar-frontend-stub] no global signal handler: "
             "RemoveInput will not prune scene items");

    // Default scene.
    obs_scene_t *scene = obs_scene_create("Default");
    if (!scene) {
        blog(LOG_ERROR, "[pulsar-frontend-stub] obs_scene_create failed");
        return false;
    }
    currentScene = obs_source_get_ref(obs_scene_get_source(scene));
    // Keep the scene handle until the stable A/B wrappers are bound after any
    // bootstrap source is attached below.  Trace campaigns can explicitly
    // delegate workload ownership to the probe, in which case Default remains
    // a scene shell and contributes no WGC/CEF producer.
    scenes.push_back(obs_source_get_ref(currentScene));

    // Default transition (fade).
    obs_source_t *fade = obs_source_create_private("fade_transition", "Fade", nullptr);
    if (!fade) {
        blog(LOG_ERROR, "[pulsar-frontend-stub] fade_transition create failed");
        return false;
    }
    currentTransition = fade; // owns 1 ref
    transitions.push_back(obs_source_get_ref(currentTransition));

    // M10 PIVOT (ADR 003 §A4.3, issue #73): resolve the dormant native-stinger
    // flag ONCE here, from the environment only (operator-controlled, never
    // leaf-reachable -- see resolve_native_stinger_flag). Default OFF.
    nativeStingerEnabled = resolve_native_stinger_flag();
    blog(LOG_INFO, "[pulsar-frontend-stub] native stinger compositing %s (PULSAR_NATIVE_STINGER)",
         nativeStingerEnabled ? "ENABLED (dormant path active)" : "disabled (default; OBS hard-cut)");

    if (nativeStingerEnabled) {
        // ---- DORMANT NATIVE PATH (flag ON only, ADR §A4.3) ----
        // M10 (ADR 003 Amendment 1 §A1.1): register a STINGER transition source so
        // SetCurrentSceneTransition{name:"Stinger"} resolves and the active
        // transition can be a media-backed stinger. Its `path` is the locally
        // pinned demo asset (#64) -- resolved on this box, NEVER from a leaf value
        // (Amendment 2 §A2.1, R7 / C-PATH). transition_point defaults to the
        // asset's documented mid-point (300 ms); tp_type=0 means milliseconds.
        std::string stingerPath = resolve_stinger_asset_path();
        OBSDataAutoRelease stingerSettings = obs_data_create();
        obs_data_set_string(stingerSettings, "path", stingerPath.c_str());
        obs_data_set_int(stingerSettings, "transition_point", 300); // ms
        obs_data_set_int(stingerSettings, "tp_type", 0);            // 0 = time (ms)
        obs_data_set_bool(stingerSettings, "hw_decode", false);
        obs_source_t *stinger =
            obs_source_create_private("obs_stinger_transition", "Stinger", stingerSettings);
        if (!stinger) {
            // Non-fatal: the obs-transitions plugin registers
            // obs_stinger_transition on load_all_modules; if a build lacks it,
            // the fade still composites and the encoder is never blanked.
            blog(LOG_WARNING,
                 "[pulsar-frontend-stub] obs_stinger_transition unavailable; stinger not registered");
        } else {
            transitions.push_back(stinger); // owns 1 ref
            blog(LOG_INFO, "[pulsar-frontend-stub] stinger transition registered (path=%s)",
                 stingerPath.c_str());
        }

        // Channel 0 is libobs's main video output. M10 Gap B' fix (ADR 003 §3.3):
        // bind the ACTIVE TRANSITION -- not the raw scene -- as output source 0,
        // seeded to hold the current scene. When idle the transition renders its
        // held scene 1:1 (passthrough, obs-source-transition.c:534), so the encoder
        // always sees frames; on a scene change obs_transition_start animates the
        // blend through this same bound source. This is how upstream OBS feeds the
        // program output (OBSBasic::SetTransition).
        bindTransitionOutput(currentTransition, currentScene);
    } else {
        // ---- DEFAULT PATH (flag OFF, ADR §A4.3 / §A4.7 #69): pre-#67 hard cut ----
        // No stinger source is registered and no transition is bound to the
        // program output. Channel 0 holds the raw current scene; a program-scene
        // change does a brute hard cut (obs_set_output_source). The M10
        // transition is rendered by Solar/CEF as an overlay, never by OBS.
        obs_set_output_source(0, currentScene);
    }

    // Streaming output (rtmp_output). Created without encoders -- a Phase 7+
    // plugin (pulsar-multi-stream) configures encoders + service before
    // streaming_start succeeds.
    streamOutput = obs_output_create("rtmp_output", "PulsarStream", nullptr, nullptr);
    if (!streamOutput)
        blog(LOG_WARNING, "[pulsar-frontend-stub] rtmp_output unavailable");
    else
        obs_output_set_low_latency_interleave(streamOutput, true);
    hookOutputSignals(streamOutput, OnStreamStart, OnStreamStop);

    // Recording output (ffmpeg_muxer). Same shape: needs path + encoders before
    // recording_start succeeds, but obs-websocket can interrogate the handle.
    recordOutput = obs_output_create("ffmpeg_muxer", "PulsarRecord", nullptr, nullptr);
    if (!recordOutput)
        blog(LOG_WARNING, "[pulsar-frontend-stub] ffmpeg_muxer unavailable");
    if (recordOutput) {
        signal_handler_t *sh = obs_output_get_signal_handler(recordOutput);
        signal_handler_connect(sh, "start", OnRecordStart, this);
        signal_handler_connect(sh, "stop", OnRecordStop, this);
        signal_handler_connect(sh, "pause", OnRecordPause, this);
        signal_handler_connect(sh, "unpause", OnRecordUnpause, this);
    }

    // Replay buffer output (replay_buffer).
    replayOutput = obs_output_create("replay_buffer", "PulsarReplay", nullptr, nullptr);
    if (replayOutput) {
        signal_handler_t *sh = obs_output_get_signal_handler(replayOutput);
        signal_handler_connect(sh, "start", OnReplayStart, this);
        signal_handler_connect(sh, "stop", OnReplayStop, this);
        signal_handler_connect(sh, "saved", OnReplaySaved, this);
    }

    // Virtualcam output. obs-virtualcam may not register on every platform/build,
    // so we tolerate its absence -- get_virtualcam_output will simply return null.
    virtualcamOutput = obs_output_create("virtualcam_output", "PulsarVCam", nullptr, nullptr);
    if (virtualcamOutput)
        hookOutputSignals(virtualcamOutput, OnVCamStart, OnVCamStop);

    // The source virtual camera above is intentionally not the program return:
    // Prism scenes use it as an input camera. Keep a second native output for
    // the cockpit so the cockpit never captures its own source-mode camera.
    programReturnOutput = obs_output_create("program_return_output", "PulsarProgramReturn", nullptr, nullptr);
    if (programReturnOutput) {
        if (obs_output_get_flags(programReturnOutput) == 0) {
            obs_output_release(programReturnOutput);
            programReturnOutput = obs_output_create("program_return_output", "PulsarProgramReturn", nullptr, nullptr);
        }
        if (programReturnOutput) {
            blog(LOG_WARNING, "[pulsar-frontend-stub] program return output allocated; awaiting stable ProgramView");
        }
    }

    // The Preview return is the same raw-output primitive with a distinct
    // output name.  The win-dshow patch maps that name to its own queue and
    // filter, so consumers never alias ProgramReturn.
    previewReturnOutput = obs_output_create("preview_return_output", "PulsarPreviewReturn", nullptr, nullptr);
    if (previewReturnOutput) {
        if (obs_output_get_flags(previewReturnOutput) == 0) {
            obs_output_release(previewReturnOutput);
            previewReturnOutput = obs_output_create("preview_return_output", "PulsarPreviewReturn", nullptr, nullptr);
        }
        if (previewReturnOutput)
            blog(LOG_WARNING, "[pulsar-frontend-stub] preview return output allocated; awaiting stable PreviewView");
    }

    // Streaming service placeholder -- NEUTRAL by construction (#136).
    //
    // It used to be an `rtmp_common` / "Twitch" service, which made the boot
    // state depend on a REFUSAL for its safety: the v5 egress gate
    // (include/pulsar-stream-egress.h) had to catch it at StartStream so the
    // cleartext ingest resolution never happened. Safe, but by rebuttal.
    // An empty `rtmp_custom` names no platform, resolves nothing out of any
    // downloaded list, and simply has no destination to connect to -- there is
    // nothing to refuse before the operator (or a v5 client, via
    // SetStreamServiceSettings) has said where to stream. The gate still holds
    // afterwards; it is no longer what makes the DEFAULT path safe.
    //
    // A service object must still exist: obs_output_set_service() needs one and
    // GetStreamServiceSettings reads it back. pulsar-multi-stream replaces it
    // via set_streaming_service.
    OBSDataAutoRelease svcSettings = obs_data_create();
    streamService = obs_service_create("rtmp_custom", "PulsarService", svcSettings, nullptr);
    if (!streamService)
        blog(LOG_WARNING, "[pulsar-frontend-stub] rtmp_custom service unavailable");

    // ---- Phase 6 + 12a: capture source + encoders + record path ----
    // Encoders are bound to the global libobs video/audio mixers and
    // attached to recordOutput so obs_output_start (recording) can run
    // without a Phase 7+ destination plugin. obs_x264 + ffmpeg_aac are
    // always available in upstream's plugin set.
    //
    // Phase 12a: explicit settings replace x264's defaults so the
    // pipeline produces a 1080p60-grade stream out of the box and so
    // operators (and Phase 12b's adaptive loop) can tune bitrates at
    // runtime via obs_encoder_update.
    int videoBitrate = 6000; // kbps -- 1080p60 baseline for Twitch
    if (const char *e = std::getenv("PULSAR_VIDEO_BITRATE"); e && *e) {
        int v = std::atoi(e);
        if (v >= 200 && v <= 50000) videoBitrate = v;
        else blog(LOG_WARNING, "[pulsar-frontend-stub] PULSAR_VIDEO_BITRATE=%s rejected", e);
    }
    // ---- ADR 004 §3.1-3.2: boot-time video encoder selection (x264 fallback) --
    // PULSAR_VIDEO_ENCODER picks a family (whitelisted set); it resolves to a
    // concrete obs id against the live obs_enum_encoder_types() set. Family
    // absent, unknown env value, or a null create() all degrade to obs_x264
    // with today's byte-identical settings -- the spawn never fails on encoder
    // choice. Encoder identity joins fps/resolution in the boot-fixed tier.
    std::string encoderFamily = "x264";
    if (const char *e = std::getenv("PULSAR_VIDEO_ENCODER"); e && *e) {
        std::string req = toLower(e);
        if (req == "x264" || req == "nvenc" || req == "qsv" || req == "amf" || req == "auto")
            encoderFamily = req;
        else
            blog(LOG_WARNING, "[pulsar-frontend-stub] PULSAR_VIDEO_ENCODER=%s not in "
                 "{x264,nvenc,qsv,amf,auto}; using x264", e);
    }

    const char *encoderId = resolveEncoderId(encoderFamily);
    bool encoderFallback = false;
    if (!encoderId) {
        blog(LOG_WARNING, "[pulsar-frontend-stub] encoder '%s' unavailable on this "
             "machine, falling back to x264", encoderFamily.c_str());
        encoderFallback = true;
    }

    OBSDataAutoRelease vEncSettings = obs_data_create();
    if (!encoderFallback) {
        // Validate/normalise every knob before create (ADR §3.2) so a bad env
        // value never reaches obs_video_encoder_create as a hard error.
        const char *reportFamily = encoderFamilyForId(encoderId);

        std::string rateControl = "CBR";
        if (const char *e = std::getenv("PULSAR_VIDEO_RATE_CONTROL"); e && *e) {
            std::string rc = toUpper(e);
            if (rc == "CBR" || rc == "VBR" || rc == "CQP") rateControl = rc;
            else blog(LOG_WARNING, "[pulsar-frontend-stub] PULSAR_VIDEO_RATE_CONTROL=%s "
                      "not in {CBR,VBR,CQP}; using CBR", e);
        }

        std::string profile = "high";
        if (const char *e = std::getenv("PULSAR_VIDEO_PROFILE"); e && *e) {
            std::string p = toLower(e);
            if (p == "baseline" || p == "main" || p == "high") profile = p;
            else blog(LOG_WARNING, "[pulsar-frontend-stub] PULSAR_VIDEO_PROFILE=%s "
                      "not in {baseline,main,high}; using high", e);
        }

        int keyintSec = 2;
        if (const char *e = std::getenv("PULSAR_VIDEO_KEYINT_SEC"); e && *e) {
            int v = std::atoi(e);
            if (v >= 0 && v <= 20) keyintSec = v;
            else blog(LOG_WARNING, "[pulsar-frontend-stub] PULSAR_VIDEO_KEYINT_SEC=%s "
                      "rejected (0..20); using 2", e);
        }

        PresetSet presets = presetsForFamily(reportFamily);
        std::string preset = presets.dflt;
        if (const char *e = std::getenv("PULSAR_VIDEO_PRESET"); e && *e) {
            if (const char *canonical = canonicalInSet(presets.values, e)) preset = canonical;
            else blog(LOG_WARNING, "[pulsar-frontend-stub] PULSAR_VIDEO_PRESET=%s unknown "
                      "for encoder '%s'; using default '%s'", e, reportFamily, presets.dflt);
        }

        obs_data_set_int(vEncSettings, "bitrate", videoBitrate);
        obs_data_set_string(vEncSettings, "rate_control", rateControl.c_str());
        obs_data_set_int(vEncSettings, "keyint_sec", keyintSec);
        obs_data_set_string(vEncSettings, presetPropForId(encoderId, presets.prop), preset.c_str());
        obs_data_set_string(vEncSettings, "profile", profile.c_str());
        const EnvBool nvencLowLatency =
            parse_env_bool(std::getenv("PULSAR_NVENC_LOW_LATENCY"));
        if (nvencLowLatency == EnvBool::Invalid) {
            blog(LOG_WARNING, "[pulsar-frontend-stub] PULSAR_NVENC_LOW_LATENCY "
                 "rejected; preserving the encoder quality defaults");
        }
        const bool enableNvencLowLatency =
            nvencLowLatency == EnvBool::Unset || nvencLowLatency == EnvBool::Enabled;
        if (std::strcmp(reportFamily, "nvenc") == 0 && enableNvencLowLatency) {
            // Select NVENC's ULL scheduling path, but deliberately preserve
            // the historical multipass, lookahead and B-frame settings. They
            // are compression tools, not an OBS-side packet backlog, and the
            // quality A/B gate requires them to remain available.
            obs_data_set_string(vEncSettings, "tune", "ull");
            blog(LOG_INFO, "[pulsar-frontend-stub] NVENC latency profile: "
                 "tune=ull with quality tools preserved");
        } else if (std::strcmp(reportFamily, "nvenc") == 0) {
            blog(LOG_INFO, "[pulsar-frontend-stub] NVENC quality profile preserved: "
                 "PULSAR_NVENC_LOW_LATENCY=0 or invalid");
        } else if (std::strcmp(encoderId, "obs_x264") == 0) {
            obs_data_set_string(vEncSettings, "tune", "zerolatency"); // x264-only knob
        }

        videoEncoder = obs_video_encoder_create(encoderId, "PulsarVideoEnc", vEncSettings, nullptr);
        if (!videoEncoder && std::strcmp(encoderId, "obs_x264") != 0) {
            // §3.2(2): create() returned null for the GPU encoder -> typed fallback.
            blog(LOG_WARNING, "[pulsar-frontend-stub] encoder '%s' (%s) failed to create, "
                 "falling back to x264", reportFamily, encoderId);
            encoderFallback = true;
        }
    }

    if (encoderFallback) {
        // §3.2(3): the x264 fallback path is byte-for-byte today's behaviour,
        // regardless of any GPU-oriented env knobs.
        obs_data_clear(vEncSettings);
        applyX264Defaults(vEncSettings, videoBitrate);
        encoderId = "obs_x264";
        videoEncoder = obs_video_encoder_create("obs_x264", "PulsarVideoEnc", vEncSettings, nullptr);
    }

    encoderFamily = encoderFamilyForId(encoderId); // reflect what actually bound
    if (!videoEncoder) {
        blog(LOG_WARNING, "[pulsar-frontend-stub] video encoder '%s' unavailable", encoderId);
    } else {
        if (recordOutput)
            obs_output_set_video_encoder(recordOutput, videoEncoder);
        if (streamOutput)
            obs_output_set_video_encoder(streamOutput, videoEncoder);
        // ADR Prism 024 §3.1: the replay buffer BORROWS the very same encoder --
        // encode-once / fan-out, the pattern pulsar-multi-stream::ensure_output
        // already runs for every destination. Arming the buffer must never add
        // a video encoder to the process.
        if (replayOutput)
            obs_output_set_video_encoder(replayOutput, videoEncoder);
        blog(LOG_INFO, "[pulsar-frontend-stub] video encoder allocated: family=%s id=%s, "
             "%d kbps (stable ProgramView binding follows)", encoderFamily.c_str(), encoderId, videoBitrate);
    }

    // ---- Audio encoders: N tracks, routed per output (issue #168) ---------
    // Before #168 there was exactly one encoder, hard-bound to slot 0 of the
    // three outputs -- which is why a mixer bit set on an input above track 1
    // reached nothing at all (#157 named that lie; this delivers the function).
    //
    // PULSAR_AUDIO_TRACKS creates N encoders, encoder i on libobs mixer index i.
    // PULSAR_{RECORD,STREAM,REPLAY}_AUDIO_TRACKS then pick which of those tracks
    // each output carries -- the three have no reason to agree. Every default
    // reproduces the single-encoder wiring exactly.
    int audioBitrate = 160; // kbps
    if (const char *e = std::getenv("PULSAR_AUDIO_BITRATE"); e && *e) {
        int v = std::atoi(e);
        if (v >= 32 && v <= 512) audioBitrate = v;
        else blog(LOG_WARNING, "[pulsar-frontend-stub] PULSAR_AUDIO_BITRATE=%s rejected", e);
    }

    int audioTrackCount = 1;
    if (const char *e = std::getenv("PULSAR_AUDIO_TRACKS"); e && *e) {
        int v = std::atoi(e);
        if (v >= 1 && v <= static_cast<int>(MAX_AUDIO_MIXES)) audioTrackCount = v;
        else blog(LOG_WARNING, "[pulsar-frontend-stub] PULSAR_AUDIO_TRACKS=%s rejected "
                  "(1..%d); using 1", e, static_cast<int>(MAX_AUDIO_MIXES));
    }

    for (int track = 1; track <= audioTrackCount; ++track) {
        int trackBitrate = audioBitrate;
        const std::string bitrateVar = "PULSAR_AUDIO_BITRATE_" + std::to_string(track);
        if (const char *e = std::getenv(bitrateVar.c_str()); e && *e) {
            int v = std::atoi(e);
            if (v >= 32 && v <= 512) trackBitrate = v;
            else blog(LOG_WARNING, "[pulsar-frontend-stub] %s=%s rejected (32..512); "
                      "using %d", bitrateVar.c_str(), e, trackBitrate);
        }

        OBSDataAutoRelease aEncSettings = obs_data_create();
        obs_data_set_int(aEncSettings, "bitrate", trackBitrate);
        // Track 1 keeps the historical encoder name: it is the one logs and
        // clients already know, and renaming it would break the single-track
        // case for nothing.
        const std::string encName =
            (track == 1) ? "PulsarAudioEnc" : "PulsarAudioEnc" + std::to_string(track);
        obs_encoder_t *enc = obs_audio_encoder_create(
            "ffmpeg_aac", encName.c_str(), aEncSettings,
            static_cast<size_t>(track - 1), nullptr);
        if (!enc) {
            blog(LOG_WARNING, "[pulsar-frontend-stub] ffmpeg_aac encoder unavailable "
                 "for track %d", track);
            continue;
        }
        if (!programAudio) {
            blog(LOG_WARNING, "[pulsar-program-audio] failed to bind common route to audio track %d",
                 track);
        } else {
            obs_encoder_set_audio(enc, programAudio);
        }
        audioEncoders[track - 1] = enc;
        blog(LOG_INFO, "[pulsar-frontend-stub] aac track %d configured: %d kbps "
             "(mixer index %d)", track, trackBitrate, track - 1);
    }

    // Per-output track selection. The SLOT index is sequential -- OBS's own
    // convention: slot 0 carries the first selected track whatever its number.
    // Which track a slot carries is therefore only knowable from the encoder's
    // mixer index, never from the slot number; readers that infer it from the
    // slot repeat #157's mistake one level down.
    auto bindOutputTracks = [&](obs_output_t *output, const char *envName,
                                const char *label) {
        if (!output)
            return;
        const std::vector<int> tracks = parseAudioTrackList(envName, audioTrackCount);
        std::string applied;
        size_t slot = 0;
        for (int track : tracks) {
            obs_encoder_t *enc = audioEncoders[track - 1];
            if (!enc)
                continue;
            obs_output_set_audio_encoder(output, enc, slot++);
            if (!applied.empty())
                applied += ",";
            applied += std::to_string(track);
        }
        blog(LOG_INFO, "[pulsar-frontend-stub] %s audio tracks: %s", label,
             applied.empty() ? "none" : applied.c_str());
    };
    bindOutputTracks(recordOutput, "PULSAR_RECORD_AUDIO_TRACKS", "record");
    bindOutputTracks(streamOutput, "PULSAR_STREAM_AUDIO_TRACKS", "stream");
    // Same borrow as the video encoder above -- arming the buffer never adds an
    // encoder to the process.
    bindOutputTracks(replayOutput, "PULSAR_REPLAY_AUDIO_TRACKS", "replay");

    // Capture source. Phase 6 uses window_capture (Windows). The window
    // descriptor follows obs's "<title>:<class>:<exe>" format. PULSAR_CAPTURE_WINDOW
    // overrides the default; when unset we leave the source unbound (it
    // produces black frames but the pipeline still encodes / records).
    //
    // A traced #246 run sets PULSAR_TRACE_EXTERNAL_LANE_WORKLOAD=1.  In that
    // mode the probe owns the public A/B producers and the Default bootstrap
    // must not create a second WGC instance against the same target.  The
    // runtime session still declares the requested source kind below; source
    // registration/readiness is proven by the probe's A/B GetInput* and
    // screenshot checks, never by this declaration or by Default.
    const bool externalLaneWorkload =
        g_runtimeTelemetry.environmentTruthy(std::getenv("PULSAR_TRACE_EXTERNAL_LANE_WORKLOAD"));
    const char *captureWindowEnv = std::getenv("PULSAR_CAPTURE_WINDOW");
    const bool captureWindowRequested = captureWindowEnv && *captureWindowEnv;
    const bool cefWorkloadRequested =
        g_runtimeTelemetry.environmentTruthy(std::getenv("PULSAR_WORKLOAD_CEF"));
    if (externalLaneWorkload) {
        blog(LOG_INFO,
             "[pulsar-frontend-stub] external lane workload owner: suppressing Default PulsarCapture/PulsarCefWorkload; probe must bind public A/B producers");
    } else {
        OBSDataAutoRelease captureSettings = obs_data_create();
        if (captureWindowRequested) {
            obs_data_set_string(captureSettings, "window", captureWindowEnv);
            blog(LOG_INFO, "[pulsar-frontend-stub] window_capture target: %s", captureWindowEnv);
        } else {
            blog(LOG_INFO, "[pulsar-frontend-stub] window_capture has no target (set PULSAR_CAPTURE_WINDOW); will produce black frames");
        }
        obs_data_set_int(captureSettings, "method", 2);    // WGC -- works against most modern apps incl. CEF/Electron
        obs_data_set_bool(captureSettings, "cursor", true);
        obs_data_set_bool(captureSettings, "client_area", true);
        captureSource = obs_source_create("window_capture", "PulsarCapture", captureSettings, nullptr);
        if (!captureSource) {
            blog(LOG_WARNING, "[pulsar-frontend-stub] window_capture source unavailable");
        } else if (scene) {
            captureItem = obs_scene_add(scene, captureSource);
        }
    }

    // #246 resource campaigns can request a real CEF workload in addition to
    // the WGC/window_capture source.  The source is intentionally created by
    // the frontend at boot (after obs_load_all_modules) so `browser_source`
    // resolves to the bundled pulsar-browser/CEF implementation rather than a
    // metadata-only flag.  It remains an ordinary visible scene item, which
    // keeps BrowserSource's CEF lifecycle and render/tick costs in the same
    // Program composition observed by the raw/DirectShow/encoded-output boundaries.
    if (cefWorkloadRequested && !externalLaneWorkload) {
        obs_video_info cefVideo = {};
        obs_get_video_info(&cefVideo);
        const int cefWidth = cefVideo.base_width > 0 ? cefVideo.base_width : 1920;
        const int cefHeight = cefVideo.base_height > 0 ? cefVideo.base_height : 1080;
        const int cefFps = cefVideo.fps_num > 0 && cefVideo.fps_den > 0
                               ? static_cast<int>((std::max)(
                                     uint32_t{1}, static_cast<uint32_t>(cefVideo.fps_num / cefVideo.fps_den)))
                               : 60;
        const char *configuredUrl = std::getenv("PULSAR_CEF_URL");
        // Keep an explicit deterministic fallback for direct runtime launches.
        // The probe normally supplies its ephemeral localhost URL, but a
        // missing override must never silently turn a trace into a network
        // availability test against obsproject.com.
        const char *cefUrl = configuredUrl && *configuredUrl ? configuredUrl :
                             "data:text/html,%3Chtml%3E%3Cbody%20style%3D%22margin%3A0%3Bbackground%3A%23132238%3Bcolor%3A%23f6fbff%3Bfont-family%3AArial%22%3E%3Ch1%3EPULSAR%20CEF%20%23246%3C%2Fh1%3E%3C%2Fbody%3E%3C%2Fhtml%3E";
        OBSDataAutoRelease cefSettings = obs_data_create();
        obs_data_set_string(cefSettings, "url", cefUrl);
        obs_data_set_bool(cefSettings, "is_local_file", false);
        obs_data_set_int(cefSettings, "width", cefWidth);
        obs_data_set_int(cefSettings, "height", cefHeight);
        obs_data_set_bool(cefSettings, "fps_custom", true);
        obs_data_set_int(cefSettings, "fps", cefFps);
        obs_data_set_bool(cefSettings, "shutdown", false);
        obs_data_set_bool(cefSettings, "restart_when_active", false);
        obs_data_set_bool(cefSettings, "reroute_audio", false);
        // Browser content is a telemetry workload, not an OBS control plane.
        // Keep the same None policy as pulsar-scene-source/obs-websocket.
        obs_data_set_int(cefSettings, "webpage_control_level", 0);
        cefSource = obs_source_create("browser_source", "PulsarCefWorkload", cefSettings, nullptr);
        if (!cefSource) {
            blog(LOG_WARNING, "[pulsar-frontend-stub] browser_source CEF workload unavailable");
        } else if (scene) {
            cefItem = obs_scene_add(scene, cefSource);
            if (!cefItem) {
                blog(LOG_WARNING, "[pulsar-frontend-stub] browser_source CEF scene item unavailable");
                obs_source_release(cefSource);
                cefSource = nullptr;
            } else {
                blog(LOG_INFO, "[pulsar-frontend-stub] browser_source CEF workload bound url=%s %dx%d@%dfps",
                     cefUrl, cefWidth, cefHeight, cefFps);
            }
        } else {
            blog(LOG_WARNING, "[pulsar-frontend-stub] browser_source CEF workload has no bootstrap scene");
            obs_source_release(cefSource);
            cefSource = nullptr;
        }
    } else if (cefWorkloadRequested) {
        blog(LOG_INFO,
             "[pulsar-frontend-stub] external lane workload owner: suppressing Default PulsarCefWorkload; probe must bind public A/B browser_source producers");
    }

    // Resolve the dual-lane capability once, before binding the output views.
    // The legacy disable switch is consumed here (rather than only being set
    // by the probe), so a reference run genuinely retains one canvas and an
    // operator can perform a deterministic compatibility-path boot.
    const DualLaneActivation activation = resolve_dual_lane_activation();
    const bool resourceReference = PulsarRuntimeTelemetry::resourceReferenceRequested();
    const DualLaneRollbackSetting rollbackSetting = resolve_dual_lane_rollback_after_takes();
    // A reference resource phase is an explicit compatibility measurement,
    // so its effective topology is single-canvas even if a caller forgot the
    // legacy disable variable.  Keep the effective decision in the startup
    // log so evidence cannot mistake an enabled request for an enabled path.
    dualLaneEnabled = activation.enabled && !resourceReference && rollbackSetting.valid;
    const char *activationReason = resourceReference
                                       ? "resource-reference"
                                       : (!rollbackSetting.valid ? rollbackSetting.reason : activation.reason);
    rollbackAfterTakes = rollbackSetting.takes;
    blog(LOG_INFO,
         "[pulsar-dual-lane] activation=%s source=%s rollback_after_takes=%llu "
         "flag_resolved_at=setup",
         dualLaneEnabled ? "enabled" : "disabled", activationReason,
         static_cast<unsigned long long>(rollbackAfterTakes));

    // Build the two independent hot roots only after the bootstrap scene has
    // its initial producer.  ProgramView is the existing main canvas/video_t;
    // PreviewView is the single auxiliary mix.  Production encoder/Program
    // return stay on the main video, while Preview return uses the auxiliary.
    // Resource reference campaigns intentionally measure the legacy single
    // canvas before the dual-lane topology is allocated.  This is opt-in and
    // only reachable with an explicit trace resource mode; normal runtime
    // starts retain the dual-lane setup and Cut semantics.
    if (!dualLaneEnabled || resourceReference) {
        if (resourceReference)
            blog(LOG_INFO, "[pulsar-runtime-telemetry] reference resource mode: retaining legacy single canvas");
        else
            blog(LOG_INFO, "[pulsar-dual-lane] compatibility single-canvas path selected");
    } else if (!setupDualLane(scene)) {
        blog(LOG_WARNING, "[pulsar-frontend-stub] dual-lane setup unavailable; keeping legacy canvas path");
    }

    // Bind the requested encoder exactly once for both the hot dual-lane and
    // compatibility/reference paths.  The latter uses libobs's main video_t
    // directly because it intentionally has no auxiliary PreviewView.
    // obs_encoder_set_video rejects active/initialized encoders; no Cut path
    // can reach this setup-only binding.
    if (videoEncoder) {
        video_t *encoderVideo = programVideo ? programVideo : obs_get_video();
        if (encoderVideo) {
            obs_encoder_set_video(videoEncoder, encoderVideo);
            blog(LOG_INFO, "[pulsar-dual-lane] encoder video_t bound once to ProgramView");
        } else {
            blog(LOG_ERROR, "[pulsar-dual-lane] encoder video_t binding skipped: no Program video");
        }
    }
    obs_scene_release(scene);
    scene = nullptr;

    // ---- Phase 9: audio sources on the main mixer ----
    // libobs has 6 main "channels" addressed via obs_set_output_source.
    // Channel 0 is video (already bound to the Default scene above).
    // Channels 1-5 are audio inputs that the audio encoder mixes
    // together before encoding. We follow the OBS Studio convention:
    //   1 -> Desktop Audio (system playback loopback)
    //   2 -> Process Audio (per-process loopback, Phase 9 optional)
    //   3 -> Microphone
    //
    // device_id="" means "default device" -- libobs/win-wasapi resolves
    // it at runtime against the system's current default endpoint.
    // Operators can pin a specific device via env vars.
    OBSDataAutoRelease desktopSettings = obs_data_create();
    if (const char *id = std::getenv("PULSAR_DESKTOP_AUDIO_DEVICE_ID"); id && *id)
        obs_data_set_string(desktopSettings, "device_id", id);
    else
        obs_data_set_string(desktopSettings, "device_id", "default");
    // Keep the live mixer clock on libobs' fixed audio timeline.  Device
    // timing is useful for monitoring, but it can make the WASAPI capture
    // source wait for endpoint-clock alignment before handing audio to the
    // common Program route.  Pulsar's encoder/interleaver already has an
    // explicit bounded audio policy, so opt out before source creation.
    obs_data_set_bool(desktopSettings, "use_device_timing", false);
    blog(LOG_INFO, "[pulsar-frontend-stub] desktop audio configured use_device_timing=false");
    desktopAudioSource = obs_source_create("wasapi_output_capture", "PulsarDesktopAudio",
                                            desktopSettings, nullptr);
    if (desktopAudioSource) {
        obs_set_output_source(1, desktopAudioSource);
        blog(LOG_INFO, "[pulsar-frontend-stub] desktop audio bound to channel 1");
    } else {
        blog(LOG_WARNING, "[pulsar-frontend-stub] wasapi_output_capture unavailable");
    }

    // Microphone: opt-in via PULSAR_MIC_DEVICE_ID. Unlike desktop
    // playback (which has a default endpoint everywhere), input devices
    // are absent on CI runners, on servers, and on plenty of dev
    // workstations. Auto-creating the source against `device_id=default`
    // when no mic exists triggers a "Device '' invalidated. Retrying"
    // spam every ~2 s for the lifetime of the process. Make the user
    // ask for it explicitly. Pass `default` to fall back to the system's
    // current default input endpoint.
    if (const char *id = std::getenv("PULSAR_MIC_DEVICE_ID"); id && *id) {
        OBSDataAutoRelease micSettings = obs_data_create();
        obs_data_set_string(micSettings, "device_id", id);
        micAudioSource = obs_source_create("wasapi_input_capture", "PulsarMic",
                                            micSettings, nullptr);
        if (micAudioSource) {
            obs_set_output_source(3, micAudioSource);
            blog(LOG_INFO, "[pulsar-frontend-stub] mic bound to channel 3 (device_id=%s)", id);
        } else {
            blog(LOG_WARNING, "[pulsar-frontend-stub] wasapi_input_capture unavailable");
        }
    } else {
        blog(LOG_INFO, "[pulsar-frontend-stub] mic skipped (PULSAR_MIC_DEVICE_ID unset)");
    }

    // Process audio (per-process loopback). Requires Windows 10 build 19041+
    // and the wasapi_process_output_capture source ID, which only exists in
    // recent win-wasapi builds. Skip if env var unset OR source unavailable
    // (older Windows or older win-wasapi).
    if (const char *exe = std::getenv("PULSAR_PROCESS_AUDIO_NAME"); exe && *exe) {
        OBSDataAutoRelease procSettings = obs_data_create();
        // win-wasapi process loopback settings: priority=0 means match by
        // executable; "window" is the value field even though it carries
        // an exe path -- inherited from the window-capture-style API in
        // upstream win-capture.
        obs_data_set_int(procSettings, "priority", 0);
        obs_data_set_string(procSettings, "window", exe);
        processAudioSource = obs_source_create("wasapi_process_output_capture",
                                                "PulsarProcessAudio",
                                                procSettings, nullptr);
        if (processAudioSource) {
            obs_set_output_source(2, processAudioSource);
            blog(LOG_INFO, "[pulsar-frontend-stub] process audio (%s) bound to channel 2", exe);
        } else {
            blog(LOG_WARNING, "[pulsar-frontend-stub] wasapi_process_output_capture not available "
                              "(needs Win10 19041+ and recent win-wasapi); process audio skipped");
        }
    } else {
        blog(LOG_INFO, "[pulsar-frontend-stub] PULSAR_PROCESS_AUDIO_NAME unset; process audio not wired");
    }

    // Recording directory resolution. PULSAR_RECORD_DIR overrides the default,
    // which is "<cwd>/recordings". The directory is created lazily on the
    // first recording_start so we don't fail setup if the FS is read-only.
    if (const char *envDir = std::getenv("PULSAR_RECORD_DIR"); envDir && *envDir) {
        recordDirectory = envDir;
    } else {
        recordDirectory = (std::filesystem::current_path() / "recordings").string();
    }
    blog(LOG_INFO, "[pulsar-frontend-stub] recordings will land under: %s", recordDirectory.c_str());

    // Recording container selection (issue #166). PULSAR_RECORD_CONTAINER
    // overrides the default "mp4"; the only other admitted value is "mkv"
    // (comparison is case-insensitive). Resolved ONCE here -- like every
    // other PULSAR_* knob -- and applied to BOTH extension sites in
    // obs_frontend_recording_start(): the initial file's suffix (path) and
    // the "extension" muxer setting that governs every file split_file
    // produces afterwards. Treating them separately is exactly the failure
    // this issue exists to close: a mixed-container archive whose first file
    // is .mkv and whose split files stay .mp4.
    if (const char *envContainer = std::getenv("PULSAR_RECORD_CONTAINER"); envContainer && *envContainer) {
        std::string lower;
        lower.reserve(std::strlen(envContainer));
        for (const char *p = envContainer; *p; ++p)
            lower += static_cast<char>(std::tolower(static_cast<unsigned char>(*p)));
        if (lower == "mp4" || lower == "mkv") {
            recordContainer = lower;
        } else {
            blog(LOG_WARNING, "[pulsar-frontend-stub] PULSAR_RECORD_CONTAINER=%s rejected "
                 "(mp4|mkv); using %s", envContainer, recordContainer.c_str());
        }
    }
    blog(LOG_INFO, "[pulsar-frontend-stub] recording container: %s", recordContainer.c_str());
    // Seed the container into recordOutput's own settings at boot, not only at
    // the first recording_start, so GetCapabilities (pulsar-multi-stream) can
    // read the effective value off the live output before any recording has
    // ever run -- record_container is boot-fixed, so the truth must be
    // established here, not lazily on first use.
    if (recordOutput) {
        OBSDataAutoRelease containerSeed = obs_data_create();
        obs_data_set_string(containerSeed, "extension", recordContainer.c_str());
        obs_output_update(recordOutput, containerSeed);
    }

    // ---- ADR Prism 024 §3.1: replay-buffer output settings ----
    // The output was created with null settings, so every key below fell back
    // to replay_buffer_defaults (obs-ffmpeg-mux.c:1250-1257) -- including an
    // EMPTY "directory", which makes generate_filename() build a path rooted at
    // "/" and the save fail. Posting real settings is the other half of the
    // wiring, next to the borrowed encoders above.
    //
    // Bounds mirror the Prism-side registry keys (replay.durationSec [10,300],
    // replay.maxSizeMb): out-of-range values are rejected with a warning and the
    // default kept, exactly like every other PULSAR_* knob.
    if (const char *e = std::getenv("PULSAR_REPLAY_MAX_TIME_SEC"); e && *e) {
        int v = std::atoi(e);
        if (v >= 10 && v <= 300) replayMaxTimeSec = v;
        else blog(LOG_WARNING, "[pulsar-frontend-stub] PULSAR_REPLAY_MAX_TIME_SEC=%s "
                  "rejected (10..300); using %d", e, replayMaxTimeSec);
    }
    if (const char *e = std::getenv("PULSAR_REPLAY_MAX_SIZE_MB"); e && *e) {
        int v = std::atoi(e);
        if (v >= 16 && v <= 8192) replayMaxSizeMb = v;
        else blog(LOG_WARNING, "[pulsar-frontend-stub] PULSAR_REPLAY_MAX_SIZE_MB=%s "
                  "rejected (16..8192); using %d", e, replayMaxSizeMb);
    }
    if (replayOutput) {
        OBSDataAutoRelease replaySettings = obs_data_create();
        obs_data_set_string(replaySettings, "directory", recordDirectory.c_str());
        // "format" is the filename template consumed by
        // os_generate_formatted_filename; the "pulsar-" prefix keeps replays
        // recognisable next to the recordings written by recording_start.
        obs_data_set_string(replaySettings, "format", "pulsar-replay-%CCYY%MM%DD-%hh%mm%ss");
        obs_data_set_string(replaySettings, "extension", "mp4");
        obs_data_set_bool(replaySettings, "allow_spaces", false);
        obs_data_set_int(replaySettings, "max_time_sec", replayMaxTimeSec);
        obs_data_set_int(replaySettings, "max_size_mb", replayMaxSizeMb);
        obs_output_update(replayOutput, replaySettings);
        blog(LOG_INFO, "[pulsar-frontend-stub] replay buffer configured: dir=%s "
             "max_time_sec=%d max_size_mb=%d", recordDirectory.c_str(),
             replayMaxTimeSec, replayMaxSizeMb);
    }

    // #246: install the producer callbacks only when a runtime trace was
    // explicitly requested.  The raw callback is attached to libobs's main
    // Program mix (the same video_t used by the encoder); the packet callback
    // is attached to the configured stream output's encoder callback.  It is a
    // pre-network boundary; RTMP receiver/decoder timing remains external and
    // is never inferred from this record.
    obs_video_info telemetryVideo = {};
    if (obs_get_video_info(&telemetryVideo)) {
        // In external-owner mode these are declarations of the producer kinds
        // that the probe will bind after PULSAR_READY, not proof that Default
        // owns a source.  The probe's A/B registration, settings and pixels
        // are the readiness evidence; retaining the declaration here keeps the
        // session correlated to the requested workload without adding a third
        // producer pair.
        const bool wgcWorkload = externalLaneWorkload ? captureWindowRequested :
                                  captureItem && captureWindowRequested;
        const bool cefWorkload = externalLaneWorkload ? cefWorkloadRequested :
                                 cefItem && cefSource;
        const bool wgcSourceBound = externalLaneWorkload ? captureWindowRequested :
                                    captureItem != nullptr;
        g_runtimeTelemetry.initialize(encoderFamily.c_str(), telemetryVideo, wgcWorkload, cefWorkload,
                                      wgcSourceBound, videoEncoder, streamOutput, programReturnOutput,
                                      previewReturnOutput,
                                      programVideo ? programVideo : obs_get_video(),
                                      previewVideo, currentScene, previewScene);
        if (g_runtimeTelemetry.enabled()) {
            runtimeTelemetryVideo = programVideo ? programVideo : obs_get_video();
            runtimeTelemetryRawConnected = obs_video_add_borrowed_callback(
                runtimeTelemetryVideo, pulsar_runtime_raw_video_callback, nullptr);
            if (!runtimeTelemetryRawConnected)
                blog(LOG_ERROR, "[pulsar-runtime-telemetry] failed to install borrowed ProgramView/raw callback");
            if (streamOutput)
                obs_output_add_packet_callback(streamOutput, pulsar_runtime_packet_callback, nullptr);
            blog(LOG_INFO, "[pulsar-runtime-telemetry] borrowed ProgramView/raw and encoded-output callbacks installed");

            // A trace campaign may opt into the real ProgramReturn producer;
            // ordinary headless starts keep this output dormant.  The
            // DirectShow filter remains an independent consumer and must be
            // started by the campaign/host, so this only creates the shared
            // queue at the producer boundary.
            if (programReturnOutput && g_runtimeTelemetry.environmentTruthy(std::getenv("PULSAR_PROGRAM_RETURN_AUTOSTART"))) {
                if (!obs_output_start(programReturnOutput))
                    blog(LOG_WARNING, "[pulsar-runtime-telemetry] ProgramReturn autostart declined: %s",
                         obs_output_get_last_error(programReturnOutput));
                else
                    blog(LOG_INFO, "[pulsar-runtime-telemetry] ProgramReturn producer autostarted");
            }
        }
    }

    return true;
}

void PulsarFrontendAPI::clear_libobs_scene_data()
{
    // Keep one owning reference per enumerated object while the source graph
    // is being changed.  obs_enum_* holds libobs's enumeration lock only for
    // the callback; retaining the refs here makes the subsequent remove and
    // prune phases independent of that lock and of source_remove callbacks.
    std::vector<obs_source_t *> sceneRefs;
    std::vector<obs_source_t *> sourceRefs;
    const auto collect_ref = [](void *param, obs_source_t *source) {
        auto *refs = static_cast<std::vector<obs_source_t *> *>(param);
        if (obs_source_t *ref = obs_source_get_ref(source))
            refs->push_back(ref);
        return true;
    };
    obs_enum_scenes(collect_ref, &sceneRefs);
    obs_enum_sources(collect_ref, &sourceRefs);

    std::set<obs_source_t *> sceneSet(sceneRefs.begin(), sceneRefs.end());
    std::set<obs_source_t *> removed;
    blog(LOG_INFO,
         "PULSAR_FRONTEND_CLEANUP event=source_graph_begin scenes=%llu sources=%llu",
         static_cast<unsigned long long>(sceneRefs.size()),
         static_cast<unsigned long long>(sourceRefs.size()));

    // Match OBSBasic's dependency order: scene roots first, then the rest of
    // the global source registry.  A source can appear in both enumerations;
    // the identity set makes the remove operation exactly once per object.
    for (obs_source_t *scene : sceneRefs) {
        if (scene && removed.insert(scene).second)
            obs_source_remove(scene);
    }
    for (obs_source_t *source : sourceRefs) {
        if (source && removed.insert(source).second)
            obs_source_remove(source);
    }

    // obs_source_remove() only marks a source and emits source_remove.  Scene
    // items own the remaining refs, so pruning must happen after enumeration
    // has returned and after all roots have been marked removed.
    for (obs_source_t *scene : sceneRefs) {
        if (sceneSet.find(scene) == sceneSet.end())
            continue;
        if (obs_scene_t *sc = obs_scene_from_source(scene))
            obs_scene_prune_sources(sc);
    }

    // Release the enumeration refs only after the second-phase prune.  The
    // frontend-owned refs are released later by teardown(); the final scan
    // must not run until those refs, views, and vectors have also gone away.
    for (obs_source_t *source : sourceRefs)
        if (source)
            obs_source_release(source);
    for (obs_source_t *scene : sceneRefs)
        if (scene)
            obs_source_release(scene);
}

void PulsarFrontendAPI::verify_libobs_scene_data_drained()
{
    // This is intentionally a separate phase from clear_libobs_scene_data().
    // That phase still runs with source_remove connected and releases only
    // its enumeration refs.  This phase is called after teardown has released
    // every frontend member ref, so an object retained in the registry is a
    // real orphan rather than a legitimate setup owner.
    blog(LOG_INFO, "PULSAR_FRONTEND_CLEANUP event=source_graph_verify_begin");

    // Destruction is deferred by libobs.  Follow OBSBasic's contract and wait
    // until the queue reports empty.  This is a synchronous upstream
    // primitive; the headless parent/harness supervises the overall process
    // lifetime rather than pretending that a loop count is a wall-clock bound.
    int pass = 0;
    while (obs_wait_for_destroy_queue())
        ++pass;

    std::size_t orphan_scenes = 0;
    std::size_t orphan_sources = 0;
    obs_enum_scenes(
        [](void *param, obs_source_t *) {
            ++*static_cast<std::size_t *>(param);
            return true;
        },
        &orphan_scenes);
    obs_enum_sources(
        [](void *param, obs_source_t *) {
            ++*static_cast<std::size_t *>(param);
            return true;
        },
        &orphan_sources);
    if (orphan_scenes != 0 || orphan_sources != 0) {
        g_frontend_cleanup_succeeded.store(false, std::memory_order_release);
        blog(LOG_ERROR,
             "PULSAR_FRONTEND_CLEANUP event=source_graph_orphans scenes=%llu sources=%llu",
             static_cast<unsigned long long>(orphan_scenes),
             static_cast<unsigned long long>(orphan_sources));
    } else {
        blog(LOG_INFO,
             "PULSAR_FRONTEND_CLEANUP event=source_graph_drained destroy_passes=%d",
             pass);
    }
}

void PulsarFrontendAPI::teardown()
{
    auto release_source_vec = [](std::vector<obs_source_t *> &v) {
        for (obs_source_t *s : v)
            if (s)
                obs_source_release(s);
        v.clear();
    };

    // Stop admitting new supported WebSocket mutations and drain one which
    // is already in the dispatch path before any frontend-owned libobs state
    // is released. The bridge contains no frontend pointer, so a late proc
    // lookup cannot dereference this object after teardown.
    g_dualLaneControlBridge.deactivate();
    if (dualLaneTransitionsEnabled)
        obs_remove_tick_callback(&OnDualLaneTick, this);
    {
        std::lock_guard<std::mutex> lock(dualLaneMutex);
        dualLaneTransitionFinalPending = false;
        dualLaneTransitionAbortPending = false;
        if (dualLaneTransition.active())
            dualLaneTransition.abort("shutdown");
    }
    if (previewVideo)
        obs_video_remove_borrowed_callback(previewVideo, OnSceneSwitchPreviewVideoFrame, this);

    // Stop callbacks before releasing their output/video owners.  The global
    // telemetry proc remains installed but is disabled, so a late module call
    // is harmless and does not retain this frontend object.
    if (g_runtimeTelemetry.enabled()) {
        if (runtimeTelemetryRawConnected && runtimeTelemetryVideo)
            obs_video_remove_borrowed_callback(runtimeTelemetryVideo,
                                               pulsar_runtime_raw_video_callback, nullptr);
        runtimeTelemetryRawConnected = false;
        runtimeTelemetryVideo = nullptr;
        if (streamOutput)
            obs_output_remove_packet_callback(streamOutput, pulsar_runtime_packet_callback, nullptr);
    }
    g_runtimeTelemetry.shutdown();

    // Drain active outputs gracefully before release. A user who Ctrl+C's
    // mid-recording would otherwise hit obs_output_release on a live
    // output, which races with the muxer thread still writing frames.
    stop_output_and_wait(streamOutput, "stream");
    stop_output_and_wait(recordOutput, "record");
    stop_output_and_wait(replayOutput, "replay");
    stop_output_and_wait(virtualcamOutput, "virtualcam");
    stop_output_and_wait(programReturnOutput, "program-return");
    stop_output_and_wait(previewReturnOutput, "preview-return");

    // Prevent a graphics-thread callback from dereferencing this frontend
    // while its stable views and role refs are being torn down.
    {
        std::lock_guard<std::mutex> lk(dualLaneMutex);
        dualLaneOperational = false;
        dualLaneReady = false;
        dualLaneCutPending.store(false);
    }
    obs_view_cancel_atomic_swap();

    // Unbind every main mixer channel (video on 0, audio on 1/2/3) before
    // releasing the underlying sources. Otherwise libobs keeps refs past
    // teardown and logs leaked-source warnings at obs_shutdown.
    obs_set_output_source(0, nullptr);
    obs_set_output_source(1, nullptr);
    obs_set_output_source(2, nullptr);
    obs_set_output_source(3, nullptr);

    // Clear every view root before removing the libobs source graph.  The
    // main view is a libobs-owned canvas, while the auxiliary views are
    // frontend-owned; both can otherwise retain a scene after the source
    // enumeration below and make the orphan check meaningless.
    if (programView)
        obs_view_set_source(programView, 0, nullptr);
    if (previewView)
        obs_view_set_source(previewView, 0, nullptr);
    if (vcamView)
        obs_view_set_source(vcamView, 0, nullptr);

    // M10: output 0 is now a transition holding the scene (and possibly a
    // stinger media source as an active child). Clear each transition's held
    // sources before releasing so libobs drops those child refs cleanly and
    // does not warn about a leaked scene/media source at obs_shutdown.
    for (obs_source_t *t : transitions)
        if (t)
            obs_transition_clear(t);

    // Mirror OBSBasic::ClearSceneData: remove the complete libobs source
    // graph while the frontend's source_remove callback is still installed,
    // prune scene items in a second phase, drain deferred destruction, then
    // check for orphans.  This is deliberately before dropping our setup refs
    // or disconnecting the callback; otherwise sources created by WebSocket
    // remain owned by libobs until obs_free_data(), where the CEF/plugin
    // teardown order is already too late to be safe.
    clear_libobs_scene_data();

    // The source graph is now drained; no callback may walk this object while
    // its remaining setup refs are released below.
    if (signal_handler_t *globalSh = obs_get_signal_handler())
        signal_handler_disconnect(globalSh, "source_remove", OnSourceRemove, this);

    if (streamOutput) {
        signal_handler_t *sh = obs_output_get_signal_handler(streamOutput);
        if (sh) {
            signal_handler_disconnect(sh, "start", OnStreamStart, this);
            signal_handler_disconnect(sh, "stop", OnStreamStop, this);
        }
        obs_output_release(streamOutput);
        streamOutput = nullptr;
    }
    if (recordOutput) {
        signal_handler_t *sh = obs_output_get_signal_handler(recordOutput);
        if (sh) {
            signal_handler_disconnect(sh, "start", OnRecordStart, this);
            signal_handler_disconnect(sh, "stop", OnRecordStop, this);
            signal_handler_disconnect(sh, "pause", OnRecordPause, this);
            signal_handler_disconnect(sh, "unpause", OnRecordUnpause, this);
        }
        obs_output_release(recordOutput);
        recordOutput = nullptr;
    }
    if (replayOutput) {
        signal_handler_t *sh = obs_output_get_signal_handler(replayOutput);
        if (sh) {
            signal_handler_disconnect(sh, "start", OnReplayStart, this);
            signal_handler_disconnect(sh, "stop", OnReplayStop, this);
            signal_handler_disconnect(sh, "saved", OnReplaySaved, this);
        }
        obs_output_release(replayOutput);
        replayOutput = nullptr;
    }
    if (virtualcamOutput) {
        signal_handler_t *sh = obs_output_get_signal_handler(virtualcamOutput);
        if (sh) {
            signal_handler_disconnect(sh, "start", OnVCamStart, this);
            signal_handler_disconnect(sh, "stop", OnVCamStop, this);
        }
        obs_output_release(virtualcamOutput);
        virtualcamOutput = nullptr;
    }
    if (programReturnOutput) {
        obs_output_release(programReturnOutput);
        programReturnOutput = nullptr;
    }
    if (previewReturnOutput) {
        obs_output_release(previewReturnOutput);
        previewReturnOutput = nullptr;
    }
    if (programView) {
        // ProgramView aliases libobs's main canvas.  Channel 0 was cleared
        // above, but the main view/video remains owned by libobs for the rest
        // of the OBS context lifetime; never remove or destroy it here.
        programView = nullptr;
        programVideo = nullptr;
    }
    if (previewView) {
        obs_view_remove(previewView);
        obs_view_set_source(previewView, 0, nullptr);
        obs_view_destroy(previewView);
        previewView = nullptr;
        previewVideo = nullptr;
    }
    if (vcamView) {
        obs_view_remove(vcamView);
        obs_view_set_source(vcamView, 0, nullptr);
        obs_view_destroy(vcamView);
        vcamView = nullptr;
        vcamVideo = nullptr;
    }
    if (streamService) {
        obs_service_release(streamService);
        streamService = nullptr;
    }

    // Encoders + capture source. Encoders carry refs from outputs which
    // were already released above, so this just drops the setup-time ref.
    if (videoEncoder) {
        obs_encoder_release(videoEncoder);
        videoEncoder = nullptr;
    }
    for (obs_encoder_t *&enc : audioEncoders) {
        if (!enc)
            continue;
        obs_encoder_release(enc);
        enc = nullptr;
    }
    if (captureSource) {
        // The scene owns the sceneitem, which holds its own ref to the
        // source. Release the setup-time ref; scene teardown clears the rest.
        obs_source_release(captureSource);
        captureSource = nullptr;
        captureItem = nullptr;
    }
    if (cefSource) {
        // The scene owns the sceneitem, which holds its own ref to the CEF
        // source. Release only the setup-time ref here.
        obs_source_release(cefSource);
        cefSource = nullptr;
        cefItem = nullptr;
    }
    if (desktopAudioSource) {
        obs_source_release(desktopAudioSource);
        desktopAudioSource = nullptr;
    }
    if (processAudioSource) {
        obs_source_release(processAudioSource);
        processAudioSource = nullptr;
    }
    if (micAudioSource) {
        obs_source_release(micAudioSource);
        micAudioSource = nullptr;
    }
    // Non-owning route identity; libobs owns the audio bus.  Clear it after
    // every encoder/output has released its reference so no late frontend
    // callback can mistake a torn-down bus for a live Program route.
    programAudio = nullptr;

    for (obs_source_t *&lane : laneSources) {
        if (!lane)
            continue;
        obs_source_release(lane);
        lane = nullptr;
    }
    laneItems[0] = nullptr;
    laneItems[1] = nullptr;

    if (currentTransition) {
        obs_source_release(currentTransition);
        currentTransition = nullptr;
    }
    if (currentScene) {
        obs_source_release(currentScene);
        currentScene = nullptr;
    }
    if (previewScene) {
        obs_source_release(previewScene);
        previewScene = nullptr;
    }
    if (programSelection) {
        obs_source_release(programSelection);
        programSelection = nullptr;
    }
    if (previewSelection) {
        obs_source_release(previewSelection);
        previewSelection = nullptr;
    }
    release_source_vec(scenes);
    release_source_vec(transitions);
    dualLaneStingerTransition = nullptr;
    dualLaneTransitionsEnabled = false;

    // Only now are all frontend-owned source refs gone.  The orphan scan is
    // deliberately after this point; scanning earlier would report the
    // sources that teardown still legitimately owns as leaks.
    verify_libobs_scene_data_drained();

    std::lock_guard<std::mutex> lk(callbacksMutex);
    eventCallbacks.clear();
    saveCallbacks.clear();
    preloadCallbacks.clear();
}

void PulsarFrontendAPI::emit(obs_frontend_event event)
{
    on_event(event);
}

void PulsarFrontendAPI::obs_frontend_get_scenes(struct obs_frontend_source_list *sources)
{
    // ADR Prism 026 §3.1 / issue #119 -- NO MIRROR of a state libobs owns.
    // This used to iterate the internal `scenes` vector, which is only ever
    // appended to at setup(). A scene created by any other path (an
    // obs-websocket Scenes/CreateScene request, a plugin, a collection load)
    // went straight to libobs and was therefore INVISIBLE to GetSceneList,
    // even though CreateScene returned a real sceneUuid.
    //
    // libobs is the truth: obs_scene_create() registers the scene on the main
    // canvas (obs-scene.c:1794-1797), which is exactly what obs_enum_scenes
    // walks (obs.c:1888-1891). Filtered like upstream's scene list: groups are
    // OBS_SOURCE_TYPE_SCENE sources too (group_info, obs-scene.c:1762-1764) and
    // live on the same canvas, but upstream never lists them as scenes --
    // same filter as Utils::Obs::ArrayHelper::GetCanvasGroupList.
    obs_enum_scenes(
        [](void *param, obs_source_t *scene) {
            auto *out = static_cast<struct obs_frontend_source_list *>(param);
            if (obs_source_is_group(scene))
                return true; // groups are not scenes for the frontend API
            obs_source_t *ref = obs_source_get_ref(scene);
            if (ref)
                da_push_back(out->sources, &ref);
            return true;
        },
        sources);
}

obs_source_t *PulsarFrontendAPI::obs_frontend_get_current_scene(void)
{
    std::lock_guard<std::mutex> lk(dualLaneMutex);
    obs_source_t *scene = dualLaneReady ? programSelection : currentScene;
    return scene ? obs_source_get_ref(scene) : nullptr;
}

void PulsarFrontendAPI::obs_frontend_set_current_scene(obs_source_t *scene)
{
    if (!scene)
        return;
    bool dualLaneTake = false;
    bool dualLaneDirect = false;
    {
        std::lock_guard<std::mutex> lk(dualLaneMutex);
        if (dualLaneReady && !dualLaneOperational) {
            blog(LOG_WARNING,
                 "[pulsar-dual-lane] Program mutation rejected: rollback freeze is active");
            return;
        }
        if (dualLaneReady && dualLaneCutPending.load()) {
            blog(LOG_WARNING, "[pulsar-dual-lane] Program mutation rejected while Take is pending");
            return;
        }
        if (dualLaneReady && g_runtimeTelemetry.integrityFaulted()) {
            blog(LOG_ERROR,
                 "[pulsar-dual-lane] scene switch rejected: runtime telemetry integrity fail-stop is degraded");
            return;
        }
        if (dualLaneReady && programSelection == scene)
            return;
        if (!dualLaneReady && currentScene == scene)
            return;
        if (dualLaneReady) {
            // The websocket studio-mode trigger calls this method with the
            // frozen logical Preview selection.  The Cut itself swaps the two
            // fixed physical roots; public scene identity is swapped in the
            // graphics-thread callback at the same frame boundary.
            dualLaneTake = studioMode && scene == previewSelection;
            dualLaneDirect = !studioMode && !dualLaneTake;
            if (dualLaneTake) {
                // queueDualLaneCut takes the same mutex after this scope.
            } else if (!dualLaneDirect) {
                blog(LOG_WARNING, "[pulsar-dual-lane] scene switch rejected: select Preview before Take");
                return;
            } else if (scene == previewSelection) {
                // Direct mode has no atomic role swap.  Reusing the Preview
                // source would put one stateful producer in both physical
                // lanes, so require the studio-mode Take path for this case.
                blog(LOG_WARNING,
                     "[pulsar-dual-lane] direct scene switch rejected: scene aliases Preview; use Take");
                return;
            }
        }
    }

    if (dualLaneTake) {
        // Re-checks the role and pending guard under the role mutex, then
        // queues both routes together.  There is deliberately no fallback to
        // obs_set_output_source here: that would bypass the frame boundary.
        if (queueDualLaneCut(scene))
            return;
        return;
    }

    if (dualLaneDirect) {
        {
            std::lock_guard<std::mutex> lk(dualLaneMutex);
            if (!dualLaneInvariantLocked("direct-before"))
                return;
            if (!replaceLaneCompositionLocked(onAirLane, scene))
                return;
            if (programSelection)
                obs_source_release(programSelection);
            programSelection = obs_source_get_ref(scene);
            if (!dualLaneInvariantLocked("direct-after"))
                return;
        }
        // ProgramView is the libobs main view, so its channel 0 already holds
        // the stable physical OnAir root.  Mutating the lane composition does
        // not rebind the view or the encoder video_t.
        emit(OBS_FRONTEND_EVENT_SCENE_CHANGED);
        return;
    }

    if (dualLaneReady) {
        // In studio mode an on-air change must go through Preview and the
        // atomic pair primitive.  Do not silently fall back to a one-view
        // rebind that could expose an intermediate route.
        return;
    }

    obs_source_t *prev = currentScene;
    currentScene = obs_source_get_ref(scene);
    // The single-lane reference binds public scenes directly, unlike the
    // dual-lane path whose physical roots remain stable. Keep the profiler's
    // Program role aligned with the actual bound source after every legacy
    // scene change.
    g_runtimeTelemetry.updateMixRoots(currentScene, nullptr);

    if (!nativeStingerEnabled) {
        // ---- DEFAULT PATH (flag OFF, ADR 003 §A4.3 / §A4.7 #69) ----
        // Brute hard cut, the pre-#67 behaviour: bind the raw scene to the
        // program output. NO transition is started, the stinger source is never
        // touched, and the encoder is never blanked (output 0 swaps atomically
        // from one held scene to the next). The M10 animated transition is
        // rendered by Solar/CEF as an overlay, not by OBS (C-MECH: zero native
        // transition fires in the M10 chain).
        obs_set_output_source(0, currentScene);
        if (prev)
            obs_source_release(prev);
        emit(OBS_FRONTEND_EVENT_SCENE_CHANGED);
        return;
    }

    // ---- DORMANT NATIVE PATH (flag ON only) ----
    // M10 Gap B' fix (ADR 003 §3.3 / Amendment 1 §A1.1): composite the ACTIVE
    // transition through the program output instead of a raw hard cut. The
    // transition is already bound to output source 0 (bindTransitionOutput),
    // holding the previous scene; obs_transition_start runs the fade/stinger
    // from that held scene to `currentScene` over transitionDuration ms.
    //
    // The encoder is NEVER blanked: output 0 stays the transition the whole
    // time (it renders the from-scene, then the blend, then the to-scene --
    // obs-source-transition.c). A media-backed stinger decodes its .webm and
    // composites it over the switch window; on completion the transition holds
    // the new scene 1:1.
    bool animated = false;
    if (currentTransition) {
        // Re-bind defensively in case the active transition changed since the
        // last frame (obs_frontend_set_current_transition rebinds too, but a
        // direct set keeps output 0 correct and self-sizes to the canvas).
        OBSSourceAutoRelease bound = obs_get_output_source(0);
        if (bound.Get() != currentTransition)
            bindTransitionOutput(currentTransition, prev ? prev : currentScene);
        animated = obs_transition_start(currentTransition, OBS_TRANSITION_MODE_AUTO,
                                        transitionDuration, currentScene);
    }
    if (!animated) {
        // Fallback hard cut (dev checkpoint only, Amendment 1 §A1.5): no
        // transition available, or transition_start declined. Bind the scene
        // directly so the encoder still gets frames.
        obs_set_output_source(0, currentScene);
    }

    if (prev)
        obs_source_release(prev);
    emit(OBS_FRONTEND_EVENT_SCENE_CHANGED);
}

void PulsarFrontendAPI::obs_frontend_get_transitions(struct obs_frontend_source_list *sources)
{
    for (obs_source_t *s : transitions) {
        obs_source_t *ref = obs_source_get_ref(s);
        if (ref)
            da_push_back(sources->sources, &ref);
    }
}

obs_source_t *PulsarFrontendAPI::obs_frontend_get_current_transition(void)
{
    return currentTransition ? obs_source_get_ref(currentTransition) : nullptr;
}

void PulsarFrontendAPI::obs_frontend_set_current_transition(obs_source_t *transition)
{
    if (!transition)
        return;
    if (currentTransition == transition)
        return;
    obs_source_t *prev = currentTransition;
    currentTransition = obs_source_get_ref(transition);

    // M10 PIVOT (ADR 003 §A4.3, issue #73): only the dormant native path routes
    // the active transition through the program output. With the flag OFF the
    // transition is purely bookkeeping for the obs-ws Get/SetCurrentSceneTransition
    // API -- output 0 keeps holding the raw scene and a program-scene change stays
    // a hard cut, so selecting a transition can never composite one.
    if (nativeStingerEnabled) {
        // M10: route the newly selected transition through the program output so a
        // subsequent SetCurrentProgramScene composites IT (e.g. switch Fade->
        // Stinger over obs-websocket). Seed it with the current scene and bind it
        // to output 0; if a transition is mid-animation we still re-point so the
        // next switch uses the new transition. Encoder is never blanked: the new
        // transition holds currentScene 1:1 the moment it is bound.
        if (!(prev && obs_transition_is_active(prev)))
            bindTransitionOutput(currentTransition, currentScene);
    }

    if (prev)
        obs_source_release(prev);
    emit(OBS_FRONTEND_EVENT_TRANSITION_CHANGED);
}

void PulsarFrontendAPI::obs_frontend_streaming_start(void)
{
    if (!streamOutput || obs_output_active(streamOutput))
        return;

    // Issue #131 -- THE binding that was missing. `streamOutput` is an
    // rtmp_output, i.e. a service-flagged output: obs_output_start() bails out
    // on its very first line when `output->service` is NULL
    // (upstream/libobs/obs-output.c, flag_service -> obs_service_can_try_to_connect
    // / obs_service_initialize). Nothing in this stub ever called
    // obs_output_set_service(), so SetStreamServiceSettings genuinely updated
    // `streamService` (GetStreamServiceSettings re-read it) yet the v5
    // single-stream StartStream path could not succeed by ANY combination of
    // requests. The encoders were already attached (setup(), above).
    //
    // Doctrine, not a new decision: pulsar-multi-stream/src/plugin-main.cpp:9-16
    // ("Approach A") promises the v5 StartStream / StartRecord path keeps
    // working for Stream Deck / Companion / Streamer.bot alongside the additive
    // multi-destination API. Multi-stream builds its own rtmp_custom outputs and
    // never touches `streamOutput` / `streamService`, so there is nothing to
    // arbitrate: StartStream simply becomes one more destination sharing the
    // same encoders (encode-once / fan-out-N).
    // #114 REGRESSION GUARD (Bastion C1, form (b)) -- see
    // include/pulsar-stream-egress.h for the full rationale. The binding below
    // is what makes this path a live egress; without this gate it would hand
    // `streamOutput` any `rtmp_common` service a v5 client pushed, whose ingest
    // is resolved out of a downloaded list and can degrade to cleartext (#135
    // widened the refusal from Twitch to the whole type). The BOOT PLACEHOLDER
    // is no longer one of those since #136 -- it is an empty rtmp_custom, which
    // this gate refuses for want of a server, not for what it names. This is
    // the LAST seam before libobs, so it holds whatever the caller did upstream
    // of it.
    //
    // Refusing = not emitting the output's "starting" signal, which is exactly
    // what a libobs refusal looks like: OutputHelper::SettleStart reads
    // Refused, and OutputStartFailure quotes the cause we plant below via
    // obs_output_set_last_error (OutputEffect.h reads it first). The v5 client
    // gets a named refusal, not a silent no-op.
    std::string egressRefusal;
    if (!pulsar::ValidateStreamServiceEgress(streamService, egressRefusal)) {
        obs_output_set_last_error(streamOutput, egressRefusal.c_str());
        blog(LOG_WARNING, "[pulsar-frontend-stub] StartStream refused: %s", egressRefusal.c_str());
        return;
    }

    obs_output_set_service(streamOutput, streamService);

    // ADR-005 §3.5 / #186: a fresh attempt opens here, right before the
    // libobs call that is about to arm either a synchronous decline (below)
    // or the async "start"/"stop" pair OnStreamStart/OnStreamStop settle.
    // The egressRefusal path above never reaches here, so it never opens or
    // settles an attempt -- it is Pulsar's own policy gate, not a libobs
    // go-live attempt, same precedent as multi-stream's
    // validate_destination_input (plugin-main.cpp) never calling
    // emit_output_failed either.
    streamAttempt.fetch_add(1);
    streamAttemptStartNs.store(std::chrono::steady_clock::now().time_since_epoch().count());
    streamAttemptWentActive.store(false);

    // Honesty corollary (issue #131, #120 family): STREAMING_STARTING used to be
    // emitted BEFORE obs_output_start(), so a REFUSED start still put a
    // StreamStateChanged/OBS_WEBSOCKET_OUTPUT_STARTING event on the wire -- an
    // event asserting an action that never happened. Emit it only once libobs
    // has really taken the action.
    //
    // This does NOT affect the #120 refusal verification: Utils::Obs::OutputHelper's
    // ActionWatch/SettleStart listen to the OUTPUT's own libobs "starting" signal
    // (obs-output.c), not to this frontend event, so their Refused/Pending verdict
    // is decided by libobs regardless of when the frontend event is emitted.
    if (!obs_output_start(streamOutput)) {
        const char *declineErr = obs_output_get_last_error(streamOutput);
        blog(LOG_INFO, "[pulsar-frontend-stub] obs_output_start (stream) declined: %s", declineErr);
        // ADR-005 §3.5 / #186: sole verdict authority for a synchronous
        // decline (same rationale as plugin-main.cpp's registry start()) --
        // OBS_OUTPUT_ERROR (-4) is the honest sentinel, no "stop" signal will
        // ever fire for an attempt libobs refused before starting it.
        EmitOutputAttemptSettledViaGlobalProc("stream", "stream", streamAttempt.load(), /*live=*/false,
                                               /*is_local=*/false, -4, declineErr, /*duration_ms=*/0);
        return;
    }
    emit(OBS_FRONTEND_EVENT_STREAMING_STARTING);
}

void PulsarFrontendAPI::obs_frontend_streaming_stop(void)
{
    if (!streamOutput || !obs_output_active(streamOutput))
        return;
    emit(OBS_FRONTEND_EVENT_STREAMING_STOPPING);
    obs_output_stop(streamOutput);
}

void PulsarFrontendAPI::obs_frontend_recording_start(void)
{
    if (!recordOutput || obs_output_active(recordOutput))
        return;

    // Resolve a fresh timestamped path (container per recordContainer, issue
    // #166) under recordDirectory and bind it to ffmpeg_muxer's settings just
    // before start. mkdir-as-needed so a missing recordings/ folder doesn't
    // fail silently inside libobs.
    std::error_code ec;
    std::filesystem::create_directories(recordDirectory, ec);
    if (ec) {
        blog(LOG_WARNING, "[pulsar-frontend-stub] could not mkdir %s: %s",
             recordDirectory.c_str(), ec.message().c_str());
        return;
    }

    auto now = std::chrono::system_clock::now();
    auto t = std::chrono::system_clock::to_time_t(now);
    std::tm tm{};
#ifdef _WIN32
    localtime_s(&tm, &t);
#else
    localtime_r(&t, &tm);
#endif
    char stamp[64];
    std::strftime(stamp, sizeof(stamp), "%Y%m%d-%H%M%S", &tm);
    std::filesystem::path recordPath = std::filesystem::path(recordDirectory) /
                                ("pulsar-" + std::string(stamp) + "." + recordContainer);

    OBSDataAutoRelease settings = obs_data_create();
    obs_data_set_string(settings, "path", recordPath.string().c_str());
    // Issue #169. `path` names the FIRST file only. ffmpeg_muxer's manual split
    // (proc "split_file") is a no-op unless the `split_file` setting is on --
    // split_file_proc reports it back through `split_file_enabled` and returns
    // without arming anything (obs-ffmpeg-mux.c:84-93) -- and the file it
    // switches to is built by generate_filename() from directory/format/
    // extension, NOT from `path` (obs-ffmpeg-mux.c:574-602). An empty
    // "directory" would root the next file at "/" and lose it, exactly the
    // replay-buffer trap fixed at boot below.
    //
    // Both thresholds stay unset (max_time_sec / max_size_mb default to 0), and
    // should_split() only ever returns true for a threshold > 0
    // (obs-ffmpeg-mux.c:695-708): nothing splits on its own. The single trigger
    // is an explicit SplitRecordFile.
    obs_data_set_bool(settings, "split_file", true);
    obs_data_set_string(settings, "directory", recordDirectory.c_str());
    obs_data_set_string(settings, "format", "pulsar-%CCYY%MM%DD-%hh%mm%ss");
    // Same recordContainer as `path` above -- this is the field generate_filename()
    // actually reads for every file split_file produces AFTER the first one
    // (obs-ffmpeg-mux.c:574-602). Setting it from anything other than
    // recordContainer would split the archive across two containers mid-stream
    // (issue #166).
    obs_data_set_string(settings, "extension", recordContainer.c_str());
    obs_data_set_bool(settings, "allow_spaces", false);
    // Empty muxer_settings -> ffmpeg picks defaults from extension (mp4/mkv -> faststart on stop for mp4).
    obs_output_update(recordOutput, settings);

    emit(OBS_FRONTEND_EVENT_RECORDING_STARTING);
    if (!obs_output_start(recordOutput)) {
        blog(LOG_INFO, "[pulsar-frontend-stub] obs_output_start (record) declined: %s",
             obs_output_get_last_error(recordOutput));
        return;
    }
    blog(LOG_INFO, "[pulsar-frontend-stub] recording -> %s", recordPath.string().c_str());
}

void PulsarFrontendAPI::obs_frontend_recording_stop(void)
{
    if (!recordOutput || !obs_output_active(recordOutput))
        return;
    emit(OBS_FRONTEND_EVENT_RECORDING_STOPPING);
    obs_output_stop(recordOutput);
}

void PulsarFrontendAPI::obs_frontend_recording_pause(bool pause)
{
    if (!recordOutput || !obs_output_active(recordOutput))
        return;
    obs_output_pause(recordOutput, pause);
}

// Issue #169 / ADR Prism 028 §3.5 -- publish the cause where every consumer
// already looks. Both entry points below return `bool` and the websocket layer
// turns a `false` into an error; without a cause on the output that error can
// only be generic, which ADR Prism 026 §3.2 forbids. Same idiom as the replay
// buffer's on-air refusal: obs_output_set_last_error() + a log line, read back
// verbatim by pulsar-websocket. Nothing has to clear it -- obs_output_actual_start()
// wipes last_error_message on the next real start (obs-output.c:365).
static void refuse_record_proc(obs_output_t *output, const char *what, const char *cause)
{
    if (output)
        obs_output_set_last_error(output, cause);
    blog(LOG_WARNING, "[pulsar-frontend-stub] %s refused: %s", what, cause);
}

// Issue #169. Both of these were stubbed to an unconditional `false`: the
// SplitRecordFile / CreateRecordChapter requests were registered but could only
// ever fail. There is no splitting logic to write here -- upstream
// (OBSStudioAPI.cpp:261-289) delegates to the recording output's proc handler,
// and the stub owns that output directly, so the same delegation applies as-is.
bool PulsarFrontendAPI::obs_frontend_recording_split_file(void)
{
    if (!recordOutput || !obs_output_active(recordOutput)) {
        refuse_record_proc(recordOutput, "split_file",
            "no recording is running -- start the recording before splitting its file.");
        return false;
    }
    // Upstream guards on the same pair (active && !paused): a split taken while
    // the muxer is paused would be applied against a frozen packet timeline.
    if (recordingPaused.load()) {
        refuse_record_proc(recordOutput, "split_file",
            "the recording is paused -- resume it before splitting its file.");
        return false;
    }

    proc_handler_t *ph = obs_output_get_proc_handler(recordOutput);
    uint8_t stack[128];
    calldata cd;
    calldata_init_fixed(&cd, stack, sizeof(stack));
    if (!ph || !proc_handler_call(ph, "split_file", &cd)) {
        char cause[320];
        std::snprintf(cause, sizeof(cause),
                 "the recording output (%s) does not expose the \"split_file\" procedure.",
                 obs_output_get_id(recordOutput));
        refuse_record_proc(recordOutput, "split_file", cause);
        return false;
    }
    // The proc always answers; `split_file_enabled` is the muxer telling us
    // whether it armed a split or ignored the call. Reading the return code
    // alone would report a success the muxer never took.
    if (!calldata_bool(&cd, "split_file_enabled")) {
        refuse_record_proc(recordOutput, "split_file",
            "file splitting is disabled on the recording output -- its \"split_file\" "
            "setting is off, so the muxer ignored the request.");
        return false;
    }

    blog(LOG_INFO, "[pulsar-frontend-stub] split_file armed; the muxer switches file on "
         "the next keyframe (RecordFileChanged carries the new name)");
    return true;
}

bool PulsarFrontendAPI::obs_frontend_recording_add_chapter(const char *name)
{
    if (!recordOutput || !obs_output_active(recordOutput)) {
        refuse_record_proc(recordOutput, "add_chapter",
            "no recording is running -- start the recording before adding a chapter.");
        return false;
    }
    if (recordingPaused.load()) {
        refuse_record_proc(recordOutput, "add_chapter",
            "the recording is paused -- resume it before adding a chapter.");
        return false;
    }

    proc_handler_t *ph = obs_output_get_proc_handler(recordOutput);
    calldata_t cd = {};
    calldata_set_string(&cd, "chapter_name", name);
    bool called = ph && proc_handler_call(ph, "add_chapter", &cd);
    calldata_free(&cd);

    if (!called) {
        // Not a defect of this port: chapter markers live on the hybrid-MP4
        // output only (mp4_output.c:218 registers "add_chapter"; ffmpeg_muxer
        // registers "split_file" alone, obs-ffmpeg-mux.c:107). Name the output
        // that refused rather than let the client read a generic failure.
        char cause[320];
        std::snprintf(cause, sizeof(cause),
                 "the recording output (%s) does not expose the \"add_chapter\" procedure "
                 "-- chapter markers exist only on the hybrid-MP4 output (mp4_output).",
                 obs_output_get_id(recordOutput));
        refuse_record_proc(recordOutput, "add_chapter", cause);
        return false;
    }

    blog(LOG_INFO, "[pulsar-frontend-stub] chapter added: %s", name ? name : "(unnamed)");
    return true;
}

void PulsarFrontendAPI::obs_frontend_replay_buffer_start(void)
{
    if (!replayOutput || obs_output_active(replayOutput))
        return;

    // ADR Prism 024 §3.1 -- "pas de replay hors antenne", explicit no-go. The
    // buffer lives off the shared encoders; arming it while they are idle would
    // make obs_output_start spin them up for the replay alone -- a partial,
    // invisible pipeline burning CPU off-air. Refuse, loudly, and never touch
    // the encoder. Being on-air (stream) or recording both qualify: either one
    // already has the encoders running, so the buffer only taps their packets.
    if (!videoEncoder || !obs_encoder_active(videoEncoder)) {
        // The refusal is decided HERE, before obs_output_start, so libobs never
        // records a cause of its own and obs_output_get_last_error stays empty.
        // A consumer that reads state off the server (pulsar-websocket, issue
        // #120) would then have nothing to quote and fall back to a generic
        // "not configured" -- which is false: the encoders are attached, they
        // are simply not running. Publish the cause where every consumer
        // already looks. Nothing has to clear it: obs_output_actual_start()
        // wipes last_error_message on the next real start (obs-output.c:365).
        if (videoEncoder)
            obs_output_set_last_error(replayOutput,
                "the encoders are idle -- nothing is streaming or recording. The replay "
                "buffer borrows the live encoders, it does not start them. Start the "
                "stream or the recording first, then arm the buffer.");
        blog(LOG_WARNING, "[pulsar-frontend-stub] replay buffer start refused: encoders "
             "idle (not streaming or recording). Arm the buffer once the broadcast "
             "is up -- it borrows the live encoders, it does not start them.");
        return;
    }

    emit(OBS_FRONTEND_EVENT_REPLAY_BUFFER_STARTING);
    if (!obs_output_start(replayOutput)) {
        const char *err = obs_output_get_last_error(replayOutput);
        blog(LOG_INFO, "[pulsar-frontend-stub] obs_output_start (replay) declined: %s",
             err ? err : "(null)");
        return;
    }
    blog(LOG_INFO, "[pulsar-frontend-stub] replay buffer armed (%d s / %d MB) -> %s",
         replayMaxTimeSec, replayMaxSizeMb, recordDirectory.c_str());
}

void PulsarFrontendAPI::obs_frontend_replay_buffer_save(void)
{
    if (!replayOutput || !obs_output_active(replayOutput))
        return;
    calldata_t cd = {};
    proc_handler_t *ph = obs_output_get_proc_handler(replayOutput);
    if (ph)
        proc_handler_call(ph, "save", &cd);
    calldata_free(&cd);
}

void PulsarFrontendAPI::obs_frontend_replay_buffer_stop(void)
{
    if (!replayOutput || !obs_output_active(replayOutput))
        return;
    emit(OBS_FRONTEND_EVENT_REPLAY_BUFFER_STOPPING);
    obs_output_stop(replayOutput);
}

void PulsarFrontendAPI::obs_frontend_start_virtualcam(void)
{
    if (!virtualcamOutput || obs_output_active(virtualcamOutput))
        return;
    // Load-order guard: outputs are created at startup, but the
    // "virtualcam_output" type is registered LATER by win-dshow's
    // obs_module_load. An output created before that registration carries no
    // info (obs_output_get_flags()==0) and can never start. Re-create it now
    // that all modules are loaded so it binds the real win-dshow output info.
    if (obs_output_get_flags(virtualcamOutput) == 0) {
        obs_output_release(virtualcamOutput);
        virtualcamOutput = obs_output_create("virtualcam_output", "PulsarVCam", nullptr, nullptr);
        if (!virtualcamOutput) {
            blog(LOG_WARNING,
                 "[pulsar-frontend-stub] virtualcam_output type unavailable (win-dshow not loaded?)");
            return;
        }
        hookOutputSignals(virtualcamOutput, OnVCamStart, OnVCamStop);
    }
    // A raw output (virtualcam_output) needs a video+audio mix bound before it
    // can start (upstream BasicOutputHandler::StartVirtualCam). Default is the
    // program (ProgramView => obs_get_video()). Source mode (Zab): if a
    // dedicated "ZabVirtualCamSource" scene exists — Prism builds it with the
    // source the OPERATOR chose in settings — expose THAT through a private view
    // so the virtual cam carries just their camera, reusable everywhere. The
    // engine never picks a source itself; without that scene it's the program.
    video_t *vcamMix = obs_get_video();
    obs_source_t *vcamScene = obs_get_source_by_name("ZabVirtualCamSource");
    if (vcamScene) {
        if (!vcamView)
            vcamView = obs_view_create();
        obs_view_set_source(vcamView, 0, vcamScene);
        if (!vcamVideo)
            vcamVideo = obs_view_add(vcamView);
        if (vcamVideo)
            vcamMix = vcamVideo;
        obs_source_release(vcamScene);
        blog(LOG_INFO,
             "[pulsar-frontend-stub] virtual cam SOURCE mode -> 'ZabVirtualCamSource'");
    }
    obs_output_set_media(virtualcamOutput, vcamMix, programAudio);
    // ADR-005 §3.5 / #186: same attempt-open point as obs_frontend_streaming_start.
    vcamAttempt.fetch_add(1);
    vcamAttemptStartNs.store(std::chrono::steady_clock::now().time_since_epoch().count());
    vcamAttemptWentActive.store(false);
    if (!obs_output_start(virtualcamOutput)) {
        const char *vcamErr = obs_output_get_last_error(virtualcamOutput);
        blog(LOG_INFO, "[pulsar-frontend-stub] obs_output_start (vcam) declined: %s",
             vcamErr ? vcamErr : "(null)");
        EmitOutputAttemptSettledViaGlobalProc("virtualcam", "virtualcam", vcamAttempt.load(), /*live=*/false,
                                               /*is_local=*/true, -4, vcamErr, /*duration_ms=*/0);
    }
}

void PulsarFrontendAPI::obs_frontend_stop_virtualcam(void)
{
    if (!virtualcamOutput || !obs_output_active(virtualcamOutput))
        return;
    obs_output_stop(virtualcamOutput);
}

} // namespace

// Dispatch OBS_TASK_UI tasks synchronously on the calling thread. Without a
// handler libobs drops them with a LOG_ERROR ("UI task could not be queued"),
// which silently breaks every websocket request that goes through
// obs_queue_task(OBS_TASK_UI, ...). With wait=true the obs-websocket caller
// already expects to block until completion, so synchronous dispatch matches
// the sync semantics; with wait=false the task still runs synchronously which
// is acceptable for a headless service that has no separate UI loop.
static void pulsar_ui_task_handler(obs_task_t task, void *param, bool /*wait*/)
{
    if (task)
        task(param);
}

extern "C" void pulsar_frontend_init(void)
{
    g_frontend_cleanup_succeeded.store(true, std::memory_order_release);
    if (g_api) {
        blog(LOG_WARNING, "[pulsar-frontend-stub] init called twice");
        return;
    }
    // Install the vtable BEFORE obs_load_all_modules so plugins (notably
    // obs-websocket) find a populated callback table at obs_module_load time.
    // Heavy state -- scenes, encoders, outputs, sources, services -- depends
    // on plugins that aren't loaded yet, so it lives in setup() called from
    // pulsar_frontend_finished_loading() once obs_post_load_modules has run.
    g_rollbackMarkerWriter.start();
    g_dualLaneControlBridge.install();
    g_runtimeTelemetry.install();
    auto *api = new PulsarFrontendAPI();
    g_api = api;
    obs_frontend_set_callbacks_internal(api);
    obs_set_ui_task_handler(pulsar_ui_task_handler);
    blog(LOG_INFO, "[pulsar-frontend-stub] callbacks installed");
}

extern "C" void pulsar_frontend_finished_loading(void)
{
    if (!g_api)
        return;
    if (!g_api->setup())
        blog(LOG_WARNING, "[pulsar-frontend-stub] setup() reported partial failure");
    if (g_sceneSwitchVendorStorage.start())
        g_sceneSwitchVendor.store(&g_sceneSwitchVendorStorage, std::memory_order_release);
    g_api->emit(OBS_FRONTEND_EVENT_FINISHED_LOADING);
}

extern "C" void pulsar_frontend_shutdown(void)
{
    if (!g_api)
        return;
    g_sceneSwitchVendor.store(nullptr, std::memory_order_release);
    g_sceneSwitchVendorStorage.stop();
    // Close the supported WebSocket mutation gate before emitting EXIT. This
    // waits for an already-running mutation but admits no new one while the
    // frontend object and its stable lane roots are being destroyed.
    g_dualLaneControlBridge.deactivate();
    g_api->emit(OBS_FRONTEND_EVENT_EXIT);
    // Hand ownership back to obs-frontend-api.dll, which deletes the object;
    // its destructor releases all libobs handles (outputs, scene, transition,
    // service) before any final obs_shutdown call.
    obs_frontend_set_callbacks_internal(nullptr);
    g_api = nullptr;
    blog(LOG_INFO, "[pulsar-frontend-stub] callbacks uninstalled");
}

extern "C" bool pulsar_frontend_cleanup_succeeded(void)
{
    return g_frontend_cleanup_succeeded.load(std::memory_order_acquire);
}
