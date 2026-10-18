#include "pulsar-preview-frame.h"
#include <obs.hpp>
#include <graphics/vec4.h>
#include <atomic>
#include <cstring>

namespace pulsar_transition {
namespace {
constexpr const char *kSourceId = "pulsar_preview_frame";
struct Frame {
    obs_source_t *owner = nullptr;
    obs_source_t *base = nullptr;
    gs_texrender_t *texture = nullptr;
    uint32_t width = 0;
    uint32_t height = 0;
    std::atomic<bool> ready{false};
};

const char *name(void *) { return "Pulsar outgoing Preview frame"; }
void *create(obs_data_t *settings, obs_source_t *owner)
{
    auto *frame = new Frame;
    frame->owner = owner;
    frame->base = obs_get_source_by_name(obs_data_get_string(settings, "base_source"));
    frame->width = static_cast<uint32_t>(obs_data_get_int(settings, "width"));
    frame->height = static_cast<uint32_t>(obs_data_get_int(settings, "height"));
    if (!frame->base || !frame->width || !frame->height) {
        if (frame->base) obs_source_release(frame->base);
        delete frame;
        return nullptr;
    }
    return frame;
}
void destroy(void *data)
{
    auto *frame = static_cast<Frame *>(data);
    obs_enter_graphics();
    gs_texrender_destroy(frame->texture);
    obs_leave_graphics();
    obs_source_release(frame->base);
    delete frame;
}
uint32_t width(void *data) { return static_cast<Frame *>(data)->width; }
uint32_t height(void *data) { return static_cast<Frame *>(data)->height; }
void active(void *data, obs_source_enum_proc_t callback, void *param)
{
    // Keep the real composite hot while it is prepared behind the captured
    // image. Showing its live item before hiding this source avoids producer
    // deactivation/restart at the visual cut.
    auto *frame = static_cast<Frame *>(data);
    callback(frame->owner, frame->base, param);
}
void render(void *data, gs_effect_t *)
{
    auto *frame = static_cast<Frame *>(data);
    if (!frame->ready.load(std::memory_order_acquire)) {
        if (!frame->texture) frame->texture = gs_texrender_create(GS_RGBA, GS_ZS_NONE);
        if (!frame->texture) return;
        gs_blend_state_push();
        gs_blend_function(GS_BLEND_ONE, GS_BLEND_ZERO);
        const bool begun = gs_texrender_begin(frame->texture, frame->width, frame->height);
        if (begun) {
            vec4 blank;
            vec4_zero(&blank);
            gs_clear(GS_CLEAR_COLOR, &blank, 0.0f, 0);
            gs_ortho(0.0f, static_cast<float>(frame->width), 0.0f,
                     static_cast<float>(frame->height), -100.0f, 100.0f);
            obs_source_video_render(frame->base);
            gs_texrender_end(frame->texture);
            frame->ready.store(true, std::memory_order_release);
        }
        gs_blend_state_pop();
        if (!begun) return;
    }
    auto *effect = obs_get_base_effect(OBS_EFFECT_DEFAULT);
    gs_effect_set_texture(gs_effect_get_param_by_name(effect, "image"), gs_texrender_get_texture(frame->texture));
    while (gs_effect_loop(effect, "Draw")) gs_draw_sprite(gs_texrender_get_texture(frame->texture), 0, 0, 0);
}
}

void register_preview_frame_source()
{
    obs_source_info info = {};
    info.id = kSourceId;
    info.type = OBS_SOURCE_TYPE_INPUT;
    info.output_flags = OBS_SOURCE_VIDEO | OBS_SOURCE_CUSTOM_DRAW;
    info.get_name = name;
    info.create = create;
    info.destroy = destroy;
    info.get_width = width;
    info.get_height = height;
    info.video_render = render;
    info.enum_active_sources = active;
    obs_register_source(&info);
}

obs_source_t *create_preview_frame(obs_source_t *base, uint32_t width, uint32_t height)
{
    OBSDataAutoRelease settings = obs_data_create();
    obs_data_set_string(settings, "base_source", obs_source_get_name(base));
    obs_data_set_int(settings, "width", width);
    obs_data_set_int(settings, "height", height);
    return obs_source_create_private(kSourceId, "PulsarOutgoingPreviewFrame", settings);
}

bool preview_frame_ready(obs_source_t *source)
{
    if (!source || std::strcmp(obs_source_get_id(source), kSourceId) != 0) return false;
    auto *frame = static_cast<Frame *>(obs_obj_get_data(source));
    return frame && frame->ready.load(std::memory_order_acquire);
}
}
