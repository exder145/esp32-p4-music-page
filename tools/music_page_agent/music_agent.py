# -*- coding: utf-8 -*-
"""网易云音乐远控代理：把电脑上正在播放的歌暴露给 AIBox 音乐页（局域网
HTTP），并转发板子发来的播放控制指令。

用法：
    pip install -r requirements.txt
    python music_agent.py            # 默认 HTTP 端口 8777
    python music_agent.py --port 9000

行为：
  - 每 2 秒向 255.255.255.255:9878 广播 "MUSICAGENT|<本机IP>|<端口>"，
    板子收到后按 1 秒间隔来拉状态。
  - GET /state            播放状态 JSON（标题/歌手/专辑/进度/封面哈希/当前歌词行）
  - GET /lyrics?h=<哈希>  整首 LRC 文本（板端自己按进度滚动显示）
  - GET /art?h=<哈希>     240x240 JPEG 封面（Pillow 缩放自网易云专辑图）
  - GET /art_blur?h=<哈希> 96x34 RGB565 背景小图（板端放大后当毛玻璃背景）
  - POST /cmd             {"action": "play_pause" | "next" | "prev"}

曲目信息的来源是网易云主窗口标题（"歌名 - 歌手"），不是 SMTC：实测网易云
3.x 的 SMTC 会话只更新播放/暂停状态，媒体属性（歌名/歌手）和切歌指令都是
冻结的，这也是之前"屏幕永远停在一首歌、电脑切歌不同步"的原因。切歌指令走
系统媒体键（WM_APPCOMMAND），和键盘上的多媒体键同一条链路；播放/暂停则要
"真点一下网易云界面底部的播放键"——SMTC 的 play/toggle 和合成媒体键都会把
歌换成随机一首（详见 click_player_button 上方的说明）。

网易云不报播放进度（timeline 的 position 永远是 None），所以进度由代理的
本地时钟推算：切歌归零、暂停冻结、播放累加；在电脑上拖动进度条无法感知
（曾用截图 + OCR 读播放条来同步，太麻烦且不稳，已按要求整条移除）。

封面、歌词都按"标题+歌手+时长"到网易云匹配原曲后异步准备，/state 不等它们；
板端随后用 art_hash 来取，取不到时本代理会回退到最近一次准备的曲目。

注意：首次运行需允许 Windows 防火墙放行 UDP 9878 与 TCP 端口。
"""
import argparse
import asyncio
import datetime
import hashlib
import io
import re
import traceback
import socket
import struct
import sys
import time
import unicodedata

from aiohttp import web

try:
    import aiohttp as _aiohttp
except ImportError:   # 只在真正联网时才需要
    _aiohttp = None

try:
    import winrt.windows.media.control as wmc
    import winrt.windows.storage.streams as wss
    WINRT_OK = True
except ImportError:
    WINRT_OK = False   # 只在真正启动服务时才报错，导入本模块做单测不依赖 WinRT

try:
    from PIL import Image, ImageFilter
except ImportError:
    Image = None

BROADCAST_PORT = 9878
BROADCAST_INTERVAL = 2.0
ART_SIZE = 240
BLUR_SIZE = (96, 34)
LYRIC_MAX_CHARS = 80
EXTRAS_RETRY_SECONDS = 30.0   # 匹配失败后的重试间隔（失败结果不再永久缓存）
CLOCK_WRAP_GRACE = 3.0        # 本地时钟超过时长多久才认定"又从头播了一遍"
STATE_LOG_EVERY = 10          # 每 N 次 /state 打一行日志，便于排查"界面停在一首歌"

# 运行期统计（只用来节流日志）
_STATS = {"state": 0}
EXTRAS_LIMIT = 6              # 曲目缓存条数
SEARCH_LIMIT = 10             # 搜索结果取前 10 条，再按标题/歌手/时长挑原曲
SEARCH_TIMEOUT = 6.0

# 优先网易云的会话；找不到时回退到任意 SMTC 会话
NETEASE_KEYS = ("netease", "cloudmusic", "163")

LYRIC_TAG = re.compile(r"\[(\d+):(\d+)(?:[.:](\d+))?\]")
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "http://music.163.com/"}
CREDIT_PREFIX = ("作词", "作曲", "编曲", "制作", "填词", "谱曲", "混音", "母带",
                 "lyric", "compos", "arrang", "produc", "guitar", "bass", "drum",
                 "piano", "violin", "written", "混缩", "监制", "出品", "统筹")


def parse_lrc(text: str):
    """把 LRC 文本解析为 [(时间ms, 歌词)] 升序列表；无内容返回 None。"""
    lines = []
    for raw in (text or "").splitlines():
        tags = LYRIC_TAG.findall(raw)
        if not tags:
            continue
        content = LYRIC_TAG.sub("", raw).strip()
        if not content or content.lower().startswith(CREDIT_PREFIX):
            continue
        for minutes, seconds, frac in tags:
            ms = (int(minutes) * 60 + int(seconds)) * 1000 + \
                 int((frac or "0").ljust(3, "0")[:3])
            lines.append((ms, content))
    lines.sort()
    return lines or None


def lines_to_lrc(lines) -> str:
    """把歌词行重新拼成标准 LRC（毫秒精度），交给板端解析显示。"""
    if not lines:
        return ""
    out = []
    for ms, text in lines:
        minutes, rest = divmod(int(ms), 60_000)
        seconds, millis = divmod(rest, 1000)
        out.append("[%02d:%02d.%02d]%s" % (minutes, seconds, millis // 10, text))
    return "\n".join(out) + "\n"


TRANSLATION_SEPARATOR = "\x1f"      # LRC 里分隔"原文"和"译文"，板端换成换行


def is_chinese_dominant(lines) -> bool:
    """歌词是不是中文为主（中文歌不需要再挂中文译文）。"""
    cjk = latin = 0
    for _ms, text in lines or []:
        for ch in text:
            if "\u4e00" <= ch <= "\u9fff":
                cjk += 1
            elif ch.isascii() and ch.isalpha():
                latin += 1
    return cjk > 0 and cjk >= latin


def merge_bilingual(original, translated):
    """把中文译文并进原文：按时间戳就近对齐，拼成 "原文\\x1f译文"。

    中文歌（原文已经是中文）不拼；没有译文原样返回；译文和原文一样也不拼。
    """
    if not original or not translated:
        return original
    if is_chinese_dominant(original):
        return original
    merged = []
    j = 0
    for ms, text in original:
        # 两边都按时间升序，译文时间戳和原文不一定完全一样，往后找最接近的一条
        while (j + 1 < len(translated)
               and abs(translated[j + 1][0] - ms) < abs(translated[j][0] - ms)):
            j += 1
        trans = ""
        if abs(translated[j][0] - ms) <= 600:
            trans = (translated[j][1] or "").strip()
        if trans and trans != (text or "").strip():
            merged.append((ms, f"{text}{TRANSLATION_SEPARATOR}{trans}"))
        else:
            merged.append((ms, text))
    return merged


def pick_lyric_line(lines, position_ms: float) -> str:
    """取进度之前最近的一句歌词；进度还在第一句之前时返回空串。"""
    current = ""
    for t, text in lines or []:
        if t <= position_ms:
            current = text
        else:
            break
    return current[:LYRIC_MAX_CHARS]


def _norm(text: str) -> str:
    """标题/歌手归一化：全半角统一、去掉 (Live)/(feat. xx) 之类的括号缀、去空白标点。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    stripped = re.sub(r"[（(\[【].*?[）)\]】]", "", text).strip()
    if stripped:
        text = stripped
    return re.sub(r"[\s\-_·'\".,!?，。！？、/()（）]+", "", text)


def pick_original(songs, title: str, artist: str, duration: float = 0):
    """从搜索结果里挑"原曲"：标题越接近、歌手越对得上、时长越接近得分越高。

    只按 limit=1 取第一条会经常拿到翻唱（实测搜"晴天 周杰伦"第一条是别人的
    翻唱），这里改成对前 10 条打分。歌手是完全一致的强信号（翻唱署名不同），
    所以权重最高；标题一致次之；时长用来在同名同歌手的多个版本（Live、重制）
    里挑最接近当前播放时长的那条。
    """
    want_title = _norm(title)
    want_artist = _norm(artist)
    best, best_score = None, 0.0
    for song in songs:
        got_title = _norm(song.get("name") or "")
        if not got_title or not want_title:
            continue
        if got_title == want_title:
            score = 6.0
        elif want_title in got_title or got_title in want_title:
            score = 3.0
        else:
            continue
        artists = song.get("artists") or song.get("ar") or []
        names = [_norm(a.get("name") or "") for a in artists if isinstance(a, dict)]
        if want_artist and names:
            if want_artist in names:
                score += 8.0
            elif any(want_artist in n or n in want_artist for n in names if n):
                score += 3.0
        elif want_artist and not names:
            score -= 1.0
        song_duration = song.get("duration") or song.get("dt") or 0
        if duration and song_duration:
            diff = abs(float(song_duration) / 1000.0 - float(duration))
            if diff <= 2:
                score += 4.0
            elif diff <= 5:
                score += 2.0
            elif diff <= 10:
                score += 1.0
            elif diff > 15:
                score -= 3.0
        if score > best_score:
            best, best_score = song, score
    return best


def prop(obj, name):
    """跨版本兼容读取 WinRT 属性：新版 winrt-* 投影用 snake_case，
    个别版本把属性投成方法，这里统一做属性/方法双兼容。"""
    value = getattr(obj, name, None)
    if value is None:
        value = getattr(obj, _pascal(name), None)
    if callable(value):
        value = value()
    return value


def _pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _epoch_seconds(value) -> float:
    """把 WinRT 的 DateTime / TimeSpan 之类的对象转成 epoch 秒；不认识返回 0。"""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.datetime):
        return value.timestamp()
    stamp = getattr(value, "timestamp", None)
    if stamp is not None:
        try:
            return float(stamp() if callable(stamp) else stamp)
        except (TypeError, ValueError):
            pass
    for name in ("to_datetime", "to_seconds", "total_seconds"):
        method = getattr(value, name, None)
        if not callable(method):
            continue
        try:
            result = method()
        except Exception:
            continue
        if isinstance(result, datetime.datetime):
            return result.timestamp()
        if isinstance(result, (int, float)):
            return float(result)
    return 0.0


def extrapolate_position(position: float, last_updated, playing: bool) -> float:
    """SMTC 的 position 只在状态变化时刷新，播放中可能长时间停在 0；
    用 timeline.last_updated_time 外推，界面进度和歌词才跟得上。"""
    position = float(position or 0)
    if not playing or position <= 0:
        return position
    stamp = _epoch_seconds(last_updated)
    if stamp <= 0:
        return position
    delta = time.time() - stamp
    if 0 < delta < 3600:      # 时间戳离谱（时钟不同步）就不外推
        position += delta
    return position


def playback_is_playing(playback) -> bool:
    """判断播放/暂停。

    winrt 的 playback_status 是枚举，str() 出来是 "4" 这种数字（不是
    "PLAYING"），旧代码用字符串包含判断，结果永远判成暂停。
    """
    status = prop(playback, "playback_status")
    try:
        return int(status) == 4          # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
    except (TypeError, ValueError):
        return "PLAYING" in str(status)


def timeline_usable(timeline) -> bool:
    """播放器是否真的在维护时间线。

    网易云不填：position/end_time 恒为 0，last_updated_time 停在 1601 年。
    只有时间戳正常且有时长的才可信；否则位置只能自己攒。
    """
    if timeline is None:
        return False
    stamp = prop(timeline, "last_updated_time")
    end_time = prop(timeline, "end_time")
    if stamp is None or getattr(stamp, "year", 0) <= 2000:
        return False
    try:
        return end_time is not None and end_time.total_seconds() > 1
    except (TypeError, ValueError):
        return False


def session_app_id(session) -> str:
    return prop(session, "source_app_user_model_id") or ""


def app_display_name(session) -> str:
    sid = session_app_id(session).lower()
    if any(k in sid for k in NETEASE_KEYS):
        return "网易云音乐"
    tail = sid.split("!")[0]
    return tail.split(".")[-1] or tail or "未知应用"


def pick_session(sessions):
    for s in sessions:
        sid = session_app_id(s).lower()
        if any(k in sid for k in NETEASE_KEYS):
            return s
    return sessions[0] if sessions else None


# ── 网易云真实曲目（读窗口标题） ─────────────────────────────────
#
# 为什么不用 SMTC 的媒体属性：实测网易云 3.x 的 SMTC 会话是"冻结"的——
# get_playback_info().playback_status 会跟着暂停/播放变，但
# try_get_media_properties_async() 里的 title/artist 一直停在很久以前那首歌
# （板子因此永远显示同一首），try_skip_next/previous_async 也发不出去
# （返回 True 但歌不变）。唯一随播放实时更新的东西是主窗口标题
# "歌名 - 歌手1/歌手2"，这也是任务栏上看到的文字。所以曲目信息读窗口标题，
# 控制指令改用系统媒体键（WM_APPCOMMAND），实测真的能切歌——和键盘上的
# 播放/上一首/下一首键走同一条链路。
WM_APPCOMMAND = 0x0319
# 播放/暂停用"明确的动词"而不是 toggle：网易云的 toggle 媒体键在暂停后再点
# 偶尔只把窗口唤起、音乐并没有真的开始（用户看到"卡一下然后没播放"），
# 再点一次才动、还容易被当成切歌。所以这里同时准备 play(46)/pause(47)，
# 由当前播放状态决定发哪个，发完校验状态，不对再换通道重试。
APPCOMMAND_MEDIA = {"next": 11, "prev": 12, "play_pause": 14,
                    "play": 46, "pause": 47}
CLOUDMUSIC_EXE = "cloudmusic.exe"
# 窗口标题不是"歌名 - 歌手"的情况（浏览歌单/设置页、未播放等）
TITLE_BLOCKLIST = ("网易云音乐", "桌面歌词", "迷你播放器", "播放器", "云盘",
                   "我的音乐", "发现音乐", "私人漫游", "每日推荐", "歌单", "排行榜",
                   "搜索", "设置", "登录")

# ctypes 只在 Windows 上可用；其它平台导入本模块做单测时不报错
if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as _wt

    _user32 = ctypes.windll.user32
    _user32.GetWindowTextW.argtypes = [_wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetWindowTextLengthW.argtypes = [_wt.HWND]
    _user32.GetWindowThreadProcessId.argtypes = [_wt.HWND, ctypes.POINTER(_wt.DWORD)]
    _user32.PostMessageW.argtypes = [_wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM]
    _k32 = ctypes.windll.kernel32
    _k32.OpenProcess.restype = _wt.HANDLE
    _k32.QueryFullProcessImageNameW.argtypes = [_wt.HANDLE, _wt.DWORD,
                                                ctypes.c_wchar_p,
                                                ctypes.POINTER(_wt.DWORD)]
    _EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, _wt.HWND, _wt.LPARAM)
else:                                        # pragma: no cover - 非 Windows
    ctypes = None
    _wt = None
    _user32 = _k32 = None
    _EnumProc = None

_PROC_NAME_CACHE = {}     # pid -> (进程名小写, 记录时间)


def _process_name(pid: int) -> str:
    """按 pid 取可执行文件名（缓存 30 秒，避免每秒都开一次进程句柄）。"""
    now = time.monotonic()
    cached = _PROC_NAME_CACHE.get(pid)
    if cached and now - cached[1] < 30.0:
        return cached[0]
    name = ""
    handle = _k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = _wt.DWORD(260)
            if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = buf.value.replace("/", "\\").split("\\")[-1].lower()
        finally:
            _k32.CloseHandle(handle)
    _PROC_NAME_CACHE[pid] = (name, now)
    return name


def cloudmusic_window():
    """返回 (可见窗口句柄或 None, 主窗口标题)；找不到网易云返回 None。

    网易云会为同一进程开好几个窗口，其中一个 1x1 的隐藏窗口标题同样是
    "歌名 - 歌手"，所以哪怕主窗口最小化到托盘也能读到曲目。"""
    if _user32 is None:
        return None
    found = []

    def visit(hwnd, _):
        try:
            if _user32.GetWindowTextLengthW(hwnd) <= 0:
                return True
            pid = _wt.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if _process_name(pid.value) != CLOUDMUSIC_EXE:
                return True
            length = _user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            found.append((hwnd, bool(_user32.IsWindowVisible(hwnd)), buf.value))
        except Exception:
            pass
        return True
    try:
        _user32.EnumWindows(_EnumProc(visit), 0)
    except Exception:
        return None
    if not found:
        return None
    for hwnd, visible, title in found:
        if visible and parse_now_playing(title) is not None:
            return hwnd, title
    for hwnd, visible, title in found:            # 主窗口在托盘里时退而求其次
        if parse_now_playing(title) is not None:
            return hwnd, title
    return None


def parse_now_playing(title: str):
    """把 "歌名 - 歌手1/歌手2" 拆成 (歌名, 歌手)；不像歌曲标题时返回 None。"""
    if not title:
        return None
    title = title.strip()
    if title in TITLE_BLOCKLIST:
        return None
    if " - " not in title:
        return None
    song, _, artist = title.partition(" - ")
    song, artist = song.strip(), artist.strip()
    if not song or not artist:
        return None
    if song in TITLE_BLOCKLIST or artist in TITLE_BLOCKLIST:
        return None
    return song, artist


# ── 播放/暂停：真点一下网易云自己的播放键 ────────────────────────
#
# 实测（2026-10-24，网易云 3.x，暂停在 112 秒处）：
#   · 暂停：SMTC try_pause_async 正常，曲目不变；
#   · 恢复播放：SMTC try_play_async、try_toggle_play_pause_async 以及合成媒体键
#     （WM_APPCOMMAND 14 / 46）都会跳到随机一首歌——这就是用户说的"点播放变成
#     切歌"（随机播放开着时尤其明显，注意它连 toggle 都跳）；
#   · 用 SendInput 真点界面底部那颗圆形播放键，原曲从暂停处继续播，和用户手动
#     点一模一样。
# 所以 play_pause 改成"真实点击"，只有点不到窗口（最小化/找不到）才退回 SMTC。
PLAYER_BTN_BOTTOM = 43     # 播放键圆心到客户区底边的像素距离（1168x830 实测 787）
PLAYER_BTN_MIN_W = 600     # 客户区太小时不敢点，怕点在别的按钮上
PLAYER_BTN_MIN_H = 300
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

if sys.platform == "win32":
    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", _wt.LONG), ("dy", _wt.LONG), ("mouseData", _wt.DWORD),
                    ("dwFlags", _wt.DWORD), ("time", _wt.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(_wt.ULONG))]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("padding", ctypes.c_ubyte * 24)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", _wt.DWORD), ("u", _INPUTUNION)]
else:                                            # pragma: no cover - 非 Windows
    _MOUSEINPUT = _INPUTUNION = _INPUT = None


def click_client_point(hwnd, x: int, y: int) -> bool:
    """在窗口客户区 (x, y) 真点一下左键（会抢焦点，点完把鼠标放回原处）。"""
    if _user32 is None:
        return False
    try:
        if _user32.IsIconic(hwnd) or not _user32.IsWindowVisible(hwnd):
            return False
        origin = _wt.POINT()
        if not _user32.ClientToScreen(hwnd, ctypes.byref(origin)):
            return False
        saved = _wt.POINT()
        _user32.GetCursorPos(ctypes.byref(saved))
        _user32.SetCursorPos(origin.x + x, origin.y + y)
        events = (_INPUT * 2)()
        for i, flag in enumerate((MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)):
            events[i].type = 0                       # INPUT_MOUSE
            events[i].u.mi.dwFlags = flag
        sent = _user32.SendInput(2, events, ctypes.sizeof(_INPUT))
        _user32.SetCursorPos(saved.x, saved.y)       # 别把用户的鼠标留在窗口里
        return bool(sent)
    except Exception:
        return False


def click_player_button() -> bool:
    """点网易云界面底部的圆形播放/暂停键；窗口不可点时返回 False。"""
    window = cloudmusic_window()
    if window is None:
        return False
    rect = _wt.RECT()
    if not _user32.GetClientRect(window[0], ctypes.byref(rect)):
        return False
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width < PLAYER_BTN_MIN_W or height < PLAYER_BTN_MIN_H:
        return False
    return click_client_point(window[0], width // 2, height - PLAYER_BTN_BOTTOM)


def send_media_key(action: str) -> bool:
    """给网易云窗口发系统媒体键；成功投递返回 True。

    网易云把媒体键做成了全局快捷键，PostMessage 到它任意一个窗口都能生效
    （实测：发 next 后窗口标题里的歌名立刻变了）。"""
    if _user32 is None or action not in APPCOMMAND_MEDIA:
        return False
    window = cloudmusic_window()
    if window is None:
        return False
    hwnd, _title = window
    app_cmd = APPCOMMAND_MEDIA[action]
    try:
        return bool(_user32.PostMessageW(hwnd, WM_APPCOMMAND, 0, app_cmd << 16))
    except Exception:
        return False


async def read_thumbnail_bytes(media_props) -> bytes:
    stream = await prop(media_props, "thumbnail").open_read_async()
    try:
        size = prop(stream, "size") or 0
        if not size:
            return b""
        reader = wss.DataReader(stream)
        try:
            await reader.load_async(size)
            return bytes(reader.read_bytes(size))
        finally:
            reader.detach_stream()
    finally:
        stream.close()


def fit_square(image, size: int):
    """居中裁成方块再缩放，避免封面变形。"""
    w, h = image.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    return image.crop((left, top, left + side, top + side)).resize((size, size))


def jpeg_240(raw: bytes) -> bytes:
    image = Image.open(io.BytesIO(raw))
    if image.mode != "RGB":
        image = image.convert("RGB")
    out = io.BytesIO()
    fit_square(image, ART_SIZE).save(out, "JPEG", quality=82)
    return out.getvalue()


def blur_rgb565(raw: bytes) -> bytes:
    """96x34 小图 + 轻微高斯模糊；板端放大即得毛玻璃背景。"""
    image = Image.open(io.BytesIO(raw))
    if image.mode != "RGB":
        image = image.convert("RGB")
    small = fit_square(image, BLUR_SIZE[0]).resize(BLUR_SIZE)
    small = small.filter(ImageFilter.GaussianBlur(1.2))
    pixels = small.load()
    buf = bytearray()
    for y in range(BLUR_SIZE[1]):
        for x in range(BLUR_SIZE[0]):
            r, g, b = pixels[x, y][:3]
            buf += struct.pack("<H", (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3))
    return bytes(buf)


class Agent:
    def __init__(self, port: int):
        self.port = port
        # 哈希 -> {"ok", "lines", "lrc", "art", "blur", "fetched_at"}
        self.track_extras = {}
        self._inflight = {}       # 哈希 -> asyncio.Task，避免同一首歌并发抓取
        self.latest_hash = ""     # 最近一次 /state 报告的曲目，供 /art 无参回退
        # 本地播放时钟：播放器不报进度时靠它推算（见 _sync_clock）
        self._clock = {"hash": "", "pos": 0.0, "since": time.monotonic(),
                       "playing": False}
        self._last_window_track = None   # 网易云窗口标题最近一次解析出的 (歌名, 歌手)

    # ── 曲目信息（歌词 + 专辑图）准备 ────────────────────────────

    async def fetch_track_extras(self, title: str, artist: str, duration: float):
        """按"歌名+歌手+时长"搜网易云：返回 (歌词行列表, 专辑图 URL, 时长毫秒)。

        歌词行取自官方 LRC；专辑图在搜索结果里常为空，用 song/detail 补。
        时长用来补 SMTC 不报时长的播放器（网易云就是），只作兜底。
        搜不到或网络失败返回 (None, None, None)。"""
        # 多歌手时用第一个歌手搜更准（"歌名 甲/乙/丙" 搜索结果常为空）
        first_artist = re.split(r"[/、,，&]", artist)[0].strip() or artist
        query = f"{title} {first_artist}".strip()
        try:
            async with _aiohttp.ClientSession(headers=HTTP_HEADERS) as http:
                async with http.get(
                        "http://music.163.com/api/search/get/web",
                        params={"s": query, "type": 1, "offset": 0,
                                "limit": SEARCH_LIMIT},
                        timeout=_aiohttp.ClientTimeout(total=SEARCH_TIMEOUT)) as resp:
                    data = await resp.json(content_type=None)
                songs = (data.get("result") or {}).get("songs") or []
                song = pick_original(songs, title, artist, duration)
                if song is None:
                    print(f"[music_agent] 未匹配到原曲：{query}")
                    return None, None, None
                song_id = song.get("id")
                pic_url = (song.get("album") or {}).get("picUrl") or ""
                if song_id and not pic_url:
                    async with http.get(
                            "http://music.163.com/api/song/detail",
                            params={"id": song_id, "ids": f"[{song_id}]"},
                            timeout=_aiohttp.ClientTimeout(total=SEARCH_TIMEOUT)) as resp:
                        detail = await resp.json(content_type=None)
                    detail_songs = detail.get("songs") or []
                    if detail_songs:
                        pic_url = (detail_songs[0].get("album") or {}).get("picUrl") or ""
                async with http.get(
                        "http://music.163.com/api/song/lyric",
                        params={"id": song_id, "lv": 1, "kv": 1, "tv": -1},
                        timeout=_aiohttp.ClientTimeout(total=SEARCH_TIMEOUT)) as resp:
                    lyric_data = await resp.json(content_type=None)
            song_duration = song.get("duration") or song.get("dt") or 0
            orig_lines = parse_lrc((lyric_data.get("lrc") or {}).get("lyric") or "")
            trans_lines = parse_lrc((lyric_data.get("tlyric") or {}).get("lyric") or "")
            lines = merge_bilingual(orig_lines, trans_lines)
            bilingual = sum(1 for _ms, t in lines if TRANSLATION_SEPARATOR in t)
            print(f"[music_agent] 已匹配《{song.get('name')}》"
                  f"（id={song_id}）用于《{title}》"
                  f"，歌词 {len(lines)} 行" + (f"（双语 {bilingual} 行）" if bilingual else ""))
            return lines, pic_url or None, int(song_duration) or None
        except Exception as exc:
            print(f"[music_agent] 歌词/专辑图获取失败：{exc}")
            return None, None, None

    async def _download(self, url: str):
        try:
            async with _aiohttp.ClientSession(headers=HTTP_HEADERS) as http:
                async with http.get(
                        url, timeout=_aiohttp.ClientTimeout(total=SEARCH_TIMEOUT)) as resp:
                    if resp.status == 200:
                        return await resp.read()
        except Exception as exc:
            print(f"[music_agent] 下载失败 {url}：{exc}")
        return None

    def _start_fetch(self, key: str, title: str, artist: str, duration: float):
        """后台准备歌词与封面：/state 不等它，板端随后用 art_hash 来取。"""
        if key in self._inflight:
            return

        async def run():
            try:
                lines, pic_url, song_duration = await self.fetch_track_extras(
                    title, artist, duration)
                art = blur = b""
                if pic_url:
                    raw = await self._download(pic_url)
                    if raw and Image is not None:
                        try:
                            art = jpeg_240(raw)
                            blur = blur_rgb565(raw)
                        except Exception as exc:
                            print(f"[music_agent] 封面处理失败：{exc}")
                    elif raw:
                        print("[music_agent] 已取得封面数据但未安装 Pillow，无法转码")
                self.track_extras[key] = {
                    "ok": bool(lines), "lines": lines, "lrc": lines_to_lrc(lines),
                    "art": art, "blur": blur, "duration_ms": song_duration,
                    "fetched_at": time.time(),
                }
                while len(self.track_extras) > EXTRAS_LIMIT:
                    self.track_extras.pop(next(iter(self.track_extras)))
            finally:
                self._inflight.pop(key, None)

        self._inflight[key] = asyncio.create_task(run())

    def _extras(self, key: str):
        entry = self.track_extras.get(key)
        if entry is not None and not entry["ok"] and \
                time.time() - entry["fetched_at"] > EXTRAS_RETRY_SECONDS:
            # 失败过又过了重试间隔：当作没准备过，下一轮重新匹配
            self.track_extras.pop(key, None)
            return None
        return entry

    async def _wait_extras(self, key: str, timeout: float):
        """等后台准备完成（板端请求封面/歌词时通常已经好了，最多等一小会儿）。"""
        task = self._inflight.get(key) if key else None
        if task is None:
            return self._extras(key) if key else None
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception as exc:
            print(f"[music_agent] 曲目信息准备失败：{exc}")
        return self._extras(key)

    # ── SMTC 读取 ────────────────────────────────────────────────

    def _sync_clock(self, key: str, real_pos, playing: bool, duration):
        """推算当前播放位置（秒）。

        播放器报位置时直接用它的；网易云永远不报（timeline 的 position 是
        None），就用"开始播放的时刻"起一个本地时钟累加：暂停时冻结、切歌时
        归零、重新播放时从冻结位置继续。这样歌词和进度条至少一直往前走，
        并且换歌时从 0 开始。已知限制：在电脑上拖动进度条不会被感知。"""
        now = time.monotonic()
        clock = self._clock
        if clock["hash"] != key:
            clock.update(hash=key, pos=0.0, since=now, playing=False)
        if real_pos is not None:
            clock.update(pos=max(0.0, float(real_pos)), since=now, playing=playing)
        elif playing and not clock["playing"]:
            clock.update(since=now, playing=True)        # 暂停 → 播放：接着走
        elif not playing and clock["playing"]:
            clock["pos"] += now - clock["since"]         # 播放 → 暂停：结算已播时长
            clock.update(since=now, playing=False)
        pos = clock["pos"] + (now - clock["since"] if playing else 0.0)
        pos = max(0.0, pos)
        if duration and duration > 0.5:
            if playing and pos > duration + CLOCK_WRAP_GRACE:
                # 单曲循环：同一首播完时 hash 不变、网易云也不报时间线，只能靠
                # 本地时钟溢出判断"又从头开始了"。留 3 秒宽限，给自动切歌换标题
                # 留出时间；确实停在末尾时状态会变成暂停，不会走到这里。
                pos = (pos - duration) % duration
                clock.update(pos=pos, since=now)
            else:
                pos = min(pos, duration - 0.5)
        return pos

    async def current_state(self) -> dict:
        manager = await wmc.GlobalSystemMediaTransportControlsSessionManager.request_async()
        sessions = list(manager.get_sessions())
        session = pick_session(sessions)
        state = {"connected": True, "track": False, "playing": False,
                 "position": 0, "duration": 0, "title": "", "artist": "",
                 "album": "", "app": "", "art_hash": "", "lyric": ""}

        # 播放/暂停状态：SMTC 的 playback_status 是实时的（只有媒体属性是冻的）
        if session is not None:
            state["app"] = app_display_name(session)
            state["playing"] = playback_is_playing(session.get_playback_info())

        # 曲目：优先读网易云窗口标题（实时），SMTC 媒体属性只当兜底
        window = cloudmusic_window()
        track = parse_now_playing(window[1]) if window is not None else None
        if track is None and window is not None:
            # 标题临时变成"网易云音乐/歌单名"这类页面名时，沿用上一首，
            # 免得界面在用户翻歌单的瞬间跳回 SMTC 里那首几个月前的歌
            track = self._last_window_track
        if track is not None:
            state["title"], state["artist"] = track
            state["app"] = "网易云音乐"
            if session is None:
                state["playing"] = True              # 有歌没会话：当成在放
            # 网易云不填 SMTC 时间线，进度全部由本地时钟推算（见 _sync_clock）。
            real_pos = real_dur = None
        elif session is not None:
            props = await session.try_get_media_properties_async()
            state["title"] = prop(props, "title") or ""
            state["artist"] = prop(props, "artist") or ""
            state["album"] = prop(props, "album_title") or ""
            timeline = session.get_timeline_properties()
            usable = timeline_usable(timeline)
            position = prop(timeline, "position")
            real_pos = position.total_seconds() if (usable and position is not None) else None
            real_dur = prop(timeline, "end_time").total_seconds() if usable else None
        else:
            state["app"] = "无播放会话"
            return state

        state["track"] = bool(state["title"])
        if not state["track"]:
            return state
        if track is not None:
            self._last_window_track = track

        key = hashlib.md5("|".join((state["title"], state["artist"],
                                    state["album"])).encode("utf-8")).hexdigest()
        state["art_hash"] = key
        self.latest_hash = key
        if self._extras(key) is None:
            self._start_fetch(key, state["title"], state["artist"], real_dur or 0)
        entry = self._extras(key) or {}
        if not real_dur and entry.get("duration_ms"):
            real_dur = entry["duration_ms"] / 1000.0     # 网易云不报时长，用网易云的
        if real_dur:
            state["duration"] = real_dur
        state["position"] = self._sync_clock(key, real_pos, state["playing"], real_dur)
        # 单行歌词给旧固件用；双语歌词的 0x1F 分隔符在这里换成 " / "
        state["lyric"] = pick_lyric_line(entry.get("lines"),
                                        state["position"] * 1000).replace(
                                            TRANSLATION_SEPARATOR, " / ")
        return state

    # ── 传输控制 ─────────────────────────────────────────────────
    #
    # 一条指令按"通道 × 校验"执行：发出去后看结果（播放状态 / 窗口标题）
    # 有没有变，没变就换下一条通道重试。这样既能把网易云偶发的"收下指令
    # 但不动"兜住，也不会像 toggle 那样在状态不同步时越点越乱。

    async def _smtc_session(self):
        try:
            manager = await wmc.GlobalSystemMediaTransportControlsSessionManager.request_async()
            return pick_session(list(manager.get_sessions()))
        except Exception as exc:
            print(f"[music_agent] 取 SMTC 会话失败：{exc}")
            return None

    async def _smtc_playing(self):
        """当前是否在播放；拿不到返回 None（用于"无法校验"）。"""
        session = await self._smtc_session()
        if session is None:
            return None
        try:
            return playback_is_playing(session.get_playback_info())
        except Exception:
            return None

    async def _smtc_send(self, action: str) -> bool:
        session = await self._smtc_session()
        if session is None:
            return False
        try:
            if action == "next":
                await session.try_skip_next_async()
            elif action == "prev":
                await session.try_skip_previous_async()
            elif action == "play":
                await session.try_play_async()
            elif action == "pause":
                await session.try_pause_async()
            else:
                await session.try_toggle_play_pause_async()
            return True
        except Exception as exc:
            print(f"[music_agent] SMTC 执行 {action} 失败：{exc}")
            return False

    async def _verify_playback(self, expected: bool, timeout: float = 1.5) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            playing = await self._smtc_playing()
            if playing is None:
                return True          # 读不到状态就没法校验，别重复发指令
            if playing == expected:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.15)

    async def _verify_track(self, before: str, timeout: float = 1.8) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            window = cloudmusic_window()
            title = window[1] if window else ""
            if title and title != before:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def apply_cmd(self, action: str):
        """执行一条控制指令，返回 (是否成功, 通道名, 实际动作, 耗时毫秒)。"""
        if action == "play_pause":
            return await self._apply_play_pause()

        # 切歌：媒体键是可靠的（网易云的 SMTC 跳过是坏的，返回 True 但歌不动）
        window = cloudmusic_window()
        before_title = window[1] if window else ""
        started = time.monotonic()
        if send_media_key(action) and await self._verify_track(before_title):
            return True, "mediakey", action, int((time.monotonic() - started) * 1000)
        if await self._smtc_send(action) and await self._verify_track(before_title):
            return True, "smtc", action, int((time.monotonic() - started) * 1000)
        return False, "none", action, 0

    async def _apply_play_pause(self):
        """播放/暂停：先真点网易云自己的播放键，点不到才退回 SMTC。

        为什么不用 SMTC/媒体键恢复播放：实测它们会把歌换成随机一首（见
        click_player_button 上方注释），用户看到的就是"点播放变成切歌"。
        暂停这一侧 SMTC 是安全的，所以点不到窗口时暂停仍可正常完成。
        """
        started = time.monotonic()
        playing = await self._smtc_playing()
        if playing is None:
            action, expected = "toggle", None       # 状态未知：点一下就换状态
        else:
            expected = not playing
            action = "play" if expected else "pause"

        # 1) 真点界面按钮：唯一能"原曲续播"的通道
        if click_player_button():
            if expected is not None:
                ok = await self._verify_playback(expected)
            else:                                   # 状态未知：等状态翻转即可
                before = await self._smtc_playing()
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    now = await self._smtc_playing()
                    if now is not None and now != before:
                        break
                    await asyncio.sleep(0.15)
                ok = True
            if ok:
                return True, "click", action, int((time.monotonic() - started) * 1000)
            print("[music_agent] 点击播放键后状态没变，换通道重试")

        # 2) 兜底：暂停用 SMTC（安全）；恢复播放只能用 SMTC，可能换歌
        if expected is False:
            if await self._smtc_send("pause") and await self._verify_playback(False):
                return True, "smtc", "pause", int((time.monotonic() - started) * 1000)
        elif expected is True:
            print("[music_agent] 注意：点不到网易云播放键（窗口最小化？），"
                  "改用 SMTC 恢复播放，可能会换歌")
            if await self._smtc_send("play") and await self._verify_playback(True):
                return True, "smtc", "play", int((time.monotonic() - started) * 1000)
        else:
            if await self._smtc_send("toggle") and await self._verify_playback(
                    not (await self._smtc_playing())):
                return True, "smtc", "toggle", int((time.monotonic() - started) * 1000)
        return False, "none", action, 0

    # ── HTTP 处理 ────────────────────────────────────────────────

    async def handle_state(self, _request):
        # 板端每秒轮询一次；出错必须留下日志，否则表现为"界面一直停在上一首"
        try:
            state = await self.current_state()
        except Exception as exc:
            print(f"[music_agent] 生成播放状态失败：{exc!r}")
            traceback.print_exc()
            return web.json_response({"connected": False, "error": str(exc)})
        _STATS["state"] += 1
        if _STATS["state"] % STATE_LOG_EVERY == 0:
            print(f"[music_agent] /state 第 {_STATS['state']} 次："
                  f"《{state['title']}》{state['position']:.1f}s/{state['duration']:.1f}s"
                  f"{'（播放中）' if state['playing'] else '（暂停）'}")
        return web.json_response(state)

    def _resolve_key(self, request) -> str:
        """优先用板端带来的哈希；代理重启过、哈希不认识时回退到当前曲目。"""
        key = request.query.get("h", "")
        if key and (key in self.track_extras or key in self._inflight):
            return key
        return self.latest_hash

    async def handle_lyrics(self, request):
        """整首 LRC：板端拿到后自己按进度上下滚动，不再依赖逐行推送。"""
        key = self._resolve_key(request)
        entry = await self._wait_extras(key, 2.0)
        if not entry or not entry.get("lrc"):
            return web.Response(status=404, text="no lyrics")
        return web.Response(text=entry["lrc"], content_type="text/plain",
                            charset="utf-8", headers={"Cache-Control": "no-store"})

    async def handle_art(self, request):
        key = self._resolve_key(request)
        entry = await self._wait_extras(key, 1.5)
        if not entry or not entry.get("art"):
            return web.Response(status=404, text="no art")
        return web.Response(body=entry["art"], content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    async def handle_blur(self, request):
        key = self._resolve_key(request)
        entry = await self._wait_extras(key, 1.5)
        if not entry or not entry.get("blur"):
            return web.Response(status=404, text="no blur")
        return web.Response(body=entry["blur"], content_type="application/octet-stream",
                            headers={"Cache-Control": "no-store"})

    async def handle_cmd(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = (body or {}).get("action", "")
        if action not in ("next", "prev", "play_pause"):
            return web.json_response({"ok": False, "error": "bad action"}, status=400)
        ok, via, real, cost = await self.apply_cmd(action)
        if not ok:
            print(f"[music_agent] 控制指令未生效：{action}")
            return web.json_response({"ok": False, "error": "no effect",
                                      "action": action}, status=409)
        names = {"play": "播放", "pause": "暂停", "next": "下一首", "prev": "上一首",
                 "toggle": "切换播放"}
        print(f"[music_agent] {names.get(real, real)} 已生效（{via}，{cost}ms）")
        return web.json_response({"ok": True, "via": via, "action": real})

    async def broadcast_loop(self):
        """向全网段广播存在性；板子靠它免配置发现本机。"""
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            local_ip = probe.getsockname()[0]
        except OSError:
            local_ip = "127.0.0.1"
        finally:
            probe.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        payload = f"MUSICAGENT|{local_ip}|{self.port}".encode("ascii")
        print(f"[music_agent] 广播存在性：{payload.decode()}（UDP {BROADCAST_PORT}）")
        while True:
            try:
                sock.sendto(payload, ("255.255.255.255", BROADCAST_PORT))
            except OSError as exc:
                print(f"[music_agent] 广播失败：{exc}")
            await asyncio.sleep(BROADCAST_INTERVAL)


async def main_async(port: int):
    if Image is None:
        print("[music_agent] 未安装 Pillow，封面/背景功能将不可用（pip install pillow）")
    agent = Agent(port)
    app = web.Application()
    app.router.add_get("/state", agent.handle_state)
    app.router.add_get("/lyrics", agent.handle_lyrics)
    app.router.add_get("/art", agent.handle_art)
    app.router.add_get("/art_blur", agent.handle_blur)
    app.router.add_post("/cmd", agent.handle_cmd)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[music_agent] HTTP 服务已启动：0.0.0.0:{port}")
    await agent.broadcast_loop()


def main():
    parser = argparse.ArgumentParser(description="AIBox 网易云远控代理")
    parser.add_argument("--port", type=int, default=8777, help="HTTP 服务端口（默认 8777）")
    if not WINRT_OK:
        sys.exit("缺少 WinRT 依赖，请先执行: pip install -r requirements.txt")
    try:
        asyncio.run(main_async(parser.parse_args().port))
    except KeyboardInterrupt:
        print("[music_agent] 已退出")


if __name__ == "__main__":
    main()
