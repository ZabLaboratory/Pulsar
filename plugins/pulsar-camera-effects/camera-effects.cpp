#include <obs-module.h>
#include <algorithm>
#include <cmath>

OBS_DECLARE_MODULE()
MODULE_EXPORT const char *obs_module_description(void)
{
    return "Pulsar native camera vignette";
}

// Embedded shader: no caller-controlled file, shader or resource path.
static const char *shader = R"(
uniform float4x4 ViewProj;
uniform texture2d image;
uniform float amount;
uniform float radius;
uniform float softness;
sampler_state linearSampler { Filter = Linear; AddressU = Clamp; AddressV = Clamp; };
struct Vertex { float4 pos : POSITION; float2 uv : TEXCOORD0; };
Vertex VSDefault(Vertex v) { v.pos = mul(float4(v.pos.xyz, 1.0), ViewProj); return v; }
float4 PSVignette(Vertex v) : TARGET {
    float4 color = image.Sample(linearSampler, v.uv);
    float distance = length((v.uv - float2(0.5, 0.5)) * 2.0);
    float shade = smoothstep(radius, radius + softness, distance);
    color.rgb *= 1.0 - amount * shade;
    return color;
}
technique Draw { pass { vertex_shader = VSDefault(v); pixel_shader = PSVignette(v); } }
)";

struct Vignette {
    obs_source_t *source;
    gs_effect_t *effect;
    float amount, radius, softness;
};

static const char *name(void *) { return "Vignette"; }
static float bounded(obs_data_t *settings, const char *key, float fallback, float lo, float hi)
{
    double value = obs_data_get_double(settings, key);
    return std::isfinite(value) ? std::clamp(static_cast<float>(value), lo, hi) : fallback;
}
static void update(void *data, obs_data_t *settings)
{
    auto *filter = static_cast<Vignette *>(data);
    filter->amount = bounded(settings, "amount", .35f, 0.f, 1.f);
    filter->radius = bounded(settings, "radius", .65f, .1f, 1.5f);
    filter->softness = bounded(settings, "softness", .5f, .01f, 1.f);
}
static void *create(obs_data_t *settings, obs_source_t *source)
{
    auto *filter = new Vignette{source, nullptr, 0, 0, 0};
    obs_enter_graphics();
    filter->effect = gs_effect_create(shader, "pulsar-vignette", nullptr);
    obs_leave_graphics();
    if (!filter->effect) {
        delete filter;
        return nullptr;
    }
    update(filter, settings);
    return filter;
}
static void destroy(void *data)
{
    auto *filter = static_cast<Vignette *>(data);
    obs_enter_graphics();
    gs_effect_destroy(filter->effect);
    obs_leave_graphics();
    delete filter;
}
static void render(void *data, gs_effect_t *)
{
    auto *filter = static_cast<Vignette *>(data);
    if (filter->amount == 0.f) {
        obs_source_skip_video_filter(filter->source);
        return;
    }
    if (!obs_source_process_filter_begin(filter->source, GS_RGBA, OBS_ALLOW_DIRECT_RENDERING))
        return;
    gs_effect_set_float(gs_effect_get_param_by_name(filter->effect, "amount"), filter->amount);
    gs_effect_set_float(gs_effect_get_param_by_name(filter->effect, "radius"), filter->radius);
    gs_effect_set_float(gs_effect_get_param_by_name(filter->effect, "softness"), filter->softness);
    obs_source_process_filter_end(filter->source, filter->effect, 0, 0);
}
static void defaults(obs_data_t *settings)
{
    obs_data_set_default_double(settings, "amount", .35);
    obs_data_set_default_double(settings, "radius", .65);
    obs_data_set_default_double(settings, "softness", .5);
}
static obs_properties_t *properties(void *)
{
    auto *props = obs_properties_create();
    obs_properties_add_float_slider(props, "amount", "Intensity", 0, 1, .01);
    obs_properties_add_float_slider(props, "radius", "Clear center", .1, 1.5, .01);
    obs_properties_add_float_slider(props, "softness", "Edge softness", .01, 1, .01);
    return props;
}
bool obs_module_load(void)
{
    obs_source_info info{};
    info.id = "pulsar_vignette_filter";
    info.type = OBS_SOURCE_TYPE_FILTER;
    info.output_flags = OBS_SOURCE_VIDEO;
    info.get_name = name;
    info.create = create;
    info.destroy = destroy;
    info.update = update;
    info.video_render = render;
    info.get_defaults = defaults;
    info.get_properties = properties;
    obs_register_source(&info);
    return true;
}
