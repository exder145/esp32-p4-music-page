# music_page 组件：网易云风格黑胶音乐页（板端 UI + 远控服务）
# 接入方式见仓库根 README。UI 全程序化绘制，无图片素材依赖。

# Wi-Fi 头文件来源：P4（无射频，走 esp_wifi_remote shim）与其余带射频芯片不同
if(CONFIG_IDF_TARGET_ESP32P4)
    set(MUSIC_WIFI_REQUIRES esp_wifi_remote)
else()
    set(MUSIC_WIFI_REQUIRES esp_wifi)
endif()

idf_component_register(
    SRCS "music_page.c"
         "music_remote.c"
         "music_state.c"
         "music_lyric.c"
    INCLUDE_DIRS "include"
    REQUIRES lvgl
             json
             esp_http_client
             lwip
             esp_timer
             heap
             espressif__esp_new_jpeg
             ${MUSIC_WIFI_REQUIRES}
)
