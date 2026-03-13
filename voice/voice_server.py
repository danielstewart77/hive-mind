"""
Hive Mind -- Voice Server.

Provides STT (faster-whisper) and TTS (XTTS v2, Coqui) over HTTP.
XTTS v2 supports zero-shot voice cloning from a reference WAV clip.
Falls back to a stock speaker if no reference audio is available.
Auto-detects CUDA; falls back to CPU gracefully.
"""

import io
import logging
import os
import subprocess
import tempfile

# Check GPU compatibility BEFORE importing torch -- once torch initializes
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
                "GPU compute capability %d.%d < 7.0 -- disabling CUDA",
                major.value, minor.value,
            )
            os.environ["CUDA_VISIBLE_DEVICES"] = ""
            return False
        return True
    except Exception:
        return False

_GPU_OK = _check_gpu_early()

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import torch  # noqa: E402
from fastapi import FastAPI, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402

log = logging.getLogger("hive-mind.voice")

app = FastAPI(title="Hive Mind Voice Server")

_DEVICE = "cuda" if _GPU_OK and torch.cuda.is_available() else "cpu"
_WHISPER_MODEL = os.getenv("WHISPER_MODEL", "medium")
_XTTS_REF_AUDIO = os.getenv("XTTS_REF_AUDIO", "/usr/src/app/voice_ref/hive_mind_voice.wav")
_XTTS_LANGUAGE = os.getenv("XTTS_LANGUAGE", "en")

_whisper = None
_xtts_model = None
_use_voice_cloning = False


# ---------------------------------------------------------------------------
# Startup -- load all models once
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    global _whisper, _xtts_model, _use_voice_cloning

    log.info("Voice server starting on device: %s | TTS engine: xtts_v2", _DEVICE)

    from faster_whisper import WhisperModel
    compute_type = "float16" if _DEVICE == "cuda" else "int8"
    log.info("Loading faster-whisper %s (%s)...", _WHISPER_MODEL, compute_type)
    _whisper = WhisperModel(_WHISPER_MODEL, device=_DEVICE, compute_type=compute_type)

    from TTS.api import TTS as CoquiTTS
    log.info("Loading XTTS v2 model...")
    _xtts_model = CoquiTTS(model_name="tts_models/multilingual/multi-dataset/xtts_v2").to(_DEVICE)

    _use_voice_cloning = os.path.exists(_XTTS_REF_AUDIO)
    ref_info = _XTTS_REF_AUDIO if _use_voice_cloning else "no reference (stock voice: Claribel Dervla)"
    log.info("Voice server ready. TTS: XTTS v2 | voice cloning: %s | ref: %s", _use_voice_cloning, ref_info)


# ---------------------------------------------------------------------------
# TTS synthesis helper
# ---------------------------------------------------------------------------
def _synthesize(text: str) -> np.ndarray:
    """Synthesize text to a numpy audio array using XTTS v2."""
    if _xtts_model is None:
        raise RuntimeError("TTS model not loaded")
    if _use_voice_cloning:
        audio = _xtts_model.tts_with_vc(
            text=text,
            speaker_wav=_XTTS_REF_AUDIO,
            language=_XTTS_LANGUAGE,
        )
    else:
        audio = _xtts_model.tts(
            text=text,
            speaker="Claribel Dervla",
            language=_XTTS_LANGUAGE,
        )
    return np.array(audio, dtype=np.float32)


# ---------------------------------------------------------------------------
# Audio conversion helpers
# ---------------------------------------------------------------------------
def _ogg_to_wav(ogg_bytes: bytes) -> bytes:
    """Convert OGG/Opus -> 16kHz mono WAV (Whisper expects this)."""
    result = subprocess.run(
        ["ffmpeg", "-i", "pipe:0", "-f", "wav", "-ar", "16000", "-ac", "1", "pipe:1"],
        input=ogg_bytes,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg OGG->WAV: {result.stderr.decode()}")
    return result.stdout


def _wav_to_ogg(wav_bytes: bytes, speed: float = 1.0) -> bytes:
    """Convert WAV -> OGG/Opus (Telegram voice note format). Applies atempo if speed != 1.0."""
    cmd = ["ffmpeg", "-i", "pipe:0"]
    if speed != 1.0:
        cmd += ["-af", f"atempo={speed:.3f}"]
    cmd += ["-c:a", "libopus", "-b:a", "64k", "-f", "ogg", "pipe:1"]
    result = subprocess.run(cmd, input=wav_bytes, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg WAV->OGG: {result.stderr.decode()}")
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
    if _xtts_model is None:
        raise HTTPException(status_code=503, detail="TTS not ready")

    audio_array = _synthesize(req.text)

    wav_buf = io.BytesIO()
    sf.write(wav_buf, audio_array, 24000, format="WAV")
    ogg_bytes = _wav_to_ogg(wav_buf.getvalue(), speed=req.speed)

    log.info("TTS (XTTS v2): %d chars -> %d bytes OGG", len(req.text), len(ogg_bytes))
    return Response(content=ogg_bytes, media_type="audio/ogg")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "stt": "ready" if _whisper else "loading",
        "tts": "ready" if _xtts_model else "loading",
        "tts_engine": "xtts_v2",
        "ref_audio": _XTTS_REF_AUDIO,
        "voice_cloning": _use_voice_cloning,
        "device": _DEVICE,
        "whisper_model": _WHISPER_MODEL,
    }


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8422)
