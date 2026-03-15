"""
Hive Mind -- Voice Server.

Provides STT (faster-whisper) and TTS (Chatterbox) over HTTP.
Chatterbox supports zero-shot voice cloning from a reference WAV clip.
Voice selection via voice_id parameter (maps to voice_ref/{voice_id}.wav).
Falls back to default.wav if the requested voice file does not exist.
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

import torch  # noqa: E402
import torchaudio  # noqa: E402
from fastapi import FastAPI, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402

log = logging.getLogger("hive-mind.voice")

app = FastAPI(title="Hive Mind Voice Server")

_DEVICE = "cuda" if _GPU_OK and torch.cuda.is_available() else "cpu"
_WHISPER_MODEL = os.getenv("WHISPER_MODEL", "medium")
_VOICE_REF_DIR = os.getenv("VOICE_REF_DIR", "/usr/src/app/voice_ref")

_whisper = None
_chatterbox_model = None


# ---------------------------------------------------------------------------
# Voice reference resolution
# ---------------------------------------------------------------------------
def _resolve_voice_ref(voice_id: str, voice_ref_dir: str | None = None) -> str | None:
    """Resolve a voice_id to the corresponding WAV file path.

    Returns the path to voice_ref/{voice_id}.wav if it exists,
    falls back to voice_ref/default.wav, or returns None if neither exists.
    """
    ref_dir = voice_ref_dir or _VOICE_REF_DIR
    path = os.path.join(ref_dir, f"{voice_id}.wav")
    if os.path.exists(path):
        return path
    fallback = os.path.join(ref_dir, "default.wav")
    if os.path.exists(fallback):
        return fallback
    return None


# ---------------------------------------------------------------------------
# Startup -- load all models once
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    global _whisper, _chatterbox_model

    log.info("Voice server starting on device: %s | TTS engine: chatterbox", _DEVICE)

    from faster_whisper import WhisperModel
    compute_type = "float16" if _DEVICE == "cuda" else "int8"
    log.info("Loading faster-whisper %s (%s)...", _WHISPER_MODEL, compute_type)
    _whisper = WhisperModel(_WHISPER_MODEL, device=_DEVICE, compute_type=compute_type)

    from chatterbox.tts import ChatterboxTTS
    log.info("Loading Chatterbox TTS...")
    _chatterbox_model = ChatterboxTTS.from_pretrained(device=_DEVICE)

    log.info(
        "Voice server ready. TTS: Chatterbox | ref dir: %s",
        _VOICE_REF_DIR,
    )


# ---------------------------------------------------------------------------
# TTS synthesis helper
# ---------------------------------------------------------------------------
def _synthesize(text: str, ref_path: str | None = None):
    """Synthesize text to a WAV tensor using Chatterbox.

    Args:
        text: The text to synthesize.
        ref_path: Optional path to a reference WAV for voice cloning.

    Returns:
        A torch tensor containing the audio waveform.
    """
    if _chatterbox_model is None:
        raise RuntimeError("TTS model not loaded")
    return _chatterbox_model.generate(text, audio_prompt_path=ref_path)


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
    voice_id: str = "default"
    speed: float = 0.9


@app.post("/tts")
async def tts(req: TTSRequest):
    """Synthesise text to OGG/Opus audio."""
    if _chatterbox_model is None:
        raise HTTPException(status_code=503, detail="TTS not ready")

    ref_path = _resolve_voice_ref(req.voice_id)
    wav = _synthesize(req.text, ref_path)

    wav_buf = io.BytesIO()
    torchaudio.save(wav_buf, wav, _chatterbox_model.sr, format="WAV")
    ogg_bytes = _wav_to_ogg(wav_buf.getvalue(), speed=req.speed)

    log.info("TTS (Chatterbox): %d chars -> %d bytes OGG", len(req.text), len(ogg_bytes))
    return Response(content=ogg_bytes, media_type="audio/ogg")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "stt": "ready" if _whisper else "loading",
        "tts": "ready" if _chatterbox_model else "loading",
        "tts_engine": "chatterbox",
        "voice_ref_dir": _VOICE_REF_DIR,
        "device": _DEVICE,
        "whisper_model": _WHISPER_MODEL,
    }


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8422)
