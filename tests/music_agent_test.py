# -*- coding: utf-8 -*-
"""music_agent.py 的宿主单元测试（不需要 Windows / 网易云 / WinRT）。

跑法：python tests/music_agent_test.py -v
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "tools", "music_page_agent"))

import music_agent as ma  # noqa: E402


class LrcTest(unittest.TestCase):
    def test_parse_lrc_basic(self):
        text = ("[00:00.00]作词 : 方文山\n"
                "[00:01.20]第一句\n"
                "[00:03.45]第二句\n")
        self.assertEqual(ma.parse_lrc(text),
                         [(1200, "第一句"), (3450, "第二句")])

    def test_parse_lrc_sorts_and_handles_multi_tag(self):
        text = "[00:05.00]后唱\n[00:01.00]先唱\n[00:02:50]同一句\n"
        lines = ma.parse_lrc(text)
        self.assertEqual([t for t, _ in lines], [1000, 2500, 5000])
        self.assertEqual([s for _, s in lines], ["先唱", "同一句", "后唱"])

    def test_parse_lrc_empty_and_invalid(self):
        self.assertIsNone(ma.parse_lrc(""))
        self.assertIsNone(ma.parse_lrc("[00:01.00]\n没有时间标签的行\n"))
        self.assertIsNone(ma.parse_lrc("[ar:某某]\n[ti:歌名]\n"))

    def test_lines_to_lrc_roundtrip(self):
        # LRC 时间戳精度是 10ms（百分秒），毫秒位会丢，测试按实际精度取值
        lines = [(0, "开头"), (61_230, "一分零一秒"), (3_599_990, "快一小时")]
        text = ma.lines_to_lrc(lines)
        self.assertEqual(ma.parse_lrc(text), lines)

    def test_pick_lyric_line(self):
        lines = [(1000, "甲"), (2000, "乙")]
        self.assertEqual(ma.pick_lyric_line(lines, 0), "")
        self.assertEqual(ma.pick_lyric_line(lines, 1500), "甲")
        self.assertEqual(ma.pick_lyric_line(lines, 99_999), "乙")
        self.assertEqual(ma.pick_lyric_line(None, 1500), "")
        self.assertEqual(len(ma.pick_lyric_line([(0, "字" * 200)], 0)),
                         ma.LYRIC_MAX_CHARS)


class MatchTest(unittest.TestCase):
    # 复刻实测结果：第一条是翻唱，第二条才是原曲
    COVER = {"id": 1, "name": "晴天", "duration": 269_000,
             "artists": [{"name": "Lucky 小"}]}
    ORIGINAL = {"id": 2, "name": "晴天", "duration": 269_200,
                "artists": [{"name": "周杰伦"}]}
    LIVE = {"id": 3, "name": "晴天 (Live)", "duration": 300_000,
            "artists": [{"name": "周杰伦"}]}

    def test_pick_original_prefers_same_artist(self):
        self.assertIs(ma.pick_original([self.COVER, self.ORIGINAL], "晴天", "周杰伦", 269.0),
                      self.ORIGINAL)

    def test_pick_original_uses_duration_between_versions(self):
        songs = [self.LIVE, self.ORIGINAL]
        self.assertIs(ma.pick_original(songs, "晴天", "周杰伦", 269.0), self.ORIGINAL)
        self.assertIs(ma.pick_original(songs, "晴天", "周杰伦", 300.0), self.LIVE)

    def test_pick_original_rejects_unrelated(self):
        songs = [{"id": 9, "name": "七里香", "duration": 299_000,
                  "artists": [{"name": "周杰伦"}]}]
        self.assertIsNone(ma.pick_original(songs, "晴天", "周杰伦", 269.0))

    def test_normalize_strips_brackets_and_case(self):
        self.assertEqual(ma._norm("晴天 (Live版)"), "晴天")
        self.assertEqual(ma._norm("Jay  Chow"), "jaychow")
        # 整串都在括号里时不能归一成空串，否则会被判定为未匹配
        self.assertTrue(ma._norm("（纯音乐）"))
        self.assertEqual(ma._norm("（纯音乐）"), "纯音乐")


class PositionTest(unittest.TestCase):
    def test_extrapolate_only_while_playing(self):
        self.assertEqual(ma.extrapolate_position(10.0, None, True), 10.0)
        self.assertEqual(ma.extrapolate_position(10.0, None, False), 10.0)

    def test_extrapolate_adds_elapsed(self):
        import time as _time
        stamp = _time.time() - 5
        pos = ma.extrapolate_position(10.0, stamp, True)
        self.assertAlmostEqual(pos, 15.0, delta=0.5)

    def test_extrapolate_ignores_absurd_stamp(self):
        self.assertEqual(ma.extrapolate_position(10.0, 12_345, True), 10.0)


class StatusTest(unittest.TestCase):
    """播放状态与时间线可用性判断（网易云这两项都"不正常"）。"""

    class _Pb:
        def __init__(self, status):
            self.playback_status = status

    class _Tl:
        def __init__(self, year, end_seconds):
            self.last_updated_time = type("Stamp", (), {"year": year})()
            self.end_time = type("Span", (), {
                "total_seconds": lambda _self: end_seconds})()

    def test_playing_from_enum_number(self):
        # winrt 枚举 str() 得到 "4"：int 判断才对
        self.assertTrue(ma.playback_is_playing(self._Pb(4)))
        self.assertFalse(ma.playback_is_playing(self._Pb(5)))
        self.assertTrue(ma.playback_is_playing(self._Pb("PLAYING")))

    def test_timeline_usable(self):
        self.assertFalse(ma.timeline_usable(self._Tl(1601, 235.0)))   # 网易云
        self.assertFalse(ma.timeline_usable(self._Tl(2026, 0.0)))
        self.assertTrue(ma.timeline_usable(self._Tl(2026, 235.0)))
        self.assertFalse(ma.timeline_usable(None))


class _FakeClock:
    """可手动推进的单调时钟，用来验证播放时钟逻辑。"""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ClockTest(unittest.TestCase):
    """网易云不报进度，代理得自己攒（_sync_clock）。"""

    def setUp(self):
        from unittest import mock
        self.fake = _FakeClock()
        self.patch = mock.patch.object(ma.time, "monotonic", self.fake)
        self.patch.start()
        self.agent = ma.Agent(8777)

    def tearDown(self):
        self.patch.stop()

    def test_playing_advances_without_real_position(self):
        self.assertAlmostEqual(self.agent._sync_clock("h", None, False, 100), 0.0, delta=0.01)
        self.fake.now += 5
        self.assertAlmostEqual(self.agent._sync_clock("h", None, True, 100), 0.0, delta=0.01)
        self.fake.now += 3
        self.assertAlmostEqual(self.agent._sync_clock("h", None, True, 100), 3.0, delta=0.01)

    def test_pause_freezes_and_resume_continues(self):
        self.agent._sync_clock("h", None, True, 100)
        self.fake.now += 10
        self.agent._sync_clock("h", None, True, 100)
        self.agent._sync_clock("h", None, False, 100)      # 暂停：结算已播 10s
        self.fake.now += 30
        self.assertAlmostEqual(self.agent._sync_clock("h", None, False, 100), 10.0, delta=0.05)
        self.agent._sync_clock("h", None, True, 100)       # 继续播放
        self.fake.now += 2
        self.assertAlmostEqual(self.agent._sync_clock("h", None, True, 100), 12.0, delta=0.05)

    def test_track_change_resets_to_zero(self):
        self.agent._sync_clock("a", None, True, 100)
        self.fake.now += 40
        self.agent._sync_clock("a", None, True, 100)
        self.assertAlmostEqual(self.agent._sync_clock("b", None, True, 100), 0.0, delta=0.05)

    def test_real_position_wins_and_clamps(self):
        self.assertAlmostEqual(self.agent._sync_clock("h", 42.0, True, 200), 42.0, delta=0.01)
        self.fake.now += 1
        self.assertAlmostEqual(self.agent._sync_clock("h", 42.0, True, 200), 42.0, delta=0.01)
        self.assertLessEqual(self.agent._sync_clock("h", 999.0, True, 100), 99.6)

    def test_loop_wraps_after_duration(self):
        """单曲循环：hash 不变、网易云不报位置，播完要能自己绕回开头。"""
        self.agent._sync_clock("h", None, True, 60)
        self.fake.now += 59
        self.assertAlmostEqual(self.agent._sync_clock("h", None, True, 60), 59.0, delta=0.05)
        self.fake.now += 4        # 超过时长但还在宽限期内：仍按末尾处理
        self.assertAlmostEqual(self.agent._sync_clock("h", None, True, 60), 59.5, delta=0.05)
        self.fake.now += 5        # 超出 3 秒宽限：认定从头播第二遍
        self.assertAlmostEqual(self.agent._sync_clock("h", None, True, 60), 7.5, delta=0.6)

    def test_paused_clock_never_wraps(self):
        """暂停后时钟冻结，即使超过时长也不能绕回开头。"""
        self.agent._sync_clock("h", None, True, 30)
        self.fake.now += 20
        self.assertAlmostEqual(self.agent._sync_clock("h", None, False, 30), 20.0, delta=0.05)
        self.fake.now += 60
        self.assertAlmostEqual(self.agent._sync_clock("h", None, False, 30), 20.0, delta=0.05)


class WindowSourceTest(unittest.TestCase):
    """曲目来自网易云窗口标题、控制来自系统媒体键（SMTC 媒体属性是冻结的）。"""

    def test_parse_now_playing_basic(self):
        self.assertEqual(ma.parse_now_playing("Dirty - grandson"),
                         ("Dirty", "grandson"))
        self.assertEqual(ma.parse_now_playing("Монополия - монеточка"),
                         ("Монополия", "монеточка"))

    def test_parse_now_playing_keeps_dash_inside_artist(self):
        # 只按第一个 " - " 切：歌手名里带横线时不能吃掉后半截
        self.assertEqual(ma.parse_now_playing("A - B - C"), ("A", "B - C"))

    def test_parse_now_playing_rejects_client_pages(self):
        for title in ("网易云音乐", "桌面歌词", "迷你播放器", "播放器", "发现音乐"):
            self.assertIsNone(ma.parse_now_playing(title), title)
        self.assertIsNone(ma.parse_now_playing("没有分隔符的歌名"))
        self.assertIsNone(ma.parse_now_playing(" - 只有歌手"))
        self.assertIsNone(ma.parse_now_playing("只有歌名 - "))
        self.assertIsNone(ma.parse_now_playing(""))

    def test_send_media_key_rejects_unknown_action(self):
        self.assertFalse(ma.send_media_key("stop"))
        self.assertFalse(ma.send_media_key(""))

    @unittest.skipIf(ma._user32 is None, "需要 Windows 的 user32")
    def test_send_media_key_posts_appcommand(self):
        sent = []

        class FakeUser32:
            @staticmethod
            def PostMessageW(hwnd, msg, wp, lp):
                sent.append((hwnd, msg, wp, lp))
                return 1

        old_window, old_user32 = ma.cloudmusic_window, ma._user32
        ma.cloudmusic_window = lambda: (4321, "Dirty - grandson")
        ma._user32 = FakeUser32()
        try:
            self.assertTrue(ma.send_media_key("next"))
            self.assertTrue(ma.send_media_key("play_pause"))
            ma.cloudmusic_window = lambda: None       # 客户端没开
            self.assertFalse(ma.send_media_key("prev"))
        finally:
            ma.cloudmusic_window, ma._user32 = old_window, old_user32
        self.assertEqual(sent[0], (4321, ma.WM_APPCOMMAND, 0, 11 << 16))
        self.assertEqual(sent[1][3], 14 << 16)


class BilingualTest(unittest.TestCase):
    """外语歌要原文 + 中文译文两行。"""

    def test_chinese_song_needs_no_translation(self):
        self.assertTrue(ma.is_chinese_dominant([(0, "雨下整夜 我的爱溢出就像雨水")]))
        self.assertFalse(ma.is_chinese_dominant([(0, "You try to run me through")]))
        self.assertFalse(ma.is_chinese_dominant([]))

    def test_merge_aligned_translation(self):
        orig = [(1000, "Hello"), (3000, "World")]
        trans = [(1120, "你好"), (3100, "世界")]
        lines = ma.merge_bilingual(orig, trans)
        self.assertEqual(lines, [(1000, "Hello\x1f你好"), (3000, "World\x1f世界")])

    def test_merge_skips_chinese_song_and_identical_text(self):
        orig = [(0, "晴天")]
        self.assertEqual(ma.merge_bilingual(orig, [(0, "晴天")]), orig)
        self.assertEqual(ma.merge_bilingual([(0, "Hello")], [(0, "Hello")]),
                         [(0, "Hello")])
        self.assertEqual(ma.merge_bilingual([(0, "Hello")], None), [(0, "Hello")])

    def test_merge_does_not_pair_distant_lines(self):
        # 译文比原文晚了 5 秒，不该硬拼成双语
        self.assertEqual(ma.merge_bilingual([(0, "Hello")], [(5000, "你好")]),
                         [(0, "Hello")])


class TransportTest(unittest.IsolatedAsyncioTestCase):
    """控制指令：播放/暂停靠真点网易云按钮，切歌靠媒体键。"""

    async def asyncSetUp(self):
        self.agent = ma.Agent(8777)
        self.smtc_calls = []
        self.click_ok = True

    def _patch(self, playing, effect, click_ok=True):
        """playing：当前是否在播；effect：指令生效后的播放状态。"""
        self.click_ok = click_ok

        async def fake_playing():
            return playing

        async def fake_send(action):
            self.smtc_calls.append(action)
            return True

        async def fake_verify(expected, timeout=1.5):
            return expected == effect

        self.agent._smtc_playing = fake_playing
        self.agent._smtc_send = fake_send
        self.agent._verify_playback = fake_verify
        self._old_click = ma.click_player_button
        ma.click_player_button = lambda: self.click_ok

    async def asyncTearDown(self):
        if hasattr(self, "_old_click"):
            ma.click_player_button = self._old_click

    async def test_playing_pauses_by_clicking(self):
        self._patch(True, False)
        ok, via, real, _cost = await self.agent.apply_cmd("play_pause")
        self.assertTrue(ok)
        self.assertEqual((via, real), ("click", "pause"))
        self.assertEqual(self.smtc_calls, [])       # 点得到按钮就不要动 SMTC

    async def test_paused_resumes_by_clicking(self):
        self._patch(False, True)
        ok, via, real, _cost = await self.agent.apply_cmd("play_pause")
        self.assertTrue(ok)
        self.assertEqual((via, real), ("click", "play"))
        self.assertEqual(self.smtc_calls, [])

    async def test_click_unavailable_pauses_via_smtc(self):
        self._patch(True, False, click_ok=False)
        ok, via, real, _cost = await self.agent.apply_cmd("play_pause")
        self.assertTrue(ok)
        self.assertEqual((via, real), ("smtc", "pause"))
        self.assertEqual(self.smtc_calls, ["pause"])

    async def test_click_fails_then_smtc_pause(self):
        self._patch(True, False)
        # 第一次点击后状态没变（仍在播），验证失败 → 退回 SMTC 暂停
        seq = iter([False, True])

        async def flaky_verify(expected, timeout=1.5):
            return next(seq, True)

        self.agent._verify_playback = flaky_verify
        ok, via, real, _cost = await self.agent.apply_cmd("play_pause")
        self.assertTrue(ok)
        self.assertEqual((via, real), ("smtc", "pause"))

    async def test_next_uses_media_key(self):
        old_key = ma.send_media_key
        sent = []
        ma.send_media_key = lambda action: (sent.append(action), True)[1]
        old_track = self.agent._verify_track

        async def fake_track(before, timeout=1.8):
            return True

        self.agent._verify_track = fake_track
        try:
            ok, via, real, _cost = await self.agent.apply_cmd("next")
        finally:
            ma.send_media_key = old_key
            self.agent._verify_track = old_track
        self.assertTrue(ok)
        self.assertEqual((via, real), ("mediakey", "next"))
        self.assertEqual(sent, ["next"])

    async def test_reports_failure_when_nothing_works(self):
        self._patch(False, False, click_ok=False)   # 点击不可用，SMTC 也没生效

        async def verify(expected, timeout=1.5):
            return False

        self.agent._verify_playback = verify
        ok, via, _real, _cost = await self.agent.apply_cmd("play_pause")
        self.assertFalse(ok)
        self.assertEqual(via, "none")


class HttpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        self.agent = ma.Agent(8777)
        app = web.Application()
        app.router.add_get("/lyrics", self.agent.handle_lyrics)
        app.router.add_get("/art", self.agent.handle_art)
        app.router.add_get("/art_blur", self.agent.handle_blur)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    def _put(self, key="hash-a"):
        self.agent.track_extras[key] = {
            "ok": True, "lines": [(1000, "第一句"), (2000, "第二句")],
            "lrc": ma.lines_to_lrc([(1000, "第一句"), (2000, "第二句")]),
            "art": b"\xff\xd8jpeg", "blur": b"\x00" * (96 * 34 * 2),
            "fetched_at": 0,
        }
        self.agent.latest_hash = key

    async def test_lyrics_returns_lrc_text(self):
        self._put()
        resp = await self.client.get("/lyrics?h=hash-a")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/plain", resp.content_type)
        body = await resp.text()
        self.assertIn("[00:01.00]第一句", body)

    async def test_art_and_blur(self):
        self._put()
        art = await self.client.get("/art?h=hash-a")
        self.assertEqual(art.status, 200)
        self.assertEqual(art.content_type, "image/jpeg")
        self.assertEqual(await art.read(), b"\xff\xd8jpeg")
        blur = await self.client.get("/art_blur?h=hash-a")
        self.assertEqual(blur.status, 200)
        self.assertEqual(len(await blur.read()), 96 * 34 * 2)

    async def test_unknown_hash_falls_back_to_latest(self):
        self._put()
        resp = await self.client.get("/lyrics?h=stale-hash")
        self.assertEqual(resp.status, 200)
        no_query = await self.client.get("/art")
        self.assertEqual(no_query.status, 200)

    async def test_missing_extras_is_404(self):
        self.assertEqual((await self.client.get("/lyrics?h=nope")).status, 404)
        self.assertEqual((await self.client.get("/art?h=nope")).status, 404)
        self.assertEqual((await self.client.get("/art_blur?h=nope")).status, 404)


if __name__ == "__main__":
    unittest.main()
