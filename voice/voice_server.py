"""
Hive Mind -- Voice Server.

Provides STT (faster-whisper) and TTS (Chatterbox) over HTTP.
Chatterbox supports zero-shot voice cloning from a reference WAV clip.
Voice selection via voice_id parameter (maps to minds/{voice_id}/voice_ref.wav).
Auto-detects CUDA; falls back to CPU gracefully.
"""

import io
import logging
import os
import re
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

_whisper = None
_chatterbox_model = None


# ---------------------------------------------------------------------------
# Voice reference resolution
# ---------------------------------------------------------------------------
def _resolve_voice_ref(voice_id: str) -> str | None:
    """Resolve a voice_id to minds/{voice_id}/voice_ref.wav."""
    mind_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "minds", voice_id, "voice_ref.wav")
    if os.path.exists(mind_path):
        return mind_path
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

    log.info("Voice server ready. TTS: Chatterbox")


# ---------------------------------------------------------------------------
# Markdown stripping for TTS
# ---------------------------------------------------------------------------
def _strip_markdown(text: str) -> str:
    """Remove markdown formatting so TTS reads clean prose, not syntax."""
    # Fenced code blocks — drop entirely (reading code aloud is useless)
    text = re.sub(r"```[\s\S]*?```", "", text)
    # Inline code
    text = re.sub(r"`[^`]+`", "", text)
    # Markdown links — keep display text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Headers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Bold / italic (**, *, __, _)
    text = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_\n]+)_{1,3}", r"\1", text)
    # Bullet list markers
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    # Numbered list markers
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    # Em-dash / en-dash → comma pause
    text = text.replace("—", ", ").replace("–", ", ")
    # Bare URLs
    text = re.sub(r"https?://\S+", "", text)
    # Collapse whitespace
    text = re.sub(r"\n+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Sentence splitting for chunked TTS
# ---------------------------------------------------------------------------
_ABBREVIATIONS = frozenset({
    "Mr", "Mrs", "Ms", "Dr", "St", "Jr", "Sr", "vs", "etc",
    "Prof", "Gen", "Sgt", "Col", "Lt", "Capt", "Rev", "Vol",
    "Dept", "Est", "Inc", "Ltd", "Corp", "Co", "approx",
    "e.g", "i.e",
})

# Split after sentence-ending punctuation (including ellipsis) followed by
# whitespace.  The captured group keeps the punctuation attached to the
# preceding chunk during rejoin.
_SENTENCE_SPLIT_RE = re.compile(
    r"((?:\.{3}|[.!?]))"  # group 1: terminal punctuation (ellipsis or single)
    r"\s+",                # followed by whitespace (consumed, not kept)
)


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences at boundary punctuation.

    Splits on sentence-ending punctuation (.!?) followed by whitespace, while
    preserving common abbreviations (Dr., Mr., etc.).  Returns ``[text]`` as a
    single-element list if no splits are found or if input is empty/whitespace.
    """
    if not text or not text.strip():
        return [text]

    # re.split with a capture group returns [before, sep, after, sep, ...]
    parts = _SENTENCE_SPLIT_RE.split(text)

    # Rejoin: attach each captured punctuation to the preceding text fragment
    chunks: list[str] = []
    i = 0
    while i < len(parts):
        segment = parts[i]
        # If next element is a captured punctuation group, attach it
        if i + 1 < len(parts) and re.fullmatch(r"(?:\.{3}|[.!?])", parts[i + 1]):
            segment += parts[i + 1]
            i += 2
        else:
            i += 1
        if segment:
            chunks.append(segment)

    # Rejoin chunks that were split on abbreviation periods
    merged: list[str] = []
    for chunk in chunks:
        # Check if previous chunk ends with an abbreviation period
        if merged and merged[-1].endswith("."):
            last_word = merged[-1].rstrip(".").rsplit(None, 1)[-1] if merged[-1].rstrip(".") else ""
            if last_word in _ABBREVIATIONS:
                merged[-1] = merged[-1] + " " + chunk
                continue
        merged.append(chunk)

    return merged if merged else [text]


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
# Chunked TTS synthesis
# ---------------------------------------------------------------------------
def _synthesize_chunked(text: str, ref_path: str | None = None):
    """Synthesize text in sentence-sized chunks and concatenate the results.

    Splits *text* into sentences, synthesizes each independently via
    :func:`_synthesize`, and concatenates the resulting tensors along the time
    axis (``dim=-1``).  If only one sentence is detected, it delegates directly
    to :func:`_synthesize` without concatenation overhead.

    Falls back to a single-call ``_synthesize(text, ref_path)`` if any step in
    the chunked pipeline fails.
    """
    # Let RuntimeError from model-not-loaded propagate immediately
    if _chatterbox_model is None:
        raise RuntimeError("TTS model not loaded")

    chunks = _split_sentences(text)

    if len(chunks) == 1:
        return _synthesize(text, ref_path)

    try:
        tensors = []
        for chunk in chunks:
            tensors.append(_synthesize(chunk, ref_path))
        log.info("TTS chunked: %d sentences", len(chunks))
        return torch.cat(tensors, dim=-1)
    except Exception:
        log.warning(
            "Chunked synthesis failed for %d chunks; falling back to single call",
            len(chunks),
            exc_info=True,
        )
        return _synthesize(text, ref_path)


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
    global _whisper
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
        try:
            segments, _ = _whisper.transcribe(tmp_path, language="en")
            text = " ".join(s.text for s in segments).strip()
        except RuntimeError as exc:
            if "CUDA" not in str(exc):
                raise
            log.warning("CUDA error in STT — reinitialising whisper on CPU: %s", exc)
            from faster_whisper import WhisperModel
            _whisper = WhisperModel(_WHISPER_MODEL, device="cpu", compute_type="int8")
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
    wav = _synthesize_chunked(_strip_markdown(req.text), ref_path)

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
        "device": _DEVICE,
        "whisper_model": _WHISPER_MODEL,
    }


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8422)
