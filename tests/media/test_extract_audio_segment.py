from pathlib import Path

from packages.media.video import ffmpeg


def test_extract_audio_segment_builds_expected_args(monkeypatch, tmp_path):
    captured = {}

    class FakeRunner:
        def run(self, args, *, timeout_sec=None):
            captured["args"] = list(args)
            Path(args[-1]).write_bytes(b"fake")

            class R:
                returncode = 0

            return R()

    monkeypatch.setattr(ffmpeg, "FfmpegRunner", lambda *a, **k: FakeRunner())
    out = ffmpeg.extract_audio_segment(
        tmp_path / "in.mp3",
        45.0,
        75.0,
        tmp_path / "out.mp3",
    )
    assert out == tmp_path / "out.mp3"
    args = captured["args"]
    assert "-ss" in args and "45.000" in args
    assert "-t" in args and "30.000" in args
    assert "-vn" in args
