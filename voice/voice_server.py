"""
Hive Mind — Voice Server.

Provides STT (faster-whisper) and TTS over HTTP.
TTS backend is switchable via TTS_BACKEND env var:
  - "fish"  → Fish Speech (separate container, HTTP client)
  - "bark"  → Bark (local, neural, fallback)
All models load at startup and stay resident.
Auto-detects CUDA; falls back to CPU gracefully.
"""

import base64
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
_TTS_BACKEND = os.getenv("TTS_BACKEND", "fish")       # "fish" or "bark"
_BARK_SPEAKER = os.getenv("BARK_SPEAKER", "v2/en_speaker_9")
_FISH_URL = os.getenv("FISH_SPEECH_URL", "http://fish-speech:8080")
_FISH_REF_AUDIO = os.getenv("FISH_REF_AUDIO", "/usr/src/app/voice_ref/hive_mind_voice.wav")
_FISH_REF_TEXT = os.getenv("FISH_REF_TEXT", "/usr/src/app/voice_ref/hive_mind_voice.txt")

_whisper = None
_bark_loaded = False
_bark_sample_rate = 24000
_fish_ref_audio_b64: str | None = None
_fish_ref_text: str | None = None


# ---------------------------------------------------------------------------
# Startup — load all models once
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    global _whisper, _bark_loaded, _bark_sample_rate, _fish_ref_audio_b64, _fish_ref_text

    log.info("Voice server starting on device: %s | TTS backend: %s", _DEVICE, _TTS_BACKEND)

    from faster_whisper import WhisperModel
    compute_type = "float16" if _DEVICE == "cuda" else "int8"
    log.info("Loading faster-whisper %s (%s)...", _WHISPER_MODEL, compute_type)
    _whisper = WhisperModel(_WHISPER_MODEL, device=_DEVICE, compute_type=compute_type)

    if _TTS_BACKEND == "fish":
        if os.path.exists(_FISH_REF_AUDIO):
            with open(_FISH_REF_AUDIO, "rb") as f:
                _fish_ref_audio_b64 = base64.b64encode(f.read()).decode()
            log.info("Fish Speech: loaded reference audio from %s", _FISH_REF_AUDIO)
        else:
            log.warning("Fish Speech: reference audio not found at %s", _FISH_REF_AUDIO)

        if os.path.exists(_FISH_REF_TEXT):
            with open(_FISH_REF_TEXT, "r") as f:
                _fish_ref_text = f.read().strip()
            log.info("Fish Speech: loaded reference text (%d chars)", len(_fish_ref_text))
        else:
            log.warning("Fish Speech: reference text not found at %s", _FISH_REF_TEXT)

        log.info("Voice server ready. TTS: Fish Speech at %s", _FISH_URL)

    else:
        if _DEVICE == "cuda":
            os.environ["SUNO_USE_SMALL_MODELS"] = "0"
            os.environ["SUNO_OFFLOAD_CPU"] = "0"
        else:
            os.environ["SUNO_USE_SMALL_MODELS"] = "1"

        from bark import preload_models, SAMPLE_RATE
        log.info("Loading Bark models (speaker: %s)...", _BARK_SPEAKER)
        preload_models()
        _bark_sample_rate = SAMPLE_RATE
        _bark_loaded = True
        log.info("Voice server ready. TTS: Bark at %dHz", _bark_sample_rate)


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


def _wav_to_ogg(wav_bytes: bytes, speed: float = 1.0) -> bytes:
    """Convert WAV → OGG/Opus (Telegram voice note format). Applies atempo if speed != 1.0."""
    cmd = ["ffmpeg", "-i", "pipe:0"]
    if speed != 1.0:
        cmd += ["-af", f"atempo={speed:.3f}"]
    cmd += ["-c:a", "libopus", "-b:a", "64k", "-f", "ogg", "pipe:1"]
    result = subprocess.run(cmd, input=wav_bytes, capture_output=True)
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
    voice: str = "default"
    speed: float = 0.9


@app.post("/tts")
async def tts(req: TTSRequest):
    """Synthesise text to OGG/Opus audio."""
    if _TTS_BACKEND == "fish":
        import aiohttp
        payload: dict = {"text": req.text, "format": "wav", "streaming": False}
        if _fish_ref_audio_b64 and _fish_ref_text:
            payload["references"] = [{"audio": _fish_ref_audio_b64, "text": _fish_ref_text}]

        async with aiohttp.ClientSession() as session:
            async with session.post(f"{_FISH_URL}/v1/tts", json=payload, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise HTTPException(status_code=502, detail=f"Fish Speech error {resp.status}: {body}")
                wav_bytes = await resp.read()

        ogg_bytes = _wav_to_ogg(wav_bytes, speed=req.speed)
        log.info("TTS (Fish Speech): %d chars → %d bytes OGG", len(req.text), len(ogg_bytes))
        return Response(content=ogg_bytes, media_type="audio/ogg")

    else:
        if not _bark_loaded:
            raise HTTPException(status_code=503, detail="TTS not ready")

        from bark import generate_audio
        audio_array = generate_audio(req.text, history_prompt=_BARK_SPEAKER)

        wav_buf = io.BytesIO()
        sf.write(wav_buf, audio_array, _bark_sample_rate, format="WAV")
        ogg_bytes = _wav_to_ogg(wav_buf.getvalue(), speed=req.speed)

        log.info("TTS (Bark/%s): %d chars → %d bytes OGG", _BARK_SPEAKER, len(req.text), len(ogg_bytes))
        return Response(content=ogg_bytes, media_type="audio/ogg")


# ---------------------------------------------------------------------------
# Backend switch endpoint (live, no restart needed)
# ---------------------------------------------------------------------------
class BackendRequest(BaseModel):
    backend: str          # "fish" or "bark"
    ref_audio_path: str | None = None   # override Fish ref audio path
    ref_text_path: str | None = None    # override Fish ref text path


@app.post("/backend")
async def set_backend(req: BackendRequest):
    """Switch TTS backend at runtime without restarting."""
    global _TTS_BACKEND, _fish_ref_audio_b64, _fish_ref_text, _bark_loaded, _bark_sample_rate

    if req.backend not in ("fish", "bark"):
        raise HTTPException(status_code=400, detail="backend must be 'fish' or 'bark'")

    if req.backend == "fish":
        audio_path = req.ref_audio_path or _FISH_REF_AUDIO
        text_path = req.ref_text_path or _FISH_REF_TEXT
        if os.path.exists(audio_path):
            with open(audio_path, "rb") as f:
                _fish_ref_audio_b64 = base64.b64encode(f.read()).decode()
        if os.path.exists(text_path):
            with open(text_path, "r") as f:
                _fish_ref_text = f.read().strip()

    elif req.backend == "bark" and not _bark_loaded:
        if _DEVICE == "cuda":
            os.environ["SUNO_USE_SMALL_MODELS"] = "0"
            os.environ["SUNO_OFFLOAD_CPU"] = "0"
        else:
            os.environ["SUNO_USE_SMALL_MODELS"] = "1"
        from bark import preload_models, SAMPLE_RATE
        log.info("Loading Bark models on backend switch...")
        preload_models()
        _bark_sample_rate = SAMPLE_RATE
        _bark_loaded = True

    _TTS_BACKEND = req.backend
    log.info("TTS backend switched to: %s", _TTS_BACKEND)
    return {"backend": _TTS_BACKEND, "status": "ok"}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "stt": "ready" if _whisper else "loading",
        "tts": "ready" if (_TTS_BACKEND == "fish" or _bark_loaded) else "loading",
        "tts_engine": _TTS_BACKEND,
        "fish_url": _FISH_URL if _TTS_BACKEND == "fish" else None,
        "bark_speaker": _BARK_SPEAKER if _TTS_BACKEND == "bark" else None,
        "device": _DEVICE,
        "whisper_model": _WHISPER_MODEL,
    }


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8422)
