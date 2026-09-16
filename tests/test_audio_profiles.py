import asyncio
import contextlib
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import main


def make_profile(token, width, height, *, audio=False, encoding="H264", source=None):
    return SimpleNamespace(
        token=token,
        Name=token,
        VideoEncoderConfiguration=SimpleNamespace(
            Encoding=encoding,
            Resolution=SimpleNamespace(Width=width, Height=height),
        ),
        AudioEncoderConfiguration=SimpleNamespace(Encoding="AAC") if audio else None,
        VideoSourceConfiguration=SimpleNamespace(SourceToken=source) if source is not None else None,
    )


def candidate(token, uri=None):
    return {"profile_token": token, "profile_name": token, "uri": uri or f"rtsp://camera/{token}"}


class AudioProfileTests(unittest.TestCase):
    def test_webrtc_vad_rejects_silence(self):
        completed = SimpleNamespace(returncode=0, stdout=b"\0" * (16000 * 2), stderr=b"")
        with patch.object(main.subprocess, "run", return_value=completed):
            self.assertFalse(main.detect_voice_activity("rtsp://camera/audio"))

    def test_main_stream_audio_does_not_change_video_substream_selection(self):
        main_stream = make_profile("main", 1920, 1080, audio=True)
        substream = make_profile("sub", 640, 360)
        profiles = [main_stream, substream]

        video = main.select_lowest_resolution_profile(profiles)

        self.assertIs(video, substream)
        self.assertEqual(main.select_audio_profiles(profiles, video), [main_stream, substream])

    def test_prefer_existing_video_audio_then_smallest_alternative(self):
        selected = make_profile("selected", 640, 480, audio=True)
        large = make_profile("large", 1920, 1080, audio=True)
        small = make_profile("small", 320, 240, audio=True)

        self.assertEqual(main.select_audio_profiles([large, small, selected], selected), [selected, small, large])

    def test_audio_fallback_excludes_other_camera_sources(self):
        selected = make_profile("sub", 640, 360, source="camera-1")
        same_source = make_profile("main", 1920, 1080, audio=True, source="camera-1")
        other_source = make_profile("other", 320, 240, audio=True, source="camera-2")

        self.assertEqual(main.select_audio_profiles([other_source, selected, same_source], selected), [same_source, selected])

    def test_missing_candidate_source_is_kept_for_compatibility(self):
        selected = make_profile("sub", 640, 360, source="camera-1")
        unknown_source = make_profile("audio", 1920, 1080, audio=True)

        self.assertEqual(main.select_audio_profiles([selected, unknown_source], selected), [unknown_source, selected])

    def test_unknown_selected_source_keeps_advertised_audio_candidates(self):
        for source in (None, ""):
            with self.subTest(source=source):
                selected = make_profile("sub", 640, 360, source=source)
                audio = make_profile("audio", 1920, 1080, audio=True, source="camera-1")

                self.assertEqual(main.select_audio_profiles([selected, audio], selected), [audio, selected])

    def test_absent_audio_metadata_still_tries_video_uri(self):
        selected = make_profile("sub", 640, 360)
        media = Mock()
        result = main.get_audio_stream_candidates(media, [selected], selected, "rtsp://camera/sub", "camera")

        self.assertEqual(result, [candidate("sub")])
        media.GetStreamUri.assert_not_called()

    def test_failed_audio_uri_resolution_falls_back_to_video(self):
        selected = make_profile("sub", 640, 360)
        audio = make_profile("main", 1920, 1080, audio=True)
        media = Mock()
        media.GetStreamUri.side_effect = RuntimeError("unsupported profile")

        result = main.get_audio_stream_candidates(media, [audio, selected], selected, "rtsp://camera/sub", "camera")

        self.assertEqual(result, [candidate("sub")])

    def test_audio_uri_timeout_restores_transport_and_preserves_video_fallback(self):
        selected = make_profile("sub", 640, 360)
        audio = make_profile("main", 1920, 1080, audio=True)
        media = Mock()
        transport = media.zeep_client.transport
        transport.operation_timeout = 30.0

        def fail_with_timeout(_):
            self.assertEqual(transport.operation_timeout, (1.0, 1.0))
            raise TimeoutError("audio URI lookup timed out")

        media.GetStreamUri.side_effect = fail_with_timeout
        with patch.object(main.time, "monotonic", side_effect=[100.0, 100.0]):
            result = main.get_audio_stream_candidates(media, [audio, selected], selected, "rtsp://camera/sub", "camera")

        self.assertEqual(result, [candidate("sub")])
        self.assertEqual(transport.operation_timeout, 30.0)
        media.GetStreamUri.assert_called_once()

    def test_exhausted_lookup_budget_skips_alternatives_but_keeps_video_uri(self):
        selected = make_profile("sub", 640, 360)
        first = make_profile("first", 1280, 720, audio=True)
        second = make_profile("second", 1920, 1080, audio=True)
        media = Mock()
        media.create_type.side_effect = lambda _: SimpleNamespace()
        media.GetStreamUri.return_value = SimpleNamespace(Uri="rtsp://camera/first")
        media.zeep_client.transport.operation_timeout = None

        with patch.object(main.time, "monotonic", side_effect=[100.0, 100.0, 102.1]):
            result = main.get_audio_stream_candidates(media, [selected, first, second], selected, "rtsp://camera/sub", "camera")

        self.assertEqual(result, [candidate("first"), candidate("sub")])
        media.GetStreamUri.assert_called_once()
        self.assertEqual(media.GetStreamUri.call_args.args[0].ProfileToken, "first")
        self.assertIsNone(media.zeep_client.transport.operation_timeout)

    def test_duplicate_audio_uris_are_probed_once_and_loopback_is_fixed(self):
        selected = make_profile("sub", 640, 360)
        first = make_profile("first", 1280, 720, audio=True)
        second = make_profile("second", 1920, 1080, audio=True)
        media = Mock()
        media.create_type.side_effect = lambda _: SimpleNamespace()
        media.GetStreamUri.return_value = SimpleNamespace(Uri="rtsp://0.0.0.0:8554/audio?transport=1")

        result = main.get_audio_stream_candidates(media, [selected, first, second], selected, "rtsp://camera/sub", "camera")

        self.assertEqual(result, [candidate("first", "rtsp://camera:8554/audio?transport=1"), candidate("sub")])

    def test_inaccurate_audio_metadata_can_fall_back_to_original_video_stream(self):
        selected = make_profile("sub", 640, 360)
        advertised = make_profile("main", 1920, 1080, audio=True)
        media = Mock()
        media.GetStreamUri.return_value = SimpleNamespace(Uri="rtsp://camera/main")
        candidates = main.get_audio_stream_candidates(media, [selected, advertised], selected, "rtsp://camera/sub", "camera")

        with patch.object(main, "sync_detect_audio_volume", side_effect=[None, -32.5]) as detect:
            result, _ = main.sync_detect_audio_candidates(candidates)

        self.assertEqual(detect.call_args_list, [call("rtsp://camera/main"), call("rtsp://camera/sub")])
        self.assertEqual(result["audio_profile_token"], "sub")
        self.assertEqual(result["audio_volume_db"], -32.5)

    def test_failed_first_audio_candidate_uses_next_and_stops_after_success(self):
        candidates = [candidate("first"), candidate("second"), candidate("third")]
        with patch.object(main, "sync_detect_audio_volume", side_effect=[None, -23.5]) as detect:
            result, attempts = main.sync_detect_audio_candidates(candidates)

        self.assertEqual(detect.call_args_list, [call(candidates[0]["uri"]), call(candidates[1]["uri"])])
        self.assertEqual(result["audio_volume_db"], -23.5)
        self.assertEqual(result["audio_profile_token"], "second")
        self.assertTrue(result["audio_probe_success"])
        self.assertEqual([(item["profile_token"], item["success"]) for item in attempts], [("first", False), ("second", True)])

    def test_all_audio_candidates_fail(self):
        with patch.object(main, "sync_detect_audio_volume", return_value=None) as detect:
            result, attempts = main.sync_detect_audio_candidates([candidate("first"), candidate("second")])

        self.assertIsNone(result)
        self.assertEqual(detect.call_count, 2)
        self.assertEqual(len(attempts), 2)

    def test_valid_silent_volume_is_still_a_success(self):
        with patch.object(main, "sync_detect_audio_volume", return_value=-99.0):
            result, _ = main.sync_detect_audio_candidates([candidate("audio")])

        self.assertTrue(result["audio_probe_success"])
        self.assertEqual(result["audio_volume_db"], -99.0)


class AudioWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_authenticates_both_streams_and_exports_actual_audio_profile(self):
        queue = asyncio.Queue()
        analysis = main.build_default_analysis_data()
        analysis.update({
            "audio_probe_success": True,
            "audio_volume_db": -45.0,
            "audio_profile_token": "actual-audio",
            "audio_profile_name": "Audio main",
        })
        video_metrics = {
            "width": 640,
            "height": 360,
            "fps": 10,
            "encoding": "H264",
            "profile_token": "sub",
            "profile_name": "Video sub",
            "audio_candidates": [candidate("main"), candidate("sub")],
        }
        onvif_result = (
            SimpleNamespace(Manufacturer="test", Model="test", FirmwareVersion="1"),
            "rtsp://camera/sub", None, "00:00:00:00:00:00", 0, video_metrics, -1,
        )
        with (
            patch.object(main, "cv_queue", queue),
            patch.object(main, "cv_stream_cache", {}),
            patch.object(main, "cv_analysis_cache", {"camera_80": {"time": time.time(), "data": analysis}}),
            patch.object(main, "cv_probing_targets", set()),
            patch.object(main, "last_cv_cache_clean", time.time()),
            patch.object(main, "thread_pool", None),
            patch.object(main, "sync_onvif_probe", return_value=onvif_result),
        ):
            response = await main.probe(
                target="camera", user="user@x", password="p:a/ss", port=80,
                expected_pan=None, expected_tilt=None, expected_zoom=None,
            )

        cache_key, video_uri, audio_candidates = queue.get_nowait()
        self.assertEqual(cache_key, "camera_80")
        self.assertEqual(video_uri, "rtsp://user%40x:p%3Aa%2Fss@camera/sub")
        self.assertEqual([item["uri"] for item in audio_candidates], [
            "rtsp://user%40x:p%3Aa%2Fss@camera/main",
            "rtsp://user%40x:p%3Aa%2Fss@camera/sub",
        ])
        metrics = response.body.decode()
        self.assertIn("onvif_audio_probe_success 1.0", metrics)
        self.assertIn('onvif_audio_stream_profile_info{name="Audio main",token="actual-audio"} 1.0', metrics)
        self.assertIn('onvif_video_stream_profile_info{name="Video sub",token="sub"} 1.0', metrics)
        self.assertIn("onvif_audio_mean_volume_db -45.0", metrics)

    async def test_worker_reads_video_substream_and_audio_main_stream(self):
        queue = asyncio.Queue()
        await queue.put(("camera_80", "rtsp://camera/sub", [candidate("main")]))
        video_cache = {}
        analysis_cache = {}
        frame_result = {
            "stream_exists": True,
            "capture_opened": True,
            "open_attempts": 1,
            "read_attempts": 1,
            "analysis": main.build_default_analysis_data(),
        }
        with (
            patch.object(main, "cv_queue", queue),
            patch.object(main, "cv_stream_cache", video_cache),
            patch.object(main, "cv_analysis_cache", analysis_cache),
            patch.object(main, "cv_probing_targets", {"camera_80"}),
            patch.object(main, "process_pool", None),
            patch.object(main.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(main, "sync_detect_stream_frame", return_value=frame_result) as frame,
            patch.object(main, "sync_detect_audio_volume", return_value=-42.0) as audio,
        ):
            worker = asyncio.create_task(main.cv_worker(0, 0))
            try:
                await asyncio.wait_for(queue.join(), timeout=5)
            finally:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

        frame.assert_called_once_with("rtsp://camera/sub")
        audio.assert_called_once_with("rtsp://camera/main")
        self.assertTrue(video_cache["camera_80"]["data"]["stream_exists"])
        self.assertEqual(analysis_cache["camera_80"]["data"]["audio_volume_db"], -42.0)
        self.assertEqual(analysis_cache["camera_80"]["data"]["audio_profile_token"], "main")


if __name__ == "__main__":
    unittest.main()
