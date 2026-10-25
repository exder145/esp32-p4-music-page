// -*- coding: utf-8 -*-
// 网易云远控服务：通过局域网 HTTP 与电脑端代理（tools/music_agent）通信。
// 未发现代理时自动进入演示模式，保证音乐页单独可用。
//
// 线程约定：网络操作全部在内部任务执行；界面线程只调用本头文件的
// 快照/封面/歌词读取接口（互斥锁保护），绝不直接触碰网络。
#pragma once
#include <stddef.h>

#include "music_state.h"
#include "esp_err.h"
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    MUSIC_CMD_PLAY_PAUSE,
    MUSIC_CMD_NEXT,
    MUSIC_CMD_PREV,
} music_cmd_t;

#define MUSIC_ART_W 240
#define MUSIC_ART_H 240
#define MUSIC_BLUR_W 96
#define MUSIC_BLUR_H 34

// 启动远控任务（重复调用安全）。退出页面时应调用 music_remote_stop()。
esp_err_t music_remote_start(void);

// 停止远控任务；内部用信号量立即唤醒，任务在毫秒级内退出。
void music_remote_stop(void);

// 拷贝一份当前状态快照，未启动时返回全零快照。
void music_remote_get_snapshot(music_state_t *out);

// 读取当前封面（MUSIC_ART_W×MUSIC_ART_H 的 RGB565 像素，小端序）。
// seq 返回封面版本号；与上次不同即表示封面已更换。无封面返回 false。
bool music_remote_get_art(const void **pixels, uint32_t *seq);

// 读取背景模糊小图（MUSIC_BLUR_W×MUSIC_BLUR_H 的 RGB565，需界面放大铺满）。
bool music_remote_get_blur(const void **pixels);

// 取当前曲目的完整 LRC 歌词文本（换歌后由代理提供）。
// *seq 传入调用方已处理的版本号：版本没变时直接返回 false，不拷贝；
// 有新版本时把歌词拷进 dst 并回写 *seq，返回 true。seq 可为 NULL（总是拷贝）。
bool music_remote_copy_lyrics(char *dst, size_t dst_size, uint32_t *seq);

// 发送控制指令（非阻塞，交给内部任务发送 HTTP 请求）。
void music_remote_send_cmd(music_cmd_t cmd);

#ifdef __cplusplus
}
#endif
