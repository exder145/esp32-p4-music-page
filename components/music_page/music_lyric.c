// -*- coding: utf-8 -*-
// LRC 歌词解析实现：单遍扫描 + 插入排序，全部在调用方缓冲里原地进行，
// 解析完成后不再分配任何内存（界面换歌时只复用同一块缓冲）。
#include "music_lyric.h"

#include <string.h>

// 解析 "[mm:ss]" / "[mm:ss.xxx]" / "[mm:ss:xx]" 形式的时间标签。
// s 指向 '['，end 是本行结束位置；成功返回标签之后的位置，失败返回 NULL。
static const char *parse_stamp(const char *s, const char *end, uint32_t *out_ms) {
    if (s >= end || *s != '[') return NULL;
    const char *p = s + 1;

    uint32_t minutes = 0;
    int digits = 0;
    while (p < end && *p >= '0' && *p <= '9') {
        if (++digits > 4) return NULL;
        minutes = minutes * 10 + (uint32_t)(*p - '0');
        ++p;
    }
    if (digits == 0 || p >= end || *p != ':') return NULL;
    ++p;

    uint32_t seconds = 0;
    digits = 0;
    while (p < end && *p >= '0' && *p <= '9') {
        if (++digits > 2) return NULL;
        seconds = seconds * 10 + (uint32_t)(*p - '0');
        ++p;
    }
    if (digits == 0 || seconds > 59) return NULL;

    // 毫秒部分：".xx" 或 ":xx"（网易云对唱版把分钟与毫秒都写成冒号）
    uint32_t frac = 0;
    int fdigits = 0;
    if (p < end && (*p == '.' || *p == ':')) {
        ++p;
        while (p < end && *p >= '0' && *p <= '9') {
            if (fdigits < 3) frac = frac * 10 + (uint32_t)(*p - '0');
            ++fdigits;
            ++p;
        }
        for (int i = fdigits; i < 3; ++i) frac *= 10;   // ".5" = 500ms
    }
    if (p >= end || *p != ']') return NULL;
    ++p;

    *out_ms = (minutes * 60 + seconds) * 1000 + (frac > 999 ? 999 : frac);
    return p;
}

bool music_lyric_init(music_lyric_t *lyr, char *text, size_t text_size,
                      music_lyric_line_t *lines, int line_cap) {
    if (lyr == NULL || text == NULL || text_size < 32 || lines == NULL || line_cap < 1) {
        return false;
    }
    memset(lyr, 0, sizeof(*lyr));
    lyr->text = text;
    lyr->text_size = text_size;
    lyr->lines = lines;
    lyr->line_cap = line_cap;
    text[0] = '\0';
    return true;
}

void music_lyric_clear(music_lyric_t *lyr) {
    if (lyr == NULL) return;
    lyr->count = 0;
    if (lyr->text != NULL && lyr->text_size > 0) lyr->text[0] = '\0';
}

// 时间升序插入；已有相同时间戳时放弃（重复标签在部分 LRC 里很常见）。
static void insert_line(music_lyric_t *lyr, uint32_t time_ms, const char *text) {
    int pos = 0;
    while (pos < lyr->count && lyr->lines[pos].time_ms < time_ms) ++pos;
    if (pos < lyr->count && lyr->lines[pos].time_ms == time_ms) return;
    if (lyr->count >= lyr->line_cap) return;
    for (int i = lyr->count; i > pos; --i) lyr->lines[i] = lyr->lines[i - 1];
    lyr->lines[pos].time_ms = time_ms;
    lyr->lines[pos].text = text;
    ++lyr->count;
}

int music_lyric_parse(music_lyric_t *lyr, const char *lrc) {
    if (lyr == NULL || lyr->text == NULL || lyr->lines == NULL || lrc == NULL) return 0;
    // 只清结果、不动文本：界面传进来的lrc常常就是 lyr->text 本身（拷过来再解析），
    // 这里如果先清空 text 就等于把输入也清空了。
    lyr->count = 0;

    if (lrc != lyr->text) {
        size_t n = strlen(lrc);
        if (n >= lyr->text_size) {
            // 装不下就退到最后一个完整行，别把某句歌词切成半截
            n = lyr->text_size - 1;
            while (n > 0 && lrc[n] != '\n') --n;
            if (n > 0) ++n;
        }
        memmove(lyr->text, lrc, n);
        lyr->text[n] = '\0';
    }

    size_t used = 0;
    size_t cap = lyr->text_size;
    const char *p = lyr->text;
    while (*p != '\0' && lyr->count < lyr->line_cap) {
        const char *eol = strchr(p, '\n');
        size_t len = (eol != NULL) ? (size_t)(eol - p) : strlen(p);
        // 去掉行尾的 \r 与空白（LRC 常见 CRLF）
        while (len > 0 && (p[len - 1] == '\r' || p[len - 1] == ' ' || p[len - 1] == '\t')) --len;
        const char *line_end = p + len;

        // 一行可以有多个时间标签（副歌复用同一句）
        uint32_t stamps[8];
        int nstamp = 0;
        const char *q = p;
        while (q < line_end && *q == '[') {
            uint32_t ms = 0;
            const char *next = parse_stamp(q, line_end, &ms);
            if (next == NULL) break;
            if (nstamp < (int)(sizeof(stamps) / sizeof(stamps[0]))) stamps[nstamp++] = ms;
            q = next;
            while (q < line_end && (*q == ' ' || *q == '\t')) ++q;   // 标签之间允许空格
        }
        // 标签之后是歌词文本；跳过前导空白
        while (q < line_end && (*q == ' ' || *q == '\t')) ++q;
        size_t tlen = (size_t)(line_end - q);

        if (nstamp > 0 && tlen > 0 && used + tlen + 1 <= cap) {
            char *dst = lyr->text + used;
            // 原地压缩：dst 永远在 q 之前，但仍可能重叠，用 memmove
            memmove(dst, q, tlen);
            dst[tlen] = '\0';
            used += tlen + 1;
            for (int i = 0; i < nstamp && lyr->count < lyr->line_cap; ++i) {
                insert_line(lyr, stamps[i], dst);
            }
        }
        if (eol == NULL) break;
        p = eol + 1;
    }
    return lyr->count;
}

int music_lyric_index_at(const music_lyric_t *lyr, uint32_t position_ms) {
    if (lyr == NULL || lyr->count <= 0) return -1;
    if (position_ms < lyr->lines[0].time_ms) return -1;
    int lo = 0, hi = lyr->count - 1, found = 0;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        if (lyr->lines[mid].time_ms <= position_ms) {
            found = mid;
            lo = mid + 1;
        } else {
            hi = mid - 1;
        }
    }
    return found;
}

uint32_t music_lyric_time_at(const music_lyric_t *lyr, int index) {
    if (lyr == NULL || index < 0 || index >= lyr->count) return 0;
    return lyr->lines[index].time_ms;
}

const char *music_lyric_text_at(const music_lyric_t *lyr, int index) {
    if (lyr == NULL || index < 0 || index >= lyr->count) return NULL;
    return lyr->lines[index].text;
}
