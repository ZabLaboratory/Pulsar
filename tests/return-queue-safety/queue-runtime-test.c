#include <windows.h>
#include <assert.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "shared-memory-queue.h"
#include "tiny-nv12-scale.h"

#define SIZE (64 * 64 * 3 / 2)
static video_queue_t *writer;
static HANDLE producer_done;

static void publish(uint64_t frame)
{
    uint8_t pixels[SIZE];
    memset(pixels, (int)(frame % 251), sizeof(pixels));
    uint8_t *planes[] = {pixels, pixels + 64 * 64};
    uint32_t stride[] = {64, 64};
    struct video_queue_frame_metadata metadata = {0};
    metadata.frame_id = frame;
    metadata.pts_ns = frame * 16666667;
    metadata.valid = 1;
    assert(video_queue_write_ex(writer, planes, stride, metadata.pts_ns,
                               VIDEO_QUEUE_PIXEL_FORMAT_NV12, &metadata));
}

static DWORD WINAPI produce(void *unused)
{
    (void)unused;
    for (uint64_t frame = 2; frame <= 1000; ++frame) {
        publish(frame);
        Sleep(1);
    }
    SetEvent(producer_done);
    return 0;
}

int main(void)
{
    wchar_t name[100];
    swprintf(name, 100, L"Local\\Pulsar253Test-ProgramReturnVideo-%lu", GetCurrentProcessId());
    writer = video_queue_create_named(64, 64, 166667, name);
    assert(writer);
    video_queue_t *reader = video_queue_open_named(name);
    assert(reader);
    assert(!video_queue_has_unread_frame(NULL));
    assert(!video_queue_has_unread_frame(reader));
    publish(1);
    assert(video_queue_state(reader) == SHARED_QUEUE_STATE_READY);
    assert(video_queue_has_unread_frame(reader));
    nv12_scale_t scaler = {0};
    nv12_scale_init(&scaler, TARGET_FORMAT_NV12, 64, 64, 64, 64);
    uint8_t output[SIZE];
    uint64_t pts = 0;
    struct video_queue_frame_metadata metadata = {0};
    assert(video_queue_read_ex(reader, &scaler, output, &pts, &metadata));
    assert(metadata.frame_id == 1 && pts == 16666667);
    assert(!video_queue_has_unread_frame(reader));
    producer_done = CreateEventW(NULL, TRUE, FALSE, NULL);
    assert(producer_done);
    HANDLE thread = CreateThread(NULL, 0, produce, NULL, 0, NULL);
    assert(thread);
    uint64_t last = 1;
    unsigned samples = 0;
    /* Sleep(1) may use a 15.6ms quantum in an ordinary Windows process. */
    DWORD deadline = GetTickCount() + 25000;
    while (WaitForSingleObject(producer_done, 0) != WAIT_OBJECT_0 || video_queue_has_unread_frame(reader)) {
        assert((LONG)(deadline - GetTickCount()) > 0);
        if (video_queue_has_unread_frame(reader) && video_queue_read_ex(reader, &scaler, output, &pts, &metadata)) {
            assert(metadata.frame_id > last);
            assert(pts == metadata.frame_id * 16666667);
            for (unsigned i = 0; i < SIZE; ++i)
                assert(output[i] == (uint8_t)(metadata.frame_id % 251));
            last = metadata.frame_id;
            ++samples;
        } else {
            Sleep(0);
        }
    }
    assert(WaitForSingleObject(thread, 1000) == WAIT_OBJECT_0);
    assert(last == 1000 && samples > 0);
    assert(!video_queue_has_unread_frame(reader));
    video_queue_close(writer);
    assert(!video_queue_has_unread_frame(reader));
    video_queue_close(reader);
    CloseHandle(thread);
    CloseHandle(producer_done);
    printf("PASS: readiness, read-only reader, stop, and %u coherent concurrent publications\n", samples);
    return 0;
}
