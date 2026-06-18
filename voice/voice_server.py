"""
Hive Mind -- Voice Server.

Provides STT (faster-whisper) and TTS over HTTP. The TTS engine is selected at
startup via the ``TTS_ENGINE`` env var:

- ``chatterbox`` (default) -- zero-shot voice cloning from a reference WAV clip.
  Voice selection via ``voice_id`` (maps to ``minds/{voice_id}/voice_ref.wav``).
- ``kokoro`` -- fast non-cloning engine for minds that don't need a cloned
  voice. ``voice_id`` maps to a Kokoro voice name via ``KOKORO_VOICE_MAP``,
  falling back to ``KOKORO_DEFAULT_VOICE``.

The STT half (faster-whisper) is shared by both engines. The two engines ship
in separate images (Dockerfile.voice / Dockerfile.voice.kokoro) so their model
stacks never collide; this single module serves both via the toggle.
Auto-detects CUDA; falls back to CPU gracefully.
"""

import io
import json
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

# TTS engine selection -- "chatterbox" (default, cloning) or "kokoro" (fast, non-cloning)
_TTS_ENGINE = os.getenv("TTS_ENGINE", "chatterbox").lower()
_KOKORO_LANG = os.getenv("KOKORO_LANG", "a")  # 'a' = American English
_KOKORO_DEFAULT_VOICE = os.getenv("KOKORO_DEFAULT_VOICE", "af_heart")
_KOKORO_SR = 24000  # Kokoro's native output sample rate

_whisper = None
_chatterbox_model = None
_kokoro_pipeline = None


# ---------------------------------------------------------------------------
# Voice reference resolution
# ---------------------------------------------------------------------------
_MINDS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "minds")
_MIND_ID_TO_NAME: dict[str, str] = {}


def _load_mind_id_map() -> dict[str, str]:
    """Build {mind_id (UUID) -> short_name} by scanning minds/*/runtime.yaml.
    Lets the voice server resolve a UUID voice_id back to the on-disk folder
    name when callers (post agent_id→mind_id rename) pass the UUID.
    """
    mapping: dict[str, str] = {}
    if not os.path.isdir(_MINDS_DIR):
        return mapping
    for short_name in os.listdir(_MINDS_DIR):
        rt = os.path.join(_MINDS_DIR, short_name, "runtime.yaml")
        if not os.path.isfile(rt):
            continue
        try:
            with open(rt) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("mind_id:"):
                        mid = line.split(":", 1)[1].strip().strip('"').strip("'")
                        if mid:
                            mapping[mid] = short_name
                        break
        except OSError:
            continue
    return mapping


def _resolve_voice_ref(voice_id: str) -> str | None:
    """Resolve a voice_id to minds/{short_name}/voice_ref.wav.

    Accepts either the short name ("ada") or the canonical mind_id (UUID).
    Short-name lookup is tried first; if that misses, we fall through to a
    UUID→short-name map built from each mind's runtime.yaml.
    """
    direct = os.path.join(_MINDS_DIR, voice_id, "voice_ref.wav")
    if os.path.exists(direct):
        return direct

    short = _MIND_ID_TO_NAME.get(voice_id)
    if short is None:
        # Lazy refresh: a mind may have been added since startup.
        _MIND_ID_TO_NAME.update(_load_mind_id_map())
        short = _MIND_ID_TO_NAME.get(voice_id)
    if short:
        by_id = os.path.join(_MINDS_DIR, short, "voice_ref.wav")
        if os.path.exists(by_id):
            return by_id
    return None


# ---------------------------------------------------------------------------
# Kokoro voice resolution
# ---------------------------------------------------------------------------
_KOKORO_VOICE_MAP: dict[str, str] = {}


def _load_kokoro_voice_map() -> dict[str, str]:
    """Build {voice_id -> kokoro_voice_name} from the KOKORO_VOICE_MAP env var.

    The env value is a JSON object mapping each mind's voice_id (short name or
    UUID) to a Kokoro voice name (e.g. ``{"ada": "af_bella"}``). Returns an
    empty map if unset or malformed.
    """
    raw = os.getenv("KOKORO_VOICE_MAP", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("KOKORO_VOICE_MAP is not valid JSON; ignoring")
        return {}
    if not isinstance(data, dict):
        log.warning("KOKORO_VOICE_MAP is not a JSON object; ignoring")
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _resolve_kokoro_voice(voice_id: str) -> str:
    """Map a voice_id to a Kokoro voice name.

    Falls back to KOKORO_DEFAULT_VOICE when unmapped. Unlike Chatterbox, Kokoro
    takes an explicit voice string on every call, so there is no last-used-clip
    cache and no cross-mind voice bleed to guard against -- a sane default is safe.
    """
    return _KOKORO_VOICE_MAP.get(voice_id, _KOKORO_DEFAULT_VOICE)


# ---------------------------------------------------------------------------
# Startup -- load all models once
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    global _whisper, _chatterbox_model, _kokoro_pipeline

    log.info("Voice server starting on device: %s | TTS engine: %s", _DEVICE, _TTS_ENGINE)

    from faster_whisper import WhisperModel
    compute_type = "float16" if _DEVICE == "cuda" else "int8"
    log.info("Loading faster-whisper %s (%s)...", _WHISPER_MODEL, compute_type)
    _whisper = WhisperModel(_WHISPER_MODEL, device=_DEVICE, compute_type=compute_type)

    if _TTS_ENGINE == "kokoro":
        from kokoro import KPipeline
        log.info("Loading Kokoro TTS (lang=%s)...", _KOKORO_LANG)
        _kokoro_pipeline = KPipeline(lang_code=_KOKORO_LANG)
        _KOKORO_VOICE_MAP.update(_load_kokoro_voice_map())
        log.info(
            "Voice server ready. TTS: Kokoro | default voice: %s | voice map entries: %d",
            _KOKORO_DEFAULT_VOICE, len(_KOKORO_VOICE_MAP),
        )
    else:
        from chatterbox.tts import ChatterboxTTS
        log.info("Loading Chatterbox TTS...")
        _chatterbox_model = ChatterboxTTS.from_pretrained(device=_DEVICE)
        _MIND_ID_TO_NAME.update(_load_mind_id_map())
        log.info("Voice server ready. TTS: Chatterbox | known mind_ids: %d", len(_MIND_ID_TO_NAME))


def _tts_ready() -> bool:
    """Whether the active TTS engine's model is loaded."""
    if _TTS_ENGINE == "kokoro":
        return _kokoro_pipeline is not None
    return _chatterbox_model is not None


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


def _synthesize_kokoro(text: str, voice: str):
    """Synthesize text to a WAV tensor using Kokoro.

    Kokoro does its own internal sentence chunking and yields one audio segment
    per chunk; we concatenate them along the time axis. Returns a
    ``(channels, time)`` tensor ready for :func:`torchaudio.save`.
    """
    if _kokoro_pipeline is None:
        raise RuntimeError("TTS model not loaded")
    segments = []
    for _, _, audio in _kokoro_pipeline(text, voice=voice):
        segments.append(audio if torch.is_tensor(audio) else torch.from_numpy(audio))
    if not segments:
        raise RuntimeError("Kokoro produced no audio")
    wav = torch.cat(segments, dim=-1)
    return wav.unsqueeze(0) if wav.dim() == 1 else wav


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
    if not _tts_ready():
        raise HTTPException(status_code=503, detail="TTS not ready")

    text = _strip_markdown(req.text)

    if _TTS_ENGINE == "kokoro":
        voice = _resolve_kokoro_voice(req.voice_id)
        wav = _synthesize_kokoro(text, voice)
        sample_rate = _KOKORO_SR
        engine_label = "Kokoro"
    else:
        ref_path = _resolve_voice_ref(req.voice_id)
        if ref_path is None:
            # Chatterbox keeps the last-used reference clip cached; passing None
            # silently reuses the previous callers voice. Fail loud at the
            # boundary so cross-mind voice bleed is impossible.
            log.warning("TTS voice_ref not found for voice_id=%r", req.voice_id)
            raise HTTPException(
                status_code=400,
                detail=f"voice_ref not found for voice_id={req.voice_id!r}",
            )
        wav = _synthesize_chunked(text, ref_path)
        sample_rate = _chatterbox_model.sr
        engine_label = "Chatterbox"

    wav_buf = io.BytesIO()
    torchaudio.save(wav_buf, wav, sample_rate, format="WAV")
    ogg_bytes = _wav_to_ogg(wav_buf.getvalue(), speed=req.speed)

    log.info("TTS (%s): %d chars -> %d bytes OGG", engine_label, len(req.text), len(ogg_bytes))
    return Response(content=ogg_bytes, media_type="audio/ogg")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "stt": "ready" if _whisper else "loading",
        "tts": "ready" if _tts_ready() else "loading",
        "tts_engine": _TTS_ENGINE,
        "device": _DEVICE,
        "whisper_model": _WHISPER_MODEL,
    }


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8422)
