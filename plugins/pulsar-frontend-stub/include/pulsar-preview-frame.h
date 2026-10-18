#pragma once
#include <obs.h>

namespace pulsar_transition {
// One bounded GPU frame for a persistent composite. The factory is registered
// at frontend startup. Capture/render happens only on the OBS graphics thread.
void register_preview_frame_source();
obs_source_t *create_preview_frame(obs_source_t *base, uint32_t width, uint32_t height);
bool preview_frame_ready(obs_source_t *source);
}
