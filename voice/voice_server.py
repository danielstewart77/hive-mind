"""
Hive Mind — Voice Server.

Provides STT (faster-whisper) and TTS (Kokoro) over HTTP.
Both models load at startup and stay resident.
Auto-detects CUDA; falls back to CPU gracefully.
"""

import io
import logging
import os
import subprocess
import tempfile

# Check GPU compatibility BEFORE importing torch — once torch initializes
# CUDA, it's too late to hide the device from downstream libraries.
def _check_gpu_early() -> bool:
    """Return True if CUDA GPU is usable (compute capability >= 7.0).
    If the GPU is too old, hide it via env var before torch loads."""
    try:
        import ctypes
        libcuda = ctypes.CDLL("libcuda.so.1")
        if libcuda.cuInit(0) != 0:
            return False
        dev = ctypes.c_int()
        if libcuda.cuDeviceGet(ctypes.byref(dev), 0) != 0:
            return False
        major, minor = ctypes.c_int(), ctypes.c_int()
        libcuda.cuDeviceGetAttribute(ctypes.byref(major), 75, dev)  # CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR
        libcuda.cuDeviceGetAttribute(ctypes.byref(minor), 76, dev)  # CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MINOR
        if major.value < 7:
            logging.getLogger("hive-mind.voice").warning(
                "GPU compute capability %d.%d < 7.0 — disabling CUDA",
                major.value, minor.value,
            )
            os.environ["CUDA_VISIBLE_DEVICES"] = ""
            return False
        return True
    except Exception:
        return False

_GPU_OK = _check_gpu_early()

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

log = logging.getLogger("hive-mind.voice")

app = FastAPI(title="Hive Mind Voice Server")

_DEVICE = "cuda" if _GPU_OK and torch.cuda.is_available() else "cpu"
_WHISPER_MODEL = os.getenv("WHISPER_MODEL", "medium")
_KOKORO_VOICE = os.getenv("KOKORO_VOICE", "bf_alice")

_whisper = None
_kokoro: dict = {}  # lang_code -> KPipeline


def _pipeline_for(voice: str):
    """Return the KPipeline for the given voice name (routes by prefix)."""
    lang = "b" if voice.startswith("b") else "a"
    if lang not in _kokoro:
        raise HTTPException(status_code=503, detail=f"Kokoro pipeline '{lang}' not ready")
    return _kokoro[lang]


# ---------------------------------------------------------------------------
# Startup — load both models once
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    global _whisper, _kokoro

    log.info("Voice server starting on device: %s", _DEVICE)

    from faster_whisper import WhisperModel
    compute_type = "float16" if _DEVICE == "cuda" else "int8"
    log.info("Loading faster-whisper %s (%s)...", _WHISPER_MODEL, compute_type)
    _whisper = WhisperModel(_WHISPER_MODEL, device=_DEVICE, compute_type=compute_type)

    from kokoro import KPipeline
    log.info("Loading Kokoro v1.0 (American + British)...")
    _kokoro["a"] = KPipeline(lang_code="a", device=_DEVICE)
    _kokoro["b"] = KPipeline(lang_code="b", device=_DEVICE)

    log.info("Voice server ready.")


# ---------------------------------------------------------------------------
# Audio conversion helpers
# ---------------------------------------------------------------------------
def _ogg_to_wav(ogg_bytes: bytes) -> bytes:
    """Convert OGG/Opus → 16kHz mono WAV (Whisper expects this)."""
    result = subprocess.run(
        ["ffmpeg", "-i", "pipe:0", "-f", "wav", "-ar", "16000", "-ac", "1", "pipe:1"],
        input=ogg_bytes,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg OGG→WAV: {result.stderr.decode()}")
    return result.stdout


def _wav_to_ogg(wav_bytes: bytes) -> bytes:
    """Convert WAV → OGG/Opus (Telegram voice note format)."""
    result = subprocess.run(
        ["ffmpeg", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "64k", "-f", "ogg", "pipe:1"],
        input=wav_bytes,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg WAV→OGG: {result.stderr.decode()}")
    return result.stdout


# ---------------------------------------------------------------------------
# STT endpoint
# ---------------------------------------------------------------------------
@app.post("/stt")
async def stt(file: UploadFile):
    """Transcribe uploaded audio (OGG or WAV) to text."""
    if _whisper is None:
        raise HTTPException(status_code=503, detail="STT model not ready")

    audio_bytes = await file.read()

    # Convert OGG → WAV if needed
    fname = file.filename or ""
    ctype = file.content_type or ""
    if "ogg" in ctype or fname.endswith(".ogg") or fname.endswith(".oga"):
        wav_bytes = _ogg_to_wav(audio_bytes)
    else:
        wav_bytes = audio_bytes

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp_path = f.name

    try:
        segments, _ = _whisper.transcribe(tmp_path, language="en")
        text = " ".join(s.text for s in segments).strip()
    finally:
        os.unlink(tmp_path)

    log.info("STT: %r", text[:80])
    return {"text": text}


# ---------------------------------------------------------------------------
# TTS endpoint
# ---------------------------------------------------------------------------
class TTSRequest(BaseModel):
    text: str
    voice: str = _KOKORO_VOICE
    speed: float = 1.0


@app.post("/tts")
async def tts(req: TTSRequest):
    """Synthesise text to OGG/Opus audio."""
    if _kokoro is None:
        raise HTTPException(status_code=503, detail="TTS model not ready")

    pipeline = _pipeline_for(req.voice)
    samples = []
    for _, _, audio in pipeline(req.text, voice=req.voice, speed=req.speed):
        samples.append(audio)

    if not samples:
        raise HTTPException(status_code=500, detail="TTS produced no audio")

    audio_array = np.concatenate(samples)

    wav_buf = io.BytesIO()
    sf.write(wav_buf, audio_array, 24000, format="WAV")
    ogg_bytes = _wav_to_ogg(wav_buf.getvalue())

    log.info("TTS: %d chars → %d bytes OGG", len(req.text), len(ogg_bytes))
    return Response(content=ogg_bytes, media_type="audio/ogg")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "stt": "ready" if _whisper else "loading",
        "tts": "ready" if _kokoro else "loading",
        "tts_voices": list(_kokoro.keys()),
        "device": _DEVICE,
        "whisper_model": _WHISPER_MODEL,
        "kokoro_voice": _KOKORO_VOICE,
    }


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8422)
