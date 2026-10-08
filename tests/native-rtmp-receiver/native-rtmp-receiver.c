/* A single-owner reference receiver: real libavformat -> libavcodec, no CLI
 * scheduler or diagnostic-log parsing in the measurement path. */
#include <windows.h>
#include <stdint.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/imgutils.h>
#include <libavutil/md5.h>
#include "util/platform.h"

static uint64_t frame_index, packet_index;
static int hash_frames;
static const AVRational milliseconds = {1, 1000};

static int failure(const char *stage, int code)
{
    char message[AV_ERROR_MAX_STRING_SIZE];
    av_strerror(code, message, sizeof(message));
    fprintf(stderr, "native receiver: %s: %s (%d)\n", stage, message, code);
    /* Diagnostic text remains on stderr; stdout has one structured writer. */
    printf("{\"kind\":\"error\",\"code\":%d}\n", code);
    return code;
}

static unsigned mean_plane(const uint8_t *data, int stride, int x, int y, int width, int height)
{
    unsigned sum = 0;
    for (int row = 0; row < height; row++)
        for (int col = 0; col < width; col++)
            sum += data[(y + row) * stride + x + col];
    unsigned count = (unsigned)(width * height);
    return (sum + count / 2) / count;
}

static int receive_frames(AVCodecContext *decoder, AVFrame *frame, AVRational timebase)
{
    for (;;) {
        int result = avcodec_receive_frame(decoder, frame);
        uint64_t ready_ns = os_gettime_ns();
        if (result == AVERROR(EAGAIN) || result == AVERROR_EOF)
            return 0;
        if (result < 0)
            return failure("receive frame", result);
        if ((frame->format != AV_PIX_FMT_YUV420P && frame->format != AV_PIX_FMT_YUVJ420P) ||
            frame->width != 1920 || frame->height != 1080 || frame->pts == AV_NOPTS_VALUE)
            return failure("unsupported decoded frame identity/format", AVERROR(EINVAL));
        int64_t pts_ms = av_rescale_q(frame->pts, timebase, milliseconds);
        unsigned y = mean_plane(frame->data[0], frame->linesize[0], 1888, 508, 32, 32);
        unsigned u = mean_plane(frame->data[1], frame->linesize[1], 944, 254, 16, 16);
        unsigned v = mean_plane(frame->data[2], frame->linesize[2], 944, 254, 16, 16);
        char md5_text[33] = {0};
        if (hash_frames) {
            struct AVMD5 *md5 = av_md5_alloc();
            if (!md5)
                return failure("MD5 allocation", AVERROR(ENOMEM));
            av_md5_init(md5);
            for (int plane = 0; plane < 3; plane++) {
                int width = plane ? frame->width / 2 : frame->width;
                int height = plane ? frame->height / 2 : frame->height;
                for (int row = 0; row < height; row++)
                    av_md5_update(md5, frame->data[plane] + row * frame->linesize[plane], width);
            }
            uint8_t digest[16];
            av_md5_final(md5, digest);
            av_free(md5);
            for (int i = 0; i < 16; i++)
                snprintf(md5_text + 2 * i, 3, "%02x", digest[i]);
        }
        printf("{\"kind\":\"frame\",\"frame_index\":%" PRIu64 ",\"pts_ms\":%" PRId64
               ",\"observed_at_monotonic_ns\":%" PRIu64 ",\"y_mean\":%u,\"u_mean\":%u,\"v_mean\":%u"
               ",\"picture_type\":\"%c\",\"md5\":\"%s\"}\n",
               frame_index++, pts_ms, ready_ns, y, u, v, av_get_picture_type_char(frame->pict_type), md5_text);
        av_frame_unref(frame);
    }
}

int main(int argc, char **argv)
{
    const char *input = NULL;
    int listen = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--input") && i + 1 < argc) input = argv[++i];
        else if (!strcmp(argv[i], "--listen")) listen = 1;
        else if (!strcmp(argv[i], "--hash-frames")) hash_frames = 1;
        else return 2;
    }
    if (!input || (listen && strncmp(input, "rtmp://127.0.0.1:", 17))) return 2;
    setvbuf(stdout, NULL, _IONBF, 0);
    av_log_set_level(AV_LOG_ERROR);
    LARGE_INTEGER frequency;
    if (!QueryPerformanceFrequency(&frequency) || frequency.QuadPart <= 0) return 2;
    printf("{\"kind\":\"ready\",\"clock\":\"libobs_qpc\",\"clock_frequency\":%" PRId64
           ",\"avcodec_version\":%u,\"threads\":1}\n", frequency.QuadPart, avcodec_version());
    avformat_network_init();
    AVFormatContext *format = NULL;
    AVCodecContext *decoder = NULL;
    AVPacket *packet = NULL;
    AVFrame *frame = NULL;
    AVDictionary *options = NULL;
    int result, status = 1;
    if (listen) av_dict_set(&options, "listen", "1", 0);
    result = avformat_open_input(&format, input, NULL, &options);
    av_dict_free(&options);
    if (result < 0) { failure("open input", result); goto cleanup; }
    result = avformat_find_stream_info(format, NULL);
    if (result < 0) { failure("stream info", result); goto cleanup; }
    int video_index = av_find_best_stream(format, AVMEDIA_TYPE_VIDEO, -1, -1, NULL, 0);
    if (video_index < 0) { failure("video stream", video_index); goto cleanup; }
    AVStream *stream = format->streams[video_index];
    if (stream->codecpar->codec_id != AV_CODEC_ID_H264) { failure("codec", AVERROR(EINVAL)); goto cleanup; }
    const AVCodec *codec = avcodec_find_decoder(AV_CODEC_ID_H264);
    decoder = avcodec_alloc_context3(codec);
    if (!decoder) { failure("decoder allocation", AVERROR(ENOMEM)); goto cleanup; }
    if ((result = avcodec_parameters_to_context(decoder, stream->codecpar)) < 0) {
        failure("decoder parameters", result); goto cleanup;
    }
    decoder->pkt_timebase = stream->time_base;
    decoder->thread_count = 1;
    /* Do not set LOW_DELAY or bypass codec picture reordering. */
    if ((result = avcodec_open2(decoder, codec, NULL)) < 0) { failure("decoder open", result); goto cleanup; }
    packet = av_packet_alloc();
    frame = av_frame_alloc();
    if (!packet || !frame) { failure("frame/packet allocation", AVERROR(ENOMEM)); goto cleanup; }
    for (;;) {
        result = av_read_frame(format, packet);
        uint64_t read_ns = os_gettime_ns();
        if (result < 0) break;
        if (packet->stream_index == video_index) {
            if (packet->pts == AV_NOPTS_VALUE || packet->dts == AV_NOPTS_VALUE) {
                failure("missing packet timestamp", AVERROR(EINVAL)); goto cleanup;
            }
            printf("{\"kind\":\"packet\",\"packet_index\":%" PRIu64 ",\"packet_pts\":%" PRId64
                   ",\"packet_dts\":%" PRId64 ",\"observed_at_monotonic_ns\":%" PRIu64 "}\n",
                   packet_index++, av_rescale_q(packet->pts, stream->time_base, milliseconds),
                   av_rescale_q(packet->dts, stream->time_base, milliseconds), read_ns);
            result = avcodec_send_packet(decoder, packet);
            if (result < 0) { failure("send packet", result); goto cleanup; }
            if (receive_frames(decoder, frame, stream->time_base) < 0) goto cleanup;
        }
        av_packet_unref(packet);
    }
    /* RTMP peer shutdown may be reported as EOF or I/O closure. A tail does
     * not supply or excuse any missing pre-candidate packet in the auditor. */
    if (!packet_index) { failure("empty input", result); goto cleanup; }
    if (avcodec_send_packet(decoder, NULL) < 0 || receive_frames(decoder, frame, stream->time_base) < 0)
        goto cleanup;
    printf("{\"kind\":\"end\",\"packets\":%" PRIu64 ",\"frames\":%" PRIu64 ",\"read_status\":%d}\n",
           packet_index, frame_index, result);
    status = 0;
cleanup:
    av_frame_free(&frame);
    av_packet_free(&packet);
    avcodec_free_context(&decoder);
    avformat_close_input(&format);
    avformat_network_deinit();
    return status;
}
