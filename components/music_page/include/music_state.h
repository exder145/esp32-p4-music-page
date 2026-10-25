// -*- coding: utf-8 -*-
// 音乐远控的纯数据逻辑：时间格式化、进度计算与演示模式推进。
// 不依赖 ESP-IDF 与 LVGL，可被 tools/tests/music_state_test.py 在宿主机编译测试。
#pragma once
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MUSIC_TEXT_MAX 96
#define MUSIC_LYRIC_MAX 192

// 播放状态快照：music_remote 任务写入，界面任务读取。
typedef struct {
    bool agent_found;      // 已发现电脑端代理且未超时
    bool track_loaded;     // 代理上有正在使用的播放会话
    bool playing;
    bool demo;             // true = 未发现代理，显示板内演示数据
    bool art_ready;        // 当前封面已解码可用
    uint32_t position_ms;
    uint32_t duration_ms;
    uint32_t art_seq;      // 封面版本号，变化表示换了封面
    char title[MUSIC_TEXT_MAX];
    char artist[MUSIC_TEXT_MAX];
    char album[MUSIC_TEXT_MAX];
    char app[32];          // 播放来源应用名（如"网易云音乐"），演示模式为"演示模式"
    char lyric[MUSIC_LYRIC_MAX];  // 当前进度对应的歌词行（电脑端代理按进度挑选）
} music_state_t;

// 时间格式化为 mm:ss；超过 99 分钟时分钟位自然增长，不进位到小时。
void music_state_format_time(uint32_t ms, char *out, int out_len);

// 进度百分比 0..100；时长为 0 时返回 0，位置超过时长按 100 处理。
int music_state_progress_percent(uint32_t position_ms, uint32_t duration_ms);

// ── 演示模式 ─────────────────────────────────────────────────────
// 没发现电脑端代理时，用内置假歌单驱动界面，保证音乐页单独可用。
// 播放位置和当前曲目保存在模块内部，代理出现/消失后可无缝续播。

// 按 delta_ms 推进假进度；放完自动切下一首。输出填入 state。
void music_state_demo_advance(music_state_t *state, int delta_ms);

// 演示播放/暂停切换。
void music_state_demo_toggle(music_state_t *state);

// 演示切歌：dir = +1 下一首，-1 上一首。
void music_state_demo_skip(music_state_t *state, int dir);

#ifdef __cplusplus
}
#endif
