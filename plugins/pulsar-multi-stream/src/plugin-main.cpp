// pulsar-multi-stream -- libobs plugin DLL.
//
// Provides first-class multi-destination streaming on top of the encoders
// already wired by pulsar-frontend-stub. The plugin tracks a list of
// "destinations" -- each one a tuple { kind, url, key, enabled, output } --
// and drives them by reusing the video + audio encoders attached to the
// streaming output (frontend-stub's PulsarStream). Encode-once / fan-out-N.
//
// Architecture choice (Phase 7 PR1, ratified by the maintainer):
//   Approach A -- this plugin DOES NOT take ownership of frontend-stub's
//   single PulsarStream / PulsarRecord outputs. Those keep working with
//   the v5 StartStream / StartRecord requests for compatibility with
//   Stream Deck / Companion / Streamer.bot. Multi-destination is purely
//   additive: a v5 client uses CallVendorRequest("pulsar", "...") to
//   manage destinations through this plugin's namespace.
//
// Vendor API (registered via obs_websocket_register_vendor("pulsar")):
//   GetDestinations         -> { destinations: [{id,name,kind,url,enabled,active}, ...] }
//   CreateDestination(name, kind, url, key?) -> { id }
//   RemoveDestination(id)
//   StartDestination(id)
//   StopDestination(id)
//   StartAllDestinations
//   StopAllDestinations
//
// Kinds in PR1: "rtmp_custom" and "vod_local". For vod_local the "url"
// field is reused as the output file path. The "twitch" kind ships in
// PR2 (Phase 7e) -- it's an rtmp_custom alias with a pinned base server
// URL plus a few input-validation rules.
//
// Threading: a single std::mutex guards the destination map. obs_output_*
// calls inside locked sections are tolerated -- libobs is internally
// thread-safe and our hold time is short. obs_websocket request handlers
// run on obs-websocket's worker thread so the lock is genuinely needed.

#include <obs-module.h>
#include <obs.h>
#include <obs.hpp>
#include <obs-frontend-api.h>
#include <util/util.hpp>

#include <obs-websocket-api.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <filesystem>
#include <map>
#include <memory>
#include <mutex>
#include <random>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

OBS_DECLARE_MODULE()
OBS_MODULE_AUTHOR("Pulsar")
OBS_MODULE_USE_DEFAULT_LOCALE("pulsar-multi-stream", "en-US")

const char *obs_module_name(void) { return "pulsar-multi-stream"; }
const char *obs_module_description(void) { return "First-class multi-destination streaming for Pulsar"; }

namespace {

enum DestinationKind {
    Kind_RtmpCustom,
    Kind_VodLocal,
    Kind_Twitch,
    Kind_Unknown,
};

// Twitch's primary RTMP ingest. Twitch publishes per-region ingests at
// https://help.twitch.tv/s/twitch-ingest-recommendation but the global
// "live" host LB-redirects to the closest one, which is fine for any
// non-low-latency use case. Twitch's stream key is per-channel.
constexpr const char *TWITCH_INGEST_URL = "rtmp://live.twitch.tv/app/";

const char *kind_to_string(DestinationKind k)
{
    switch (k) {
    case Kind_RtmpCustom: return "rtmp_custom";
    case Kind_VodLocal:   return "vod_local";
    case Kind_Twitch:     return "twitch";
    default:              return "unknown";
    }
}

DestinationKind kind_from_string(const char *s)
{
    if (!s) return Kind_Unknown;
    std::string str(s);
    if (str == "rtmp_custom") return Kind_RtmpCustom;
    if (str == "vod_local")   return Kind_VodLocal;
    if (str == "twitch")      return Kind_Twitch;
    return Kind_Unknown;
}

// Returns true if url starts with "rtmp://" or "rtmps://".
bool is_rtmp_scheme(const char *url)
{
    if (!url) return false;
    return std::strncmp(url, "rtmp://", 7) == 0 || std::strncmp(url, "rtmps://", 8) == 0;
}

// Front-load validation so we surface a typed error to the caller before
// any obs_output_* allocation happens. Phase 7e tightening:
//   rtmp_custom -> url must be rtmp[s]://, key must be non-empty
//   vod_local   -> url is a path; parent directory must exist or be creatable
//   twitch      -> key non-empty (url ignored, server is pinned)
bool validate_destination_input(DestinationKind kind, const char *url, const char *key, std::string &errOut)
{
    switch (kind) {
    case Kind_RtmpCustom:
        if (!is_rtmp_scheme(url)) {
            errOut = "rtmp_custom: url must be rtmp:// or rtmps://";
            return false;
        }
        if (!key || !*key) {
            errOut = "rtmp_custom: key required";
            return false;
        }
        return true;

    case Kind_VodLocal: {
        if (!url || !*url) {
            errOut = "vod_local: url (file path) required";
            return false;
        }
        std::filesystem::path p(url);
        auto parent = p.parent_path();
        if (parent.empty()) return true; // relative path in cwd is fine
        std::error_code ec;
        if (!std::filesystem::exists(parent, ec)) {
            std::filesystem::create_directories(parent, ec);
            if (ec) {
                errOut = "vod_local: cannot create parent dir: " + ec.message();
                return false;
            }
        }
        return true;
    }

    case Kind_Twitch:
        if (!key || !*key) {
            errOut = "twitch: key required (Twitch stream key)";
            return false;
        }
        return true;

    default:
        errOut = "unknown destination kind";
        return false;
    }
}

struct Destination {
    std::string id;
    std::string name;
    DestinationKind kind = Kind_Unknown;
    std::string url;       // RTMP server URL OR file path (vod_local)
    std::string key;       // RTMP stream key (unused for vod_local)
    bool enabled = false;  // last user intent (Start/Stop request)

    obs_output_t *output = nullptr;   // lazy-created on first start
    obs_service_t *service = nullptr; // rtmp_custom only
};

// 16-hex random id. Not a real UUID -- collisions across a single Pulsar
// runtime are practically impossible at our cardinality.
std::string make_id()
{
    static std::mt19937_64 rng(std::random_device{}());
    std::ostringstream oss;
    oss << std::hex << rng();
    auto s = oss.str();
    while (s.size() < 16) s = "0" + s;
    return s.substr(0, 16);
}

class DestinationRegistry {
public:
    std::string create(const std::string &name, DestinationKind kind,
                       const std::string &url, const std::string &key);
    bool remove(const std::string &id);
    bool start(const std::string &id, std::string &errOut);
    bool stop(const std::string &id);
    void start_all();
    void stop_all();
    void teardown_all();

    // Snapshot used by GetDestinations -- copies the descriptors plus the
    // current obs_output_active state under the lock so the caller can
    // serialise without holding it.
    struct Snapshot {
        std::string id;
        std::string name;
        std::string kind;
        std::string url;
        bool enabled;
        bool active;
    };
    std::vector<Snapshot> snapshot() const;

private:
    bool ensure_output(Destination &d, std::string &errOut);
    void release_destination_handles_locked(Destination &d);

    mutable std::mutex mu_;
    std::map<std::string, Destination> map_;
};

bool DestinationRegistry::ensure_output(Destination &d, std::string &errOut)
{
    if (!d.output) {
        const char *outId = (d.kind == Kind_VodLocal) ? "ffmpeg_muxer" : "rtmp_output";
        std::string outName = "PulsarDest_" + d.id;
        d.output = obs_output_create(outId, outName.c_str(), nullptr, nullptr);
        if (!d.output) {
            errOut = std::string("could not create ") + outId;
            return false;
        }
    }

    // Borrow encoders from the streaming output frontend-stub set up.
    // Encode-once / fan-out: every destination shares the same encoders.
    OBSOutputAutoRelease srcOutput = obs_frontend_get_streaming_output();
    if (!srcOutput) {
        errOut = "frontend streaming output unavailable";
        return false;
    }
    obs_encoder_t *vEnc = obs_output_get_video_encoder(srcOutput);
    obs_encoder_t *aEnc = obs_output_get_audio_encoder(srcOutput, 0);
    if (!vEnc || !aEnc) {
        errOut = "encoders not bound on streaming output";
        return false;
    }
    obs_output_set_video_encoder(d.output, vEnc);
    obs_output_set_audio_encoder(d.output, aEnc, 0);

    if (d.kind == Kind_RtmpCustom || d.kind == Kind_Twitch) {
        const char *server = (d.kind == Kind_Twitch) ? TWITCH_INGEST_URL : d.url.c_str();
        OBSDataAutoRelease svcSettings = obs_data_create();
        obs_data_set_string(svcSettings, "server", server);
        obs_data_set_string(svcSettings, "key", d.key.c_str());
        if (d.service) {
            obs_service_release(d.service);
            d.service = nullptr;
        }
        std::string svcName = "PulsarDestSvc_" + d.id;
        d.service = obs_service_create("rtmp_custom", svcName.c_str(), svcSettings, nullptr);
        if (!d.service) {
            errOut = "rtmp_custom service create failed";
            return false;
        }
        obs_output_set_service(d.output, d.service);
    } else if (d.kind == Kind_VodLocal) {
        OBSDataAutoRelease settings = obs_data_create();
        obs_data_set_string(settings, "path", d.url.c_str());
        obs_output_update(d.output, settings);
    } else {
        errOut = "unknown destination kind";
        return false;
    }

    return true;
}

void DestinationRegistry::release_destination_handles_locked(Destination &d)
{
    if (d.output) {
        if (obs_output_active(d.output)) {
            obs_output_stop(d.output);
            for (int i = 0; i < 50 && obs_output_active(d.output); ++i)
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
            if (obs_output_active(d.output))
                obs_output_force_stop(d.output);
        }
        obs_output_release(d.output);
        d.output = nullptr;
    }
    if (d.service) {
        obs_service_release(d.service);
        d.service = nullptr;
    }
}

std::string DestinationRegistry::create(const std::string &name, DestinationKind kind,
                                         const std::string &url, const std::string &key)
{
    std::lock_guard<std::mutex> lk(mu_);
    Destination d;
    d.id = make_id();
    d.name = name.empty() ? d.id : name;
    d.kind = kind;
    d.url = url;
    d.key = key;
    auto id = d.id;
    map_.emplace(id, std::move(d));
    return id;
}

bool DestinationRegistry::remove(const std::string &id)
{
    std::lock_guard<std::mutex> lk(mu_);
    auto it = map_.find(id);
    if (it == map_.end()) return false;
    release_destination_handles_locked(it->second);
    map_.erase(it);
    return true;
}

bool DestinationRegistry::start(const std::string &id, std::string &errOut)
{
    std::lock_guard<std::mutex> lk(mu_);
    auto it = map_.find(id);
    if (it == map_.end()) { errOut = "no such destination"; return false; }
    auto &d = it->second;
    if (!ensure_output(d, errOut)) return false;
    if (obs_output_active(d.output)) { d.enabled = true; return true; }
    if (!obs_output_start(d.output)) {
        const char *last = obs_output_get_last_error(d.output);
        errOut = last ? last : "obs_output_start declined";
        return false;
    }
    d.enabled = true;
    return true;
}

bool DestinationRegistry::stop(const std::string &id)
{
    std::lock_guard<std::mutex> lk(mu_);
    auto it = map_.find(id);
    if (it == map_.end()) return false;
    auto &d = it->second;
    d.enabled = false;
    if (d.output && obs_output_active(d.output))
        obs_output_stop(d.output);
    return true;
}

void DestinationRegistry::start_all()
{
    std::lock_guard<std::mutex> lk(mu_);
    for (auto &p : map_) {
        std::string err;
        auto &d = p.second;
        if (!ensure_output(d, err)) {
            blog(LOG_WARNING, "[pulsar-multi-stream] start_all: %s skipped (%s)", d.id.c_str(), err.c_str());
            continue;
        }
        if (obs_output_active(d.output)) continue;
        if (!obs_output_start(d.output)) {
            const char *last = obs_output_get_last_error(d.output);
            blog(LOG_WARNING, "[pulsar-multi-stream] start_all: %s declined (%s)",
                 d.id.c_str(), last ? last : "(null)");
            continue;
        }
        d.enabled = true;
    }
}

void DestinationRegistry::stop_all()
{
    std::lock_guard<std::mutex> lk(mu_);
    for (auto &p : map_) {
        auto &d = p.second;
        d.enabled = false;
        if (d.output && obs_output_active(d.output))
            obs_output_stop(d.output);
    }
}

void DestinationRegistry::teardown_all()
{
    std::lock_guard<std::mutex> lk(mu_);
    for (auto &p : map_)
        release_destination_handles_locked(p.second);
    map_.clear();
}

std::vector<DestinationRegistry::Snapshot> DestinationRegistry::snapshot() const
{
    std::lock_guard<std::mutex> lk(mu_);
    std::vector<Snapshot> out;
    out.reserve(map_.size());
    for (auto &p : map_) {
        auto &d = p.second;
        out.push_back({
            d.id, d.name, kind_to_string(d.kind), d.url, d.enabled,
            d.output && obs_output_active(d.output),
        });
    }
    return out;
}

// Module-static singletons accessed by the adaptive worker -- defined
// before the class so member functions can compile inline. g_adaptive
// lives at the end of the file alongside the rest of the singletons,
// after AdaptiveBitrate is fully defined.
DestinationRegistry *g_registry = nullptr;
obs_websocket_vendor g_vendor = nullptr;

// ---- Phase 12b: adaptive bitrate -------------------------------------------
//
// A background worker samples obs_output_get_total_frames /
// obs_output_get_frames_dropped on every active output (frontend-stub's
// streamOutput plus any active destinations) and steers the shared video
// encoder bitrate within [floor, target]:
//
//   tick (every 2 s):
//     - sum total_frames + dropped_frames across active outputs
//     - delta_total = total - last_total
//     - delta_drop  = dropped - last_dropped
//     - ratio = delta_drop / max(1, delta_total)
//     - if ratio > 0.01  : new = max(floor, current * 0.85), reset stable
//     - else if stable >= 15 ticks AND current < target : new = min(target, current * 1.05)
//     - else                                            : new = current; stable++
//     - if new != current : obs_encoder_update + emit pulsar:BitrateAdjusted
//
// The target is sampled from the encoder's settings the first time the
// loop sees a video encoder; SetVideoSettings while adaptive is active
// is overridden on the next tick (documented). Floor is 30 % of target.
//
// Opt-out via PULSAR_ADAPTIVE_BITRATE=off at boot OR vendor request
// SetAdaptiveEnabled(enabled=false) at runtime.
class AdaptiveBitrate {
public:
    struct State {
        bool enabled = true;
        long long target_kbps = 0;
        long long current_kbps = 0;
        long long floor_kbps = 0;
        int stable_ticks = 0;
        uint64_t adjustments_total = 0;
        uint64_t last_delta_total = 0;
        uint64_t last_delta_dropped = 0;
        double last_drop_ratio = 0.0;
    };

    void start();
    void stop();
    void set_enabled(bool e);
    State get_state() const;

private:
    void run();
    obs_encoder_t *get_video_encoder() const;
    void aggregate_frame_counts(uint64_t &totalOut, uint64_t &droppedOut) const;
    bool apply_bitrate(obs_encoder_t *enc, long long new_kbps, const char *reason,
                       double trigger_ratio);

    static constexpr int TICK_SEC = 2;
    static constexpr double DROP_THRESHOLD = 0.01;
    static constexpr double DOWN_FACTOR = 0.85;
    static constexpr double UP_FACTOR = 1.05;
    static constexpr int STABLE_TICKS_FOR_UP = 15; // ~30 s
    static constexpr double FLOOR_RATIO = 0.30;

    std::atomic<bool> running_{false};
    std::thread worker_;
    std::condition_variable cv_;
    std::mutex cv_mu_;

    mutable std::mutex state_mu_;
    State state_;

    // Sampling state -- worker-thread only.
    uint64_t last_total_ = 0;
    uint64_t last_dropped_ = 0;
    bool first_tick_ = true;
};

obs_encoder_t *AdaptiveBitrate::get_video_encoder() const
{
    OBSOutputAutoRelease srcOutput = obs_frontend_get_streaming_output();
    if (!srcOutput) return nullptr;
    return obs_output_get_video_encoder(srcOutput);
}

void AdaptiveBitrate::aggregate_frame_counts(uint64_t &totalOut, uint64_t &droppedOut) const
{
    totalOut = 0;
    droppedOut = 0;

    // frontend-stub's legacy streamOutput
    OBSOutputAutoRelease streamOutput = obs_frontend_get_streaming_output();
    if (streamOutput && obs_output_active(streamOutput)) {
        totalOut += static_cast<uint64_t>(obs_output_get_total_frames(streamOutput));
        droppedOut += static_cast<uint64_t>(obs_output_get_frames_dropped(streamOutput));
    }

    // Active destinations -- borrow output ptrs through the registry, but
    // do NOT take refs (we just need transient stat reads). The registry
    // mutex guards this safely; obs_output_get_total_frames is internally
    // thread-safe.
    if (g_registry) {
        for (auto &snap : g_registry->snapshot()) {
            // The snapshot doesn't expose the obs_output_t*, but
            // get_total_frames is keyed off the live output. Easier path:
            // re-look-up by name. We named outputs "PulsarDest_<id>".
            std::string outName = "PulsarDest_" + snap.id;
            OBSOutputAutoRelease out = obs_get_output_by_name(outName.c_str());
            if (out && obs_output_active(out)) {
                totalOut += static_cast<uint64_t>(obs_output_get_total_frames(out));
                droppedOut += static_cast<uint64_t>(obs_output_get_frames_dropped(out));
            }
        }
    }
}

bool AdaptiveBitrate::apply_bitrate(obs_encoder_t *enc, long long new_kbps, const char *reason,
                                     double trigger_ratio)
{
    OBSDataAutoRelease patch = obs_data_create();
    obs_data_set_int(patch, "bitrate", new_kbps);
    obs_encoder_update(enc, patch);

    // Emit pulsar:BitrateAdjusted vendor event so Prism can surface the
    // change in its operator UI. Event payload is a fresh obs_data_t we
    // own and free after the emit (vendor_emit_event does not change the
    // refcount).
    if (g_vendor) {
        OBSDataAutoRelease ev = obs_data_create();
        obs_data_set_int(ev, "bitrate", new_kbps);
        obs_data_set_int(ev, "target", state_.target_kbps);
        obs_data_set_int(ev, "floor", state_.floor_kbps);
        obs_data_set_string(ev, "reason", reason);
        obs_data_set_double(ev, "drop_ratio", trigger_ratio);
        obs_websocket_vendor_emit_event(g_vendor, "BitrateAdjusted", ev);
    }

    blog(LOG_INFO, "[pulsar-multi-stream] adaptive: %lld -> %lld kbps (%s, ratio=%.4f)",
         static_cast<long long>(state_.current_kbps), new_kbps, reason, trigger_ratio);
    return true;
}

void AdaptiveBitrate::run()
{
    while (running_.load()) {
        // Sleep with cancel awareness so stop() returns within a tick.
        {
            std::unique_lock<std::mutex> lk(cv_mu_);
            cv_.wait_for(lk, std::chrono::seconds(TICK_SEC),
                         [this] { return !running_.load(); });
        }
        if (!running_.load()) break;

        bool enabled;
        {
            std::lock_guard<std::mutex> lk(state_mu_);
            enabled = state_.enabled;
        }
        if (!enabled) continue;

        obs_encoder_t *enc = get_video_encoder();
        if (!enc) continue;

        // First time we see an encoder, latch its bitrate as the target.
        if (state_.target_kbps == 0) {
            OBSDataAutoRelease s = obs_encoder_get_settings(enc);
            long long t = obs_data_get_int(s, "bitrate");
            std::lock_guard<std::mutex> lk(state_mu_);
            state_.target_kbps = t;
            state_.current_kbps = t;
            state_.floor_kbps = static_cast<long long>(t * FLOOR_RATIO);
            blog(LOG_INFO, "[pulsar-multi-stream] adaptive armed: target=%lld floor=%lld",
                 t, state_.floor_kbps);
        }

        uint64_t total = 0, dropped = 0;
        aggregate_frame_counts(total, dropped);

        if (first_tick_) {
            first_tick_ = false;
            last_total_ = total;
            last_dropped_ = dropped;
            continue;
        }

        // Aggregated counts can drop between ticks if an output stopped
        // (its frames leave the sum). Clamp to 0 instead of underflowing
        // uint64_t -- a negative delta means "no decision data this tick".
        uint64_t dTotal = (total >= last_total_) ? (total - last_total_) : 0;
        uint64_t dDrop  = (dropped >= last_dropped_) ? (dropped - last_dropped_) : 0;
        last_total_ = total;
        last_dropped_ = dropped;

        // Reading from the encoder rather than state_.current_kbps so an
        // external SetVideoSettings call gets reconciled into our model.
        OBSDataAutoRelease s = obs_encoder_get_settings(enc);
        long long current = obs_data_get_int(s, "bitrate");
        double ratio = dTotal ? (double)dDrop / (double)dTotal : 0.0;

        long long new_kbps = current;
        const char *reason = nullptr;

        if (ratio > DROP_THRESHOLD) {
            long long floor = state_.floor_kbps ? state_.floor_kbps : 200;
            new_kbps = std::max<long long>(floor,
                static_cast<long long>(current * DOWN_FACTOR));
            reason = "drops";
        } else if (state_.stable_ticks >= STABLE_TICKS_FOR_UP &&
                   current < state_.target_kbps) {
            new_kbps = std::min<long long>(state_.target_kbps,
                static_cast<long long>(current * UP_FACTOR));
            reason = "recovery";
        }

        {
            std::lock_guard<std::mutex> lk(state_mu_);
            state_.last_delta_total = dTotal;
            state_.last_delta_dropped = dDrop;
            state_.last_drop_ratio = ratio;
            if (reason && new_kbps != current) {
                apply_bitrate(enc, new_kbps, reason, ratio);
                state_.current_kbps = new_kbps;
                state_.adjustments_total += 1;
                state_.stable_ticks = 0;
            } else {
                state_.current_kbps = current;
                if (ratio <= DROP_THRESHOLD) state_.stable_ticks += 1;
                else state_.stable_ticks = 0;
            }
        }
    }
}

void AdaptiveBitrate::start()
{
    if (running_.exchange(true)) return; // already running

    if (const char *e = std::getenv("PULSAR_ADAPTIVE_BITRATE"); e &&
        (std::string(e) == "off" || std::string(e) == "0" || std::string(e) == "false")) {
        std::lock_guard<std::mutex> lk(state_mu_);
        state_.enabled = false;
        blog(LOG_INFO, "[pulsar-multi-stream] adaptive bitrate disabled by PULSAR_ADAPTIVE_BITRATE=%s", e);
    } else {
        blog(LOG_INFO, "[pulsar-multi-stream] adaptive bitrate enabled");
    }
    worker_ = std::thread(&AdaptiveBitrate::run, this);
}

void AdaptiveBitrate::stop()
{
    if (!running_.exchange(false)) return;
    cv_.notify_all();
    if (worker_.joinable()) worker_.join();
}

void AdaptiveBitrate::set_enabled(bool e)
{
    std::lock_guard<std::mutex> lk(state_mu_);
    state_.enabled = e;
    // Reset accumulators so a freshly enabled loop doesn't slam the
    // encoder based on stale samples.
    if (e) {
        first_tick_ = true;
        state_.stable_ticks = 0;
    }
}

AdaptiveBitrate::State AdaptiveBitrate::get_state() const
{
    std::lock_guard<std::mutex> lk(state_mu_);
    return state_;
}

// ---- module-static singletons (continued) ----------------------------------

AdaptiveBitrate *g_adaptive = nullptr;

// ---- vendor request handlers ----------------------------------------------

void on_get_destinations(obs_data_t * /*req*/, obs_data_t *res, void *)
{
    auto items = g_registry->snapshot();
    OBSDataArrayAutoRelease arr = obs_data_array_create();
    for (auto &s : items) {
        OBSDataAutoRelease item = obs_data_create();
        obs_data_set_string(item, "id", s.id.c_str());
        obs_data_set_string(item, "name", s.name.c_str());
        obs_data_set_string(item, "kind", s.kind.c_str());
        obs_data_set_string(item, "url", s.url.c_str());
        obs_data_set_bool(item, "enabled", s.enabled);
        obs_data_set_bool(item, "active", s.active);
        obs_data_array_push_back(arr, item);
    }
    obs_data_set_array(res, "destinations", arr);
}

void on_create_destination(obs_data_t *req, obs_data_t *res, void *)
{
    const char *name = obs_data_get_string(req, "name");
    const char *kindS = obs_data_get_string(req, "kind");
    const char *url  = obs_data_get_string(req, "url");
    const char *key  = obs_data_get_string(req, "key");

    DestinationKind kind = kind_from_string(kindS);
    if (kind == Kind_Unknown) {
        obs_data_set_string(res, "error", "kind must be 'rtmp_custom', 'vod_local', or 'twitch'");
        return;
    }

    std::string err;
    if (!validate_destination_input(kind, url, key, err)) {
        obs_data_set_string(res, "error", err.c_str());
        return;
    }

    // For Twitch the server URL is fixed; the user-supplied url field is
    // ignored. Stash the pinned URL so GetDestinations + diagnostics
    // surface it consistently.
    const char *storeUrl = (kind == Kind_Twitch) ? TWITCH_INGEST_URL : url;

    auto id = g_registry->create(name ? name : "", kind, storeUrl, key ? key : "");
    obs_data_set_string(res, "id", id.c_str());
}

void on_remove_destination(obs_data_t *req, obs_data_t *res, void *)
{
    const char *id = obs_data_get_string(req, "id");
    bool ok = id && *id && g_registry->remove(id);
    obs_data_set_bool(res, "removed", ok);
    if (!ok) obs_data_set_string(res, "error", "no such destination");
}

void on_start_destination(obs_data_t *req, obs_data_t *res, void *)
{
    const char *id = obs_data_get_string(req, "id");
    if (!id || !*id) {
        obs_data_set_string(res, "error", "id required");
        return;
    }
    std::string err;
    bool ok = g_registry->start(id, err);
    obs_data_set_bool(res, "started", ok);
    if (!ok) obs_data_set_string(res, "error", err.c_str());
}

void on_stop_destination(obs_data_t *req, obs_data_t *res, void *)
{
    const char *id = obs_data_get_string(req, "id");
    if (!id || !*id) {
        obs_data_set_string(res, "error", "id required");
        return;
    }
    obs_data_set_bool(res, "stopped", g_registry->stop(id));
}

void on_start_all(obs_data_t * /*req*/, obs_data_t *res, void *)
{
    g_registry->start_all();
    obs_data_set_bool(res, "ok", true);
}

void on_stop_all(obs_data_t * /*req*/, obs_data_t *res, void *)
{
    g_registry->stop_all();
    obs_data_set_bool(res, "ok", true);
}

// ---- Phase 12a: video / encoder settings vendor requests --------------

// Returns the encoders the streaming output is wired to. Lazy: callers
// dispose of the OBSOutputAutoRelease, but the encoder pointers belong to
// frontend-stub and must NOT be released.
static void get_current_encoders(obs_encoder_t *&vEnc, obs_encoder_t *&aEnc)
{
    OBSOutputAutoRelease srcOutput = obs_frontend_get_streaming_output();
    if (!srcOutput) {
        vEnc = nullptr;
        aEnc = nullptr;
        return;
    }
    vEnc = obs_output_get_video_encoder(srcOutput);
    aEnc = obs_output_get_audio_encoder(srcOutput, 0);
}

void on_get_video_settings(obs_data_t * /*req*/, obs_data_t *res, void *)
{
    obs_video_info ovi = {};
    if (obs_get_video_info(&ovi)) {
        obs_data_set_int(res, "fps", ovi.fps_num / (ovi.fps_den ? ovi.fps_den : 1));
        obs_data_set_int(res, "width", static_cast<long long>(ovi.output_width));
        obs_data_set_int(res, "height", static_cast<long long>(ovi.output_height));
    }
    obs_encoder_t *vEnc = nullptr, *aEnc = nullptr;
    get_current_encoders(vEnc, aEnc);
    if (vEnc) {
        OBSDataAutoRelease s = obs_encoder_get_settings(vEnc);
        obs_data_set_int(res, "video_bitrate", obs_data_get_int(s, "bitrate"));
        obs_data_set_string(res, "video_rate_control", obs_data_get_string(s, "rate_control"));
        obs_data_set_int(res, "video_keyint_sec", obs_data_get_int(s, "keyint_sec"));
    }
    if (aEnc) {
        OBSDataAutoRelease s = obs_encoder_get_settings(aEnc);
        obs_data_set_int(res, "audio_bitrate", obs_data_get_int(s, "bitrate"));
    }
}

void on_get_adaptive_state(obs_data_t * /*req*/, obs_data_t *res, void *)
{
    if (!g_adaptive) {
        obs_data_set_string(res, "error", "adaptive subsystem not initialised");
        return;
    }
    auto s = g_adaptive->get_state();
    obs_data_set_bool(res, "enabled", s.enabled);
    obs_data_set_int(res, "target_kbps", s.target_kbps);
    obs_data_set_int(res, "current_kbps", s.current_kbps);
    obs_data_set_int(res, "floor_kbps", s.floor_kbps);
    obs_data_set_int(res, "stable_ticks", s.stable_ticks);
    obs_data_set_int(res, "adjustments_total",
                     static_cast<long long>(s.adjustments_total));
    obs_data_set_int(res, "last_delta_total",
                     static_cast<long long>(s.last_delta_total));
    obs_data_set_int(res, "last_delta_dropped",
                     static_cast<long long>(s.last_delta_dropped));
    obs_data_set_double(res, "last_drop_ratio", s.last_drop_ratio);
}

void on_set_adaptive_enabled(obs_data_t *req, obs_data_t *res, void *)
{
    if (!g_adaptive) {
        obs_data_set_string(res, "error", "adaptive subsystem not initialised");
        return;
    }
    if (!obs_data_has_user_value(req, "enabled")) {
        obs_data_set_string(res, "error", "enabled (bool) required");
        return;
    }
    bool e = obs_data_get_bool(req, "enabled");
    g_adaptive->set_enabled(e);
    obs_data_set_bool(res, "enabled", e);
}

void on_set_video_settings(obs_data_t *req, obs_data_t *res, void *)
{
    // fps / width / height are pinned at obs_reset_video time. Surface a
    // typed rejection if a client tries to mutate them; pulsar-headless
    // would have to restart for that to take effect.
    if (obs_data_has_user_value(req, "fps") || obs_data_has_user_value(req, "width") ||
        obs_data_has_user_value(req, "height")) {
        obs_data_set_string(res, "error",
            "fps / width / height are fixed at boot via PULSAR_FPS / PULSAR_RESOLUTION; "
            "restart pulsar.exe with new env vars to change them");
        return;
    }

    obs_encoder_t *vEnc = nullptr, *aEnc = nullptr;
    get_current_encoders(vEnc, aEnc);

    bool changed = false;

    if (obs_data_has_user_value(req, "video_bitrate") && vEnc) {
        long long newKbps = obs_data_get_int(req, "video_bitrate");
        if (newKbps < 200 || newKbps > 50000) {
            obs_data_set_string(res, "error", "video_bitrate must be in [200, 50000] kbps");
            return;
        }
        OBSDataAutoRelease patch = obs_data_create();
        obs_data_set_int(patch, "bitrate", newKbps);
        obs_encoder_update(vEnc, patch);
        obs_data_set_int(res, "video_bitrate", newKbps);
        changed = true;
    }

    if (obs_data_has_user_value(req, "audio_bitrate") && aEnc) {
        long long newKbps = obs_data_get_int(req, "audio_bitrate");
        if (newKbps < 32 || newKbps > 512) {
            obs_data_set_string(res, "error", "audio_bitrate must be in [32, 512] kbps");
            return;
        }
        if (obs_encoder_active(aEnc)) {
            // ffmpeg_aac re-init on bitrate change is not supported mid-stream;
            // reject so we don't introduce hidden encoder restarts.
            obs_data_set_string(res, "error",
                "audio_bitrate cannot change while audio encoder is active; stop all "
                "outputs first");
            return;
        }
        OBSDataAutoRelease patch = obs_data_create();
        obs_data_set_int(patch, "bitrate", newKbps);
        obs_encoder_update(aEnc, patch);
        obs_data_set_int(res, "audio_bitrate", newKbps);
        changed = true;
    }

    obs_data_set_bool(res, "changed", changed);
}

} // namespace

// ---- module entry points ---------------------------------------------------

bool obs_module_load(void)
{
    blog(LOG_INFO, "[pulsar-multi-stream] obs_module_load");
    g_registry = new DestinationRegistry();
    g_adaptive = new AdaptiveBitrate();
    return true;
}

// Vendor registration must happen here (post-load) so obs-websocket's
// proc handler is published. This is the constraint stamped on the
// obs_websocket_register_vendor doxygen.
void obs_module_post_load(void)
{
    g_vendor = obs_websocket_register_vendor("pulsar");
    if (!g_vendor) {
        blog(LOG_WARNING, "[pulsar-multi-stream] obs-websocket not present; vendor API disabled");
        return;
    }

    obs_websocket_vendor_register_request(g_vendor, "GetDestinations",      on_get_destinations,    nullptr);
    obs_websocket_vendor_register_request(g_vendor, "CreateDestination",    on_create_destination,  nullptr);
    obs_websocket_vendor_register_request(g_vendor, "RemoveDestination",    on_remove_destination,  nullptr);
    obs_websocket_vendor_register_request(g_vendor, "StartDestination",     on_start_destination,   nullptr);
    obs_websocket_vendor_register_request(g_vendor, "StopDestination",      on_stop_destination,    nullptr);
    obs_websocket_vendor_register_request(g_vendor, "StartAllDestinations", on_start_all,           nullptr);
    obs_websocket_vendor_register_request(g_vendor, "StopAllDestinations",  on_stop_all,            nullptr);
    obs_websocket_vendor_register_request(g_vendor, "GetVideoSettings",     on_get_video_settings,  nullptr);
    obs_websocket_vendor_register_request(g_vendor, "SetVideoSettings",     on_set_video_settings,  nullptr);
    obs_websocket_vendor_register_request(g_vendor, "GetAdaptiveState",     on_get_adaptive_state,  nullptr);
    obs_websocket_vendor_register_request(g_vendor, "SetAdaptiveEnabled",   on_set_adaptive_enabled, nullptr);

    blog(LOG_INFO, "[pulsar-multi-stream] vendor 'pulsar' registered with 11 requests");

    // Phase 12b: spin up the adaptive bitrate worker AFTER vendor registration
    // so its emit_event path has a valid handle.
    if (g_adaptive) g_adaptive->start();
}

void obs_module_unload(void)
{
    blog(LOG_INFO, "[pulsar-multi-stream] obs_module_unload");
    // Order matters: stop the adaptive thread BEFORE tearing down the
    // registry, otherwise the worker can read freed Destination snapshots
    // mid-tick.
    if (g_adaptive) {
        g_adaptive->stop();
        delete g_adaptive;
        g_adaptive = nullptr;
    }
    if (g_registry) {
        g_registry->teardown_all();
        delete g_registry;
        g_registry = nullptr;
    }
    g_vendor = nullptr; // obs-websocket cleans its own vendor table
}
