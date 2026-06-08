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
// Scenes are bound to libobs main mixer channel 0 via
// obs_set_output_source(0, ...) on setup AND on every set_current_scene.
// Without this binding, the encoder receives no frames and obs_output_start
// declines silently with last_error=null.
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
#include <util/util.hpp>

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "pulsar-frontend-stub.h"

namespace {

template <typename T> struct StubCallback {
    T cb;
    void *priv;
    StubCallback(T c, void *p) : cb(c), priv(p) {}
};

class PulsarFrontendAPI : public obs_frontend_callbacks {
public:
    PulsarFrontendAPI() = default;
    ~PulsarFrontendAPI() override { teardown(); }

    bool setup();
    void emit(obs_frontend_event event);

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
    bool obs_frontend_recording_split_file(void) override { return false; }
    bool obs_frontend_recording_add_chapter(const char *) override { return false; }

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
    void obs_frontend_preview_program_trigger_transition(void) override {}
    bool obs_frontend_preview_enabled(void) override { return previewEnabled; }
    void obs_frontend_set_preview_enabled(bool enable) override { previewEnabled = enable; }
    obs_source_t *obs_frontend_get_current_preview_scene(void) override
    {
        if (!studioMode)
            return nullptr;
        obs_source_t *s = previewScene ? previewScene : currentScene;
        return s ? obs_source_get_ref(s) : nullptr;
    }
    void obs_frontend_set_current_preview_scene(obs_source_t *scene) override
    {
        if (!scene)
            return;
        if (previewScene)
            obs_source_release(previewScene);
        previewScene = obs_source_get_ref(scene);
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

    static void OnStreamStart(void *param, calldata_t *)
    {
        static_cast<PulsarFrontendAPI *>(param)->emit(OBS_FRONTEND_EVENT_STREAMING_STARTED);
    }
    static void OnStreamStop(void *param, calldata_t *)
    {
        static_cast<PulsarFrontendAPI *>(param)->emit(OBS_FRONTEND_EVENT_STREAMING_STOPPED);
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
        // The replay-buffer output exposes the saved path through its
        // proc handler "get_last_replay". Phase 5 leaves lastReplay as
        // the empty default; pulsar-multi-stream (Phase 7+) will fetch
        // and surface the path when replay buffer becomes wired.
        static_cast<PulsarFrontendAPI *>(param)->emit(OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED);
    }
    static void OnVCamStart(void *param, calldata_t *)
    {
        static_cast<PulsarFrontendAPI *>(param)->emit(OBS_FRONTEND_EVENT_VIRTUALCAM_STARTED);
    }
    static void OnVCamStop(void *param, calldata_t *)
    {
        static_cast<PulsarFrontendAPI *>(param)->emit(OBS_FRONTEND_EVENT_VIRTUALCAM_STOPPED);
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

    // state
    obs_source_t *currentScene = nullptr;
    obs_source_t *previewScene = nullptr;
    obs_source_t *currentTransition = nullptr;
    std::vector<obs_source_t *> scenes;     // one entry, owned (refcount held).
    std::vector<obs_source_t *> transitions; // fade + stinger, owned.

    obs_output_t *streamOutput = nullptr;
    obs_output_t *recordOutput = nullptr;
    obs_output_t *replayOutput = nullptr;
    obs_output_t *virtualcamOutput = nullptr;
    obs_service_t *streamService = nullptr;

    obs_encoder_t *videoEncoder = nullptr;
    obs_encoder_t *audioEncoder = nullptr;
    obs_source_t *captureSource = nullptr;
    obs_sceneitem_t *captureItem = nullptr;

    // Audio sources bound to libobs main mixer channels 1-3 (the AAC encoder
    // mixes channels 0..5 into mixer index 0). Source IDs come from
    // upstream's win-wasapi plugin which is loaded by obs_load_all_modules
    // on Windows. These are NOT scene items -- audio sources live on
    // libobs's audio routing graph, not the visual scene.
    obs_source_t *desktopAudioSource = nullptr; // channel 1
    obs_source_t *processAudioSource = nullptr; // channel 2 (optional)
    obs_source_t *micAudioSource = nullptr;     // channel 3

    std::string recordDirectory; // resolved at setup() from env or default

    int transitionDuration = 300;
    int tbarPosition = 0;
    bool studioMode = false;
    bool previewEnabled = true;
    std::atomic<bool> recordingPaused{false};
    std::string lastRecording;
    std::string lastReplay;

    std::mutex callbacksMutex;
    std::vector<StubCallback<obs_frontend_event_cb>> eventCallbacks;
    std::vector<StubCallback<obs_frontend_save_cb>> saveCallbacks;
    std::vector<StubCallback<obs_frontend_save_cb>> preloadCallbacks;
};

bool PulsarFrontendAPI::setup()
{
    // Default scene.
    obs_scene_t *scene = obs_scene_create("Default");
    if (!scene) {
        blog(LOG_ERROR, "[pulsar-frontend-stub] obs_scene_create failed");
        return false;
    }
    currentScene = obs_source_get_ref(obs_scene_get_source(scene));
    obs_scene_release(scene); // currentScene holds the ref now
    scenes.push_back(obs_source_get_ref(currentScene));

    // Default transition (fade).
    obs_source_t *fade = obs_source_create_private("fade_transition", "Fade", nullptr);
    if (!fade) {
        blog(LOG_ERROR, "[pulsar-frontend-stub] fade_transition create failed");
        return false;
    }
    currentTransition = fade; // owns 1 ref
    transitions.push_back(obs_source_get_ref(currentTransition));

    // M10 (ADR 003 Amendment 1 §A1.1): register a STINGER transition source so
    // SetCurrentSceneTransition{name:"Stinger"} resolves and the active
    // transition can be a media-backed stinger. Its `path` is the locally
    // pinned demo asset (#64) -- resolved on this box, NEVER from a leaf value
    // (Amendment 2 §A2.1, R7 / C-PATH). transition_point defaults to the
    // asset's documented mid-point (300 ms); tp_type=0 means milliseconds.
    {
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
    }

    // Channel 0 is libobs's main video output. M10 Gap B' fix (ADR 003 §3.3):
    // bind the ACTIVE TRANSITION -- not the raw scene -- as output source 0,
    // seeded to hold the current scene. When idle the transition renders its
    // held scene 1:1 (passthrough, obs-source-transition.c:534), so the encoder
    // always sees frames; on a scene change obs_transition_start animates the
    // blend through this same bound source. This is how upstream OBS feeds the
    // program output (OBSBasic::SetTransition).
    bindTransitionOutput(currentTransition, currentScene);

    // Streaming output (rtmp_output). Created without encoders -- a Phase 7+
    // plugin (pulsar-multi-stream) configures encoders + service before
    // streaming_start succeeds.
    streamOutput = obs_output_create("rtmp_output", "PulsarStream", nullptr, nullptr);
    if (!streamOutput)
        blog(LOG_WARNING, "[pulsar-frontend-stub] rtmp_output unavailable");
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

    // Streaming service (rtmp_common). Configured to a placeholder Twitch-style
    // service; pulsar-multi-stream will replace it via set_streaming_service.
    OBSDataAutoRelease svcSettings = obs_data_create();
    obs_data_set_string(svcSettings, "service", "Twitch");
    streamService = obs_service_create("rtmp_common", "PulsarService", svcSettings, nullptr);
    if (!streamService)
        blog(LOG_WARNING, "[pulsar-frontend-stub] rtmp_common service unavailable");

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
    OBSDataAutoRelease vEncSettings = obs_data_create();
    obs_data_set_int(vEncSettings, "bitrate", videoBitrate);
    obs_data_set_string(vEncSettings, "rate_control", "CBR");
    obs_data_set_int(vEncSettings, "keyint_sec", 2);    // GOP = 2s -- Twitch / RTMP target
    obs_data_set_string(vEncSettings, "preset", "veryfast");
    obs_data_set_string(vEncSettings, "profile", "high");
    obs_data_set_string(vEncSettings, "tune", "zerolatency");
    videoEncoder = obs_video_encoder_create("obs_x264", "PulsarVideoEnc", vEncSettings, nullptr);
    if (!videoEncoder) {
        blog(LOG_WARNING, "[pulsar-frontend-stub] obs_x264 encoder unavailable");
    } else {
        obs_encoder_set_video(videoEncoder, obs_get_video());
        if (recordOutput)
            obs_output_set_video_encoder(recordOutput, videoEncoder);
        if (streamOutput)
            obs_output_set_video_encoder(streamOutput, videoEncoder);
        blog(LOG_INFO, "[pulsar-frontend-stub] x264 configured: %d kbps CBR, keyint=2s, preset=veryfast", videoBitrate);
    }

    int audioBitrate = 160; // kbps
    if (const char *e = std::getenv("PULSAR_AUDIO_BITRATE"); e && *e) {
        int v = std::atoi(e);
        if (v >= 32 && v <= 512) audioBitrate = v;
        else blog(LOG_WARNING, "[pulsar-frontend-stub] PULSAR_AUDIO_BITRATE=%s rejected", e);
    }
    OBSDataAutoRelease aEncSettings = obs_data_create();
    obs_data_set_int(aEncSettings, "bitrate", audioBitrate);
    audioEncoder = obs_audio_encoder_create("ffmpeg_aac", "PulsarAudioEnc", aEncSettings, 0, nullptr);
    if (!audioEncoder) {
        blog(LOG_WARNING, "[pulsar-frontend-stub] ffmpeg_aac encoder unavailable");
    } else {
        obs_encoder_set_audio(audioEncoder, obs_get_audio());
        if (recordOutput)
            obs_output_set_audio_encoder(recordOutput, audioEncoder, 0);
        if (streamOutput)
            obs_output_set_audio_encoder(streamOutput, audioEncoder, 0);
        blog(LOG_INFO, "[pulsar-frontend-stub] aac configured: %d kbps", audioBitrate);
    }

    // Capture source. Phase 6 uses window_capture (Windows). The window
    // descriptor follows obs's "<title>:<class>:<exe>" format. PULSAR_CAPTURE_WINDOW
    // overrides the default; when unset we leave the source unbound (it
    // produces black frames but the pipeline still encodes / records).
    OBSDataAutoRelease captureSettings = obs_data_create();
    if (const char *envWindow = std::getenv("PULSAR_CAPTURE_WINDOW"); envWindow && *envWindow) {
        obs_data_set_string(captureSettings, "window", envWindow);
        blog(LOG_INFO, "[pulsar-frontend-stub] window_capture target: %s", envWindow);
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

    return true;
}

void PulsarFrontendAPI::teardown()
{
    auto release_source_vec = [](std::vector<obs_source_t *> &v) {
        for (obs_source_t *s : v)
            if (s)
                obs_source_release(s);
        v.clear();
    };

    // Drain active outputs gracefully before release. A user who Ctrl+C's
    // mid-recording would otherwise hit obs_output_release on a live
    // output, which races with the muxer thread still writing frames.
    stop_output_and_wait(streamOutput, "stream");
    stop_output_and_wait(recordOutput, "record");
    stop_output_and_wait(replayOutput, "replay");
    stop_output_and_wait(virtualcamOutput, "virtualcam");

    // Unbind every main mixer channel (video on 0, audio on 1/2/3) before
    // releasing the underlying sources. Otherwise libobs keeps refs past
    // teardown and logs leaked-source warnings at obs_shutdown.
    obs_set_output_source(0, nullptr);
    obs_set_output_source(1, nullptr);
    obs_set_output_source(2, nullptr);
    obs_set_output_source(3, nullptr);

    // M10: output 0 is now a transition holding the scene (and possibly a
    // stinger media source as an active child). Clear each transition's held
    // sources before releasing so libobs drops those child refs cleanly and
    // does not warn about a leaked scene/media source at obs_shutdown.
    for (obs_source_t *t : transitions)
        if (t)
            obs_transition_clear(t);

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
    if (audioEncoder) {
        obs_encoder_release(audioEncoder);
        audioEncoder = nullptr;
    }
    if (captureSource) {
        // The scene owns the sceneitem, which holds its own ref to the
        // source. Release the setup-time ref; scene teardown clears the rest.
        obs_source_release(captureSource);
        captureSource = nullptr;
        captureItem = nullptr;
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
    release_source_vec(scenes);
    release_source_vec(transitions);

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
    for (obs_source_t *s : scenes) {
        obs_source_t *ref = obs_source_get_ref(s);
        if (ref)
            da_push_back(sources->sources, &ref);
    }
}

obs_source_t *PulsarFrontendAPI::obs_frontend_get_current_scene(void)
{
    return currentScene ? obs_source_get_ref(currentScene) : nullptr;
}

void PulsarFrontendAPI::obs_frontend_set_current_scene(obs_source_t *scene)
{
    if (!scene)
        return;
    if (currentScene == scene)
        return;
    obs_source_t *prev = currentScene;
    currentScene = obs_source_get_ref(scene);

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

    // M10: route the newly selected transition through the program output so a
    // subsequent SetCurrentProgramScene composites IT (e.g. switch Fade->
    // Stinger over obs-websocket). Seed it with the current scene and bind it
    // to output 0; if a transition is mid-animation we still re-point so the
    // next switch uses the new transition. Encoder is never blanked: the new
    // transition holds currentScene 1:1 the moment it is bound.
    if (!(prev && obs_transition_is_active(prev)))
        bindTransitionOutput(currentTransition, currentScene);

    if (prev)
        obs_source_release(prev);
    emit(OBS_FRONTEND_EVENT_TRANSITION_CHANGED);
}

void PulsarFrontendAPI::obs_frontend_streaming_start(void)
{
    if (!streamOutput || obs_output_active(streamOutput))
        return;
    emit(OBS_FRONTEND_EVENT_STREAMING_STARTING);
    if (!obs_output_start(streamOutput))
        blog(LOG_INFO, "[pulsar-frontend-stub] obs_output_start (stream) declined: %s",
             obs_output_get_last_error(streamOutput));
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

    // Resolve a fresh timestamped MP4 path under recordDirectory and bind
    // it to ffmpeg_muxer's settings just before start. mkdir-as-needed so
    // a missing recordings/ folder doesn't fail silently inside libobs.
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
    std::filesystem::path mp4 = std::filesystem::path(recordDirectory) /
                                ("pulsar-" + std::string(stamp) + ".mp4");

    OBSDataAutoRelease settings = obs_data_create();
    obs_data_set_string(settings, "path", mp4.string().c_str());
    // Empty muxer_settings -> ffmpeg picks defaults from extension (mp4 -> faststart on stop).
    obs_output_update(recordOutput, settings);

    emit(OBS_FRONTEND_EVENT_RECORDING_STARTING);
    if (!obs_output_start(recordOutput)) {
        blog(LOG_INFO, "[pulsar-frontend-stub] obs_output_start (record) declined: %s",
             obs_output_get_last_error(recordOutput));
        return;
    }
    blog(LOG_INFO, "[pulsar-frontend-stub] recording -> %s", mp4.string().c_str());
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

void PulsarFrontendAPI::obs_frontend_replay_buffer_start(void)
{
    if (!replayOutput || obs_output_active(replayOutput))
        return;
    emit(OBS_FRONTEND_EVENT_REPLAY_BUFFER_STARTING);
    if (!obs_output_start(replayOutput))
        blog(LOG_INFO, "[pulsar-frontend-stub] obs_output_start (replay) declined: %s",
             obs_output_get_last_error(replayOutput));
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
    if (!obs_output_start(virtualcamOutput))
        blog(LOG_INFO, "[pulsar-frontend-stub] obs_output_start (vcam) declined: %s",
             obs_output_get_last_error(virtualcamOutput));
}

void PulsarFrontendAPI::obs_frontend_stop_virtualcam(void)
{
    if (!virtualcamOutput || !obs_output_active(virtualcamOutput))
        return;
    obs_output_stop(virtualcamOutput);
}

// Module-static slot for the singleton, kept around so finished_loading /
// shutdown can reach it. obs_frontend_set_callbacks_internal owns the
// unique_ptr inside obs-frontend-api.dll; we keep a raw observer here.
PulsarFrontendAPI *g_api = nullptr;

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
    if (g_api) {
        blog(LOG_WARNING, "[pulsar-frontend-stub] init called twice");
        return;
    }
    // Install the vtable BEFORE obs_load_all_modules so plugins (notably
    // obs-websocket) find a populated callback table at obs_module_load time.
    // Heavy state -- scenes, encoders, outputs, sources, services -- depends
    // on plugins that aren't loaded yet, so it lives in setup() called from
    // pulsar_frontend_finished_loading() once obs_post_load_modules has run.
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
    g_api->emit(OBS_FRONTEND_EVENT_FINISHED_LOADING);
}

extern "C" void pulsar_frontend_shutdown(void)
{
    if (!g_api)
        return;
    g_api->emit(OBS_FRONTEND_EVENT_EXIT);
    // Hand ownership back to obs-frontend-api.dll, which deletes the object;
    // its destructor releases all libobs handles (outputs, scene, transition,
    // service) before any final obs_shutdown call.
    obs_frontend_set_callbacks_internal(nullptr);
    g_api = nullptr;
    blog(LOG_INFO, "[pulsar-frontend-stub] callbacks uninstalled");
}
