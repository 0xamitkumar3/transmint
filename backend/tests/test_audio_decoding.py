"""Real decoding checks (no model download): guards against PyAV/faster-whisper incompatibilities."""

from pathlib import Path

import pytest

from app.services.audio_service import AudioService
from tests.conftest import make_wav_bytes


def test_faster_whisper_can_decode_audio(tmp_path: Path) -> None:
    decode_audio = pytest.importorskip("faster_whisper").decode_audio
    wav = tmp_path / "tone.wav"
    wav.write_bytes(make_wav_bytes(seconds=0.5))
    samples = decode_audio(str(wav), sampling_rate=16_000)
    assert len(samples) == 8_000


def test_probe_reports_duration(tmp_path: Path) -> None:
    wav = tmp_path / "tone.wav"
    wav.write_bytes(make_wav_bytes(seconds=2))
    info = AudioService(tmp_path, 10_000_000).probe(wav)
    assert info.duration_seconds == 2.0
    assert info.needs_conversion is False
