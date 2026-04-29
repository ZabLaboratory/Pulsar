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
    Kind_Unknown,
};

const char *kind_to_string(DestinationKind k)
{
    switch (k) {
    case Kind_RtmpCustom: return "rtmp_custom";
    case Kind_VodLocal:   return "vod_local";
    default:              return "unknown";
    }
}

DestinationKind kind_from_string(const char *s)
{
    if (!s) return Kind_Unknown;
    if (std::string(s) == "rtmp_custom") return Kind_RtmpCustom;
    if (std::string(s) == "vod_local")   return Kind_VodLocal;
    return Kind_Unknown;
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

    if (d.kind == Kind_RtmpCustom) {
        OBSDataAutoRelease svcSettings = obs_data_create();
        obs_data_set_string(svcSettings, "server", d.url.c_str());
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

// ---- module-static singletons ----------------------------------------------

DestinationRegistry *g_registry = nullptr;
obs_websocket_vendor g_vendor = nullptr;

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
        obs_data_set_string(res, "error", "kind must be 'rtmp_custom' or 'vod_local'");
        return;
    }
    if (!url || !*url) {
        obs_data_set_string(res, "error", "url required (RTMP server URL or file path)");
        return;
    }
    auto id = g_registry->create(name ? name : "", kind, url, key ? key : "");
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

} // namespace

// ---- module entry points ---------------------------------------------------

bool obs_module_load(void)
{
    blog(LOG_INFO, "[pulsar-multi-stream] obs_module_load");
    g_registry = new DestinationRegistry();
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

    obs_websocket_vendor_register_request(g_vendor, "GetDestinations",   on_get_destinations,   nullptr);
    obs_websocket_vendor_register_request(g_vendor, "CreateDestination", on_create_destination, nullptr);
    obs_websocket_vendor_register_request(g_vendor, "RemoveDestination", on_remove_destination, nullptr);
    obs_websocket_vendor_register_request(g_vendor, "StartDestination",  on_start_destination,  nullptr);
    obs_websocket_vendor_register_request(g_vendor, "StopDestination",   on_stop_destination,   nullptr);
    obs_websocket_vendor_register_request(g_vendor, "StartAllDestinations", on_start_all,       nullptr);
    obs_websocket_vendor_register_request(g_vendor, "StopAllDestinations",  on_stop_all,        nullptr);

    blog(LOG_INFO, "[pulsar-multi-stream] vendor 'pulsar' registered with 7 requests");
}

void obs_module_unload(void)
{
    blog(LOG_INFO, "[pulsar-multi-stream] obs_module_unload");
    if (g_registry) {
        g_registry->teardown_all();
        delete g_registry;
        g_registry = nullptr;
    }
    g_vendor = nullptr; // obs-websocket cleans its own vendor table
}
