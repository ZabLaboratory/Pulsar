// Pulsar frontend stub.
//
// Replaces the upstream OBS Studio Qt frontend (frontend/widgets/) with
// a minimal implementation of the obs_frontend_callbacks vtable. Without
// this, every obs_frontend_* call from a plugin (notably obs-websocket's
// EventHandler) hits a "no callbacks" guard and returns null/false,
// leaving WebSocket events frozen and getters useless.
//
// This is a static library, not an OBS plugin. pulsar-headless's main()
// calls pulsar_frontend_init() *before* obs_load_all_modules(), so the
// callback table is populated when obs-websocket registers its event
// callback in its own obs_module_load.
//
// Phase 5 keeps the model deliberately simple:
//   - one immutable scene collection ("Default")
//   - one immutable profile ("Default")
//   - one default scene created at init
//   - one fade transition
//   - stream / record / replay-buffer / virtualcam outputs created upfront
//     and signal-bridged to frontend events; they accept obs_output_start
//     once a plugin (Phase 7+) configures their encoders + service.
//
// The signal-bridging path matches what OBSBasic does upstream: the
// frontend layer triggers STARTING/STOPPING manually around the start/stop
// calls, and STARTED/STOPPED come from the output's own signal handler.
// That keeps event ordering identical for v5 clients.

#include <obs.h>
#include <obs.hpp>
#include <obs-frontend-internal.hpp>
#include <util/config-file.h>
#include <util/darray.h>
#include <util/util.hpp>

#include <atomic>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
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
    char *obs_frontend_get_current_record_output_path(void) override { return bstrdup_or_null(""); }
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

    // state
    obs_source_t *currentScene = nullptr;
    obs_source_t *previewScene = nullptr;
    obs_source_t *currentTransition = nullptr;
    std::vector<obs_source_t *> scenes;     // one entry, owned (refcount held).
    std::vector<obs_source_t *> transitions; // one entry, owned.

    obs_output_t *streamOutput = nullptr;
    obs_output_t *recordOutput = nullptr;
    obs_output_t *replayOutput = nullptr;
    obs_output_t *virtualcamOutput = nullptr;
    obs_service_t *streamService = nullptr;

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
    emit(OBS_FRONTEND_EVENT_RECORDING_STARTING);
    if (!obs_output_start(recordOutput))
        blog(LOG_INFO, "[pulsar-frontend-stub] obs_output_start (record) declined: %s",
             obs_output_get_last_error(recordOutput));
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
    auto *api = new PulsarFrontendAPI();
    if (!api->setup()) {
        delete api;
        blog(LOG_ERROR, "[pulsar-frontend-stub] setup failed");
        return;
    }
    g_api = api;
    obs_frontend_set_callbacks_internal(api);
    obs_set_ui_task_handler(pulsar_ui_task_handler);
    blog(LOG_INFO, "[pulsar-frontend-stub] callbacks installed");
}

extern "C" void pulsar_frontend_finished_loading(void)
{
    if (!g_api)
        return;
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
