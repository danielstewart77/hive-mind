"""
Hive Mind — Voice Server.

Provides STT (faster-whisper) and TTS over HTTP.
TTS engine priority: F5-TTS (voice cloning) → Kokoro (fallback).
All models load at startup and stay resident.
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
_F5_REF_AUDIO = os.getenv("F5_REF_AUDIO", "/usr/src/app/voice_ref/hive_mind_voice.wav")
_F5_REF_TEXT = os.getenv("F5_REF_TEXT", "/usr/src/app/voice_ref/hive_mind_voice.txt")
_USE_F5TTS = os.getenv("USE_F5TTS", "1").lower() in ("1", "true", "yes")

_whisper = None
_kokoro: dict = {}  # lang_code -> KPipeline
_f5tts = None
_f5_ref_text: str = ""


def _pipeline_for(voice: str):
    """Return the KPipeline for the given voice name (routes by prefix)."""
    lang = "b" if voice.startswith("b") else "a"
    if lang not in _kokoro:
        raise HTTPException(status_code=503, detail=f"Kokoro pipeline '{lang}' not ready")
    return _kokoro[lang]


# ---------------------------------------------------------------------------
# Startup — load all models once
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    global _whisper, _kokoro, _f5tts, _f5_ref_text

    log.info("Voice server starting on device: %s", _DEVICE)

    from faster_whisper import WhisperModel
    compute_type = "float16" if _DEVICE == "cuda" else "int8"
    log.info("Loading faster-whisper %s (%s)...", _WHISPER_MODEL, compute_type)
    _whisper = WhisperModel(_WHISPER_MODEL, device=_DEVICE, compute_type=compute_type)

    from kokoro import KPipeline
    log.info("Loading Kokoro v1.0 (American + British)...")
    _kokoro["a"] = KPipeline(lang_code="a", device=_DEVICE)
    _kokoro["b"] = KPipeline(lang_code="b", device=_DEVICE)

    if _USE_F5TTS and os.path.exists(_F5_REF_AUDIO):
        try:
            from f5_tts.api import F5TTS
            log.info("Loading F5-TTS (voice cloning)...")
            _f5tts = F5TTS(device=_DEVICE)
            # Load reference transcript
            ref_text_path = _F5_REF_TEXT
            if os.path.exists(ref_text_path):
                with open(ref_text_path) as f:
                    _f5_ref_text = f.read().strip()
            log.info("F5-TTS ready. Reference: %s (%d chars)", _F5_REF_AUDIO, len(_f5_ref_text))
        except Exception as e:
            log.warning("F5-TTS failed to load (%s) — falling back to Kokoro only", e)
            _f5tts = None
    else:
        if _USE_F5TTS:
            log.warning("F5_REF_AUDIO not found at %s — F5-TTS disabled", _F5_REF_AUDIO)

    log.info("Voice server ready. TTS engine: %s", "F5-TTS + Kokoro fallback" if _f5tts else "Kokoro")


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
    voice: str = "f5"   # "f5" = F5-TTS (default), anything else = Kokoro voice name
    speed: float = 1.0


@app.post("/tts")
async def tts(req: TTSRequest):
    """Synthesise text to OGG/Opus audio."""

    # F5-TTS path
    if req.voice == "f5" and _f5tts is not None:
        try:
            wav_array, sr, _ = _f5tts.infer(
                ref_file=_F5_REF_AUDIO,
                ref_text=_f5_ref_text,
                gen_text=req.text,
            )
            log.info("F5 debug: sr=%d shape=%s dtype=%s", sr,
                     getattr(wav_array, 'shape', 'n/a'),
                     getattr(wav_array, 'dtype', 'n/a'))
            import numpy as _np
            arr = wav_array
            if hasattr(arr, 'numpy'):
                arr = arr.numpy()
            arr = _np.squeeze(arr).astype(_np.float32)
            wav_buf = io.BytesIO()
            sf.write(wav_buf, arr, sr, format="WAV")
            ogg_bytes = _wav_to_ogg(wav_buf.getvalue())
            log.info("TTS (F5): %d chars → %d bytes OGG", len(req.text), len(ogg_bytes))
            return Response(content=ogg_bytes, media_type="audio/ogg")
        except Exception as e:
            log.warning("F5-TTS inference failed (%s) — falling back to Kokoro", e)

    # Kokoro fallback
    if not _kokoro:
        raise HTTPException(status_code=503, detail="TTS not ready")

    kokoro_voice = _KOKORO_VOICE if req.voice == "f5" else req.voice
    pipeline = _pipeline_for(kokoro_voice)
    samples = []
    for _, _, audio in pipeline(req.text, voice=kokoro_voice, speed=req.speed):
        samples.append(audio)

    if not samples:
        raise HTTPException(status_code=500, detail="TTS produced no audio")

    audio_array = np.concatenate(samples)
    wav_buf = io.BytesIO()
    sf.write(wav_buf, audio_array, 24000, format="WAV")
    ogg_bytes = _wav_to_ogg(wav_buf.getvalue())

    log.info("TTS (Kokoro/%s): %d chars → %d bytes OGG", kokoro_voice, len(req.text), len(ogg_bytes))
    return Response(content=ogg_bytes, media_type="audio/ogg")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "stt": "ready" if _whisper else "loading",
        "tts": "ready" if (_f5tts or _kokoro) else "loading",
        "tts_engine": "f5+kokoro" if _f5tts else "kokoro",
        "tts_voices": list(_kokoro.keys()),
        "f5_ref_audio": _F5_REF_AUDIO if _f5tts else None,
        "device": _DEVICE,
        "whisper_model": _WHISPER_MODEL,
        "kokoro_voice": _KOKORO_VOICE,
    }


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8422)
