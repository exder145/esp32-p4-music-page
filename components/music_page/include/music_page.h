// -*- coding: utf-8 -*-
// 网易云风格黑胶音乐页：毛玻璃封面背景 + 旋转黑胶 + 唱针 + 播放控制。
// 数据来自 music_remote（电脑端代理或演示模式）。
#pragma once
#include <stdbool.h>
#include "lvgl.h"

#ifdef __cplusplus
extern "C" {
#endif

// 注入界面字体（歌名/歌词/按钮，建议含 CJK 字形的中文字体，20~25px）。
// 必须在 music_page_create() 之前调用；不注入时回落 LV_FONT_DEFAULT（无 CJK）。
void music_page_set_font(const lv_font_t *font);

// 在给定 screen（用 lv_obj_create(nullptr) 创建的独立屏幕对象）上构建页面。
// 内部缓冲在首次调用时分配，页面创建后常驻。
void music_page_create(lv_obj_t *screen);

// 页面显隐：隐藏时暂停动画定时器并停止远控任务，恢复时重新拉起。
// 只能在 LVGL 任务里调用（desktop_workspace 切页流程满足）。
void music_page_set_visible(bool visible);

#ifdef __cplusplus
}
#endif
