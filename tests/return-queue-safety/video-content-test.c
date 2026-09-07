#include <windows.h>
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "media-io/video-io.h"
#include "media-io/video-frame.h"
#include "obs.h"

static volatile LONG delivered = 0;
static uint64_t cadence[4], content[4];
static HANDLE ready;

static void receive(void *unused, struct video_data *frame)
{
    (void)unused;
    LONG index = InterlockedCompareExchange(&delivered, 0, 0);
    assert(index < 4);
    cadence[index] = frame->timestamp;
    content[index] = frame->content_pts_ns;
    assert(frame->data[0][0] == 80);
    if (InterlockedIncrement(&delivered) >= 3)
        SetEvent(ready);
}

int main(void)
{
    assert(obs_startup("en-US", NULL, NULL));
    video_t *video = NULL;
    struct video_output_info info = {0};
    info.name = "Pulsar253ContentFixture";
    info.format = VIDEO_FORMAT_NV12;
    info.width = info.height = 64;
    info.fps_num = 60;
    info.fps_den = 1;
    info.cache_size = 2;
    info.colorspace = VIDEO_CS_709;
    info.range = VIDEO_RANGE_PARTIAL;
    assert(video_output_open(&video, &info) == VIDEO_OUTPUT_SUCCESS);
    ready = CreateEventW(NULL, TRUE, FALSE, NULL);
    assert(ready && video_output_connect(video, NULL, receive, NULL));
    struct video_frame frame = {0};
    assert(video_output_lock_frame_with_content(video, &frame, 3, 1000000000ULL, 1100000000ULL));
    for (unsigned row = 0; row < 64; ++row)
        memset(frame.data[0] + row * frame.linesize[0], 80, 64);
    for (unsigned row = 0; row < 32; ++row)
        memset(frame.data[1] + row * frame.linesize[1], 128, 64);
    video_output_unlock_frame(video);
    assert(WaitForSingleObject(ready, 5000) == WAIT_OBJECT_0);
    assert(delivered == 3);
    for (unsigned i = 0; i < 3; ++i) {
        assert(cadence[i] == 1000000000ULL + i * video_output_get_frame_time(video));
        assert(content[i] == 1100000000ULL);
    }
    ResetEvent(ready);
    assert(video_output_lock_frame(video, &frame, 1, 2000000000ULL));
    for (unsigned row = 0; row < 64; ++row)
        memset(frame.data[0] + row * frame.linesize[0], 80, 64);
    for (unsigned row = 0; row < 32; ++row)
        memset(frame.data[1] + row * frame.linesize[1], 128, 64);
    video_output_unlock_frame(video);
    assert(WaitForSingleObject(ready, 5000) == WAIT_OBJECT_0);
    assert(delivered == 4 && cadence[3] == 2000000000ULL && content[3] == cadence[3]);
    video_output_disconnect(video, receive, NULL);
    video_output_close(video);
    CloseHandle(ready);
    obs_shutdown();
    puts("PASS: actual video-output cache preserves content identity across repetition and legacy API");
    return 0;
}
