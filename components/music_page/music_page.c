// -*- coding: utf-8 -*-
// 网易云风格黑胶音乐页实现。
//
// 视觉全部程序化绘制，不占用素材分区：
//   背景 = 电脑端代理提供的 96x34 模糊小图放大铺满（换歌才重画一次）+ 压暗遮罩；
//   黑胶盘 340x340 静态（旋转对称，不用逐帧重绘），只有中间 240x240 的
//   封面/占位标签逐帧旋转（lv_image 软件变换，每帧约 5.8 万像素，P4 无压力）；
//   唱针独立小图，播放/暂停时用 lv_anim 摆入摆出。
// 右侧信息列：右上角状态胶囊 + 标题/歌手 + 6 行滚动歌词窗口 + 进度条 + 控制按钮。
// 歌词来自电脑端代理下发的整首 LRC（music_lyric.c 解析），本页按播放进度
// 自己滚动：当前句固定在第 3 行，逐行上移，行内再按时间做连续位移。
// 所有缓冲放 PSRAM（合计约 1.7MB）。
#include "music_page.h"
#include "music_lyric.h"
#include "music_remote.h"
#include "music_state.h"

#include <math.h>
#include <stdio.h>
#include <string.h>
#include <inttypes.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "lvgl.h"

#define TAG "music_page"

// ── 字体注入 ──────────────────────────────────────────────────────
// 界面文字（歌名/歌词/按钮）需要 CJK 字体；不同工程字体不同，这里不写死依赖，
// 由接入方在 music_page_create() 之前调 music_page_set_font() 注入。
static const lv_font_t *s_font;

static const lv_font_t *page_font(void) {
    return s_font ? s_font : LV_FONT_DEFAULT;
}

void music_page_set_font(const lv_font_t *font) { s_font = font; }

// ── 布局常量（1280x452 横屏） ─────────────────────────────────────
#define VINYL_D 340
#define VINYL_CX 300
#define VINYL_CY 226
#define COVER_D 240
#define ARM_W 72
#define ARM_H 248
// 唱针两个姿态（单位 0.1°，lv_image_set_rotation 的正角度在屏幕上是顺时针，
// 会把唱针头往左甩出去）：
//   ARM_DEG_REST = +12°：唱针抬起，悬在唱片左外侧；
//   ARM_DEG_PLAY = -8° ：唱针落下，针尖正好落在唱片外圈唱纹上（离盘心约 150px，
//                        在封面圆(r=120)之外、唱片边(r=170)之内）。
// ⚠️ 之前写成"播放 16° / 暂停 0°"，正好把两个姿态做反了：播放时唱针反而抬走，
//    暂停时又贴在盘边。
#define ARM_DEG_PLAY (-80)
#define ARM_DEG_REST 120
// 控制按钮图标尺寸（用户反馈图标偏大，比按钮小一圈更像正经播放器）
#define GLYPH_NAV_W 40
#define GLYPH_NAV_H 30
#define GLYPH_PLAY_W 34
#define GLYPH_PLAY_H 34
#define SCREEN_W 1280
#define SCREEN_H 452

// 右侧信息列
#define PANEL_X 492
#define PANEL_W 760
#define PANEL_RIGHT (PANEL_X + PANEL_W)

// 歌词窗口：5 个标签、4 行可见（第 5 行预留在窗口下方）。
// 行高 60 = 两行 20px 文字（主字体的 line_height 是 30），外语歌要显示
// "原文 + 中文译文" 两行，所以比单语时高一些、可见行数少一行。
#define LYRIC_TOP 84
#define LYRIC_ROW 60
#define LYRIC_VISIBLE 4
#define LYRIC_LABELS (LYRIC_VISIBLE + 1)
#define LYRIC_ACTIVE_ROW 2

// 每帧转 1.4°（40ms 一帧）≈ 10.3 秒一圈。原来是 2°（7.2 秒一圈），
// 用户反馈转得太快，放慢一点更像"背景在慢慢转"而不是风扇。
#define SPIN_STEP_DEG10 14
#define SPIN_TICK_MS 40

#define COL_BG_TOP 0x191922
#define COL_BG_BOT 0x0B0B10
#define COL_TEXT 0xF2F2F5
#define COL_SUB 0xADADB8
#define COL_FAINT 0x70707C
#define COL_LYRIC_NEXT 0x8A8A96      // 非当前句的歌词
#define COL_ACCENT 0xEC4141
#define COL_BAR_TRACK 0x2E2E38
#define COL_BTN 0x26262F
#define COL_BTN_BORDER 0x3A3A46
#define COL_BTN_PRESSED 0x35353F
#define COL_GLYPH 0xE9E9EF

// ── 程序化画布助手（RGB565 + 独立 A8 透明面） ─────────────────────
typedef struct {
    uint16_t *rgb;
    uint8_t *a;
    int w;
    int h;
} canvas_t;

static uint16_t to565(uint32_t c) {
    uint16_t r = (c >> 19) & 0x1F;
    uint16_t g = (c >> 10) & 0x3F;
    uint16_t b = (c >> 3) & 0x1F;
    return (uint16_t)((r << 11) | (g << 5) | b);
}

static void canvas_px(canvas_t *c, int x, int y, uint32_t color, uint8_t alpha) {
    if (x < 0 || y < 0 || x >= c->w || y >= c->h) return;
    c->rgb[y * c->w + x] = to565(color);
    c->a[y * c->w + x] = alpha;
}

static void canvas_disc(canvas_t *c, int cx, int cy, int r, uint32_t color, uint8_t alpha) {
    for (int y = cy - r; y <= cy + r; ++y) {
        for (int x = cx - r; x <= cx + r; ++x) {
            int dx = x - cx, dy = y - cy;
            if (dx * dx + dy * dy <= r * r) canvas_px(c, x, y, color, alpha);
        }
    }
}

// 圆角矩形；radius <= 0 退化为直角
static void canvas_rect(canvas_t *c, int x0, int y0, int x1, int y1,
                        uint32_t color, uint8_t alpha, int radius) {
    for (int y = y0; y <= y1; ++y) {
        for (int x = x0; x <= x1; ++x) {
            if (radius > 0) {
                int cx = x < x0 + radius ? x0 + radius : x > x1 - radius ? x1 - radius : x;
                int cy = y < y0 + radius ? y0 + radius : y > y1 - radius ? y1 - radius : y;
                int dx = x - cx, dy = y - cy;
                if (dx * dx + dy * dy > radius * radius) continue;
            }
            canvas_px(c, x, y, color, alpha);
        }
    }
}

// 竖直条（x 起 w 宽，y0..y1）
static void canvas_vbar(canvas_t *c, int x, int w, int y0, int y1,
                        uint32_t color, uint8_t alpha) {
    for (int y = y0; y <= y1; ++y) {
        for (int dx = 0; dx < w; ++dx) canvas_px(c, x + dx, y, color, alpha);
    }
}

// 指向 right_dir 的实心三角（播放器图标用）
static void canvas_tri(canvas_t *c, int x0, int x1, int y0, int y1, bool right_dir,
                       uint32_t color) {
    int h = y1 - y0, w = x1 - x0, cy = y0 + h / 2;
    for (int x = x0; x < x1; ++x) {
        // right_dir=true：底边在左、尖端朝右（▶）；false：底边在右、尖端朝左（◀）。
        // 此前两个方向写反，导致上一首/下一首/播放三个图标的朝向全部镜像。
        int t = right_dir ? (x1 - 1 - x) : (x - x0);
        int half = t * (h / 2) / w;
        for (int y = cy - half; y <= cy + half; ++y) canvas_px(c, x, y, color, 255);
    }
}

// ── 静态缓冲与图像描述符 ─────────────────────────────────────────
static uint16_t *s_vinyl_buf;      // RGB565A8：rgb 面 + a 面
static uint16_t *s_arm_buf;
static uint16_t *s_cover_buf;      // RGB565A8：rgb 面 + 圆形遮罩（封面裁成圆）
static uint16_t *s_blur_buf;       // 1280x452 RGB565，惰性分配
static uint16_t *s_glyph_prev, *s_glyph_next, *s_glyph_play, *s_glyph_pause;

static lv_image_dsc_t s_vinyl_dsc, s_arm_dsc, s_cover_dsc, s_blur_dsc;
static lv_image_dsc_t s_prev_dsc, s_next_dsc, s_play_dsc, s_pause_dsc;

static lv_obj_t *s_blur_img, *s_veil;
static lv_obj_t *s_vinyl_img, *s_cover_img, *s_arm_img;
static lv_obj_t *s_status_chip, *s_status, *s_title, *s_artist;
static lv_obj_t *s_lyric_cont, *s_lyric_empty;
static lv_obj_t *s_lyric_rows[LYRIC_LABELS];
static lv_obj_t *s_bar, *s_time_cur, *s_time_dur;
static lv_obj_t *s_btn_play, *s_play_icon, *s_pause_icon;

static lv_timer_t *s_anim_timer, *s_ui_timer;
static bool s_visible;
static uint32_t s_angle;                     // 0.1°，封面当前角度
static uint32_t s_art_seq = UINT32_MAX;      // 已应用的封面版本
static bool s_shown_playing;
static int s_shown_pct = -1;
static char s_shown_title[MUSIC_TEXT_MAX];
static char s_shown_artist[MUSIC_TEXT_MAX * 2 + 8];
static char s_shown_status[32];
static music_state_t s_snap;

// 界面自己的播放进度：每 250ms 用代理快照校准，中间按本地时钟外推，
// 这样进度条和歌词不会一秒一跳。
static uint32_t s_pos_ms;
static int64_t s_pos_sample_ms;
static bool s_playing;             // 界面认为的播放状态（按键后先本地翻转）
static int64_t s_toggle_until;     // 这个时刻之前不采信快照里的播放状态

static uint32_t display_position_ms(void);

// 歌词：整首 LRC + 解析结果 + 当前窗口起始行
static music_lyric_t s_lyric;
static char *s_lyric_text;
static music_lyric_line_t *s_lyric_lines;
static uint32_t s_lyric_seq = UINT32_MAX;
static int s_lyric_window = INT32_MIN;

static void fill_dsc(lv_image_dsc_t *dsc, const void *data, int w, int h,
                     lv_color_format_t cf) {
    memset(dsc, 0, sizeof(*dsc));
    dsc->header.magic = LV_IMAGE_HEADER_MAGIC;
    dsc->header.cf = cf;
    dsc->header.w = w;
    dsc->header.h = h;
    dsc->data = data;
    // RGB565A8 的排布是 rgb 面后跟 a 面，记录总大小便于排查
    dsc->data_size = (uint32_t)w * h * (cf == LV_COLOR_FORMAT_RGB565A8 ? 3 : 2);
}

static void *psram_alloc(size_t size) {
    return heap_caps_aligned_alloc(16, size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
}

// ── 黑胶盘：外圈槽纹 + 斜向光泽，一次绘制，永久静态 ───────────────
static void draw_vinyl(void) {
    canvas_t c = {s_vinyl_buf, (uint8_t *)(s_vinyl_buf + VINYL_D * VINYL_D), VINYL_D, VINYL_D};
    memset(c.a, 0, VINYL_D * VINYL_D);
    int r = VINYL_D / 2;
    for (int y = 0; y < VINYL_D; ++y) {
        for (int x = 0; x < VINYL_D; ++x) {
            int dx = x - r + 1, dy = y - r + 1;
            int d2 = dx * dx + dy * dy;
            if (d2 > (r - 1) * (r - 1)) continue;   // 圆外保持透明
            int dist = (int)(sqrtf((float)d2) + 0.5f);
            uint32_t col;
            if (dist > r - 2) {
                col = 0x3C3C46;                     // 外缘亮环
            } else {
                static const uint32_t bands[4] = {0x111116, 0x0A0A0E, 0x0E0E12, 0x08080B};
                col = bands[dist & 3];
                // 左上高光 / 右下暗面：给旋转的黑胶一点光泽立体感
                int cross = dx + dy;
                if (dist > 120 && cross * 10 < -9 * dist) col = 0x2E2E3A;
                else if (dist > 120 && cross * 10 > 9 * dist) col = 0x17171E;
            }
            c.rgb[y * VINYL_D + x] = to565(col);
            c.a[y * VINYL_D + x] = 255;
        }
    }
}

// ── 唱针：轴盘 + 臂杆 + 唱头，旋转轴心在 (14,14) ──────────────────
static void draw_arm(void) {
    canvas_t c = {s_arm_buf, (uint8_t *)(s_arm_buf + ARM_W * ARM_H), ARM_W, ARM_H};
    memset(c.a, 0, ARM_W * ARM_H);
    canvas_vbar(&c, 10, 9, 14, 190, 0xBFBFCA, 255);     // 臂杆
    canvas_vbar(&c, 16, 3, 14, 190, 0x8F8F9C, 255);     // 臂杆暗侧
    canvas_rect(&c, 4, 186, 46, 238, 0x26262E, 255, 6); // 唱头
    canvas_vbar(&c, 8, 3, 238, 246, 0xE9E9EF, 255);     // 针尖
    canvas_disc(&c, 14, 14, 13, 0xC9C9D4, 255);         // 轴盘
    canvas_disc(&c, 14, 14, 6, 0x8A8A98, 255);          // 轴心
}

// ── 圆形遮罩：把 240x240 的方形封面裁成正圆 ──────────────────────
// 封面的 A 面（RGB565A8 的后半段）就是遮罩：圆外全透明、边缘留 1px 渐变，
// 这样旋转时边缘没有锯齿，也不会露出方形的四个角。
static void build_cover_mask(uint8_t *alpha) {
    const float rr = (float)(COVER_D / 2) - 0.5f;
    const float mid = (float)(COVER_D - 1) / 2.0f;
    for (int y = 0; y < COVER_D; ++y) {
        for (int x = 0; x < COVER_D; ++x) {
            float dx = (float)x - mid, dy = (float)y - mid;
            float d = sqrtf(dx * dx + dy * dy);
            float a = (rr - d + 0.5f) * 255.0f;   // 边缘 1px 线性过渡
            if (a < 0.0f) a = 0.0f;
            if (a > 255.0f) a = 255.0f;
            alpha[y * COVER_D + x] = (uint8_t)a;
        }
    }
}

// ── 无封面时的占位标签：唱片内圈纸标的感觉 ───────────────────────
static void draw_cover_placeholder(void) {
    int r = COVER_D / 2;
    for (int y = 0; y < COVER_D; ++y) {
        for (int x = 0; x < COVER_D; ++x) {
            int dx = x - r, dy = y - r;
            int dist = (int)sqrtf((float)(dx * dx + dy * dy));
            uint32_t col = 0x1D1D26;
            if (dist % 24 < 2) col = 0x272731;          // 同心槽纹
            if (dist < 78) col = 0x2A2A36;              // 标签区
            if (dist >= 74 && dist < 78) col = 0x35353F;
            if (dist < 10) col = COL_ACCENT;            // 中心红点
            s_cover_buf[y * COVER_D + x] = to565(col);
        }
    }
}

static void draw_glyphs(void) {
    canvas_t prev = {s_glyph_prev, (uint8_t *)(s_glyph_prev + GLYPH_NAV_W * GLYPH_NAV_H),
                     GLYPH_NAV_W, GLYPH_NAV_H};
    canvas_t next = {s_glyph_next, (uint8_t *)(s_glyph_next + GLYPH_NAV_W * GLYPH_NAV_H),
                     GLYPH_NAV_W, GLYPH_NAV_H};
    canvas_t play = {s_glyph_play, (uint8_t *)(s_glyph_play + GLYPH_PLAY_W * GLYPH_PLAY_H),
                     GLYPH_PLAY_W, GLYPH_PLAY_H};
    canvas_t pause = {s_glyph_pause, (uint8_t *)(s_glyph_pause + GLYPH_PLAY_W * GLYPH_PLAY_H),
                      GLYPH_PLAY_W, GLYPH_PLAY_H};
    memset(prev.a, 0, GLYPH_NAV_W * GLYPH_NAV_H);
    memset(next.a, 0, GLYPH_NAV_W * GLYPH_NAV_H);
    memset(play.a, 0, GLYPH_PLAY_W * GLYPH_PLAY_H);
    memset(pause.a, 0, GLYPH_PLAY_W * GLYPH_PLAY_H);
    // 上一首/下一首：双三角，比按钮小一圈
    canvas_tri(&prev, 0, 16, 0, GLYPH_NAV_H, false, COL_GLYPH);
    canvas_tri(&prev, 24, 40, 0, GLYPH_NAV_H, false, COL_GLYPH);
    canvas_tri(&next, 0, 16, 0, GLYPH_NAV_H, true, COL_GLYPH);
    canvas_tri(&next, 24, 40, 0, GLYPH_NAV_H, true, COL_GLYPH);
    // 播放：实心三角；暂停：两根竖条
    canvas_tri(&play, 3, 31, 0, GLYPH_PLAY_H, true, COL_GLYPH);
    canvas_vbar(&pause, 4, 10, 0, GLYPH_PLAY_H, COL_GLYPH, 255);
    canvas_vbar(&pause, 20, 10, 0, GLYPH_PLAY_H, COL_GLYPH, 255);
}

// ── 模糊背景：96x34 小图双线性放大铺满，只在换歌时执行一次 ────────
static void build_blur_canvas(void) {
    const void *small;
    if (!music_remote_get_blur(&small)) return;
    if (!s_blur_buf) {
        s_blur_buf = psram_alloc(SCREEN_W * SCREEN_H * 2);
        if (!s_blur_buf) {
            ESP_LOGE(TAG, "模糊背景画布分配失败");
            return;
        }
        fill_dsc(&s_blur_dsc, s_blur_buf, SCREEN_W, SCREEN_H, LV_COLOR_FORMAT_RGB565);
        lv_image_set_src(s_blur_img, &s_blur_dsc);
        lv_obj_remove_flag(s_blur_img, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(s_veil, LV_OBJ_FLAG_HIDDEN);
    }
    const uint16_t *src = small;
    const int sw = MUSIC_BLUR_W, sh = MUSIC_BLUR_H;
    for (int y = 0; y < SCREEN_H; ++y) {
        float fy = (y + 0.5f) * sh / SCREEN_H - 0.5f;
        int y0 = (int)fy; float ty = fy - y0;
        if (y0 < 0) { y0 = 0; ty = 0; }
        if (y0 >= sh - 1) { y0 = sh - 2; ty = 1; }
        for (int x = 0; x < SCREEN_W; ++x) {
            float fx = (x + 0.5f) * sw / SCREEN_W - 0.5f;
            int x0 = (int)fx; float tx = fx - x0;
            if (x0 < 0) { x0 = 0; tx = 0; }
            if (x0 >= sw - 1) { x0 = sw - 2; tx = 1; }
            uint16_t p00 = src[y0 * sw + x0], p10 = src[y0 * sw + x0 + 1];
            uint16_t p01 = src[(y0 + 1) * sw + x0], p11 = src[(y0 + 1) * sw + x0 + 1];
            // RGB565 分量插值：拆 r/g/b 各 5/6/5 位按权重混合
            uint32_t out = 0;
            const int shifts[] = {11, 5, 0};
            const int bits[] = {5, 6, 5};
            for (int ch = 0; ch < 3; ++ch) {
                int s = shifts[ch], b = bits[ch];
                int m = (1 << b) - 1;
                float c00 = ((p00 >> s) & m), c10 = ((p10 >> s) & m);
                float c01 = ((p01 >> s) & m), c11 = ((p11 >> s) & m);
                float top = c00 + (c10 - c00) * tx;
                float bot = c01 + (c11 - c01) * tx;
                float val = top + (bot - top) * ty;
                out |= (uint32_t)(val + 0.5f) << s;
            }
            s_blur_buf[y * SCREEN_W + x] = (uint16_t)out;
        }
    }
    lv_obj_invalidate(s_blur_img);
}

// ── 歌词窗口 ─────────────────────────────────────────────────────

// 重建 5 个标签：窗口起始行以后的行依次填进第 0..4 行，当前句固定落在第 2 行。
// 只换文字和颜色，不改变行间距，配合逐帧位移就是连续的上下滚动。
// 双语歌词在 LRC 里用 0x1F 分隔原文与译文，这里换成换行，一行占两行文字。
static char s_lyric_row_text[256];

static void rebuild_lyric_window(int start) {
    lv_color_t active = lv_color_hex(COL_TEXT);
    lv_color_t other = lv_color_hex(COL_LYRIC_NEXT);
    for (int j = 0; j < LYRIC_LABELS; ++j) {
        int idx = start + j;
        const char *text = NULL;
        if (idx >= 0 && idx < s_lyric.count) text = music_lyric_text_at(&s_lyric, idx);
        if (text == NULL || text[0] == '\0') text = " ";   // 空行也占位，保证行高稳定
        size_t n = 0;
        bool two_lines = false;
        for (const char *p = text; *p && n + 1 < sizeof(s_lyric_row_text); ++p) {
            if (*p == '\x1f') {         // 原文 / 译文分隔符
                s_lyric_row_text[n++] = '\n';
                two_lines = true;
            } else {
                s_lyric_row_text[n++] = *p;
            }
        }
        s_lyric_row_text[n] = '\0';
        lv_label_set_text(s_lyric_rows[j], s_lyric_row_text);
        // 单行歌词在 60px 行里垂直居中；双语两行正好填满
        int pad = two_lines ? 0 : (LYRIC_ROW - lv_font_get_line_height(
                                              lv_obj_get_style_text_font(s_lyric_rows[j], 0))) / 2;
        lv_obj_set_style_pad_top(s_lyric_rows[j], pad > 0 ? pad : 0, 0);
        lv_obj_set_style_text_color(s_lyric_rows[j],
                                    j == LYRIC_ACTIVE_ROW ? active : other, 0);
    }
}

// 按当前进度算窗口起始行和行内像素偏移，然后整体上移。
static void lyric_follow_position(uint32_t position_ms) {
    if (s_lyric.count <= 0) return;
    int cur = music_lyric_index_at(&s_lyric, position_ms);
    if (cur < 0) cur = 0;                     // 前奏阶段先高亮第一句
    uint32_t t0 = music_lyric_time_at(&s_lyric, cur);
    uint32_t t1 = cur + 1 < s_lyric.count ? music_lyric_time_at(&s_lyric, cur + 1) : 0;
    int offset = 0;
    if (t1 > t0) {
        uint32_t span = t1 - t0;
        uint32_t passed = position_ms > t0 ? position_ms - t0 : 0;
        if (passed > span) passed = span;
        offset = (int)((uint64_t)passed * LYRIC_ROW / span);
        if (offset >= LYRIC_ROW) offset = LYRIC_ROW - 1;
    }
    int start = cur - LYRIC_ACTIVE_ROW;
    if (start != s_lyric_window) {
        s_lyric_window = start;
        rebuild_lyric_window(start);
    }
    for (int j = 0; j < LYRIC_LABELS; ++j) {
        int y = j * LYRIC_ROW - offset;
        if (lv_obj_get_y(s_lyric_rows[j]) != y) lv_obj_set_y(s_lyric_rows[j], y);
    }
}

// 从板端服务拷一份最新歌词并重新解析；没有歌词就显示占位提示。
// 换歌时先清掉上一首的解析结果，避免新歌开头还挂着旧歌词。
static void reload_lyrics(bool track_changed) {
    if (track_changed) {
        music_lyric_clear(&s_lyric);
        s_lyric_window = INT32_MIN;
    }
    if (music_remote_copy_lyrics(s_lyric_text, MUSIC_LYRIC_MAX_BYTES, &s_lyric_seq)) {
        int count = music_lyric_parse(&s_lyric, s_lyric_text);
        s_lyric_window = INT32_MIN;           // 强制重建窗口
        ESP_LOGI(TAG, "歌词已更新：%d 行", count);
    }
    bool has_lyric = s_lyric.count > 0;
    if (has_lyric) {
        lv_obj_add_flag(s_lyric_empty, LV_OBJ_FLAG_HIDDEN);
        for (int j = 0; j < LYRIC_LABELS; ++j) {
            lv_obj_remove_flag(s_lyric_rows[j], LV_OBJ_FLAG_HIDDEN);
        }
    } else {
        for (int j = 0; j < LYRIC_LABELS; ++j) {
            lv_obj_add_flag(s_lyric_rows[j], LV_OBJ_FLAG_HIDDEN);
        }
        lv_obj_remove_flag(s_lyric_empty, LV_OBJ_FLAG_HIDDEN);
    }
}

// ── 交互 ─────────────────────────────────────────────────────────

static int32_t s_arm_deg10 = ARM_DEG_REST;   // 唱针当前角度（逻辑值，可为负）

static void arm_exec_cb(void *var, int32_t value) {
    s_arm_deg10 = value;
    // lv_image_set_rotation 内部会把角度归一化到 0..3600，负值也能正常显示
    lv_image_set_rotation((lv_obj_t *)var, value);
}

// 播放：唱针落到唱片上（-8°）；暂停：唱针抬离唱片（+12°）
static void arm_anim_to(bool playing) {
    if (!s_arm_img) return;
    int32_t target = playing ? ARM_DEG_PLAY : ARM_DEG_REST;
    if (s_arm_deg10 == target) return;
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, s_arm_img);
    lv_anim_set_exec_cb(&a, arm_exec_cb);
    lv_anim_set_values(&a, s_arm_deg10, target);
    lv_anim_set_duration(&a, 380);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
    lv_anim_start(&a);
}

static void apply_playing_ui(bool playing) {
    if (s_shown_playing == playing) return;
    s_shown_playing = playing;
    if (s_play_icon) {
        if (playing) lv_obj_add_flag(s_play_icon, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_remove_flag(s_play_icon, LV_OBJ_FLAG_HIDDEN);
    }
    if (s_pause_icon) {
        if (playing) lv_obj_remove_flag(s_pause_icon, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(s_pause_icon, LV_OBJ_FLAG_HIDDEN);
    }
    arm_anim_to(playing);
}

static void on_cmd_click(lv_event_t *e) {
    music_cmd_t cmd = (music_cmd_t)(intptr_t)lv_event_get_user_data(e);
    // 先做本地反馈再发指令：代理离线时按钮也立刻有反应，不会像按在石头上
    if (cmd == MUSIC_CMD_PLAY_PAUSE) {
        s_pos_ms = display_position_ms();     // 先按旧状态算出切换瞬间的进度
        s_pos_sample_ms = lv_tick_get();
        s_playing = !s_playing;
        s_toggle_until = (int64_t)lv_tick_get() + 1500;
        apply_playing_ui(s_playing);
    }
    music_remote_send_cmd(cmd);
    ESP_LOGI(TAG, "按钮点击：%s",
             cmd == MUSIC_CMD_NEXT ? "下一首" : cmd == MUSIC_CMD_PREV ? "上一首" : "播放/暂停");
}

// 键盘/唱针之外只有上面三个圆按钮控制播放，不再做"左右滑切歌"手势。
// 反复排查的结论：EVENT 模式的触摸驱动在"这一帧没有触点"时会沿用上一次的坐标
// （TouchTask 里 point_num==0 直接 continue），下一次按下就可能和旧坐标连成一次
// 大位移，被判成手势 → 用户"点一下暂停"却切了歌。手势收益低、风险高，去掉。


// ── 定时器 ───────────────────────────────────────────────────────

// 界面自己的播放进度：播放中按本地时钟外推
static uint32_t display_position_ms(void) {
    if (!s_playing || s_snap.duration_ms == 0) return s_pos_ms;
    int64_t elapsed = (int64_t)lv_tick_get() - s_pos_sample_ms;
    if (elapsed < 0) elapsed = 0;
    uint32_t pos = s_pos_ms + (uint32_t)elapsed;
    return pos > s_snap.duration_ms ? s_snap.duration_ms : pos;
}

static void anim_tick(lv_timer_t *timer) {
    (void)timer;
    if (!s_visible) return;
    if (s_playing && s_cover_img) {
        s_angle = (s_angle + SPIN_STEP_DEG10) % 36000;
        lv_image_set_rotation(s_cover_img, s_angle);
    }
    // 歌词每 40ms 跟一次进度，行内连续上移，看起来才是滚动的
    lyric_follow_position(display_position_ms());
}

static void ui_tick(lv_timer_t *timer) {
    (void)timer;
    music_state_t snap;
    music_remote_get_snapshot(&snap);
    bool track_changed = strcmp(snap.title, s_snap.title) != 0;
    s_snap = snap;

    if (track_changed) {
        s_angle = 0;
        if (s_cover_img) lv_image_set_rotation(s_cover_img, 0);
    }
    // 播放状态：刚按过按钮的 1.5 秒内不采信快照（指令还在路上，快照必然还是旧值，
    // 否则图标会先翻过去再闪回来）
    if ((int64_t)lv_tick_get() >= s_toggle_until && snap.playing != s_playing) {
        s_playing = snap.playing;
        s_pos_ms = snap.position_ms;
        s_pos_sample_ms = lv_tick_get();
        apply_playing_ui(s_playing);
    }

    // 播放中不逐帧对齐快照（否则一秒一跳），偏差超过 1.2 秒才重新校准
    int64_t drift = (int64_t)snap.position_ms - (int64_t)display_position_ms();
    if (!s_playing || track_changed || drift > 1200 || drift < -1200) {
        s_pos_ms = snap.position_ms;
        s_pos_sample_ms = lv_tick_get();
    }

    if (strcmp(snap.title, s_shown_title) != 0) {
        snprintf(s_shown_title, sizeof(s_shown_title), "%s", snap.title);
        lv_label_set_text(s_title, snap.title[0] ? snap.title : "未在播放");
    }
    char artist_line[MUSIC_TEXT_MAX * 2 + 8];
    if (snap.artist[0] && snap.album[0]) {
        snprintf(artist_line, sizeof(artist_line), "%s · %s", snap.artist, snap.album);
    } else {
        snprintf(artist_line, sizeof(artist_line), "%s", snap.artist);
    }
    if (strcmp(artist_line, s_shown_artist) != 0) {
        snprintf(s_shown_artist, sizeof(s_shown_artist), "%s", artist_line);
        lv_label_set_text(s_artist, artist_line);
    }
    // 状态只放右上角一个小胶囊，把中间的空间全部让给歌词
    const char *status = snap.demo ? "演示模式" :
                         !snap.agent_found ? "寻找中…" : "已连接";
    if (strcmp(status, s_shown_status) != 0) {
        snprintf(s_shown_status, sizeof(s_shown_status), "%s", status);
        lv_label_set_text(s_status, status);
        lv_obj_set_style_bg_color(s_status_chip,
                                  lv_color_hex(snap.agent_found && !snap.demo
                                                   ? COL_BTN : 0x3A2A2A), 0);
    }

    uint32_t pos = display_position_ms();
    int pct = music_state_progress_percent(pos, snap.duration_ms);
    if (pct != s_shown_pct) {
        s_shown_pct = pct;
        lv_bar_set_value(s_bar, pct, LV_ANIM_OFF);
    }
    char tbuf[12];
    music_state_format_time(pos, tbuf, sizeof(tbuf));
    if (strcmp(lv_label_get_text(s_time_cur), tbuf) != 0) lv_label_set_text(s_time_cur, tbuf);
    music_state_format_time(snap.duration_ms, tbuf, sizeof(tbuf));
    if (strcmp(lv_label_get_text(s_time_dur), tbuf) != 0) lv_label_set_text(s_time_dur, tbuf);

    // 每拍都问一次歌词（内部按版本号判断，没有变化时只是加锁比一下）
    reload_lyrics(track_changed);

    const void *art_px;
    uint32_t seq;
    if (music_remote_get_art(&art_px, &seq) && seq != s_art_seq) {
        s_art_seq = seq;
        if (s_cover_img && art_px) {
            // 只覆盖 rgb 面，A 面（圆形遮罩）保持不变 → 方图变圆图
            memcpy(s_cover_buf, art_px, (size_t)COVER_D * COVER_D * 2);
            lv_obj_invalidate(s_cover_img);
        }
        build_blur_canvas();
        ESP_LOGI(TAG, "封面已更新：版本 %" PRIu32 "，曲目《%s》", seq, snap.title);
    }
}

// ── 页面构建 ─────────────────────────────────────────────────────

static const char *cmd_name(music_cmd_t cmd) {
    return cmd == MUSIC_CMD_NEXT ? "下一首" : cmd == MUSIC_CMD_PREV ? "上一首" : "播放/暂停";
}

// 按下即打日志：把"手指落点"和"按钮实际区域"一起打出来，方便核对触摸映射
// 有没有偏移（此前用户反映点暂停却切歌，怀疑是落点偏到了隔壁按钮）。
static void on_btn_pressed(lv_event_t *e) {
    music_cmd_t cmd = (music_cmd_t)(intptr_t)lv_event_get_user_data(e);
    lv_obj_t *btn = lv_event_get_target_obj(e);
    lv_area_t a;
    lv_obj_get_coords(btn, &a);
    lv_point_t p = {0, 0};
    lv_indev_t *indev = lv_indev_active();
    if (indev != NULL) lv_indev_get_point(indev, &p);
    ESP_LOGI(TAG, "按钮按下：%s 触摸(%d,%d) 区域(%d,%d)-(%d,%d)", cmd_name(cmd),
             (int)p.x, (int)p.y, (int)a.x1, (int)a.y1, (int)a.x2, (int)a.y2);
}

static lv_obj_t *circle_button(lv_obj_t *parent, int size, int x, int y,
                               music_cmd_t cmd) {
    lv_obj_t *btn = lv_button_create(parent);
    lv_obj_remove_style_all(btn);
    lv_obj_set_size(btn, size, size);
    lv_obj_set_pos(btn, x, y);
    lv_obj_set_style_radius(btn, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(btn, lv_color_hex(COL_BTN), 0);
    lv_obj_set_style_bg_opa(btn, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(btn, 1, 0);
    lv_obj_set_style_border_color(btn, lv_color_hex(COL_BTN_BORDER), 0);
    lv_obj_set_style_bg_color(btn, lv_color_hex(COL_BTN_PRESSED), LV_STATE_PRESSED);
    // 统一用 LV_EVENT_CLICKED：项目其它页面（时钟/菜单）都用它，实机触摸可靠；
    // 原来用的 SHORT_CLICKED 在 EVENT 模式的触摸驱动下经常收不到。
    lv_obj_add_event_cb(btn, on_cmd_click, LV_EVENT_CLICKED, (void *)(intptr_t)cmd);
    lv_obj_add_event_cb(btn, on_btn_pressed, LV_EVENT_PRESSED, (void *)(intptr_t)cmd);
    return btn;
}

static lv_obj_t *lyric_row(lv_obj_t *parent, int index, int pad_top) {
    lv_obj_t *label = lv_label_create(parent);
    lv_obj_set_pos(label, 0, index * LYRIC_ROW);
    lv_obj_set_size(label, PANEL_W, LYRIC_ROW);
    lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(COL_LYRIC_NEXT), 0);
    lv_obj_set_style_pad_top(label, pad_top, 0);
    // 行高 60 正好放两行文字（原文 + 译文），所以每个段落只占一行、超长用省略号，
    // 不做折行——折行会挤成三行被容器裁掉。
    lv_label_set_long_mode(label, LV_LABEL_LONG_DOT);
    lv_label_set_text(label, " ");
    return label;
}

void music_page_create(lv_obj_t *screen) {
    // 缓冲只分配一次；页面创建后与宠物页一样常驻
    if (!s_vinyl_buf) {
        s_vinyl_buf = psram_alloc(VINYL_D * VINYL_D * 3);
        s_arm_buf = psram_alloc(ARM_W * ARM_H * 3);
        s_cover_buf = psram_alloc(COVER_D * COVER_D * 3);
        s_glyph_prev = psram_alloc(GLYPH_NAV_W * GLYPH_NAV_H * 3);
        s_glyph_next = psram_alloc(GLYPH_NAV_W * GLYPH_NAV_H * 3);
        s_glyph_play = psram_alloc(GLYPH_PLAY_W * GLYPH_PLAY_H * 3);
        s_glyph_pause = psram_alloc(GLYPH_PLAY_W * GLYPH_PLAY_H * 3);
        // 歌词缓冲：整首 LRC 文本 + 行表，放 PSRAM（文本最大 16KB）
        s_lyric_text = psram_alloc(MUSIC_LYRIC_MAX_BYTES);
        s_lyric_lines = psram_alloc(MUSIC_LYRIC_MAX_LINES * sizeof(music_lyric_line_t));
        if (!s_vinyl_buf || !s_arm_buf || !s_cover_buf || !s_glyph_prev ||
            !s_glyph_next || !s_glyph_play || !s_glyph_pause || !s_lyric_text ||
            !s_lyric_lines) {
            ESP_LOGE(TAG, "音乐页缓冲分配失败");
            return;
        }
        fill_dsc(&s_vinyl_dsc, s_vinyl_buf, VINYL_D, VINYL_D, LV_COLOR_FORMAT_RGB565A8);
        fill_dsc(&s_arm_dsc, s_arm_buf, ARM_W, ARM_H, LV_COLOR_FORMAT_RGB565A8);
        fill_dsc(&s_prev_dsc, s_glyph_prev, GLYPH_NAV_W, GLYPH_NAV_H, LV_COLOR_FORMAT_RGB565A8);
        fill_dsc(&s_next_dsc, s_glyph_next, GLYPH_NAV_W, GLYPH_NAV_H, LV_COLOR_FORMAT_RGB565A8);
        fill_dsc(&s_play_dsc, s_glyph_play, GLYPH_PLAY_W, GLYPH_PLAY_H, LV_COLOR_FORMAT_RGB565A8);
        fill_dsc(&s_pause_dsc, s_glyph_pause, GLYPH_PLAY_W, GLYPH_PLAY_H, LV_COLOR_FORMAT_RGB565A8);
        draw_vinyl();
        draw_arm();
        build_cover_mask((uint8_t *)(s_cover_buf + COVER_D * COVER_D));
        draw_cover_placeholder();
        draw_glyphs();
        // 封面用 RGB565A8：A 面是事先算好的圆形遮罩，方形图也能显示成圆形唱片标
        fill_dsc(&s_cover_dsc, s_cover_buf, COVER_D, COVER_D, LV_COLOR_FORMAT_RGB565A8);
        music_lyric_init(&s_lyric, s_lyric_text, MUSIC_LYRIC_MAX_BYTES,
                         s_lyric_lines, MUSIC_LYRIC_MAX_LINES);
    }
    if (s_ui_timer) return;   // 已构建过，防御重复创建

    lv_obj_set_style_bg_color(screen, lv_color_hex(COL_BG_TOP), 0);
    lv_obj_set_style_bg_grad_color(screen, lv_color_hex(COL_BG_BOT), 0);
    lv_obj_set_style_bg_grad_dir(screen, LV_GRAD_DIR_VER, 0);
    lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_text_font(screen, page_font(), 0);
    // 手势只挂在歌词区（见下）：挂在整屏或封面上时，手指按按钮时的一点点滑动
    // 会被 LVGL 判成左右滑 → 变成"点暂停却切歌"。现在按钮与封面都不带手势。

    // 背景模糊图与压暗遮罩（有封面素材后才显示）
    s_blur_img = lv_image_create(screen);
    lv_obj_set_pos(s_blur_img, 0, 0);
    lv_obj_add_flag(s_blur_img, LV_OBJ_FLAG_HIDDEN);
    s_veil = lv_obj_create(screen);
    lv_obj_remove_style_all(s_veil);
    lv_obj_set_size(s_veil, SCREEN_W, SCREEN_H);
    lv_obj_set_style_bg_color(s_veil, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(s_veil, LV_OPA_50, 0);
    lv_obj_remove_flag(s_veil, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_veil, LV_OBJ_FLAG_HIDDEN);

    // 黑胶组合：静态盘面 → 旋转封面 → 唱针
    s_vinyl_img = lv_image_create(screen);
    lv_image_set_src(s_vinyl_img, &s_vinyl_dsc);
    lv_obj_set_pos(s_vinyl_img, VINYL_CX - VINYL_D / 2, VINYL_CY - VINYL_D / 2);
    s_cover_img = lv_image_create(screen);
    lv_image_set_src(s_cover_img, &s_cover_dsc);
    lv_obj_set_pos(s_cover_img, VINYL_CX - COVER_D / 2, VINYL_CY - COVER_D / 2);
    lv_image_set_pivot(s_cover_img, COVER_D / 2, COVER_D / 2);
    lv_image_set_antialias(s_cover_img, true);
    lv_obj_add_flag(s_cover_img, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_cover_img, on_cmd_click, LV_EVENT_CLICKED,
                        (void *)(intptr_t)MUSIC_CMD_PLAY_PAUSE);
    s_arm_img = lv_image_create(screen);
    lv_image_set_src(s_arm_img, &s_arm_dsc);
    lv_obj_set_pos(s_arm_img, 96, 46);
    lv_image_set_pivot(s_arm_img, 14, 14);
    lv_image_set_antialias(s_arm_img, true);
    // 初始姿态：暂停态（唱针抬离唱片）。之前这里没设角度，唱针停在 0°，
    // 恰好贴在盘边，用户就看成"暂停时唱针在唱片上"。
    s_arm_deg10 = ARM_DEG_REST;
    lv_image_set_rotation(s_arm_img, ARM_DEG_REST);

    // 右上角状态胶囊：只占一个小角落，不再挤占歌词区的竖排空间
    s_status_chip = lv_obj_create(screen);
    lv_obj_remove_style_all(s_status_chip);
    lv_obj_set_layout(s_status_chip, LV_LAYOUT_FLEX);
    lv_obj_set_flex_flow(s_status_chip, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(s_status_chip, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER,
                          LV_FLEX_ALIGN_CENTER);
    lv_obj_set_size(s_status_chip, LV_SIZE_CONTENT, 32);
    lv_obj_set_style_pad_hor(s_status_chip, 14, 0);
    lv_obj_set_style_radius(s_status_chip, 16, 0);
    lv_obj_set_style_bg_color(s_status_chip, lv_color_hex(COL_BTN), 0);
    lv_obj_set_style_bg_opa(s_status_chip, LV_OPA_70, 0);
    lv_obj_align(s_status_chip, LV_ALIGN_TOP_RIGHT, -10, 8);
    s_status = lv_label_create(s_status_chip);
    lv_obj_set_style_text_color(s_status, lv_color_hex(COL_FAINT), 0);
    lv_label_set_text(s_status, "寻找中…");
    snprintf(s_shown_status, sizeof(s_shown_status), "%s", "寻找中…");

    // 标题与歌手：中文标题用 20px 全字库字体（font_dingtalk_40 只有 ASCII）
    s_title = lv_label_create(screen);
    lv_obj_set_pos(s_title, PANEL_X, 10);
    lv_obj_set_width(s_title, 580);
    lv_obj_set_style_text_color(s_title, lv_color_hex(COL_TEXT), 0);
    lv_label_set_long_mode(s_title, LV_LABEL_LONG_SCROLL_CIRCULAR);
    lv_obj_set_style_anim_duration(s_title, 8000, 0);
    lv_label_set_text(s_title, "未在播放");

    s_artist = lv_label_create(screen);
    lv_obj_set_pos(s_artist, PANEL_X, 48);
    lv_obj_set_width(s_artist, 580);
    lv_obj_set_style_text_color(s_artist, lv_color_hex(COL_SUB), 0);
    lv_label_set_long_mode(s_artist, LV_LABEL_LONG_DOT);
    lv_label_set_text(s_artist, " ");

    // 歌词窗口：一屏 5 行，当前句高亮，按进度上下滚动
    s_lyric_cont = lv_obj_create(screen);
    lv_obj_remove_style_all(s_lyric_cont);
    lv_obj_set_pos(s_lyric_cont, PANEL_X, LYRIC_TOP);
    lv_obj_set_size(s_lyric_cont, PANEL_W, LYRIC_VISIBLE * LYRIC_ROW);
    lv_obj_remove_flag(s_lyric_cont, LV_OBJ_FLAG_SCROLLABLE);
    // 不加上内边距：一行折成两行时 2×24 正好填满 48 的行高，加了反而被裁掉
    for (int j = 0; j < LYRIC_LABELS; ++j) {
        s_lyric_rows[j] = lyric_row(s_lyric_cont, j, 0);
        lv_obj_add_flag(s_lyric_rows[j], LV_OBJ_FLAG_HIDDEN);
    }
    s_lyric_empty = lv_label_create(s_lyric_cont);
    lv_obj_set_size(s_lyric_empty, PANEL_W, LYRIC_VISIBLE * LYRIC_ROW);
    lv_obj_set_style_text_align(s_lyric_empty, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(s_lyric_empty, lv_color_hex(COL_FAINT), 0);
    lv_obj_set_style_pad_top(s_lyric_empty,
                             (LYRIC_VISIBLE * LYRIC_ROW - lv_font_get_line_height(
                                  page_font())) / 2, 0);
    lv_label_set_text(s_lyric_empty, "暂无歌词");
    lv_obj_add_flag(s_lyric_empty, LV_OBJ_FLAG_HIDDEN);

    // 进度条与时间
    s_bar = lv_bar_create(screen);
    lv_obj_set_size(s_bar, PANEL_W, 6);
    lv_obj_set_pos(s_bar, PANEL_X, 334);
    lv_obj_set_style_radius(s_bar, 3, 0);
    lv_obj_set_style_bg_color(s_bar, lv_color_hex(COL_BAR_TRACK), 0);
    lv_obj_set_style_bg_opa(s_bar, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(s_bar, lv_color_hex(COL_ACCENT), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(s_bar, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_bar_set_range(s_bar, 0, 100);
    lv_bar_set_value(s_bar, 0, LV_ANIM_OFF);

    s_time_cur = lv_label_create(screen);
    lv_obj_set_pos(s_time_cur, PANEL_X, 344);
    lv_obj_set_style_text_font(s_time_cur, page_font(), 0);
    lv_obj_set_style_text_color(s_time_cur, lv_color_hex(COL_SUB), 0);
    lv_label_set_text(s_time_cur, "00:00");
    s_time_dur = lv_label_create(screen);
    lv_obj_align(s_time_dur, LV_ALIGN_TOP_RIGHT, -28, 344);
    lv_obj_set_style_text_font(s_time_dur, page_font(), 0);
    lv_obj_set_style_text_color(s_time_dur, lv_color_hex(COL_SUB), 0);
    lv_label_set_text(s_time_dur, "00:00");

    // 控制按钮行：右侧列底部居中。三个圆之间留 40px 空隙，避免手指落在缝里
    // 被邻居按钮接走（用户反映"点暂停却切歌"）。
    const int center_x = PANEL_X + PANEL_W / 2;   // 872
    const int play_x = center_x - 30;             // 60px 播放键，x=842..902
    const int prev_x = center_x - 126;            // 56px 上一首，x=746..802
    const int next_x = center_x + 70;             // 56px 下一首，x=942..998
    lv_obj_t *prev = circle_button(screen, 56, prev_x, 376, MUSIC_CMD_PREV);
    lv_obj_t *next = circle_button(screen, 56, next_x, 376, MUSIC_CMD_NEXT);
    s_btn_play = circle_button(screen, 60, play_x, 374, MUSIC_CMD_PLAY_PAUSE);
    s_play_icon = lv_image_create(s_btn_play);
    lv_image_set_src(s_play_icon, &s_play_dsc);
    lv_obj_center(s_play_icon);
    s_pause_icon = lv_image_create(s_btn_play);
    lv_image_set_src(s_pause_icon, &s_pause_dsc);
    lv_obj_center(s_pause_icon);
    lv_obj_add_flag(s_pause_icon, LV_OBJ_FLAG_HIDDEN);
    lv_obj_t *prev_icon = lv_image_create(prev);
    lv_image_set_src(prev_icon, &s_prev_dsc);
    lv_obj_center(prev_icon);
    lv_obj_t *next_icon = lv_image_create(next);
    lv_image_set_src(next_icon, &s_next_dsc);
    lv_obj_center(next_icon);

    s_anim_timer = lv_timer_create(anim_tick, SPIN_TICK_MS, NULL);
    s_ui_timer = lv_timer_create(ui_tick, 250, NULL);
    lv_timer_pause(s_anim_timer);
    lv_timer_pause(s_ui_timer);
    // 打一份按钮几何，方便核对"看到的按钮"与"实际点击区域"是否一致
    // （这里用创建时给的坐标，lv_obj_get_coords 在首次排版前还拿不到值）
    ESP_LOGI(TAG, "按钮几何：上一首(%d,%d,56) 播放(%d,%d,60) 下一首(%d,%d,56)",
             prev_x, 376, play_x, 374, next_x, 376);
    ESP_LOGI(TAG, "音乐页已创建：程序化黑胶 + 旋转封面 + 唱针 + 滚动歌词");
}

void music_page_set_visible(bool visible) {
    s_visible = visible;
    if (visible) {
        // 重新进页面时强制重取歌词与进度基准，避免沿用上次的窗口位置
        s_lyric_seq = UINT32_MAX;
        s_pos_sample_ms = lv_tick_get();
        music_remote_start();
        if (s_anim_timer) lv_timer_resume(s_anim_timer);
        if (s_ui_timer) lv_timer_resume(s_ui_timer);
    } else {
        if (s_anim_timer) lv_timer_pause(s_anim_timer);
        if (s_ui_timer) lv_timer_pause(s_ui_timer);
        music_remote_stop();
    }
}
