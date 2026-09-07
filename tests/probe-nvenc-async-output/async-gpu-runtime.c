/* Exercise the real GPU encode scheduler with an independently ready batch.
 * One frame/second makes the assertion about input count, not a fragile
 * sub-millisecond wall-clock deadline. Real NVENC is qualified separately. */
#include <windows.h>
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include "obs.h"
#include "obs-encoder.h"
#include "obs-output.h"
#include "graphics/graphics.h"
#include "util/platform.h"

static HANDLE completed;
static volatile LONG delivered, submitted;
static int remaining, next_packet;
static uint64_t ready_at;
static bool fail_pending;
static const int64_t order[] = {2, 0, 1};
static const char *name(void *unused) { (void)unused; return "Pulsar async GPU fixture"; }
static void destroy(void *unused) { (void)unused; }
static void *create_encoder(obs_data_t *settings, obs_encoder_t *encoder)
{ (void)settings; return encoder; }

static bool pending(void *data, struct encoder_packet *packet, bool *received)
{
    assert(packet->encoder == data && packet->timebase_num == 1 && packet->timebase_den == 1);
    *received = remaining > 0 && os_gettime_ns() >= ready_at;
    if (!*received) return true;
    if (fail_pending) return false;
    /* Completion must be serviced before a fourth texture is submitted. */
    assert(InterlockedCompareExchange(&submitted, 0, 0) == 3);
    static uint8_t bytes[] = {0, 0, 0, 1, 0x65, 0x88};
    packet->data = bytes;
    packet->size = sizeof(bytes);
    packet->type = OBS_ENCODER_VIDEO;
    packet->keyframe = true;
    packet->pts = order[next_packet];
    packet->dts = next_packet - 2;
    ++next_packet;
    --remaining;
    return true;
}

static bool encode_texture(void *data, struct encoder_texture *texture, int64_t pts,
                           uint64_t lock_key, uint64_t *next_key,
                           struct encoder_packet *packet, bool *received)
{
    (void)data; (void)packet;
    assert(texture && texture->tex[0]);
    /* Fulfil the real shared-texture lifetime protocol, even though the mock
     * compressed bytes do not inspect the pixels. */
    obs_enter_graphics();
    assert(gs_texture_acquire_sync(texture->tex[0], lock_key, 5000) == 0);
    assert(gs_texture_release_sync(texture->tex[0], *next_key) == 0);
    obs_leave_graphics();
    LONG count = InterlockedIncrement(&submitted);
    assert(pts == count - 1 && count <= 3);
    if (count == 3) {
        remaining = 3;
        ready_at = os_gettime_ns() + 5000000ULL;
    }
    *received = false;
    return true;
}

static void *create_output(obs_data_t *settings, obs_output_t *output)
{ (void)settings; return output; }
static bool start_output(void *data)
{
    return obs_output_can_begin_data_capture(data, 0) &&
           obs_output_initialize_encoders(data, 0) && obs_output_begin_data_capture(data, 0);
}
static void stop_output(void *data, uint64_t ts)
{
    (void)ts;
    obs_output_end_data_capture(data);
    obs_output_signal_stop(data, OBS_OUTPUT_SUCCESS);
}
static void receive(void *data, struct encoder_packet *packet)
{
    (void)data;
    if (!packet && fail_pending) {
        SetEvent(completed);
        return;
    }
    assert(packet != NULL);
    LONG index = InterlockedCompareExchange(&delivered, 0, 0);
    assert(index < 3 && packet->pts == order[index] && packet->dts == index - 2);
    assert(InterlockedCompareExchange(&submitted, 0, 0) == 3);
    if (InterlockedIncrement(&delivered) == 3) SetEvent(completed);
}

int main(int argc, char **argv)
{
    (void)argv;
    fail_pending = argc > 1;
    _putenv_s("PULSAR_NVENC_ASYNC_OUTPUT", "1");
    assert(obs_startup("en-US", NULL, NULL));
    struct obs_video_info video = {0};
    video.graphics_module = "libobs-d3d11.dll";
    video.fps_num = video.fps_den = 1;
    video.base_width = video.output_width = 64;
    video.base_height = video.output_height = 64;
    video.output_format = VIDEO_FORMAT_NV12;
    video.colorspace = VIDEO_CS_709;
    video.range = VIDEO_RANGE_PARTIAL;
    video.gpu_conversion = true;
    video.scale_type = OBS_SCALE_BILINEAR;
    assert(obs_reset_video(&video) == OBS_VIDEO_SUCCESS);
    struct obs_encoder_info encoder_info = {0};
    encoder_info.id = "pulsar_async_gpu_fixture";
    encoder_info.codec = "h264";
    encoder_info.type = OBS_ENCODER_VIDEO;
    encoder_info.caps = OBS_ENCODER_CAP_PASS_TEXTURE;
    encoder_info.get_name = name;
    encoder_info.create = create_encoder;
    encoder_info.destroy = destroy;
    encoder_info.encode_texture2 = encode_texture;
    encoder_info.get_pending_packet = pending;
    obs_register_encoder(&encoder_info);
    struct obs_output_info output_info = {0};
    output_info.id = "pulsar_async_gpu_sink";
    output_info.flags = OBS_OUTPUT_VIDEO | OBS_OUTPUT_ENCODED;
    output_info.get_name = name;
    output_info.create = create_output;
    output_info.destroy = destroy;
    output_info.start = start_output;
    output_info.stop = stop_output;
    output_info.encoded_packet = receive;
    obs_register_output(&output_info);
    completed = CreateEventW(NULL, TRUE, FALSE, NULL);
    obs_encoder_t *encoder = obs_video_encoder_create(encoder_info.id, "async-gpu", NULL, NULL);
    assert(encoder && completed);
    obs_encoder_set_video(encoder, obs_get_video());
    obs_output_t *output = obs_output_create(output_info.id, "async-gpu-sink", NULL, NULL);
    assert(output);
    obs_output_set_video_encoder(output, encoder);
    assert(obs_output_start(output));
    assert(WaitForSingleObject(completed, 10000) == WAIT_OBJECT_0);
    assert(submitted == 3);
    assert(fail_pending ? delivered == 0 : (delivered == 3 && remaining == 0));
    if (fail_pending) {
        uint64_t deadline = os_gettime_ns() + 1000000000ULL;
        while (obs_encoder_active(encoder) && os_gettime_ns() < deadline) Sleep(10);
        assert(!obs_encoder_active(encoder));
        /* The same output/encoder must be restartable after the failed
         * generation, without inheriting a pending stop or queued packets. */
        fail_pending = false;
        delivered = submitted = remaining = next_packet = 0;
        ResetEvent(completed);
        assert(obs_output_start(output));
        assert(WaitForSingleObject(completed, 10000) == WAIT_OBJECT_0);
        assert(delivered == 3 && submitted == 3 && remaining == 0);
    }
    obs_output_stop(output);
    obs_output_release(output);
    obs_encoder_release(encoder);
    CloseHandle(completed);
    obs_shutdown();
    puts(argc > 1 ? "PASS: pending failure retires the GPU encoder and the same output restarts cleanly" :
                    "PASS: real GPU scheduler drains delayed reordered packets before the next input and shuts down cleanly");
    return 0;
}
