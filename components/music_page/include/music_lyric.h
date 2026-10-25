// -*- coding: utf-8 -*-
// LRC 歌词解析：把一段 LRC 文本原地切成"时间 + 行文本"，供界面按播放进度取行。
// 不依赖 ESP-IDF 与 LVGL，可被 tools/tests/music_lyric_test.py 在宿主机编译测试。
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// 单首歌最多保留的歌词行数；超出部分丢弃（正常歌词远小于此值）
#define MUSIC_LYRIC_MAX_LINES 400
// 单首歌 LRC 文本上限；板端与界面共用同一个缓冲大小
#define MUSIC_LYRIC_MAX_BYTES (16 * 1024)

typedef struct {
    uint32_t time_ms;
    const char *text;      // 指向解析缓冲内部，随缓冲失效
} music_lyric_line_t;

typedef struct {
    char *text;                  // 调用方提供的解析缓冲（原地写 '\0' 分隔各行）
    size_t text_size;
    music_lyric_line_t *lines;   // 调用方提供的行数组
    int line_cap;
    int count;
} music_lyric_t;

// 绑定调用方缓冲；text_size 至少 32、line_cap 至少 1。成功返回 true。
bool music_lyric_init(music_lyric_t *lyr, char *text, size_t text_size,
                      music_lyric_line_t *lines, int line_cap);

// 清空解析结果（保留缓冲绑定）。
void music_lyric_clear(music_lyric_t *lyr);

// 解析 LRC：结果按时间升序，重复时间戳只保留第一条，行文本已去掉首尾空白。
// 识别 "[mm:ss]"、"[mm:ss.x]"、"[mm:ss.xxx]" 与 "[mm:ss:xx]"（对唱版用冒号）。
// 没有时间标签或内容为空的行直接跳过。返回解析出的行数。
int music_lyric_parse(music_lyric_t *lyr, const char *lrc);

// 当前位置应高亮的行号：最后一个 time_ms <= position_ms 的行；
// 早于第一行返回 -1，晚于最后一行返回最后一行。没有歌词也返回 -1。
int music_lyric_index_at(const music_lyric_t *lyr, uint32_t position_ms);

// 行时间（毫秒）；index 越界返回 0。
uint32_t music_lyric_time_at(const music_lyric_t *lyr, int index);

// 行文本；index 越界返回 NULL。
const char *music_lyric_text_at(const music_lyric_t *lyr, int index);

#ifdef __cplusplus
}
#endif
