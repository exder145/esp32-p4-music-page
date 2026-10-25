// -*- coding: utf-8 -*-
#include "music_state.h"

#include <stdio.h>
#include <string.h>

typedef struct {
    const char *title;
    const char *artist;
    const char *album;
    uint32_t duration_ms;
} demo_track_t;

// 演示歌单：仅用于驱动界面动画，标题带"演示"字样避免与真实播放混淆。
static const demo_track_t k_demo_tracks[] = {
    {"夜航星（演示）", "演示模式", "未连接电脑端", 224000},
    {"晴天（演示）", "演示模式", "未连接电脑端", 269000},
    {"海阔天空（演示）", "演示模式", "未连接电脑端", 326000},
};
#define DEMO_TRACK_COUNT (int)(sizeof(k_demo_tracks) / sizeof(k_demo_tracks[0]))

static int s_demo_track;
static int64_t s_demo_pos_ms;
static bool s_demo_playing = true;

void music_state_format_time(uint32_t ms, char *out, int out_len) {
    uint32_t total = ms / 1000;
    snprintf(out, out_len, "%02u:%02u", (unsigned)(total / 60), (unsigned)(total % 60));
}

int music_state_progress_percent(uint32_t position_ms, uint32_t duration_ms) {
    if (duration_ms == 0) return 0;
    if (position_ms >= duration_ms) return 100;
    return (int)(position_ms * 100 / duration_ms);
}

static void demo_fill(music_state_t *state) {
    const demo_track_t *track = &k_demo_tracks[s_demo_track];
    state->agent_found = false;
    state->track_loaded = true;
    state->playing = s_demo_playing;
    state->demo = true;
    state->art_ready = false;   // 演示模式没有真实封面，界面用程序化占位标签
    state->position_ms = (uint32_t)s_demo_pos_ms;
    state->duration_ms = track->duration_ms;
    snprintf(state->title, MUSIC_TEXT_MAX, "%s", track->title);
    snprintf(state->artist, MUSIC_TEXT_MAX, "%s", track->artist);
    snprintf(state->album, MUSIC_TEXT_MAX, "%s", track->album);
    snprintf(state->app, sizeof(state->app), "%s", "演示模式");
    snprintf(state->lyric, MUSIC_LYRIC_MAX, "%s", "演示模式没有歌词，等电脑端接入");
}

void music_state_demo_advance(music_state_t *state, int delta_ms) {
    if (delta_ms < 0) delta_ms = 0;
    if (s_demo_playing) {
        s_demo_pos_ms += delta_ms;
        if (s_demo_pos_ms >= (int64_t)k_demo_tracks[s_demo_track].duration_ms) {
            s_demo_track = (s_demo_track + 1) % DEMO_TRACK_COUNT;
            s_demo_pos_ms = 0;
        }
    }
    demo_fill(state);
}

void music_state_demo_toggle(music_state_t *state) {
    s_demo_playing = !s_demo_playing;
    demo_fill(state);
}

void music_state_demo_skip(music_state_t *state, int dir) {
    int count = DEMO_TRACK_COUNT;
    s_demo_track = ((s_demo_track + dir) % count + count) % count;
    s_demo_pos_ms = 0;
    demo_fill(state);
}
