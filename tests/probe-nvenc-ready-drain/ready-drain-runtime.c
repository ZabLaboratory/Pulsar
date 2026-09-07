#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "obs.h"
#include "obs-encoder.h"
#include "obs-output.h"
#include "media-io/video-io.h"
#include "media-io/video-frame.h"

#define PULSAR_CHECK(expr)                                                         \
	do {                                                                         \
		if (!(expr)) {                                                        \
			fprintf(stderr, "CHECK FAILED: %s (%s:%d)\n", #expr, __FILE__, \
				__LINE__);                                              \
			abort();                                                       \
		}                                                                    \
	} while (0)

static HANDLE completed;
static volatile LONG delivered;
static int submitted, remaining, next_packet;
static const int64_t order[] = {2, 0, 1};
static uint64_t interval;
static const char *name(void *unused) { (void)unused; return "Pulsar ready-batch fixture"; }
static void destroy(void *unused) { (void)unused; }
static void *create_encoder(obs_data_t *settings, obs_encoder_t *encoder)
{ (void)settings; return encoder; }

static bool pending(void *data, struct encoder_packet *packet, bool *received)
{
    PULSAR_CHECK(packet->encoder == data && packet->timebase_num == 1 && packet->timebase_den == 60);
    *received = remaining != 0;
    if (!*received) return true;
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

static bool encode(void *data, struct encoder_frame *frame, struct encoder_packet *packet, bool *received)
{
    PULSAR_CHECK(frame->pts == submitted && frame->data[0][0] == 80);
    ++submitted;
    if (submitted == 3) remaining = 3;
    return pending(data, packet, received);
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
    PULSAR_CHECK(packet != NULL);
    LONG index = InterlockedCompareExchange(&delivered, 0, 0);
    PULSAR_CHECK(index < 3 && packet->pts == order[index] && packet->dts == index - 2);
    if (InterlockedIncrement(&delivered) == 3) SetEvent(completed);
}
int main(void)
{
    PULSAR_CHECK(obs_startup("en-US", NULL, NULL));
    struct obs_encoder_info encoder_info = {0};
    encoder_info.id = "pulsar_ready_batch_fixture";
    encoder_info.codec = "h264";
    encoder_info.type = OBS_ENCODER_VIDEO;
    encoder_info.get_name = name;
    encoder_info.create = create_encoder;
    encoder_info.destroy = destroy;
    encoder_info.encode = encode;
    encoder_info.get_pending_packet = pending;
    obs_register_encoder(&encoder_info);
    struct obs_output_info output_info = {0};
    output_info.id = "pulsar_ready_batch_sink";
    output_info.flags = OBS_OUTPUT_VIDEO | OBS_OUTPUT_ENCODED;
    output_info.get_name = name;
    output_info.create = create_output;
    output_info.destroy = destroy;
    output_info.start = start_output;
    output_info.stop = stop_output;
    output_info.encoded_packet = receive;
    obs_register_output(&output_info);
    video_t *video = NULL;
    struct video_output_info info = {0};
    info.name = "Pulsar253ReadyBatch";
    info.format = VIDEO_FORMAT_NV12;
    info.width = info.height = 64;
    info.fps_num = 60; info.fps_den = 1; info.cache_size = 4;
    info.colorspace = VIDEO_CS_709; info.range = VIDEO_RANGE_PARTIAL;
    PULSAR_CHECK(video_output_open(&video, &info) == VIDEO_OUTPUT_SUCCESS);
    interval = video_output_get_frame_time(video);
    completed = CreateEventW(NULL, TRUE, FALSE, NULL);
    obs_encoder_t *encoder = obs_video_encoder_create(encoder_info.id, "ready-batch", NULL, NULL);
    PULSAR_CHECK(encoder && completed);
    obs_encoder_set_video(encoder, video);
    obs_output_t *output = obs_output_create(output_info.id, "ready-batch-sink", NULL, NULL);
    PULSAR_CHECK(output);
    obs_output_set_video_encoder(output, encoder);
    /* Video-only outputs use default_encoded_callback, not the interleaver's
     * packet-timing observers. Exact timing association is covered by the
     * authenticated real A/V probe, not claimed by this mock-output test. */
    PULSAR_CHECK(obs_output_start(output));
    for (unsigned i = 0; i < 3; ++i) {
        struct video_frame frame = {0};
        PULSAR_CHECK(video_output_lock_frame_with_content(video, &frame, 1,
            1000000000ULL + i * interval, 1001000000ULL + i * interval));
        for (unsigned row = 0; row < 64; ++row)
            memset(frame.data[0] + row * frame.linesize[0], 80, 64);
        for (unsigned row = 0; row < 32; ++row)
            memset(frame.data[1] + row * frame.linesize[1], 128, 64);
        video_output_unlock_frame(video);
    }
    PULSAR_CHECK(WaitForSingleObject(completed, 5000) == WAIT_OBJECT_0);
    PULSAR_CHECK(delivered == 3 && submitted == 3 && remaining == 0);
    obs_output_stop(output);
    obs_output_release(output);
    obs_encoder_release(encoder);
    video_output_close(video);
    CloseHandle(completed);
    obs_shutdown();
    puts("PASS: libobs drains a reordered three-packet batch without another input, preserving exact PTS/DTS");
    return 0;
}
