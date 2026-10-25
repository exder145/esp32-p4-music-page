"""编译 music_lyric.c，验证 LRC 解析、时间排序与进度取行逻辑。"""
import ctypes
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class LyricLine(ctypes.Structure):
    _fields_ = [
        ("time_ms", ctypes.c_uint32),
        ("text", ctypes.c_char_p),
    ]


class Lyric(ctypes.Structure):
    _fields_ = [
        ("text", ctypes.c_void_p),
        ("text_size", ctypes.c_size_t),
        ("lines", ctypes.POINTER(LyricLine)),
        ("line_cap", ctypes.c_int),
        ("count", ctypes.c_int),
    ]


class MusicLyricTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.temp = tempfile.TemporaryDirectory()
        dll = Path(cls.temp.name) / "music_lyric.dll"
        compiler = shutil.which("gcc") or "gcc"
        subprocess.run(
            [compiler, "-shared", "-std=c11", "-I", str(root / "components/music_page/include"),
             str(root / "components/music_page/music_lyric.c"), "-o", str(dll)],
            check=True,
        )
        cls.lib = ctypes.CDLL(str(dll))
        cls.lib.music_lyric_init.argtypes = [
            ctypes.POINTER(Lyric), ctypes.c_void_p, ctypes.c_size_t,
            ctypes.POINTER(LyricLine), ctypes.c_int]
        cls.lib.music_lyric_init.restype = ctypes.c_bool
        cls.lib.music_lyric_clear.argtypes = [ctypes.POINTER(Lyric)]
        cls.lib.music_lyric_parse.argtypes = [ctypes.POINTER(Lyric), ctypes.c_char_p]
        cls.lib.music_lyric_parse.restype = ctypes.c_int
        cls.lib.music_lyric_index_at.argtypes = [ctypes.POINTER(Lyric), ctypes.c_uint32]
        cls.lib.music_lyric_index_at.restype = ctypes.c_int
        cls.lib.music_lyric_time_at.argtypes = [ctypes.POINTER(Lyric), ctypes.c_int]
        cls.lib.music_lyric_time_at.restype = ctypes.c_uint32
        cls.lib.music_lyric_text_at.argtypes = [ctypes.POINTER(Lyric), ctypes.c_int]
        cls.lib.music_lyric_text_at.restype = ctypes.c_char_p

    @classmethod
    def tearDownClass(cls):
        import _ctypes
        _ctypes.FreeLibrary(cls.lib._handle)
        del cls.lib
        cls.temp.cleanup()

    # ── 辅助 ─────────────────────────────────────────────────────
    def make(self, cap=4096, line_cap=64):
        buf = ctypes.create_string_buffer(cap)
        lines = (LyricLine * line_cap)()
        lyr = Lyric()
        self.assertTrue(self.lib.music_lyric_init(
            ctypes.byref(lyr), ctypes.cast(buf, ctypes.c_void_p), cap,
            ctypes.cast(lines, ctypes.POINTER(LyricLine)), line_cap))
        self._keep = (buf, lines)   # 防止被 GC 回收
        return lyr

    def parse(self, lrc, cap=4096, line_cap=64):
        lyr = self.make(cap, line_cap)
        count = self.lib.music_lyric_parse(ctypes.byref(lyr), lrc.encode("utf-8"))
        self.assertEqual(count, lyr.count)
        return lyr, count

    def texts(self, lyr):
        return [self.lib.music_lyric_text_at(ctypes.byref(lyr), i).decode("utf-8")
                for i in range(lyr.count)]

    # ── 用例 ─────────────────────────────────────────────────────
    def test_basic_lines_sorted_by_time(self):
        lyr, count = self.parse(
            "[00:12.30]第二句\n[00:03.10]第一句\n[00:20.00]第三句\n")
        self.assertEqual(count, 3)
        self.assertEqual(self.texts(lyr), ["第一句", "第二句", "第三句"])
        self.assertEqual(self.lib.music_lyric_time_at(ctypes.byref(lyr), 0), 3100)
        self.assertEqual(self.lib.music_lyric_time_at(ctypes.byref(lyr), 2), 20000)

    def test_millisecond_formats(self):
        lyr, count = self.parse(
            "[00:01]整秒\n[00:02.5]一位小数\n[00:03.25]两位小数\n[01:04:456]冒号毫秒\n")
        self.assertEqual(count, 4)
        times = [self.lib.music_lyric_time_at(ctypes.byref(lyr), i) for i in range(4)]
        self.assertEqual(times, [1000, 2500, 3250, 64456])

    def test_multi_tag_line_reuses_text(self):
        lyr, count = self.parse("[00:05.00][00:10.00]副歌\n[00:07.00]主歌\n")
        self.assertEqual(count, 3)
        self.assertEqual(self.texts(lyr), ["副歌", "主歌", "副歌"])

    def test_duplicate_timestamp_kept_once(self):
        lyr, count = self.parse("[00:05.00]第一版\n[00:05.00]第二版\n")
        self.assertEqual(count, 1)
        self.assertEqual(self.texts(lyr), ["第一版"])

    def test_crlf_and_leading_space(self):
        lyr, count = self.parse("[00:01.00]   缩进歌词   \r\n[00:02.00]\t制表符\r\n")
        self.assertEqual(count, 2)
        self.assertEqual(self.texts(lyr), ["缩进歌词", "制表符"])

    def test_invalid_and_empty_lines_skipped(self):
        lyr, count = self.parse(
            "没有标签的一行\n[xx:yy]坏标签\n[00:03.00]\n[]\n[00:04.00]有效\n")
        self.assertEqual(count, 1)
        self.assertEqual(self.texts(lyr), ["有效"])

    def test_empty_input(self):
        lyr, count = self.parse("")
        self.assertEqual(count, 0)
        self.assertEqual(self.lib.music_lyric_index_at(ctypes.byref(lyr), 5000), -1)
        self.assertIsNone(self.lib.music_lyric_text_at(ctypes.byref(lyr), 0))

    def test_index_at_boundaries(self):
        lyr, _ = self.parse("[00:10.00]甲\n[00:20.00]乙\n[00:30.00]丙\n")
        index = self.lib.music_lyric_index_at
        self.assertEqual(index(ctypes.byref(lyr), 0), -1)        # 前奏：还没到第一句
        self.assertEqual(index(ctypes.byref(lyr), 9999), -1)
        self.assertEqual(index(ctypes.byref(lyr), 10000), 0)     # 正好卡点归本句
        self.assertEqual(index(ctypes.byref(lyr), 19999), 0)
        self.assertEqual(index(ctypes.byref(lyr), 20000), 1)
        self.assertEqual(index(ctypes.byref(lyr), 999999), 2)    # 末句一直停住

    def test_parse_in_place_same_buffer(self):
        """界面用法：LRC 先拷进绑定缓冲，再拿同一块缓冲解析（曾因此解析出 0 行）。"""
        lyr = self.make(cap=4096)
        buf = self._keep[0]
        buf.value = "[00:01.00]甲\n[00:02.00]乙\n".encode("utf-8")
        count = self.lib.music_lyric_parse(ctypes.byref(lyr), buf)
        self.assertEqual(count, 2)
        self.assertEqual(self.texts(lyr), ["甲", "乙"])

    def test_text_capacity_truncates_without_overflow(self):        # 缓冲只够装两三句，其余行必须被丢弃而不是踩内存
        payload = "".join(f"[00:{i:02d}.00]{'字' * 20}\n" for i in range(30))
        lyr, count = self.parse(payload, cap=200, line_cap=64)
        self.assertGreater(count, 0)
        self.assertLess(count, 30)
        for line in self.texts(lyr):
            self.assertEqual(len(line), 20)

    def test_line_cap_limits_count(self):
        payload = "".join(f"[00:{i:02d}.00]第{i}句\n" for i in range(10))
        lyr, count = self.parse(payload, line_cap=4)
        self.assertEqual(count, 4)
        self.assertEqual(self.texts(lyr), ["第0句", "第1句", "第2句", "第3句"])

    def test_clear_resets_but_keeps_binding(self):
        lyr, count = self.parse("[00:01.00]甲\n")
        self.assertEqual(count, 1)
        self.lib.music_lyric_clear(ctypes.byref(lyr))
        self.assertEqual(lyr.count, 0)
        second = "[00:02.00]乙\n".encode("utf-8")
        self.assertEqual(self.lib.music_lyric_parse(ctypes.byref(lyr), second), 1)
        self.assertEqual(self.texts(lyr), ["乙"])

    def test_init_rejects_small_buffers(self):
        lyr = Lyric()
        buf = ctypes.create_string_buffer(8)
        lines = (LyricLine * 4)()
        self.assertFalse(self.lib.music_lyric_init(
            ctypes.byref(lyr), ctypes.cast(buf, ctypes.c_void_p), 8,
            ctypes.cast(lines, ctypes.POINTER(LyricLine)), 4))
        self.assertFalse(self.lib.music_lyric_init(
            ctypes.byref(lyr), ctypes.cast(buf, ctypes.c_void_p), 64,
            ctypes.cast(lines, ctypes.POINTER(LyricLine)), 0))


if __name__ == "__main__":
    unittest.main()
