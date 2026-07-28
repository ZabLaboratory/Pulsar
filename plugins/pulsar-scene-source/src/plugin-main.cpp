// pulsar-scene-source -- libobs plugin DLL.
//
// Provides the `pulsar:SetCaptureSource` and `pulsar:GetCaptureSource`
// vendor requests so a host application can swap the broadcast capture
// source from `window_capture` (the default set up by
// pulsar-frontend-stub at boot) to a `browser_source` pointed at a
// scene-server URL.
//
// The plumbing is intentionally surgical : the plugin walks EVERY scene
// libobs knows, removes any previously-installed Pulsar-managed capture
// items (PulsarCapture, PulsarSceneSource), and adds the new one to the
// current frontend scene. The frontend-stub-built window_capture stays
// referenced by the frontend until it is removed from every scene that
// references it ; after that libobs's internal refcount drops to zero
// and the source is freed.
//
// Browser sources created here are pinned to webpage_control_level=None
// and are destroyed — never parked — on every swap (#158 / ADR Prism
// 028 §3.2). docs/PROTOCOL.md, "Browser sources — control level and
// lifecycle", is the normative statement ; the two blocks below are its
// implementation.
//
// Threading : obs-websocket dispatches handler calls on a worker
// thread. Source / scene mutations through libobs's public API are
// thread-safe (libobs serialises behind its own locks). We don't
// hold any plugin-side lock because the only state we touch is the
// `g_active` snapshot (atomic update via std::mutex).
//
// Vendor API :
//   SetCaptureSource(kind, url, width?, height?, fps?, reroute_audio?, css?)
//     -> { kind, url, width, height, fps, reroute_audio }
//     errors : "kind_not_supported" | "url_required" | "no_current_scene"
//             | "browser_source_unavailable" | "scene_add_failed"
//   GetCaptureSource()
//     -> { kind, url, width, height, fps, reroute_audio, last_change_unix }
//     If never called : kind="window_capture" (frontend-stub default), no other fields.

#include <obs-module.h>
#include <obs.h>
#include <obs.hpp>
#include <obs-frontend-api.h>
#include <util/util.hpp>

#include <obs-websocket-api.h>

#include <chrono>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>

OBS_DECLARE_MODULE()
OBS_MODULE_AUTHOR("Pulsar")
OBS_MODULE_USE_DEFAULT_LOCALE("pulsar-scene-source", "en-US")

const char *obs_module_name(void) { return "pulsar-scene-source"; }
const char *obs_module_description(void)
{
    return "Vendor request to swap the broadcast capture source (window_capture <-> browser_source)";
}

namespace {

// Names we own. The plugin recognises these on existing scene items so
// repeated SetCaptureSource calls swap rather than stack.
constexpr const char *kCaptureSourceName    = "PulsarSceneSource";
constexpr const char *kFrontendStubName     = "PulsarCapture";

obs_websocket_vendor g_vendor = nullptr;

// obs-browser's `webpage_control_level` (ControlLevel::None == 0, see
// plugins/pulsar-browser/obs-browser-source.hpp). Duplicated as a literal
// rather than included : pulling obs-browser-source.hpp in would drag the CEF
// headers into a plugin that has no business linking CEF. The value is pinned
// by scripts/check-webpage-control-level.py, which reads BOTH this constant and
// the enum it mirrors and fails the lint job if they ever drift apart.
//
// SECURITY (#158 / ADR Prism 028 §3.2) : the URL handed to SetCaptureSource is
// arbitrary third-party content (partner overlay, sponsor widget, an authored
// Solar composition). At any level above None the page can read this process's
// OBS state through `window.obsstudio` -- streaming / recording status at
// ReadObs, the scene list at ReadUser, and it can DRIVE the program scene at
// Advanced. Nothing in Zab reads `window.obsstudio`, so nothing needs more
// than None ; pin it here rather than inherit whatever the fork defaults to.
constexpr int kWebpageControlLevelNone = 0;

struct ActiveSnapshot {
    std::string kind;            // "browser_source" once set, otherwise empty
    std::string url;
    long long   width  = 0;
    long long   height = 0;
    long long   fps    = 0;
    bool        reroute_audio = false;
    long long   last_change_unix = 0;
};

std::mutex      g_state_mtx;
ActiveSnapshot  g_active;        // last successful set ; default = empty / "window_capture" implicit

long long now_unix()
{
    auto t = std::chrono::system_clock::now().time_since_epoch();
    return std::chrono::duration_cast<std::chrono::seconds>(t).count();
}

// True if `name` is `base`, or one of libobs's automatic de-dup
// variants of it (`base 2`, `base 3`, ...). When a source is created
// with a name libobs already knows, it appends " <n>" to keep names
// unique ; an exact strcmp then misses that instance and leaves it
// stranded on the scene forever (#110). Matching the numbered suffix
// precisely — a space followed by digits only — cleans those variants
// without over-matching an unrelated source that merely shares the
// prefix (e.g. "PulsarSceneSourceCustom").
bool is_managed_variant(const char *name, const char *base)
{
    size_t base_len = std::strlen(base);
    if (std::strncmp(name, base, base_len) != 0)
        return false;
    const char *rest = name + base_len;
    if (*rest == '\0')
        return true; // exact match
    if (*rest != ' ')
        return false; // e.g. "PulsarSceneSourceCustom" — not ours
    ++rest;
    if (*rest == '\0')
        return false; // trailing space with no number — not a de-dup name
    for (const char *p = rest; *p; ++p) {
        if (*p < '0' || *p > '9')
            return false; // suffix isn't purely digits
    }
    return true;
}

// Walk every item of the given scene, remove ones whose source name
// matches the Pulsar-managed capture set (canonical name or a libobs
// de-dup variant of it). Returns the number removed.
//
// Each matched source is RENAMED to a unique throwaway name *before* its
// scene item is dropped. This is the crux of the #110 fix : obs_source_
// set_name updates libobs's global name table synchronously (under the
// sources mutex), so the canonical name "PulsarSceneSource" is freed the
// instant this returns. obs_sceneitem_remove alone only *schedules* the
// source's destruction, which libobs may defer to a later tick — relying
// on that deferred release is a real race (proven intermittently in CI :
// the replacement source, created while the old one still owned the
// canonical name, stayed stuck as "PulsarSceneSource 2"). Renaming the
// outgoing source out of the way removes the dependency on destroy timing
// entirely. The throwaway name (source pointer + counter) is guaranteed
// unique and does not match is_managed_variant, so it is never re-swept.
int remove_managed_items(obs_scene_t *scene)
{
    struct Ctx { int removed; unsigned counter; } ctx { 0, 0 };
    auto cb = [](obs_scene_t * /*scn*/, obs_sceneitem_t *item, void *param) -> bool {
        Ctx *c = static_cast<Ctx *>(param);
        obs_source_t *src = obs_sceneitem_get_source(item);
        if (src) {
            const char *n = obs_source_get_name(src);
            if (n && (is_managed_variant(n, kCaptureSourceName)
                   || is_managed_variant(n, kFrontendStubName))) {
                std::string retired = "PulsarRetired-"
                    + std::to_string(reinterpret_cast<uintptr_t>(src))
                    + "-" + std::to_string(c->counter++);
                obs_source_set_name(src, retired.c_str());
                obs_sceneitem_remove(item);
                c->removed += 1;
            }
        }
        return true; // keep walking
    };
    obs_scene_enum_items(scene, cb, &ctx);
    return ctx.removed;
}

// Same sweep, over EVERY scene libobs knows -- not just the current frontend
// one.
//
// #158 / D2 (lifecycle). The single-scene sweep left a real hole: a managed
// browser source stranded on a scene the operator has since left kept its CEF
// browser and its JS state alive indefinitely, because the sweep never visited
// that scene and nothing else dropped the reference. A third-party page could
// therefore outlive the capture-source swap that was supposed to retire it,
// invisible to the program mix and to GetCaptureSource alike. Scenes other
// than the boot one are reachable over the v5 wire (Scenes/CreateScene), so
// this is not a theoretical arrangement.
//
// obs_frontend_get_scenes enumerates libobs itself (pulsar-frontend-stub, #119
// -- no mirror), so every scene that exists is visited, including ones created
// after boot. The per-scene sweep is idempotent and the retire-rename it does
// is what frees the canonical name synchronously (see above), so visiting the
// current scene as part of the list is exactly equivalent to visiting it alone.
int remove_managed_items_everywhere()
{
    struct obs_frontend_source_list scenes = {};
    obs_frontend_get_scenes(&scenes);

    int removed = 0;
    for (size_t i = 0; i < scenes.sources.num; i++) {
        obs_scene_t *s = obs_scene_from_source(scenes.sources.array[i]);
        if (s)
            removed += remove_managed_items(s);
    }

    obs_frontend_source_list_free(&scenes);
    return removed;
}

void on_set_capture_source(obs_data_t *req, obs_data_t *res, void *)
{
    const char *kind = obs_data_get_string(req, "kind");
    if (!kind || std::strcmp(kind, "browser_source") != 0) {
        obs_data_set_string(res, "error", "kind_not_supported");
        obs_data_set_string(res, "detail",
            "P13.4 ships kind='browser_source' only ; window_capture revert lands in P13.4.b");
        return;
    }

    const char *url = obs_data_get_string(req, "url");
    if (!url || !*url) {
        obs_data_set_string(res, "error", "url_required");
        return;
    }

    long long width  = obs_data_get_int(req, "width");
    if (width  <= 0) width  = 1920;
    long long height = obs_data_get_int(req, "height");
    if (height <= 0) height = 1080;
    long long fps    = obs_data_get_int(req, "fps");
    if (fps    <= 0) fps    = 60;
    bool reroute_audio = obs_data_get_bool(req, "reroute_audio");
    const char *css = obs_data_get_string(req, "css");

    // Build the browser_source settings. The keys come from obs-browser's
    // public defaults ; values that don't apply (e.g. css="" by default)
    // are fine to set unconditionally — obs-browser honours empty strings.
    OBSDataAutoRelease settings = obs_data_create();
    obs_data_set_string(settings, "url",                url);
    obs_data_set_int   (settings, "width",              static_cast<int>(width));
    obs_data_set_int   (settings, "height",             static_cast<int>(height));
    obs_data_set_int   (settings, "fps",                static_cast<int>(fps));
    obs_data_set_bool  (settings, "fps_custom",         true);
    obs_data_set_bool  (settings, "reroute_audio",      reroute_audio);
    // LIFECYCLE (#158 / D2). `shutdown=false` + `restart_when_active=false` are
    // deliberate and are NOT the security knob :
    //   - Pulsar is scene-agnostic (single-live invariant) : a program-scene
    //     change composes INSIDE the page, it does not swap the browser source.
    //     Tearing CEF down whenever the item is not "visible" would blank the
    //     antenna on every cut, so the source is KEPT ALIVE, with its JS state,
    //     for as long as it IS the active capture source.
    //   - What bounds a third-party page's lifetime is not visibility, it is
    //     remove_managed_items_everywhere() below : the moment a capture source
    //     is replaced, the outgoing one is dropped from EVERY scene, its
    //     refcount reaches zero and libobs frees it -- CEF browser and JS state
    //     with it. A page never survives a swap and is never parked, still
    //     running, on a scene the operator has left.
    // Written up in docs/PROTOCOL.md, "Browser sources -- control level and
    // lifecycle".
    obs_data_set_bool  (settings, "shutdown",           false);
    obs_data_set_bool  (settings, "restart_when_active", false);
    // Pin the page's reach into OBS explicitly -- see kWebpageControlLevelNone.
    obs_data_set_int   (settings, "webpage_control_level", kWebpageControlLevelNone);
    if (css && *css) obs_data_set_string(settings, "css", css);

    // Create the source. Caller of obs_source_create owns one ref ; we
    // pass that ref to the scene via obs_scene_add (which adds another
    // ref) and release ours. obs_source_create returns nullptr if
    // obs-browser is not loaded — that is the empirical signal.
    OBSSourceAutoRelease new_source = obs_source_create(
        "browser_source", kCaptureSourceName, settings, nullptr);
    if (!new_source) {
        obs_data_set_string(res, "error", "browser_source_unavailable");
        obs_data_set_string(res, "detail",
            "obs-browser plugin not loaded — build with -Full and ensure ENABLE_BROWSER=ON");
        return;
    }

    // Pull the current frontend scene.
    OBSSourceAutoRelease scene_src = obs_frontend_get_current_scene();
    if (!scene_src) {
        obs_data_set_string(res, "error", "no_current_scene");
        return;
    }
    obs_scene_t *scene = obs_scene_from_source(scene_src);
    if (!scene) {
        obs_data_set_string(res, "error", "current_source_not_a_scene");
        return;
    }

    // Drop any prior managed items FIRST, on EVERY scene (#158 / D2 — a
    // stranded browser source on a scene we no longer show is a third-party
    // page still running in this process). While the old source still owns
    // the canonical name, obs_source_create above may have been de-duped to
    // "PulsarSceneSource 2" (#110). remove_managed_items renames every
    // outgoing managed source out of the canonical name SYNCHRONOUSLY (see
    // its comment), so the canonical name is guaranteed free once it returns
    // — no dependency on libobs's deferred source destruction.
    int removed = remove_managed_items_everywhere();

    // Reclaim the canonical name now that it is free, so the fresh source is
    // always "PulsarSceneSource" — never a numbered variant a name-based
    // consumer (Prism's findBrowserSourceName) could lock onto. No-op on the
    // first call, where no de-dup occurred. VERIFY the rename actually took
    // (read the name back) instead of assuming it did : if some source we
    // did not sweep still held the canonical name, libobs would silently
    // re-de-dup and we surface that as a warning rather than a silent drift.
    if (std::strcmp(obs_source_get_name(new_source), kCaptureSourceName) != 0) {
        obs_source_set_name(new_source, kCaptureSourceName);
        const char *applied = obs_source_get_name(new_source);
        if (!applied || std::strcmp(applied, kCaptureSourceName) != 0) {
            blog(LOG_WARNING,
                 "[pulsar-scene-source] canonical name still held after retire "
                 "(fresh source is '%s') — name-based consumers may drift",
                 applied ? applied : "(null)");
        }
    }

    obs_sceneitem_t *item = obs_scene_add(scene, new_source);
    if (!item) {
        obs_data_set_string(res, "error", "scene_add_failed");
        return;
    }

    // Stash the snapshot so GetCaptureSource can report it.
    {
        std::lock_guard<std::mutex> lk(g_state_mtx);
        g_active.kind             = "browser_source";
        g_active.url              = url;
        g_active.width            = width;
        g_active.height           = height;
        g_active.fps              = fps;
        g_active.reroute_audio    = reroute_audio;
        g_active.last_change_unix = now_unix();
    }

    blog(LOG_INFO,
         "[pulsar-scene-source] SetCaptureSource browser_source url='%s' "
         "%lldx%lld@%lldfps reroute_audio=%d (removed %d prior managed items)",
         url, width, height, fps, reroute_audio ? 1 : 0, removed);

    obs_data_set_string(res, "kind",          "browser_source");
    obs_data_set_string(res, "url",           url);
    obs_data_set_int   (res, "width",         width);
    obs_data_set_int   (res, "height",        height);
    obs_data_set_int   (res, "fps",           fps);
    obs_data_set_bool  (res, "reroute_audio", reroute_audio);
    obs_data_set_int   (res, "removed_prior", removed);
}

void on_get_capture_source(obs_data_t * /*req*/, obs_data_t *res, void *)
{
    std::lock_guard<std::mutex> lk(g_state_mtx);
    if (g_active.kind.empty()) {
        // Never set since boot — frontend-stub's window_capture is the
        // implicit default. Surface that so the caller knows whether
        // SetCaptureSource has run yet.
        obs_data_set_string(res, "kind", "window_capture");
        obs_data_set_int   (res, "last_change_unix", 0);
        return;
    }
    obs_data_set_string(res, "kind",             g_active.kind.c_str());
    obs_data_set_string(res, "url",              g_active.url.c_str());
    obs_data_set_int   (res, "width",            g_active.width);
    obs_data_set_int   (res, "height",           g_active.height);
    obs_data_set_int   (res, "fps",              g_active.fps);
    obs_data_set_bool  (res, "reroute_audio",    g_active.reroute_audio);
    obs_data_set_int   (res, "last_change_unix", g_active.last_change_unix);
}

} // namespace

// ---- module entry points ---------------------------------------------------

bool obs_module_load(void)
{
    blog(LOG_INFO, "[pulsar-scene-source] obs_module_load");
    return true;
}

// Vendor registration must happen post-load so obs-websocket's proc
// handler is published. Same constraint as pulsar-multi-stream.
void obs_module_post_load(void)
{
    g_vendor = obs_websocket_register_vendor("pulsar-scene");
    if (!g_vendor) {
        blog(LOG_WARNING,
             "[pulsar-scene-source] obs-websocket not present ; vendor API disabled");
        return;
    }

    // Distinct vendor namespace `pulsar-scene` (NOT `pulsar`) :
    // obs-websocket's vendor_register_cb in our pulsar-websocket fork
    // refuses to register the same vendor name twice. The second
    // plugin to call it gets NULL and its requests never bind. Each
    // Pulsar plugin therefore owns its own namespace ; client code
    // calls `CallVendorRequest("pulsar-scene", ...)` for capture
    // source, `CallVendorRequest("pulsar", ...)` for the destinations
    // and adaptive bitrate surface.
    obs_websocket_vendor_register_request(g_vendor, "SetCaptureSource", on_set_capture_source, nullptr);
    obs_websocket_vendor_register_request(g_vendor, "GetCaptureSource", on_get_capture_source, nullptr);

    blog(LOG_INFO, "[pulsar-scene-source] vendor 'pulsar-scene' SetCaptureSource + GetCaptureSource registered");
}

void obs_module_unload(void)
{
    blog(LOG_INFO, "[pulsar-scene-source] obs_module_unload");
    g_vendor = nullptr; // obs-websocket cleans its own vendor table
}
