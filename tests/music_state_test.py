"""编译 music_state.c，验证时间格式化、进度计算与演示模式推进逻辑。"""
import ctypes
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class MusicState(ctypes.Structure):
    _fields_ = [
        ("agent_found", ctypes.c_bool),
        ("track_loaded", ctypes.c_bool),
        ("playing", ctypes.c_bool),
        ("demo", ctypes.c_bool),
        ("art_ready", ctypes.c_bool),
        ("position_ms", ctypes.c_uint32),
        ("duration_ms", ctypes.c_uint32),
        ("art_seq", ctypes.c_uint32),
        ("title", ctypes.c_char * 96),
        ("artist", ctypes.c_char * 96),
        ("album", ctypes.c_char * 96),
        ("app", ctypes.c_char * 32),
        ("lyric", ctypes.c_char * 192),
    ]


class MusicStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.temp = tempfile.TemporaryDirectory()
        dll = Path(cls.temp.name) / "music_state.dll"
        compiler = shutil.which("gcc") or "gcc"
        subprocess.run(
            [compiler, "-shared", "-std=c11", "-I", str(root / "components/music_page/include"),
             str(root / "components/music_page/music_state.c"), "-o", str(dll)],
            check=True,
        )
        cls.lib = ctypes.CDLL(str(dll))
        cls.lib.music_state_format_time.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_int]
        cls.lib.music_state_progress_percent.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        cls.lib.music_state_progress_percent.restype = ctypes.c_int
        for name in ("music_state_demo_advance", "music_state_demo_toggle",
                     "music_state_demo_skip"):
            fn = getattr(cls.lib, name)
            if name == "music_state_demo_advance":
                fn.argtypes = [ctypes.POINTER(MusicState), ctypes.c_int]
            elif name == "music_state_demo_skip":
                fn.argtypes = [ctypes.POINTER(MusicState), ctypes.c_int]
            else:
                fn.argtypes = [ctypes.POINTER(MusicState)]
            fn.restype = None

    @classmethod
    def tearDownClass(cls):
        import _ctypes
        _ctypes.FreeLibrary(cls.lib._handle)
        del cls.lib
        cls.temp.cleanup()

    def test_format_time(self):
        buf = ctypes.create_string_buffer(12)
        self.lib.music_state_format_time(0, buf, 12)
        self.assertEqual(buf.value, b"00:00")
        self.lib.music_state_format_time(83_000, buf, 12)
        self.assertEqual(buf.value, b"01:23")
        self.lib.music_state_format_time(3_661_000, buf, 12)
        self.assertEqual(buf.value, b"61:01")   # 超过 99 分钟不进位到小时

    def test_progress_percent(self):
        self.assertEqual(self.lib.music_state_progress_percent(0, 0), 0)
        self.assertEqual(self.lib.music_state_progress_percent(5_000, 10_000), 50)
        self.assertEqual(self.lib.music_state_progress_percent(20_000, 10_000), 100)

    def test_demo_advance_respects_pause(self):
        state = MusicState()
        self.lib.music_state_demo_toggle(ctypes.byref(state))   # 初始为播放，切到暂停
        self.assertFalse(state.playing)
        self.lib.music_state_demo_advance(ctypes.byref(state), 5_000)
        self.assertEqual(state.position_ms, 0)
        self.assertTrue(state.demo)
        self.assertFalse(state.agent_found)
        self.lib.music_state_demo_toggle(ctypes.byref(state))   # 恢复播放
        self.lib.music_state_demo_advance(ctypes.byref(state), 2_500)
        self.assertEqual(state.position_ms, 2_500)

    def test_demo_skip_changes_track_and_wraps(self):
        state = MusicState()
        self.lib.music_state_demo_advance(ctypes.byref(state), 0)   # 先填充当前曲目
        base = state.title
        self.lib.music_state_demo_skip(ctypes.byref(state), 1)
        self.assertNotEqual(state.title, base)
        self.assertEqual(state.position_ms, 0)   # 切歌后进度清零
        self.lib.music_state_demo_skip(ctypes.byref(state), -1)
        self.assertEqual(state.title, base)
        self.lib.music_state_demo_skip(ctypes.byref(state), -1)
        self.lib.music_state_demo_skip(ctypes.byref(state), -1)
        self.lib.music_state_demo_skip(ctypes.byref(state), -1)
        self.assertEqual(state.title, base)      # 反向整整一圈回到原曲目

    def test_demo_track_ends_advance_to_next(self):
        state = MusicState()
        self.lib.music_state_demo_skip(ctypes.byref(state), -1)
        duration = state.duration_ms
        self.lib.music_state_demo_advance(ctypes.byref(state), 400_000)
        self.assertEqual(state.position_ms, 0)   # 放完自动清零进下一首
        self.assertNotEqual(state.duration_ms, duration)


if __name__ == "__main__":
    unittest.main()
