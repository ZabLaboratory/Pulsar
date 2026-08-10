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
// #167 -- the SAME probe the nv-filters module gates itself on, so the
// manifest publishes the load decision rather than a guess at it. It is the
// first thing here that pulls in <windows.h>, hence NOMINMAX: this file uses
// std::min / std::max, which the Win32 min/max MACROS would eat.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <pulsar-nv-secure-load.h>

#include <obs-websocket-api.h>

// ADR-005 §3.6: header-only reference for kServerMaxLines -- see the
// CMakeLists.txt comment by PULSAR_LOG_HANDLER_HEADERS for why this does
// not introduce a link dependency on pulsar-headless.
#include <log-handler.h>

#include <algorithm>
#include <atomic>
#include <cctype>
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
#include <utility>
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
    Kind_YouTube,
    Kind_Unknown,
};

// Fail closed at compile time: no future edit may point a pinned, named
// platform destination at a cleartext ingest. Every named-platform kind
// carries a bearer stream key in the RTMP connect handshake, so the scheme is
// the only thing standing between that key and the wire.
constexpr bool is_rtmps_literal(const char *s)
{
    return s[0] == 'r' && s[1] == 't' && s[2] == 'm' && s[3] == 'p' && s[4] == 's' && s[5] == ':' && s[6] == '/' &&
           s[7] == '/';
}

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
static_assert(is_rtmps_literal(TWITCH_INGEST_URL),
              "TWITCH_INGEST_URL must use the rtmps:// scheme -- the stream key must never travel in cleartext");

// YouTube's primary RTMPS ingest. Source, checked into this tree rather than
// taken on trust: the "Primary YouTube ingest server" of the "YouTube - RTMPS"
// entry of upstream/plugins/rtmp-services/data/services.json (the service list
// OBS itself ships and keeps current). YouTube's stream key is per-channel and
// persistent across broadcasts unless the streamer resets it.
//
// The same rtmps:// requirement as Twitch, and here it is a live choice rather
// than a formality: that same services.json entry ALSO lists two legacy
// cleartext hosts (rtmp://a.rtmp.youtube.com/live2 and its backup). Pointing
// the pinned destination at one of them would put a persistent bearer key on
// the wire in the clear at every go-live -- the static_assert below exists to
// make that edit fail the build rather than ship.
constexpr const char *YOUTUBE_INGEST_URL = "rtmps://a.rtmps.youtube.com:443/live2";
static_assert(is_rtmps_literal(YOUTUBE_INGEST_URL),
              "YOUTUBE_INGEST_URL must use the rtmps:// scheme -- the stream key must never travel in cleartext");

const char *kind_to_string(DestinationKind k)
{
    switch (k) {
    case Kind_RtmpCustom: return "rtmp_custom";
    case Kind_VodLocal:   return "vod_local";
    case Kind_Twitch:     return "twitch";
    case Kind_YouTube:    return "youtube";
    default:              return "unknown";
    }
}

// The ingest URL Pulsar pins for a named-platform kind, or nullptr for a kind
// whose server the client legitimately chooses. Single source of the
// pinned-vs-client-supplied split, read by ensure_output (which configures the
// service) and by CreateDestination (which stores the URL the caller will see
// back). ADR 010 §3.3 R1: a named-platform stream key is a bearer credential
// for an account we do not own, so it may only ever be handed to an ingest
// this binary pins -- never to a URL that arrived over the wire. Adding a
// platform kind here, and nowhere else, is what keeps that true.
const char *pinned_ingest_url(DestinationKind k)
{
    switch (k) {
    case Kind_Twitch:  return TWITCH_INGEST_URL;
    case Kind_YouTube: return YOUTUBE_INGEST_URL;
    default:           return nullptr;
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
    case Kind_Twitch:
    case Kind_YouTube:    return "rtmp_output";
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
    if (str == "youtube")     return Kind_YouTube;
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
//   youtube     -> key non-empty (url ignored, server is pinned)
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

    case Kind_YouTube:
        if (!key || !*key) {
            errOut = "youtube: key required (YouTube stream key)";
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

    // rtmp_custom is the one kind that streams to a server the caller chose;
    // every other RTMP kind is a named platform whose ingest we pin here. The
    // client's url never reaches the service settings for those (ADR 010
    // §3.3 R1) -- it is not merely overridden, it is not read.
    const char *pinned = pinned_ingest_url(d.kind);
    if (d.kind == Kind_RtmpCustom || pinned) {
        const char *server = pinned ? pinned : d.url.c_str();
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
        obs_data_set_string(res, "error",
                            "kind must be 'rtmp_custom', 'vod_local', 'twitch', or 'youtube'");
        return;
    }

    std::string err;
    if (!validate_destination_input(kind, url, key, err)) {
        obs_data_set_string(res, "error", err.c_str());
        return;
    }

    // For a named platform the server URL is fixed; the user-supplied url
    // field is ignored. Stash the pinned URL so GetDestinations + diagnostics
    // surface it consistently.
    const char *pinned = pinned_ingest_url(kind);
    const char *storeUrl = pinned ? pinned : url;

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
// PULSAR_VIDEO_KEYINT_SEC accepts 0..20 (pulsar-frontend-stub.cpp:992-997).
static constexpr long long kPolicyKeyintSecMin = 0;
static constexpr long long kPolicyKeyintSecMax = 20;

// ---- Audio block (Prism ADR 027 §3.3 bloc 2, issue #143) -------------------
//
// Prism used to offer three headphone-monitoring keys as `applyClass: live`,
// "verified by read-back" of a state nobody established comes back -- because
// nothing in this tree ever called obs_set_audio_monitoring_device(), so no
// monitoring device was ever bound and no write path existed over the wire
// (regime was read-only, not live: ADR 027 §3.2 `live` requires the write AND
// the read-back to be genuinely supported hot).
//
// pulsar-headless now calls obs_set_audio_monitoring_device("Default",
// "default") once, unconditionally, at boot (reset_audio(),
// pulsar-headless/main.cpp) -- BEFORE obs-websocket registers a single
// request handler, so any process able to answer GetCapabilities has the
// device genuinely bound already. libobs also SEEDS monitoring_device_name/
// _id with that same "Default"/"default" pair inside obs_init_audio()
// (upstream/libobs/obs.c:916-917) before anyone chooses anything, which used
// to make the seed and a real explicit bind indistinguishable by id alone --
// moot now that the explicit call always runs first, so `device_bound` below
// no longer excludes the "default" id as a false positive.

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

// The audio TRACKS an output carries, read off the encoders bound to its slots.
//
// The slot index is NOT the track number (issue #168): OBS packs the selected
// tracks into consecutive slots, so an output carrying tracks 1 and 3 binds them
// at slots 0 and 1. The track an encoder pulls from is its own mixer index --
// obs_encoder_get_mixer_index -- and nothing else. Deriving it from the slot
// would be the same class of inference #157 was about, one level down.
//
// Tracks come back 1-based, the way a client numbers them.
static void read_output_audio_tracks(obs_output_t *output, std::vector<long long> &tracks)
{
    tracks.clear();
    if (!output) return;
    for (size_t i = 0; i < static_cast<size_t>(MAX_AUDIO_MIXES); i++) {
        obs_encoder_t *enc = obs_output_get_audio_encoder(output, i);
        if (!enc) continue;
        tracks.push_back(static_cast<long long>(obs_encoder_get_mixer_index(enc)) + 1);
    }
}

// Tracks the streaming output actually carries. Returns false when no output is
// bound (off-air), in which case the caller declares the answer absent instead
// of guessing one.
static bool read_bound_audio_tracks(long long &bound, std::vector<long long> &tracks)
{
    OBSOutputAutoRelease srcOutput = obs_frontend_get_streaming_output();
    if (!srcOutput) return false;
    read_output_audio_tracks(srcOutput, tracks);
    bound = static_cast<long long>(tracks.size());
    return true;
}

// Every audio encoder bound to the streaming output, in slot order. The
// pointers belong to frontend-stub and must NOT be released.
static void get_current_audio_encoders(std::vector<obs_encoder_t *> &encs)
{
    encs.clear();
    OBSOutputAutoRelease srcOutput = obs_frontend_get_streaming_output();
    if (!srcOutput) return;
    for (size_t i = 0; i < static_cast<size_t>(MAX_AUDIO_MIXES); i++) {
        if (obs_encoder_t *enc = obs_output_get_audio_encoder(srcOutput, i))
            encs.push_back(enc);
    }
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

// Reads the [min,max,step] an int property advertises. Returns false when the
// property is missing or is not an int -- the caller then declares the
// capability absent.
static bool read_int_range(obs_properties_t *props, const char *name, long long &lo, long long &hi,
                           long long &step)
{
    if (!props) return false;
    obs_property_t *p = obs_properties_get(props, name);
    if (!p || obs_property_get_type(p) != OBS_PROPERTY_INT) return false;

    const long long a = obs_property_int_min(p);
    const long long b = obs_property_int_max(p);
    const long long s = obs_property_int_step(p);
    if (b < a) return false;

    lo = a;
    hi = b;
    step = s > 0 ? s : 1;
    return true;
}

// Reads the [min,max] kbps window the encoder advertises for "bitrate".
// Returns false when the property is missing or is not an int range -- the
// caller then declares the capability absent.
static bool read_bitrate_window(obs_encoder_t *enc, const char *fallbackId, long long &minKbps,
                                long long &maxKbps, long long &stepKbps)
{
    PropsGuard props(encoder_properties_or_id(enc, fallbackId));
    long long lo = 0, hi = 0, step = 0;
    if (!read_int_range(props.get(), "bitrate", lo, hi, step)) return false;
    if (lo <= 0) return false;

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

// ---- Encoder block (Prism ADR 027 §3.3 bloc 1, issue #142) -----------------
//
// Per enumerated family: presets, H.264 profiles, rate-controls, keyint bounds
// and the family's own bitrate window. Every value below is READ from that
// family's libobs properties -- nothing here is a list of encoder values held
// in this file. The only literals are (a) the libobs PROPERTY NAMES the same
// concept goes by across plugins and (b) Pulsar's own boot policy, which can
// only narrow what libobs advertises (§3.1). A family the binary does not
// register produces no entry at all.

// libobs property names, not values. The preset knob is "preset" for x264 /
// AMF / obs-nvenc (nvenc-properties.c:142), "preset2" for the ffmpeg / compat
// NVENC path (nvenc-compat.c:183), and "target_usage" for QSV
// (obs-qsv11.c:390). No encoder exposes two of them, so first match wins.
static const char *const kPresetPropNames[] = {"preset", "preset2", "target_usage"};
static const char *const kProfilePropNames[] = {"profile"};
static const char *const kRateControlPropNames[] = {"rate_control"};

// The preset an encoder ACTUALLY carries, read under whichever of those names
// its plugin uses. Reading only "preset" reported "" for every QSV spawn, whose
// key is "target_usage" -- the same mismatch that made the boot setter a no-op
// there. First non-empty wins: no encoder exposes two of these.
static const char *applied_preset(obs_data_t *s)
{
    for (const char *name : kPresetPropNames) {
        const char *v = obs_data_get_string(s, name);
        if (v && *v) return v;
    }
    return "";
}

static bool iequals(const char *a, const char *b)
{
    if (!a || !b) return false;
    for (; *a && *b; ++a, ++b) {
        if (std::tolower(static_cast<unsigned char>(*a)) !=
            std::tolower(static_cast<unsigned char>(*b)))
            return false;
    }
    return *a == *b;
}

// Pulsar's own boot policy, mirrored from pulsar-frontend-stub.cpp:976-997 --
// the values PULSAR_VIDEO_RATE_CONTROL / PULSAR_VIDEO_PROFILE /
// PULSAR_VIDEO_KEYINT_SEC actually accept. Same rule as the bitrate window
// above: the manifest publishes the INTERSECTION with what libobs advertises,
// so it can never announce a value the boot setter would silently replace.
static bool policy_admits_rate_control(const char *v)
{
    return iequals(v, "CBR") || iequals(v, "VBR") || iequals(v, "CQP");
}

static bool policy_admits_profile(const char *v)
{
    return iequals(v, "baseline") || iequals(v, "main") || iequals(v, "high");
}

// Presets are deliberately NOT narrowed. The boot whitelist is a per-family
// table (pulsar-frontend-stub.cpp:presetsForFamily) and mirroring it here would
// recreate the very decree this block removes. The QSV divergence that made a
// narrowing actively wrong -- the table held speed/balanced/quality against a
// knob whose values are TU1..TU7 -- is fixed at the source: that table now
// holds the encoder's own seven levels, so the published list and the boot
// whitelist agree without either one copying the other.
static bool admits_any(const char *) { return true; }

// Reads a libobs string-list property into an array of {value} items, keeping
// only the entries the given Pulsar policy admits. Returns false when no such
// property exists, when it is not a string list, or when nothing survives --
// in every one of those cases the caller omits the key rather than publishing
// an empty or invented list.
template <typename Admits>
static bool read_string_list(obs_properties_t *props, const char *const *names, size_t nameCount,
                             Admits admits, obs_data_array_t *out)
{
    if (!props) return false;
    for (size_t n = 0; n < nameCount; ++n) {
        obs_property_t *p = obs_properties_get(props, names[n]);
        if (!p || obs_property_get_type(p) != OBS_PROPERTY_LIST) continue;
        if (obs_property_list_format(p) != OBS_COMBO_FORMAT_STRING) continue;

        bool any = false;
        const size_t count = obs_property_list_item_count(p);
        for (size_t i = 0; i < count; ++i) {
            if (obs_property_list_item_disabled(p, i)) continue;
            const char *v = obs_property_list_item_string(p, i);
            // libobs spells "no value" as an empty string (x264's <None>
            // profile entry); it is not a selectable value.
            if (!v || !*v) continue;
            if (!admits(v)) continue;
            OBSDataAutoRelease item = obs_data_create();
            obs_data_set_string(item, "value", v);
            obs_data_array_push_back(out, item);
            any = true;
        }
        return any; // the property answered; its answer stands, empty included
    }
    return false;
}

// ---- Adapters and scales (Prism ADR 027 Amendment 1, issue #159) -----------
//
// Two facts of the machine that the manifest did not cover, and that a
// consumer therefore had to decree: WHICH graphics adapters exist, and WHICH
// output resolutions this Pulsar admits for the canvas it is running. Both are
// read here; neither is a table held in this file.

// Sink for gs_enum_adapters(). libobs hands back the adapter name and the
// index obs_video_info.adapter is expressed in, so the pair travels together:
// a name without its index would be unusable, an index without its name
// unverifiable.
struct AdapterSink {
    obs_data_array_t *out;
    size_t count;
};

static bool push_adapter(void *param, const char *name, uint32_t index)
{
    auto *sink = static_cast<AdapterSink *>(param);
    if (name && *name) {
        OBSDataAutoRelease item = obs_data_create();
        obs_data_set_string(item, "value", name);
        obs_data_set_int(item, "index", static_cast<long long>(index));
        obs_data_array_push_back(sink->out, item);
        ++sink->count;
    }
    return true; // keep walking; we want the whole list, not the first hit
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

// Publishes an int window as {min,max,step} under `key`, narrowed by the
// Pulsar policy window. Omits the key when the property is unreadable or the
// intersection is empty.
static void set_int_window(obs_data_t *dst, const char *key, obs_properties_t *props,
                           const char *propName, long long policyLo, long long policyHi)
{
    long long lo = 0, hi = 0, step = 0;
    if (!read_int_range(props, propName, lo, hi, step)) return;
    if (!narrow_to_policy(lo, hi, policyLo, policyHi)) return;

    OBSDataAutoRelease w = obs_data_create();
    obs_data_set_int(w, "min", lo);
    obs_data_set_int(w, "max", hi);
    obs_data_set_int(w, "step", step);
    obs_data_set_obj(dst, key, w);
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
        obs_data_set_string(res, "video_preset", applied_preset(s));
        obs_data_set_string(res, "video_profile", obs_data_get_string(s, "profile"));
    }
    if (aEnc) {
        OBSDataAutoRelease s = obs_encoder_get_settings(aEnc);
        obs_data_set_int(res, "audio_bitrate", obs_data_get_int(s, "bitrate"));
    }
}

// ---- Audio tracks: what each output carries, and what each track carries ---
//
// GetAudioTracks answers the CONFIGURATION question -- which encoder sits in
// which slot of which output, and which mix it pulls from. That is enough to
// see that several encoders are bound to distinct slots, and that the three
// outputs carry different track sets (issue #168 criteria 1 and 3).
//
// It is NOT enough to answer the question that matters: is an input routed to
// track N effectively CONSUMED by track N's encoder? Re-reading the input
// cannot answer it either -- libobs writes the mixer bit whatever anything
// downstream carries, and hands every fresh source audio_mixers = 0xFF. That is
// exactly the trap #157 was about. MeasureAudioTrackFlow below is the read that
// does answer it.
static const struct {
    const char *name;
    obs_output_t *(*get)(void);
} kAudioTrackOutputs[] = {
    {"stream", obs_frontend_get_streaming_output},
    {"record", obs_frontend_get_recording_output},
    {"replay", obs_frontend_get_replay_buffer_output},
};

void on_get_audio_tracks(obs_data_t * /*req*/, obs_data_t *res, void *)
{
    obs_data_set_int(res, "count", static_cast<long long>(MAX_AUDIO_MIXES));

    OBSDataArrayAutoRelease outputs = obs_data_array_create();
    for (const auto &desc : kAudioTrackOutputs) {
        OBSOutputAutoRelease output = desc.get();
        if (!output) continue; // absent, never a fabricated empty entry

        OBSDataAutoRelease entry = obs_data_create();
        obs_data_set_string(entry, "output", desc.name);

        OBSDataArrayAutoRelease slots = obs_data_array_create();
        for (size_t i = 0; i < static_cast<size_t>(MAX_AUDIO_MIXES); i++) {
            obs_encoder_t *enc = obs_output_get_audio_encoder(output, i);
            if (!enc) continue;

            OBSDataAutoRelease slot = obs_data_create();
            obs_data_set_int(slot, "slot", static_cast<long long>(i));
            obs_data_set_int(slot, "track",
                             static_cast<long long>(obs_encoder_get_mixer_index(enc)) + 1);
            obs_data_set_string(slot, "encoder", obs_encoder_get_name(enc));
            if (const char *codec = obs_encoder_get_codec(enc))
                obs_data_set_string(slot, "codec", codec);
            OBSDataAutoRelease settings = obs_encoder_get_settings(enc);
            obs_data_set_int(slot, "bitrate", obs_data_get_int(settings, "bitrate"));
            obs_data_set_bool(slot, "active", obs_encoder_active(enc));
            obs_data_set_int(slot, "encoded_frames",
                             static_cast<long long>(obs_encoder_get_encoded_frames(enc)));
            obs_data_array_push_back(slots, slot);
        }
        obs_data_set_array(entry, "slots", slots);
        obs_data_array_push_back(outputs, entry);
    }
    obs_data_set_array(res, "outputs", outputs);
}

// The measurement is taken on the SAME libobs audio mix the track's encoder is
// attached to: obs_audio_encoder_create(..., mixer_idx, ...) and
// obs_add_raw_audio_callback(mixer_idx, ...) both register as inputs of
// obs->audio.mixes[mixer_idx] (libobs/obs.c:3143 -> media-io/audio-io.c:289).
// What comes back is therefore the signal the encoder is fed, not a mirror of a
// configuration and not a read-back of the input's mixer bits. A mix nothing is
// routed to reports a flat zero; routing an audible input to track N moves N's
// peak and no other.
//
// The callbacks live for the duration of the call only -- a permanently
// connected raw callback would force libobs to mix all six buses for the
// lifetime of the process, which is a real cost paid for an introspection
// nobody asked for.
struct AudioTrackFlow {
    std::atomic<uint64_t> frames{0};
    // max |sample| x 1000. Integer because it is written from the audio thread
    // and read from the request thread; the float would need its own lock for
    // no gain at this resolution.
    std::atomic<uint64_t> peak_milli{0};
};

static void audio_track_flow_callback(void *param, size_t /*mix_idx*/, struct audio_data *data)
{
    auto *flow = static_cast<AudioTrackFlow *>(param);
    if (!flow || !data) return;

    flow->frames.fetch_add(data->frames, std::memory_order_relaxed);

    // ONLY the first audio_output_get_planes() entries of data->data are
    // written: libobs builds `struct audio_data` on the stack, uninitialised,
    // and fills data[0..planes-1] (media-io/audio-io.c:107-124). The remaining
    // MAX_AV_PLANES entries are stack garbage, not null -- walking all eight
    // dereferences them and takes the process down.
    const size_t planes = audio_output_get_planes(obs_get_audio());
    float peak = 0.0f;
    for (size_t plane = 0; plane < planes && plane < MAX_AV_PLANES; plane++) {
        const float *samples = reinterpret_cast<const float *>(data->data[plane]);
        if (!samples) continue;
        for (uint32_t i = 0; i < data->frames; i++) {
            const float magnitude = samples[i] < 0.0f ? -samples[i] : samples[i];
            if (magnitude > peak) peak = magnitude;
        }
    }

    const uint64_t milli = static_cast<uint64_t>(peak * 1000.0f);
    uint64_t previous = flow->peak_milli.load(std::memory_order_relaxed);
    while (milli > previous &&
           !flow->peak_milli.compare_exchange_weak(previous, milli, std::memory_order_relaxed)) {
    }
}

void on_measure_audio_track_flow(obs_data_t *req, obs_data_t *res, void *)
{
    long long durationMs = 300;
    if (obs_data_has_user_value(req, "duration_ms"))
        durationMs = obs_data_get_int(req, "duration_ms");
    if (durationMs < 50 || durationMs > 2000) {
        obs_data_set_string(res, "error", "duration_ms must be in [50, 2000]");
        return;
    }

    AudioTrackFlow flows[MAX_AUDIO_MIXES];
    for (size_t i = 0; i < static_cast<size_t>(MAX_AUDIO_MIXES); i++)
        obs_add_raw_audio_callback(i, nullptr, audio_track_flow_callback, &flows[i]);
    std::this_thread::sleep_for(std::chrono::milliseconds(durationMs));
    for (size_t i = 0; i < static_cast<size_t>(MAX_AUDIO_MIXES); i++)
        obs_remove_raw_audio_callback(i, audio_track_flow_callback, &flows[i]);

    obs_data_set_int(res, "duration_ms", durationMs);

    long long bound = 0;
    std::vector<long long> boundTracks;
    const bool haveStreamOutput = read_bound_audio_tracks(bound, boundTracks);

    OBSDataArrayAutoRelease tracks = obs_data_array_create();
    for (size_t i = 0; i < static_cast<size_t>(MAX_AUDIO_MIXES); i++) {
        OBSDataAutoRelease entry = obs_data_create();
        const long long track = static_cast<long long>(i) + 1;
        obs_data_set_int(entry, "track", track);
        obs_data_set_int(entry, "frames",
                         static_cast<long long>(flows[i].frames.load(std::memory_order_relaxed)));
        obs_data_set_double(entry, "peak",
                            static_cast<double>(flows[i].peak_milli.load(std::memory_order_relaxed)) /
                                1000.0);
        // Omitted, never guessed, when there is no streaming output to read.
        if (haveStreamOutput)
            obs_data_set_bool(entry, "encoder_bound",
                              std::find(boundTracks.begin(), boundTracks.end(), track) !=
                                  boundTracks.end());
        obs_data_array_push_back(tracks, entry);
    }
    obs_data_set_array(res, "tracks", tracks);
}

// ---- Monitoring device selection (#173) ------------------------------------
//
// pulsar-headless binds "Default"/"default" at boot, which makes monitoring
// *work*; it does not let an operator choose WHICH output sounds. These two
// requests are that choice, and nothing more.
//
// The list is an instance list, read from libobs at call time
// (obs_enum_audio_monitoring_devices -> WASAPI eRender, DEVICE_STATE_ACTIVE):
// plugging a headset in changes the answer without restarting Pulsar. libobs
// does NOT enumerate a pseudo-entry for the OS default -- OBS's own UI prepends
// it -- so we prepend it here, with libobs's own dynamic id "default", because
// it is the device the boot bind picked and a list that omitted it could not
// name the device already in force.

using MonitoringDevice = std::pair<std::string, std::string>; // {id, name}

static bool collect_monitoring_device(void *data, const char *name, const char *id)
{
    if (id && *id)
        static_cast<std::vector<MonitoringDevice> *>(data)->emplace_back(id, name ? name : "");
    return true;
}

static std::vector<MonitoringDevice> enumerate_monitoring_devices()
{
    std::vector<MonitoringDevice> devices;
    devices.emplace_back("default", "Default");
    obs_enum_audio_monitoring_devices(collect_monitoring_device, &devices);
    return devices;
}

void on_get_monitoring_device_list(obs_data_t * /*req*/, obs_data_t *res, void *)
{
    const bool available = obs_audio_monitoring_available();
    obs_data_set_bool(res, "available", available);

    OBSDataArrayAutoRelease arr = obs_data_array_create();
    if (available) {
        for (const auto &dev : enumerate_monitoring_devices()) {
            OBSDataAutoRelease item = obs_data_create();
            obs_data_set_string(item, "id", dev.first.c_str());
            obs_data_set_string(item, "name", dev.second.c_str());
            obs_data_array_push_back(arr, item);
        }
    }
    obs_data_set_array(res, "devices", arr);

    const char *activeName = nullptr;
    const char *activeId = nullptr;
    if (available) obs_get_audio_monitoring_device(&activeName, &activeId);
    if (activeId && *activeId) {
        obs_data_set_string(res, "active_device_id", activeId);
        obs_data_set_string(res, "active_device_name", activeName ? activeName : "");
    }
}

void on_set_monitoring_device(obs_data_t *req, obs_data_t *res, void *)
{
    if (!obs_audio_monitoring_available()) {
        obs_data_set_string(res, "error",
                            "audio monitoring is not available on this build");
        return;
    }

    const char *wanted = obs_data_get_string(req, "device_id");
    if (!wanted || !*wanted) {
        obs_data_set_string(res, "error", "device_id required");
        return;
    }

    // obs_set_audio_monitoring_device() stores ANY non-empty {name,id} pair and
    // returns true (upstream/libobs/obs.c:2981) -- an unknown id only surfaces
    // later, as silence, when a monitor is created. So the id is checked against
    // the enumeration BEFORE the write: a device the machine does not have is
    // refused by name here, never accepted into a mute failure.
    const auto devices = enumerate_monitoring_devices();
    const auto it = std::find_if(devices.begin(), devices.end(),
                                 [&](const MonitoringDevice &d) { return d.first == wanted; });
    if (it == devices.end()) {
        obs_data_set_string(res, "error",
                            (std::string("no such monitoring device: '") + wanted +
                             "' is not among the playback devices this machine enumerates")
                                .c_str());
        return;
    }

    if (!obs_set_audio_monitoring_device(it->second.c_str(), it->first.c_str())) {
        obs_data_set_string(res, "error",
                            (std::string("libobs refused monitoring device '") + wanted + "'")
                                .c_str());
        return;
    }

    // Read-back (ADR Prism 026 §3.2): the write is only reported once libobs
    // reports the new device as the one in force. It proves the bind took, not
    // that sound comes out of it -- that is the manual criterion of #173.
    const char *nowName = nullptr;
    const char *nowId = nullptr;
    obs_get_audio_monitoring_device(&nowName, &nowId);
    if (!nowId || it->first != nowId) {
        obs_data_set_string(res, "error",
                            (std::string("monitoring device read-back mismatch: asked '") +
                             wanted + "', libobs reports '" + (nowId ? nowId : "") + "'")
                                .c_str());
        return;
    }

    obs_data_set_bool(res, "changed", true);
    obs_data_set_string(res, "device_id", nowId);
    obs_data_set_string(res, "device_name", nowName ? nowName : "");
    blog(LOG_INFO, "[pulsar-multi-stream] monitoring device set to '%s' (%s)",
         nowName ? nowName : "", nowId);
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
        // Multi-track (#168): this scalar names ONE bitrate, so it is applied to
        // every audio encoder the streaming output carries. Patching only slot 0
        // would answer success while leaving tracks 2..N at their boot value --
        // a partial write reported as a whole one.
        std::vector<obs_encoder_t *> aEncs;
        get_current_audio_encoders(aEncs);
        for (obs_encoder_t *enc : aEncs) {
            if (!obs_encoder_active(enc)) continue;
            // ffmpeg_aac re-init on bitrate change is not supported mid-stream;
            // reject so we don't introduce hidden encoder restarts.
            obs_data_set_string(res, "error",
                "audio_bitrate cannot change while audio encoder is active; stop all "
                "outputs first");
            return;
        }
        OBSDataAutoRelease patch = obs_data_create();
        obs_data_set_int(patch, "bitrate", newKbps);
        for (obs_encoder_t *enc : aEncs)
            obs_encoder_update(enc, patch);
        obs_data_set_int(res, "audio_bitrate", newKbps);
        obs_data_set_int(res, "audio_tracks_updated", static_cast<long long>(aEncs.size()));
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

    // First registered obs id per family, kept to read that family's libobs
    // properties below. Both arrays hold libobs-owned static strings.
    const char *familyId[4] = {nullptr, nullptr, nullptr, nullptr};
    const char *familyName[4] = {nullptr, nullptr, nullptr, nullptr};

    const char *encId = nullptr;
    for (size_t i = 0; obs_enum_encoder_types(i, &encId); ++i) {
        const char *fam = encoder_family_for_id(encId);
        if (!fam) continue; // non-whitelisted / non-H.264-streaming id: dropped
        int idx = family_index(fam);
        if (seen[idx]) continue;
        seen[idx] = true;
        if (idx < 4) {
            familyId[idx] = encId;
            familyName[idx] = fam;
        }
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

    // ADR 027 §3.3 bloc 1 (#142): what each enumerated family actually offers.
    // The whole block is boot-fixed -- preset / profile / rate-control /
    // keyint are chosen by PULSAR_VIDEO_* at spawn and SetVideoSettings
    // refuses them hot (see on_set_video_settings above). Every value is read
    // from that family's libobs properties; an unreadable one is omitted, and
    // a family the binary does not register is simply not in the list.
    {
        OBSDataArrayAutoRelease families = obs_data_array_create();
        for (int idx = 0; idx < 4; ++idx) {
            if (!familyId[idx]) continue; // family not compiled into this build

            // For the family that is actually bound, read the live encoder's
            // own properties; for the others, the registered id's.
            const bool isActive = vEnc && std::strcmp(familyName[idx], activeFamily) == 0;
            PropsGuard props(encoder_properties_or_id(isActive ? vEnc : nullptr, familyId[idx]));

            OBSDataAutoRelease fam = obs_data_create();
            obs_data_set_string(fam, "value", familyName[idx]);

            OBSDataArrayAutoRelease presets = obs_data_array_create();
            if (read_string_list(props.get(), kPresetPropNames,
                                 sizeof(kPresetPropNames) / sizeof(*kPresetPropNames),
                                 admits_any, presets))
                obs_data_set_array(fam, "presets", presets);

            OBSDataArrayAutoRelease profiles = obs_data_array_create();
            if (read_string_list(props.get(), kProfilePropNames,
                                 sizeof(kProfilePropNames) / sizeof(*kProfilePropNames),
                                 policy_admits_profile, profiles))
                obs_data_set_array(fam, "profiles", profiles);

            OBSDataArrayAutoRelease rateControls = obs_data_array_create();
            if (read_string_list(props.get(), kRateControlPropNames,
                                 sizeof(kRateControlPropNames) / sizeof(*kRateControlPropNames),
                                 policy_admits_rate_control, rateControls))
                obs_data_set_array(fam, "rate_controls", rateControls);

            set_int_window(fam, "keyint_sec", props.get(), "keyint_sec", kPolicyKeyintSecMin,
                           kPolicyKeyintSecMax);
            set_int_window(fam, "bitrate", props.get(), "bitrate", kPolicyVideoBitrateMinKbps,
                           kPolicyVideoBitrateMaxKbps);

            obs_data_array_push_back(families, fam);
        }
        // No whitelisted family at all would mean no streaming encoder: declare
        // the block absent rather than publishing an empty list.
        if (obs_data_array_count(families) > 0) {
            OBSDataAutoRelease entry = obs_data_create();
            obs_data_set_string(entry, "applicability", kRegimeBootFixed);
            obs_data_set_array(entry, "values", families);
            obs_data_set_obj(caps, "encoder_families", entry);
        }
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

    // ---- nv_filters : the capability probe behind the inventory (#167) -----
    //
    // The `filters` inventory above answers "is nvidia_audiofx_filter here",
    // and nothing else. When it is NOT there, the consumer is left guessing
    // between "this build has no nv-filters", "the SDK is missing", "the SDK
    // is too old" and "the models are missing" -- four states with four
    // different answers for an operator. This entry is what distinguishes
    // them, without anyone having to read a log.
    //
    // It is also the manifest half of Prism ADR 023 Amendment 3 §A3.4: the
    // module now REFUSES TO LOAD unless this same probe is positive
    // (plugins/pulsar-nv-secure-load/, shared verbatim with the loader), so
    // publishing it is publishing the actual load decision, not a
    // reconstruction of it.
    //
    // Regime read-only: nothing over the wire can install an SDK or move the
    // directory, and the module load decision was taken at boot. Absence is an
    // answer throughout -- `version` is OMITTED, not zeroed, when the version
    // resource cannot be read, exactly as ADR 027 §3.3 §1 requires. That is
    // also why an unreadable version can never satisfy `usable`.
    {
        struct pulsar_nv_probe_result probe;
        pulsar_nv_probe(&probe);

        auto version_text = [](unsigned int v, char *out, size_t len) {
            snprintf(out, len, "%u.%u.%u.%u", (v >> 24) & 0xffu, (v >> 16) & 0xffu, (v >> 8) & 0xffu, v & 0xffu);
        };

        auto sdk_entry = [&](const struct pulsar_nv_sdk_probe &p) {
            obs_data_t *e = obs_data_create();
            char buf[32];
            // `dir` itself is deliberately NOT published: the consumer has no
            // use for it, and a filesystem path is not something a manifest
            // should hand out. What it needs is whether one was accepted.
            obs_data_set_bool(e, "directory_designated", p.dir_valid);
            obs_data_set_bool(e, "dlls_present", p.dlls_present);
            obs_data_set_bool(e, "models_present", p.models_present);
            if (p.version_readable) {
                version_text(p.version, buf, sizeof(buf));
                obs_data_set_string(e, "version", buf);
            }
            version_text(p.min_version, buf, sizeof(buf));
            obs_data_set_string(e, "min_version", buf);
            obs_data_set_bool(e, "usable", p.usable);
            return e;
        };

        // Read back what actually happened, rather than re-deriving it: if a
        // filter type is registered, the module loaded. The two agreeing is
        // the point -- a positive probe with nothing registered would mean the
        // gate and the loader had drifted apart.
        bool registered = false;
        const char *kind = nullptr;
        for (size_t idx = 0; obs_enum_filter_types(idx, &kind); idx++) {
            if (kind && (strcmp(kind, "nvidia_audiofx_filter") == 0 || strcmp(kind, "nv_greenscreen_filter") == 0 ||
                         strcmp(kind, "nv_blur_filter") == 0 || strcmp(kind, "nv_background_blur_filter") == 0)) {
                registered = true;
                break;
            }
        }

        OBSDataAutoRelease entry = obs_data_create();
        obs_data_set_string(entry, "applicability", kRegimeReadOnly);
        obs_data_set_bool(entry, "module_loaded", registered);
        OBSDataAutoRelease afx = sdk_entry(probe.afx);
        OBSDataAutoRelease vfx = sdk_entry(probe.vfx);
        obs_data_set_obj(entry, "afx", afx);
        obs_data_set_obj(entry, "vfx", vfx);
        obs_data_set_obj(caps, "nv_filters", entry);
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

    // Monitoring: LIVE. pulsar-headless calls obs_set_audio_monitoring_device
    // once at boot, unconditionally, in reset_audio() -- BEFORE obs-websocket
    // registers a single request handler (main.cpp's boot order: reset_video
    // -> reset_audio -> install frontend callbacks -> load modules). Any
    // process able to answer GetCapabilities has therefore ALREADY had the
    // device genuinely bound.
    //
    // The id bound is a RESOLVED CONCRETE endpoint id, not the literal
    // "default" sentinel -- see resolve_default_render_device_id() in
    // pulsar-headless/main.cpp for why: every WASAPI-backed ZabCapture
    // source's own device_id also defaults to that same literal "default"
    // string, and OBS_SOURCE_DO_NOT_SELF_MONITOR silently disables a
    // source's monitor whenever its device_id string-matches the bound
    // monitoring id -- a real feedback guard for a microphone, a false
    // positive for every captured-app/desktop-audio source, and with no
    // error surfaced (SetInputAudioMonitorType still reports success; only
    // the antenna/stream mix, a separate code path, keeps the audio). So
    // `device_id` reported here is normally a GUID string, e.g.
    // `{0.0.0.00000000}.{guid}`, not "default" -- expected, not a bug. The
    // "default" sentinel is used only as a fallback if resolution itself
    // failed at boot (logged there), which still leaves this block's own
    // check exactly as strict as before: `available` and `device_bound` are
    // ALWAYS emitted; the identity keys only when a device is genuinely
    // bound (still possible to be false: obs_audio_monitoring_available()
    // itself can be false on a platform build with no monitoring backend).
    {
        OBSDataAutoRelease entry = obs_data_create();
        obs_data_set_string(entry, "applicability", kRegimeLive);

        const bool available = obs_audio_monitoring_available();
        obs_data_set_bool(entry, "available", available);

        const char *devName = nullptr;
        const char *devId = nullptr;
        if (available) obs_get_audio_monitoring_device(&devName, &devId);

        const bool bound = available && devId && *devId;
        obs_data_set_bool(entry, "device_bound", bound);
        if (bound) {
            obs_data_set_string(entry, "device_id", devId);
            obs_data_set_string(entry, "device_name", devName ? devName : "");
        }
        // #173: WHICH device sounds is now choosable hot, through the vendor
        // pair GetMonitoringDeviceList / SetMonitoringDevice. The flag tracks
        // obs_audio_monitoring_available() rather than being a literal `true`,
        // because on a build with no monitoring backend both requests refuse --
        // and a consumer that offered a selector on that build would offer a
        // control the binary cannot honour (ADR 027 §3.1).
        obs_data_set_bool(entry, "device_selectable", available);
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
        std::vector<long long> boundTracks;
        if (read_bound_audio_tracks(bound, boundTracks)) {
            obs_data_set_int(entry, "bound", bound);
            // WHICH tracks, not just how many: with per-output routing (#168) a
            // consumer cannot infer {1, 3} from "bound: 2", and the slot order
            // does not tell it either.
            OBSDataArrayAutoRelease arr = obs_data_array_create();
            for (long long track : boundTracks) {
                OBSDataAutoRelease item = obs_data_create();
                obs_data_set_int(item, "value", track);
                obs_data_array_push_back(arr, item);
            }
            obs_data_set_array(entry, "tracks", arr);
        }
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

    // ---- Adapters and scales (ADR 027 Amendment 1, #159) -------------------
    //
    // Graphics adapters: enumerated by libobs itself (gs_enum_adapters, which
    // dispatches to the graphics subsystem's device_enum_adapters -- d3d11 on
    // Windows). Nothing about this list is written here; the names and the
    // indices are the ones the subsystem reports, and the index is the number
    // obs_video_info.adapter is expressed in, so the consumer can match the
    // active one against the list instead of assuming 0.
    //
    // Regime is READ-ONLY, for the same reason as the colorimetry entry:
    // pulsar-headless pins ovi.adapter at obs_reset_video
    // (plugins/pulsar-headless/main.cpp) and exposes NO env var and no request
    // to select another. `boot-fixed` would advertise a respawn knob that does
    // not exist. If such an env var ever lands, this entry moves; declaring it
    // today would be the decree §3.1 forbids.
    //
    // Enumeration is only valid inside the graphics context; outside it,
    // gs_enum_adapters returns without calling back at all -- and an empty walk
    // publishes NO entry, so the consumer keeps its own assumption rather than
    // reading "this machine has no GPU".
    {
        OBSDataArrayAutoRelease adapters = obs_data_array_create();
        AdapterSink sink{adapters, 0};
        obs_enter_graphics();
        gs_enum_adapters(push_adapter, &sink);
        obs_leave_graphics();

        if (sink.count > 0) {
            OBSDataAutoRelease entry = obs_data_create();
            obs_data_set_string(entry, "applicability", kRegimeReadOnly);
            obs_data_set_array(entry, "values", adapters);
            obs_video_info ovi = {};
            // The active index is a separate read: when it fails, the list
            // still stands and only the "which one" is declared absent.
            if (obs_get_video_info(&ovi))
                obs_data_set_int(entry, "active_index", static_cast<long long>(ovi.adapter));
            obs_data_set_obj(caps, "graphics_adapters", entry);
        }
    }

    // Output scales: which output resolutions are admissible for the canvas
    // this Pulsar is actually running.
    //
    // The admitted set is DERIVED from what this binary can establish, not from
    // a ladder of downscale factors held here -- publishing one would announce
    // resolutions Pulsar cannot honour, which is exactly §3.1's prohibition.
    // What it can establish, today, is one thing: reset_video() sets base AND
    // output from the single PULSAR_RESOLUTION value
    // (plugins/pulsar-headless/main.cpp), on_set_video_settings refuses
    // width/height hot, and nothing in this tree ever calls
    // obs_encoder_set_scaled_size(), so no pre-encode downscale exists either.
    // The admitted set is therefore exactly the output resolution libobs
    // reports -- read, not assumed, and it will grow by itself the day a
    // downscale path lands.
    //
    // Regime is `boot-fixed`: PULSAR_RESOLUTION genuinely selects it at spawn,
    // and SetVideoSettings genuinely refuses it hot.
    {
        obs_video_info ovi = {};
        if (obs_get_video_info(&ovi) && ovi.base_width > 0 && ovi.base_height > 0 &&
            ovi.output_width > 0 && ovi.output_height > 0) {
            OBSDataAutoRelease entry = obs_data_create();
            obs_data_set_string(entry, "applicability", kRegimeBootFixed);

            OBSDataAutoRelease canvas = obs_data_create();
            obs_data_set_int(canvas, "width", static_cast<long long>(ovi.base_width));
            obs_data_set_int(canvas, "height", static_cast<long long>(ovi.base_height));
            obs_data_set_obj(entry, "canvas", canvas);

            OBSDataArrayAutoRelease values = obs_data_array_create();
            OBSDataAutoRelease item = obs_data_create();
            const std::string token =
                std::to_string(ovi.output_width) + "x" + std::to_string(ovi.output_height);
            obs_data_set_string(item, "value", token.c_str());
            obs_data_set_int(item, "width", static_cast<long long>(ovi.output_width));
            obs_data_set_int(item, "height", static_cast<long long>(ovi.output_height));
            // A single ratio is only meaningful when both axes share it. Cross
            // multiplication keeps the test exact; a non-uniform pair publishes
            // the two resolutions and omits `scale` rather than picking an axis.
            if (static_cast<unsigned long long>(ovi.output_width) * ovi.base_height ==
                static_cast<unsigned long long>(ovi.output_height) * ovi.base_width)
                obs_data_set_double(item, "scale",
                                    static_cast<double>(ovi.output_width) /
                                        static_cast<double>(ovi.base_width));
            obs_data_array_push_back(values, item);

            obs_data_set_array(entry, "values", values);
            obs_data_set_obj(caps, "output_scales", entry);
        }
    }

    // ---- Recording container + marker support (issue #166, ADR Prism 028 §3.5 B5/B6) ----
    //
    // record_container is boot-fixed: PULSAR_RECORD_CONTAINER selects it once
    // in pulsar-frontend-stub's setup() and nothing in this tree mutates it
    // hot -- switching containers mid-archive would corrupt the file being
    // written. The value published here is READ off the recording output's
    // own live settings ("extension"), which pulsar-frontend-stub seeds at
    // boot alongside the same knob -- not a duplicated parse of an env var
    // this plugin never touches, and readable even before any recording has
    // ever started.
    //
    // record_markers is read-only: SplitRecordFile / CreateRecordChapter
    // delegate straight to the recording output's proc handler and neither
    // request writes anything back, so there is no apply-class to advertise.
    // Both booleans are derived from the recording output's own registered
    // id, the one input that actually determines which procs its proc
    // handler carries: obs-ffmpeg-mux.c registers "split_file" alone
    // (obs-ffmpeg-mux.c:107), mp4_output.c additionally registers
    // "add_chapter" (mp4_output.c:218). Calling either proc here to probe for
    // its presence is not an option: on an ACTIVE recording, split_file
    // genuinely arms a real split and add_chapter genuinely writes a real
    // chapter -- a capability *read* must not become a capability *side
    // effect*, so the id is read instead of the proc invoked.
    {
        OBSOutputAutoRelease recOutput = obs_frontend_get_recording_output();
        if (recOutput) {
            OBSDataAutoRelease settings = obs_output_get_settings(recOutput);
            const char *ext = obs_data_get_string(settings, "extension");
            if (ext && *ext) {
                OBSDataAutoRelease entry = obs_data_create();
                obs_data_set_string(entry, "applicability", kRegimeBootFixed);
                obs_data_set_string(entry, "value", ext);
                OBSDataArrayAutoRelease values = obs_data_array_create();
                for (const char *v : {"mp4", "mkv"}) {
                    OBSDataAutoRelease item = obs_data_create();
                    obs_data_set_string(item, "value", v);
                    obs_data_array_push_back(values, item);
                }
                obs_data_set_array(entry, "values", values);
                obs_data_set_obj(caps, "record_container", entry);
            }

            const char *outputId = obs_output_get_id(recOutput);
            if (outputId) {
                const bool hasSplitFile = std::strcmp(outputId, "ffmpeg_muxer") == 0;
                const bool hasAddChapter = std::strcmp(outputId, "mp4_output") == 0;
                OBSDataAutoRelease entry = obs_data_create();
                obs_data_set_string(entry, "applicability", kRegimeReadOnly);
                obs_data_set_bool(entry, "split_file", hasSplitFile);
                obs_data_set_bool(entry, "add_chapter", hasAddChapter);
                obs_data_set_obj(caps, "record_markers", entry);
            }
        }
    }

    obs_data_set_obj(res, "capabilities", caps);
}

// ---- ADR-005 §3.6: diagnostic surface --------------------------------------
//
// pulsar-headless's log diagnostics (g_log_diagnostics / g_log_sink -- this
// module has no compile-time link to that .exe's translation unit) and
// obs-websocket's bind state (Config is private to that DLL) are both
// reached via the SAME global proc handler bridge obs-websocket's own API
// handle uses (WebSocketApi::get_ph_cb / "obs_websocket_api_get_ph"): see
// main.cpp's "pulsar_log_get_diagnostics" / "pulsar_log_stop_file_write"
// and obs-websocket.cpp's "pulsar_websocket_is_loopback_only".

void on_get_diagnostics(obs_data_t *req, obs_data_t *res, void *)
{
    long long maxLines = obs_data_has_user_value(req, "max_lines")
                              ? obs_data_get_int(req, "max_lines")
                              : static_cast<long long>(pulsar_log::DiagnosticsRing::kServerMaxLines);
    if (maxLines < 0) maxLines = 0;

    proc_handler_t *globalPh = obs_get_proc_handler();

    calldata_t diagCd = {0, 0, 0, 0};
    calldata_set_int(&diagCd, "max_lines", maxLines);
    const bool diagCalled = globalPh && proc_handler_call(globalPh, "pulsar_log_get_diagnostics", &diagCd);
    if (!diagCalled) {
        obs_data_set_string(res, "error", "pulsar-headless diagnostics are unavailable");
        calldata_free(&diagCd);
        return;
    }

    // Counters + output state carry no message content -- served
    // unconditionally, in both bind postures (ADR §3.6, RC24).
    const long long errors = calldata_int(&diagCd, "errors");
    const long long warnings = calldata_int(&diagCd, "warnings");
    const long long infos = calldata_int(&diagCd, "infos");
    const long long debugs = calldata_int(&diagCd, "debugs");
    obs_data_set_int(res, "count_error", errors);
    obs_data_set_int(res, "count_warn", warnings);
    obs_data_set_int(res, "count_info", infos);
    obs_data_set_int(res, "count_debug", debugs);

    OBSDataArrayAutoRelease outputs = obs_data_array_create();
    for (const auto &desc : kAudioTrackOutputs) {
        OBSOutputAutoRelease output = desc.get();
        if (!output) continue; // absent, never a fabricated entry
        OBSDataAutoRelease entry = obs_data_create();
        obs_data_set_string(entry, "output", desc.name);
        obs_data_set_bool(entry, "active", obs_output_active(output));
        obs_data_array_push_back(outputs, entry);
    }
    obs_data_set_array(res, "outputs", outputs);

    OBSDataArrayAutoRelease destinations = obs_data_array_create();
    for (auto &s : g_registry->snapshot()) {
        OBSDataAutoRelease entry = obs_data_create();
        obs_data_set_string(entry, "id", s.id.c_str());
        obs_data_set_string(entry, "name", s.name.c_str());
        obs_data_set_string(entry, "kind", s.kind.c_str());
        obs_data_set_bool(entry, "enabled", s.enabled);
        obs_data_set_bool(entry, "active", s.active);
        obs_data_array_push_back(destinations, entry);
    }
    obs_data_set_array(res, "destinations", destinations);

    // Everything below carries message content -- the log path and the
    // WARN/ERROR tail -- and is refused outright, with a named reason, the
    // moment the bind is not provably loopback-only (ADR §3.6, RC24). A
    // failure to even query the bind state (obs-websocket absent) is the
    // same refusal: unknown is never treated as loopback.
    calldata_t lbCd = {0, 0, 0, 0};
    const bool lbCalled = globalPh && proc_handler_call(globalPh, "pulsar_websocket_is_loopback_only", &lbCd);
    const bool loopback = lbCalled && calldata_bool(&lbCd, "loopback");
    calldata_free(&lbCd);

    if (!loopback) {
        obs_data_set_string(res, "error",
                            lbCalled ? "log content refused: obs-websocket is not bound to the loopback "
                                       "interface (PULSAR_WS_BIND); counters and output state above remain valid"
                                     : "log content refused: bind state could not be determined; counters and "
                                       "output state above remain valid");
        void *linesPtr = nullptr;
        if (calldata_get_ptr(&diagCd, "lines", &linesPtr) && linesPtr)
            obs_data_array_release(static_cast<obs_data_array_t *>(linesPtr));
        calldata_free(&diagCd);
        return;
    }

    const char *path = calldata_string(&diagCd, "path");
    if (calldata_bool(&diagCd, "path_known"))
        obs_data_set_string(res, "log_path", path ? path : "");

    void *linesPtr = nullptr;
    if (calldata_get_ptr(&diagCd, "lines", &linesPtr) && linesPtr) {
        OBSDataArrayAutoRelease lines = static_cast<obs_data_array_t *>(linesPtr);
        obs_data_set_array(res, "recent_warn_error_lines", lines);
    }
    calldata_free(&diagCd);
}

// §3.6.2: asymmetric kill switch. Takes no request fields -- there is no
// path to parameterise, it acts on the one file sink pulsar-headless
// already owns. A second call is a no-op reporting `already_stopped`, not
// an error, and never reopens the file (pulsar_log_stop_file_write_cb,
// main.cpp, guards on LogFileSink::opened()).
void on_stop_log_file_write(obs_data_t * /*req*/, obs_data_t *res, void *)
{
    proc_handler_t *globalPh = obs_get_proc_handler();
    calldata_t cd = {0, 0, 0, 0};
    const bool called = globalPh && proc_handler_call(globalPh, "pulsar_log_stop_file_write", &cd);
    if (!called) {
        obs_data_set_string(res, "error", "pulsar-headless log sink is unavailable");
        calldata_free(&cd);
        return;
    }

    obs_data_set_bool(res, "stopped", calldata_bool(&cd, "stopped"));
    obs_data_set_bool(res, "already_stopped", calldata_bool(&cd, "already_stopped"));
    if (const char *path = calldata_string(&cd, "path"))
        obs_data_set_string(res, "log_path", path);
    calldata_free(&cd);
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
    obs_websocket_vendor_register_request(g_vendor, "GetAudioTracks",       on_get_audio_tracks,    nullptr);
    obs_websocket_vendor_register_request(g_vendor, "MeasureAudioTrackFlow", on_measure_audio_track_flow, nullptr);
    obs_websocket_vendor_register_request(g_vendor, "GetAdaptiveState",     on_get_adaptive_state,  nullptr);
    obs_websocket_vendor_register_request(g_vendor, "SetAdaptiveEnabled",   on_set_adaptive_enabled, nullptr);
    obs_websocket_vendor_register_request(g_vendor, "GetMonitoringDeviceList", on_get_monitoring_device_list, nullptr);
    obs_websocket_vendor_register_request(g_vendor, "SetMonitoringDevice",  on_set_monitoring_device, nullptr);
    obs_websocket_vendor_register_request(g_vendor, "GetDiagnostics",      on_get_diagnostics,     nullptr);
    obs_websocket_vendor_register_request(g_vendor, "StopLogFileWrite",    on_stop_log_file_write, nullptr);

    blog(LOG_INFO, "[pulsar-multi-stream] vendor 'pulsar' registered with 18 requests");

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
