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

#include <algorithm>
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

// Twitch's global ingest, TLS-terminated. This is the `url_template_secure`
// of the "Default" entry of https://ingest.twitch.tv/ingests -- the global
// host LB-redirects to the closest region, which is fine for any
// non-low-latency use case. Twitch's stream key is per-channel.
//
// Must stay rtmps://: the stream key is a bearer credential and travels in
// the RTMP connect handshake, so a plain rtmp:// ingest puts it on the wire
// in cleartext at every go-live. The legacy `live.twitch.tv` host is not in
// Twitch's published ingest list and is not documented to terminate TLS.
// librtmp derives the transport from this scheme alone (parseurl.c, protocol
// RTMP_PROTOCOL_RTMPS -> RTMP_FEATURE_SSL, default port 443) and fails the
// connection outright if the TLS handshake or certificate verification fails
// (rtmp.c RTMP_Connect1) -- there is no downgrade path to cleartext.
constexpr const char *TWITCH_INGEST_URL = "rtmps://ingest.global-contribute.live-video.net/app/";

// Fail closed at compile time: no future edit may point the pinned Twitch
// destination at a cleartext ingest.
static_assert(TWITCH_INGEST_URL[0] == 'r' && TWITCH_INGEST_URL[1] == 't' && TWITCH_INGEST_URL[2] == 'm' &&
                      TWITCH_INGEST_URL[3] == 'p' && TWITCH_INGEST_URL[4] == 's' && TWITCH_INGEST_URL[5] == ':' &&
                      TWITCH_INGEST_URL[6] == '/' && TWITCH_INGEST_URL[7] == '/',
              "TWITCH_INGEST_URL must use the rtmps:// scheme -- the stream key must never travel in cleartext");

const char *kind_to_string(DestinationKind k)
{
    switch (k) {
    case Kind_RtmpCustom: return "rtmp_custom";
    case Kind_VodLocal:   return "vod_local";
    case Kind_Twitch:     return "twitch";
    default:              return "unknown";
    }
}

// The obs output type each kind is served by. Single source of truth: both
// ensure_output() (which instantiates it) and the capability manifest (which
// declares the kind available only if that type is registered in this binary)
// read it from here, so the two can never disagree.
const char *output_id_for_kind(DestinationKind k)
{
    switch (k) {
    case Kind_RtmpCustom:
    case Kind_Twitch:     return "rtmp_output";
    case Kind_VodLocal:   return "ffmpeg_muxer";
    default:              return nullptr;
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
        const char *outId = output_id_for_kind(d.kind);
        if (!outId) {
            errOut = "unknown destination kind";
            return false;
        }
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
        // Drain the worker thread before release so we don't free
        // the output struct while a thread is mid-callback on it.
        //
        // Strategy : graceful stop first (lets ffmpeg_muxer write its
        // moov atom, lets rtmp_output close the connection cleanly),
        // wait up to 3 s for active() to drop, then a fixed tail to
        // cover the worker-mid-callback window where active() flips
        // to false slightly before the worker thread fully exits.
        // force_stop is deliberately NOT called -- it short-circuits
        // the flush path and races the worker more aggressively than
        // a graceful stop.
        obs_output_stop(d.output);
        for (int i = 0; i < 150 && obs_output_active(d.output); ++i)
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
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
    // obs_output_stop is idempotent and also valid in the "starting"
    // window where active() reports false. Calling it unconditionally
    // means stopping a destination whose connect attempt is still in
    // flight (e.g. rtmp_custom against a dead address) cleanly drains
    // the worker thread instead of leaking it until release.
    if (d.output)
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
        // Same rationale as stop() : skip the active() gate so an
        // output stuck in the starting state cleanly drains.
        if (d.output)
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
        // Monotonic count of completed sampling ticks (incremented after
        // first_tick_ initialisation). Lets external observers tell that
        // the worker is alive even when no bitrate adjustment has fired.
        uint64_t samples_total = 0;
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
            state_.samples_total += 1;
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

// ADR 004 §3.3 mapping: concrete obs encoder id -> reported family short name.
// The ONLY strings ever returned to the wire are the four whitelisted families;
// a non-H.264-streaming id maps to nullptr and is dropped, so an over-wide or
// unknown obs id can never leak a raw obs string into the capabilities payload.
static const char *encoder_family_for_id(const char *id)
{
    if (!id) return nullptr;
    if (std::strcmp(id, "obs_x264") == 0)
        return "x264";
    if (std::strcmp(id, "jim_nvenc") == 0 || std::strcmp(id, "obs_nvenc_h264_tex") == 0 ||
        std::strcmp(id, "ffmpeg_nvenc") == 0)
        return "nvenc";
    if (std::strcmp(id, "obs_qsv11_v2") == 0 || std::strcmp(id, "obs_qsv11") == 0)
        return "qsv";
    if (std::strcmp(id, "h264_texture_amf") == 0)
        return "amf";
    return nullptr;
}

// Family of the encoder currently bound to the streaming output, or "x264" as
// the safe whitelisted default when nothing is bound / the id is unrecognised
// (frontend-stub only ever creates whitelisted ids, so this is defensive).
static const char *active_encoder_family(obs_encoder_t *vEnc)
{
    if (!vEnc) return "x264";
    const char *fam = encoder_family_for_id(obs_encoder_get_id(vEnc));
    return fam ? fam : "x264";
}

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

// ---- Capability manifest (Prism ADR 027 §3.1/§3.2, issue #141) --------------
//
// GetCapabilities is the single authoritative statement of what THIS Pulsar can
// do. Two structural rules hold for every entry, now and for the encoder /
// audio / inventory / video blocks that land in #142-#144:
//
//   1. Values are READ, never decreed. A number that cannot be obtained from
//      libobs is declared ABSENT (the key is simply omitted) -- it is never
//      replaced by a plausible constant. Absence is a positive answer and is
//      distinct from a regime: the consumer keeps its own static bound.
//   2. Every entry carries its APPLICATION REGIME alongside its values, so the
//      consumer derives its apply-class instead of decreeing one.
//
// Payload shape (additive; the pre-#141 top-level keys are kept verbatim so a
// client that only knows the old contract keeps working):
//
//   {
//     "version": 1,
//     "encoders": [{value}], "active_encoder": "...",        // legacy mirrors
//     "video_bitrate": {min,max}, "audio_bitrate": [{value}], // legacy mirrors
//     "capabilities": {
//       "<name>": { "applicability": "live"|"boot-fixed"|"read-only", ... }
//     }
//   }
//
// A consumer that does not know "capabilities" ignores it; a consumer that does
// must tolerate entries it has never heard of.

// Bumped only on a STRUCTURAL change to the payload. Adding a new entry under
// "capabilities" is additive and does NOT bump it -- that is the whole point of
// the block being a map.
static constexpr long long kCapabilityManifestVersion = 1;

// The three regimes of ADR 027 §3.2. kRegimeReadOnly is carried by the whole
// audio block (#143) and by the video colorimetry entry (#144).
static constexpr const char *kRegimeLive = "live";
static constexpr const char *kRegimeBootFixed = "boot-fixed";
static constexpr const char *kRegimeReadOnly = "read-only";

// Pulsar's own service policy for the streaming bitrates. This is NOT an
// encoder capability -- it is the range Pulsar itself accepts, enforced by
// on_set_video_settings below and mirrored by the PULSAR_VIDEO_BITRATE boot
// clamp (pulsar-frontend-stub.cpp:942). The manifest publishes the
// INTERSECTION of this policy with what the encoder advertises, so the
// manifest can never announce a value the setter would reject (ADR 027 §3.1:
// the manifest may only narrow, never widen).
static constexpr long long kPolicyVideoBitrateMinKbps = 200;
static constexpr long long kPolicyVideoBitrateMaxKbps = 50000;
static constexpr long long kPolicyAudioBitrateMinKbps = 32;
static constexpr long long kPolicyAudioBitrateMaxKbps = 512;

// ---- Audio block (Prism ADR 027 §3.3 bloc 2, issue #143) -------------------
//
// Prism offers three headphone-monitoring keys as `applyClass: live`, "verified
// by read-back" of a state nobody established comes back. It does not: NOTHING
// in this tree ever calls obs_set_audio_monitoring_device(), so no monitoring
// device is ever bound by Pulsar and no write path exists over the wire. The
// regime is therefore read-only, not live (ADR 027 §3.2: `live` requires the
// write AND the read-back to be genuinely supported hot).
//
// One trap deserves the explicit sentinel test below: libobs SEEDS
// monitoring_device_name/_id with "Default"/"default" inside obs_init_audio()
// (upstream/libobs/obs.c:916-917), before anyone chooses anything. Reporting
// that seed as a bound device would republish exactly the fiction this block
// exists to kill -- so a device counts as bound only when the id is present and
// is not that seed. `device_bound` is always emitted, true or false: an absent
// device is a positive, readable "no", never a silence (criterion 2).
static constexpr const char *kMonitoringDeviceSeedId = "default";

// Canonical name of a libobs speaker layout. Returns nullptr for
// SPEAKERS_UNKNOWN -- the layout is then declared absent rather than published
// as a placeholder string.
static const char *speaker_layout_name(enum speaker_layout speakers)
{
    switch (speakers) {
    case SPEAKERS_MONO:    return "mono";
    case SPEAKERS_STEREO:  return "stereo";
    case SPEAKERS_2POINT1: return "2.1";
    case SPEAKERS_4POINT0: return "4.0";
    case SPEAKERS_4POINT1: return "4.1";
    case SPEAKERS_5POINT1: return "5.1";
    case SPEAKERS_7POINT1: return "7.1";
    case SPEAKERS_UNKNOWN: return nullptr;
    }
    return nullptr;
}

// Number of audio tracks the streaming output actually carries, read by walking
// its mixer slots. Returns false when no output is bound (off-air), in which
// case the caller declares the count absent instead of guessing one.
static bool read_bound_audio_tracks(long long &bound)
{
    OBSOutputAutoRelease srcOutput = obs_frontend_get_streaming_output();
    if (!srcOutput) return false;
    bound = 0;
    for (size_t i = 0; i < static_cast<size_t>(MAX_AUDIO_MIXES); i++) {
        if (obs_output_get_audio_encoder(srcOutput, i)) bound++;
    }
    return true;
}

// obs_properties_t has no OBSRefAutoRelease alias in obs.hpp; scope it here.
class PropsGuard {
public:
    explicit PropsGuard(obs_properties_t *p) : props(p) {}
    ~PropsGuard() { if (props) obs_properties_destroy(props); }
    PropsGuard(const PropsGuard &) = delete;
    PropsGuard &operator=(const PropsGuard &) = delete;
    obs_properties_t *get() const { return props; }
    explicit operator bool() const { return props != nullptr; }

private:
    obs_properties_t *props;
};

// Properties of the encoder actually bound to the streaming output, or -- when
// nothing is bound (off-air detection) -- of the registered encoder id that
// active_encoder_family()/frontend-stub would use. Both paths read libobs; the
// caller never synthesises a value.
static obs_properties_t *encoder_properties_or_id(obs_encoder_t *enc, const char *fallbackId)
{
    if (enc) return obs_encoder_properties(enc);
    if (fallbackId && *fallbackId) return obs_get_encoder_properties(fallbackId);
    return nullptr;
}

// Reads the [min,max] kbps window the encoder advertises for "bitrate".
// Returns false when the property is missing or is not an int range -- the
// caller then declares the capability absent.
static bool read_bitrate_window(obs_encoder_t *enc, const char *fallbackId, long long &minKbps,
                                long long &maxKbps, long long &stepKbps)
{
    PropsGuard props(encoder_properties_or_id(enc, fallbackId));
    if (!props) return false;
    obs_property_t *p = obs_properties_get(props.get(), "bitrate");
    if (!p || obs_property_get_type(p) != OBS_PROPERTY_INT) return false;

    const long long lo = obs_property_int_min(p);
    const long long hi = obs_property_int_max(p);
    const long long step = obs_property_int_step(p);
    if (lo <= 0 || hi < lo) return false;

    minKbps = lo;
    maxKbps = hi;
    stepKbps = step > 0 ? step : 1;
    return true;
}

// Narrows an encoder-advertised window by the Pulsar policy window. Returns
// false when the intersection is empty, in which case the entry is omitted
// rather than published as a window nothing would accept.
static bool narrow_to_policy(long long &lo, long long &hi, long long policyLo, long long policyHi)
{
    lo = std::max(lo, policyLo);
    hi = std::min(hi, policyHi);
    return lo <= hi;
}

// Pushes an { "value": <id> } item per enumerated id. Returns the number of
// items pushed so the caller can declare an empty inventory ABSENT rather than
// publish an empty list (§3.2: absence is a positive answer, an empty array
// would read as "this binary registers nothing", which is not what an
// enumeration that yielded nothing means).
template <typename EnumFn>
static size_t collect_ids(obs_data_array_t *out, EnumFn enumerate)
{
    size_t n = 0;
    const char *id = nullptr;
    for (size_t i = 0; enumerate(i, &id); ++i) {
        if (!id || !*id) continue;
        OBSDataAutoRelease item = obs_data_create();
        obs_data_set_string(item, "value", id);
        obs_data_array_push_back(out, item);
        ++n;
    }
    return n;
}

// True iff this binary registers that obs output type.
static bool output_type_registered(const char *wantedId)
{
    if (!wantedId) return false;
    const char *id = nullptr;
    for (size_t i = 0; obs_enum_output_types(i, &id); ++i) {
        if (id && std::strcmp(id, wantedId) == 0) return true;
    }
    return false;
}

// Compact token for a libobs colourspace. Transcribes the switch of
// get_video_colorspace_name() (libobs/media-io/video-io.h) -- including its
// treatment of VIDEO_CS_DEFAULT as Rec. 709 -- into the vocabulary the
// consumer already uses. An enum value libobs grows later maps to nullptr and
// the entry is declared absent, never guessed.
static const char *colorspace_token(enum video_colorspace cs)
{
    switch (cs) {
    case VIDEO_CS_DEFAULT:
    case VIDEO_CS_709:      return "709";
    case VIDEO_CS_601:      return "601";
    case VIDEO_CS_SRGB:     return "srgb";
    case VIDEO_CS_2100_PQ:  return "2100pq";
    case VIDEO_CS_2100_HLG: return "2100hlg";
    }
    return nullptr;
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
        // ADR 004 §3.4: complete off-air snapshot of the boot-fixed encoder.
        obs_data_set_string(res, "video_encoder", active_encoder_family(vEnc));
        obs_data_set_string(res, "video_preset", obs_data_get_string(s, "preset"));
        obs_data_set_string(res, "video_profile", obs_data_get_string(s, "profile"));
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
    obs_data_set_int(res, "samples",
                     static_cast<long long>(s.samples_total));
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

    // ADR 004 §3.4: encoder identity/preset/profile join the boot-fixed tier.
    // A live encoder swap would tear down and recreate the whole output binding
    // -- exactly the fragility the boot-fixed tier exists to avoid. Reject with
    // the same typed "respawn to change" contract as fps.
    if (obs_data_has_user_value(req, "video_encoder") ||
        obs_data_has_user_value(req, "video_preset") ||
        obs_data_has_user_value(req, "video_profile")) {
        obs_data_set_string(res, "error",
            "video_encoder / video_preset / video_profile are fixed at boot via "
            "PULSAR_VIDEO_ENCODER; restart pulsar.exe with new env vars to change them");
        return;
    }

    obs_encoder_t *vEnc = nullptr, *aEnc = nullptr;
    get_current_encoders(vEnc, aEnc);

    bool changed = false;

    if (obs_data_has_user_value(req, "video_bitrate") && vEnc) {
        long long newKbps = obs_data_get_int(req, "video_bitrate");
        if (newKbps < kPolicyVideoBitrateMinKbps || newKbps > kPolicyVideoBitrateMaxKbps) {
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
        if (newKbps < kPolicyAudioBitrateMinKbps || newKbps > kPolicyAudioBitrateMaxKbps) {
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

// ADR 004 §3.3: enumerate the encoders this build actually exposes and report
// them as the whitelisted family short names Prism's registry consumes.
// active_encoder is the family currently bound to the streaming output (feeds
// GetVideoSettings §3.4).
//
// ADR 027 §3.2 (#141): the response now carries a "version" and a
// "capabilities" map in which every entry declares its application regime next
// to its values, and both bitrate windows are READ from the encoder's libobs
// properties instead of being written as literals. See the manifest comment
// block above get_current_encoders() for the contract.
void on_get_capabilities(obs_data_t * /*req*/, obs_data_t *res, void *)
{
    obs_data_set_int(res, "version", kCapabilityManifestVersion);
    OBSDataAutoRelease caps = obs_data_create();

    OBSDataArrayAutoRelease encoders = obs_data_array_create();
    bool seen[5] = {false, false, false, false, false}; // x264,nvenc,qsv,amf sentinel
    auto family_index = [](const char *f) -> int {
        if (std::strcmp(f, "x264") == 0) return 0;
        if (std::strcmp(f, "nvenc") == 0) return 1;
        if (std::strcmp(f, "qsv") == 0) return 2;
        if (std::strcmp(f, "amf") == 0) return 3;
        return 4;
    };

    const char *encId = nullptr;
    for (size_t i = 0; obs_enum_encoder_types(i, &encId); ++i) {
        const char *fam = encoder_family_for_id(encId);
        if (!fam) continue; // non-whitelisted / non-H.264-streaming id: dropped
        int idx = family_index(fam);
        if (seen[idx]) continue;
        seen[idx] = true;
        OBSDataAutoRelease item = obs_data_create();
        obs_data_set_string(item, "value", fam);
        obs_data_array_push_back(encoders, item);
    }
    obs_data_set_array(res, "encoders", encoders);

    obs_encoder_t *vEnc = nullptr, *aEnc = nullptr;
    get_current_encoders(vEnc, aEnc);
    const char *activeFamily = active_encoder_family(vEnc);
    obs_data_set_string(res, "active_encoder", activeFamily);

    // Encoder identity is boot-fixed: on_set_video_settings rejects any live
    // mutation of video_encoder / video_preset / video_profile (see above).
    {
        OBSDataAutoRelease entry = obs_data_create();
        obs_data_set_string(entry, "applicability", kRegimeBootFixed);
        obs_data_set_array(entry, "values", encoders);
        obs_data_set_obj(caps, "encoders", entry);
    }
    {
        OBSDataAutoRelease entry = obs_data_create();
        obs_data_set_string(entry, "applicability", kRegimeBootFixed);
        obs_data_set_string(entry, "value", activeFamily);
        obs_data_set_obj(caps, "active_encoder", entry);
    }

    // Video bitrate: read the window the active (or, off-air, the registered)
    // encoder advertises, narrowed by the Pulsar policy window. Unreadable ->
    // the entry AND its legacy mirror are omitted, never guessed.
    {
        long long lo = 0, hi = 0, step = 0;
        if (read_bitrate_window(vEnc, "obs_x264", lo, hi, step) &&
            narrow_to_policy(lo, hi, kPolicyVideoBitrateMinKbps, kPolicyVideoBitrateMaxKbps)) {
            OBSDataAutoRelease legacy = obs_data_create();
            obs_data_set_int(legacy, "min", lo);
            obs_data_set_int(legacy, "max", hi);
            obs_data_set_obj(res, "video_bitrate", legacy);

            OBSDataAutoRelease entry = obs_data_create();
            obs_data_set_string(entry, "applicability", kRegimeLive);
            obs_data_set_int(entry, "min", lo);
            obs_data_set_int(entry, "max", hi);
            obs_data_set_int(entry, "step", step);
            obs_data_set_obj(caps, "video_bitrate", entry);
        }
    }

    // Audio bitrate: ffmpeg_aac advertises "bitrate" as an int range with a
    // step (obs-ffmpeg-audio-encoders.c), NOT as a list -- so the discrete
    // ladder is DERIVED from that range, it is no longer a literal table. The
    // legacy array mirror is generated from the same derived values.
    {
        long long rawLo = 0, rawHi = 0, step = 0;
        if (read_bitrate_window(aEnc, "ffmpeg_aac", rawLo, rawHi, step)) {
            long long lo = rawLo, hi = rawHi;
            if (narrow_to_policy(lo, hi, kPolicyAudioBitrateMinKbps, kPolicyAudioBitrateMaxKbps)) {
                // Walk the encoder's own grid (rawLo + k*step) and keep the
                // points the policy admits, so a narrowed bound never invents
                // an offset value the encoder does not offer.
                OBSDataArrayAutoRelease ladder = obs_data_array_create();
                long long first = 0, last = 0;
                bool any = false;
                for (long long kbps = rawLo; kbps <= rawHi; kbps += step) {
                    if (kbps < lo) continue;
                    if (kbps > hi) break;
                    OBSDataAutoRelease item = obs_data_create();
                    obs_data_set_int(item, "value", kbps);
                    obs_data_array_push_back(ladder, item);
                    if (!any) first = kbps;
                    last = kbps;
                    any = true;
                }
                // No grid point survives the policy window -> the capability is
                // declared absent rather than published empty.
                if (any) {
                    obs_data_set_array(res, "audio_bitrate", ladder);

                    OBSDataAutoRelease entry = obs_data_create();
                    obs_data_set_string(entry, "applicability", kRegimeLive);
                    obs_data_set_int(entry, "min", first);
                    obs_data_set_int(entry, "max", last);
                    obs_data_set_int(entry, "step", step);
                    obs_data_set_array(entry, "values", ladder);
                    obs_data_set_obj(caps, "audio_bitrate", entry);
                }
            }
        }
    }

    // ---- Inventories (ADR 027 §3.3 block 3, issue #144) --------------------
    //
    // PRESENCE ONLY, never permission. This block answers "what exists in this
    // binary", and nothing else:
    //
    //   * filters -- WHICH filter types are registered. NO property bound of
    //     any filter is emitted here, now or ever: the admitted keys and their
    //     bounded schemas stay owned by Prism's closed whitelist (ADR 023 §3.3,
    //     under its own Bastion clearance). Deriving a bound from this list
    //     would void that control, which is precisely what §3.1 forbids.
    //   * source_kinds / destination_kinds -- WHICH kinds can be instantiated.
    //     The discriminated union and strict dispatch of ADR 010 are untouched:
    //     a kind the consumer does not know stays ignorable, it is never
    //     something Pulsar asks it to route.
    //
    // Regime is `live` for the three: a filter, a source and a destination of a
    // declared kind can all be created on a running Pulsar. The inventory
    // itself is fixed for the process lifetime, but the regime states when the
    // capability APPLIES, not whether the list can be rewritten.
    //
    // An enumeration that yields nothing publishes no entry at all -- the
    // consumer keeps its own static list rather than reading an empty array as
    // "this binary has none".
    {
        OBSDataArrayAutoRelease filters = obs_data_array_create();
        if (collect_ids(filters, obs_enum_filter_types) > 0) {
            OBSDataAutoRelease entry = obs_data_create();
            obs_data_set_string(entry, "applicability", kRegimeLive);
            obs_data_set_array(entry, "values", filters);
            obs_data_set_obj(caps, "filters", entry);
        }
    }

    // Inputs, not obs_enum_source_types: the latter also yields filter and
    // transition types, which are not things a consumer can create as a source.
    {
        OBSDataArrayAutoRelease sources = obs_data_array_create();
        if (collect_ids(sources, obs_enum_input_types) > 0) {
            OBSDataAutoRelease entry = obs_data_create();
            obs_data_set_string(entry, "applicability", kRegimeLive);
            obs_data_set_array(entry, "values", sources);
            obs_data_set_obj(caps, "source_kinds", entry);
        }
    }

    // Pulsar's own destination vocabulary, walked from the DestinationKind enum
    // (kind_to_string is the single source of the strings) and gated on the obs
    // output type that serves it being registered in THIS binary. A kind whose
    // output type is missing could not be started, so it is not declared.
    {
        OBSDataArrayAutoRelease kinds = obs_data_array_create();
        size_t n = 0;
        for (int k = 0; k < Kind_Unknown; ++k) {
            const DestinationKind kind = static_cast<DestinationKind>(k);
            if (!output_type_registered(output_id_for_kind(kind))) continue;
            OBSDataAutoRelease item = obs_data_create();
            obs_data_set_string(item, "value", kind_to_string(kind));
            obs_data_array_push_back(kinds, item);
            ++n;
        }
        if (n > 0) {
            OBSDataAutoRelease entry = obs_data_create();
            obs_data_set_string(entry, "applicability", kRegimeLive);
            obs_data_set_array(entry, "values", kinds);
            obs_data_set_obj(caps, "destination_kinds", entry);
        }
    }

    // ---- Video colorimetry (ADR 027 §3.3 block 4, issue #144) --------------
    //
    // Colourspace, range and pixel format are pinned once at obs_reset_video
    // (pulsar-headless/main.cpp:161-163) and read back here from libobs. The
    // regime is READ-ONLY, not boot-fixed: no env var and no request can select
    // another one, so declaring `boot-fixed` would promise a respawn knob that
    // does not exist. No set of "available" spaces is published for the same
    // reason -- nothing can select one, and announcing a choice the binary
    // cannot honour is exactly the decree §3.1 exists to prevent.
    {
        obs_video_info ovi = {};
        const char *cs = nullptr;
        if (obs_get_video_info(&ovi) && (cs = colorspace_token(ovi.colorspace)) != nullptr) {
            OBSDataAutoRelease entry = obs_data_create();
            obs_data_set_string(entry, "applicability", kRegimeReadOnly);
            obs_data_set_string(entry, "value", cs);
            // resolve_video_range / get_video_*_name are libobs' own helpers:
            // VIDEO_RANGE_DEFAULT is resolved the way libobs resolves it, not
            // the way we would guess it.
            obs_data_set_string(entry, "range",
                                get_video_range_name(ovi.output_format, ovi.range));
            obs_data_set_string(entry, "format", get_video_format_name(ovi.output_format));
            obs_data_set_obj(caps, "video_colorimetry", entry);
        }
    }

    // ---- Audio block (ADR 027 §3.3 bloc 2, #143) ---------------------------
    // Four entries, each with its own regime -- they are siblings in the map
    // rather than one nested "audio" object precisely because their regimes
    // differ, and a regime belongs to an entry, not to a family.

    // Monitoring: read-only. See the kMonitoringDeviceSeedId comment above --
    // Pulsar has no monitoring write path at all, so `live` would be a lie.
    // `available` and `device_bound` are ALWAYS emitted; the identity keys only
    // when a device is genuinely bound.
    {
        OBSDataAutoRelease entry = obs_data_create();
        obs_data_set_string(entry, "applicability", kRegimeReadOnly);

        const bool available = obs_audio_monitoring_available();
        obs_data_set_bool(entry, "available", available);

        const char *devName = nullptr;
        const char *devId = nullptr;
        if (available) obs_get_audio_monitoring_device(&devName, &devId);

        const bool bound = available && devId && *devId &&
                           std::strcmp(devId, kMonitoringDeviceSeedId) != 0;
        obs_data_set_bool(entry, "device_bound", bound);
        if (bound) {
            obs_data_set_string(entry, "device_id", devId);
            obs_data_set_string(entry, "device_name", devName ? devName : "");
        }
        obs_data_set_obj(caps, "audio_monitoring", entry);
    }

    // Tracks: read-only. `count` is libobs's own mixer-slot count (MAX_AUDIO_MIXES,
    // media-io/audio-io.h) -- the ceiling of the running libobs, not a Pulsar
    // literal. `bound` is read from the streaming output and is omitted off-air.
    {
        OBSDataAutoRelease entry = obs_data_create();
        obs_data_set_string(entry, "applicability", kRegimeReadOnly);
        obs_data_set_int(entry, "count", static_cast<long long>(MAX_AUDIO_MIXES));
        long long bound = 0;
        if (read_bound_audio_tracks(bound)) obs_data_set_int(entry, "bound", bound);
        obs_data_set_obj(caps, "audio_tracks", entry);
    }

    // Sample rate and speaker layout come from the SAME obs_get_audio_info()
    // read; when it fails both entries are declared absent rather than filled
    // with the 48000/stereo pair pulsar-headless happens to boot with.
    //
    // Regime is read-only, NOT boot-fixed: boot-fixed means "settable at boot,
    // refused hot", and there is no env knob for either -- pulsar-headless calls
    // obs_reset_audio() with fixed values (pulsar-headless/main.cpp:181-183) and
    // exposes no override. If such an env var ever lands, these move to
    // boot-fixed; declaring it today would advertise a knob that does not exist.
    {
        obs_audio_info oai = {};
        if (obs_get_audio_info(&oai)) {
            if (oai.samples_per_sec > 0) {
                OBSDataAutoRelease entry = obs_data_create();
                obs_data_set_string(entry, "applicability", kRegimeReadOnly);
                obs_data_set_int(entry, "hz", static_cast<long long>(oai.samples_per_sec));
                obs_data_set_obj(caps, "audio_sample_rate", entry);
            }
            if (const char *layout = speaker_layout_name(oai.speakers)) {
                OBSDataAutoRelease entry = obs_data_create();
                obs_data_set_string(entry, "applicability", kRegimeReadOnly);
                obs_data_set_string(entry, "layout", layout);
                obs_data_set_int(entry, "channels",
                                 static_cast<long long>(get_audio_channels(oai.speakers)));
                obs_data_set_obj(caps, "audio_speaker_layout", entry);
            }
        }
    }

    obs_data_set_obj(res, "capabilities", caps);
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
    obs_websocket_vendor_register_request(g_vendor, "GetCapabilities",      on_get_capabilities,    nullptr);
    obs_websocket_vendor_register_request(g_vendor, "GetAdaptiveState",     on_get_adaptive_state,  nullptr);
    obs_websocket_vendor_register_request(g_vendor, "SetAdaptiveEnabled",   on_set_adaptive_enabled, nullptr);

    blog(LOG_INFO, "[pulsar-multi-stream] vendor 'pulsar' registered with 12 requests");

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
