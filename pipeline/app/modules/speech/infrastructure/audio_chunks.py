"""회의 녹음을 16 kHz mono PCM16 WAV 구간으로 나눈다(PyAV). 구간 경계는 겹침 없이 무음 지점에서 자른다."""

import wave
from pathlib import Path

import av
import numpy as np

from app.modules.speech.domain.errors import MeetingTranscriptionError

SAMPLE_RATE = 16000
TARGET_SAMPLES = 600 * SAMPLE_RATE  # 구간 목표 10분(WAV 약 19 MB, 공급사 한도 25 MB 미만)
SEARCH_SAMPLES = 20 * SAMPLE_RATE  # 목표 직전 20초 안에서 경계를 찾는다
WINDOW_SAMPLES = SAMPLE_RATE // 10  # 에너지는 100 ms 창으로 비교한다
MAX_SAMPLES = 3 * 3600 * SAMPLE_RATE  # 녹음 상한 3시간
MIN_CHUNK_SAMPLES = WINDOW_SAMPLES  # 공급사 최소 길이(0.1초)보다 짧은 꼬리는 버린다


def find_cut(samples: np.ndarray) -> int:
    """목표 길이 직전 20초 중 에너지가 가장 낮은 100 ms 창의 가운데를 경계로 돌려준다. 항상 목표 길이 이하다."""
    lo = TARGET_SAMPLES - SEARCH_SAMPLES
    squares = samples[lo:TARGET_SAMPLES].astype(np.float64) ** 2
    cumulative = np.concatenate(([0.0], np.cumsum(squares)))
    energy = cumulative[WINDOW_SAMPLES:] - cumulative[:-WINDOW_SAMPLES]
    # 에너지가 같으면 목표에 가장 가까운(뒤쪽) 창을 고른다.
    start = len(energy) - 1 - int(np.argmin(energy[::-1]))
    return lo + start + WINDOW_SAMPLES // 2


def _write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(samples.astype("<i2").tobytes())


def split_recording(recording: Path, out_dir: Path) -> tuple[list[Path], float]:
    """녹음을 순서대로 WAV 파일 목록과 전체 길이(초)로 나눈다. 길이 초과·해독 불가는 MeetingTranscriptionError."""
    unreadable = MeetingTranscriptionError("MEETING_RECORDING_UNREADABLE")
    too_long = MeetingTranscriptionError("MEETING_RECORDING_TOO_LONG")
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    def emit(samples: np.ndarray) -> None:
        path = out_dir / f"{len(paths):05d}.wav"
        _write_wav(path, samples)
        paths.append(path)

    try:
        with av.open(str(recording)) as container:
            if not container.streams.audio:
                raise unreadable
            if container.duration and container.duration / av.time_base > MAX_SAMPLES / SAMPLE_RATE:
                raise too_long  # 길이를 알 수 있으면 디코딩 전에 거절한다.
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            pending: list[np.ndarray] = []
            pending_len = total = 0

            def resampled():
                for decoded in container.decode(audio=0):
                    yield from resampler.resample(decoded)
                yield from resampler.resample(None)

            for frame in resampled():
                samples = frame.to_ndarray().reshape(-1)
                total += len(samples)
                if total > MAX_SAMPLES:  # 길이를 미리 알 수 없는 컨테이너(webm 등)의 안전망
                    raise too_long
                pending.append(samples)
                pending_len += len(samples)
                while pending_len >= TARGET_SAMPLES:
                    buffer = np.concatenate(pending)
                    cut = find_cut(buffer)
                    emit(buffer[:cut])
                    pending, pending_len = [buffer[cut:]], len(buffer) - cut
            tail = np.concatenate(pending) if pending else np.empty(0, dtype=np.int16)
            if len(tail) >= MIN_CHUNK_SAMPLES:
                emit(tail)
    except (av.error.FFmpegError, OSError, ValueError) as exc:
        raise unreadable from exc
    if not paths:
        raise unreadable
    return paths, total / SAMPLE_RATE
