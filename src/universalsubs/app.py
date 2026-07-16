"""
UniversalSubs — Live translated captions over any game/app (v2)

Two engines:

  STREAMING (Deepgram + Gemini)  <- live captions that appear WHILE someone
    talks and revise themselves as more words arrive.
    Audio -> Deepgram websocket (interim + finalized transcript fragments)
          -> Gemini retranslates the utterance-so-far on each update
          -> overlay replaces the caption line in place.
    Needs a free Deepgram API key (deepgram.com, $200 credit, no card).

  CHUNKED (Gemini only)  <- v1 behavior. Caption appears after each sentence.
    Audio -> energy VAD -> Gemini (audio in, transcribe+translate in one call).
    Needs only the Gemini key.

Windows only. Games must run in BORDERLESS WINDOWED mode.
Deps: pip install pyaudiowpatch numpy requests websocket-client
"""

import base64
import ctypes
import io
import json
import os
import queue
import sys
import threading
import time
import wave
from collections import deque
from urllib.parse import urlencode

import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np
import requests

try:
    import pyaudiowpatch as pyaudio
    HAS_PAW = True
except ImportError:
    HAS_PAW = False

try:
    import websocket  # websocket-client
    HAS_WS = True
except ImportError:
    HAS_WS = False

try:
    import keyring
    HAS_KEYRING = True
except ImportError:
    HAS_KEYRING = False

try:
    from proctap import ProcessAudioCapture   # v1.x: fixed 48k stereo float32
    HAS_PROCTAP = True
except ImportError:
    HAS_PROCTAP = False

try:
    from pycaw.pycaw import AudioUtilities
    HAS_PYCAW = True
except ImportError:
    HAS_PYCAW = False

# ── CONSTANTS ─────────────────────────────────────────────────────
APP_NAME = "UniversalSubs"
try:
    from universalsubs import __version__ as APP_VERSION
except ImportError:      # running app.py as a loose script
    APP_VERSION = "0.9.2"
def _config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "UniversalSubs")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.expanduser("~")
    return d

CONFIG_PATH = os.path.join(_config_dir(), "universalsubs_config.json")
_LEGACY_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "universalsubs_config.json")
if not os.path.exists(CONFIG_PATH) and os.path.exists(_LEGACY_CONFIG):
    try:
        import shutil
        shutil.copy(_LEGACY_CONFIG, CONFIG_PATH)   # migrate old location once
    except Exception:
        pass

TARGET_SR = 16000
FRAME_MS  = 30
FRAME_LEN = TARGET_SR * FRAME_MS // 1000

# VAD tuning (chunked mode)
VAD_START_FRAMES  = 3
VAD_END_SILENCE_S = 0.5
VAD_PREROLL_S     = 0.36
VAD_MIN_UTTER_S   = 0.4
VAD_MAX_UTTER_S   = 7.0
VAD_ABS_FLOOR     = 0.006
VAD_SNR_MULT      = 3.0

# Gemini free-tier guardrails
RPM_LIMITS   = {"gemini-3.1-flash-lite": 28, "gemini-3.5-flash": 13}
MAX_INFLIGHT = 2
NO_SPEECH_TOKEN = "NO_SPEECH"

# Deepgram streaming
DG_URL_BASE = "wss://api.deepgram.com/v1/listen"
DG_PARAMS = {
    "model": "nova-3",
    "encoding": "linear16",
    "sample_rate": TARGET_SR,
    "channels": 1,
    "interim_results": "true",
    "smart_format": "true",
    "endpointing": "400",
    "utterance_end_ms": "1200",
    "diarize": "true",
}
LIVE_STALE_COMMIT_S  = 3.0    # commit a live caption if no updates for this long
DANMAKU_FINALIZE_S   = 2.5    # danmaku mode: force Deepgram to finalize pending
                              # speech this often so fragments appear promptly
MIN_PARTIAL_GAP_S    = 2.2    # throttle partial retranslations (free-tier RPM)

# VAD gating (streaming mode): only stream audio while sound is detected.
GATE_PREROLL_S   = 0.4    # audio replayed when the gate opens (don't clip words)
GATE_HANGOVER_S  = 1.5    # keep gate open after sound stops (mid-sentence pauses)
GATE_KEEPALIVE_S = 5.0    # ping Deepgram while gate is closed (hold connection)
GATE_MAX_CONT_S  = 60.0   # continuous sound this long = probably music/media,
                          # not conversation -> close gate, warn user
# After a music trip the gate LOCKS: it reopens on a real conversational
# pause (music never pauses, speech does). Safety valve: the lock also
# auto-expires so speech OVER continuous background music is never lost
# forever — worst case, captions return in bursts.
GATE_LOCK_MAX_S = 180.0   # lock auto-expiry

# Speaker colors (consistent per Deepgram speaker index; index None = default)
SPEAKER_COLORS = [
    "#f56b6b",  # S1  red,
    "#f5a35c",  # S2  orange,
    "#f2e35c",  # S3  yellow,
    "#b8f05c",  # S4  lime,
    "#6ee06e",  # S5  green,
    "#5ce8b0",  # S6  teal,
    "#5cdcf2",  # S7  cyan,
    "#6da4f7",  # S8  azure,
    "#d9b38c",  # S9  tan,
    "#b984f7",  # S10 violet,
    "#ee6ded",  # S11 magenta,
    "#f97ba3",  # S12 rose,
]

def speaker_color(idx):
    if idx is None:
        return CAPTION_FG
    return SPEAKER_COLORS[idx % len(SPEAKER_COLORS)]

# Overlay look
TRANS_KEY   = "#010101"
CAPTION_BG  = "#141414"
CAPTION_FG  = "#f2f0ec"
CAPTION_LIVE_FG = "#c9c6c0"          # live (still-revising) line, slightly muted
CAPTION_FONT = "Microsoft YaHei UI"
CAPTION_HIDE_AFTER_S = 7

# Control panel look
BG, SURFACE, SURFACE2 = "#0f0e0d", "#1c1b19", "#242321"
ACCENT, ACCENT_RED    = "#4f98a3", "#dd6974"
TEXT, TEXT_MUTED      = "#e2e0dc", "#7a7875"


# ── CONFIG ────────────────────────────────────────────────────────
def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Config save failed: {e}")


# ── SECRETS (Windows Credential Manager via keyring) ─────────────
KEYRING_SERVICE = "UniversalSubs"

def get_secret(name):
    if HAS_KEYRING:
        try:
            return keyring.get_password(KEYRING_SERVICE, name) or ""
        except Exception:
            pass
    return ""

def set_secret(name, value):
    if HAS_KEYRING and value:
        try:
            keyring.set_password(KEYRING_SERVICE, name, value)
            return True
        except Exception:
            pass
    return False

def migrate_secrets(cfg):
    """One-time: move keys out of the JSON config into the OS keyring.
    A key is only scrubbed from the file AFTER it is confirmed stored."""
    g_old = cfg.get("api_key", "")
    d_old = cfg.get("deepgram_key", "")
    moved = False
    if g_old and set_secret("gemini", g_old) and get_secret("gemini") == g_old:
        cfg.pop("api_key", None)
        moved = True
    if d_old and set_secret("deepgram", d_old) and get_secret("deepgram") == d_old:
        cfg.pop("deepgram_key", None)
        moved = True
    if moved:
        save_config(cfg)          # rewrite config without the migrated keys
    return (get_secret("gemini") or g_old, get_secret("deepgram") or d_old)


# ── GEMINI ────────────────────────────────────────────────────────
class RateLimited(Exception):
    pass

def _gemini_post(payload, api_key, model):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    r = requests.post(url, json=payload, timeout=20,
                      headers={"x-goog-api-key": api_key})
    if r.status_code == 429:
        raise RateLimited()
    r.raise_for_status()
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        return None

def gemini_caption_audio(wav_bytes, api_key, model, target_lang, context_lines):
    """Chunked mode: audio in -> translated caption out, one call."""
    ctx = ""
    if context_lines:
        ctx = ("Recent caption context (for pronouns/names only, do not repeat it):\n"
               + "\n".join(context_lines) + "\n\n")
    prompt = (
        "You are a live captioning engine for in-game voice chat.\n"
        f"{ctx}"
        f"Listen to the audio. If it contains human speech, transcribe it and "
        f"translate it into {target_lang}. Respond with ONLY the {target_lang} "
        f"translation — no quotes, no labels, caption style, concise.\n"
        f"If the audio contains no intelligible human speech, respond with "
        f"exactly: {NO_SPEECH_TOKEN}"
    )
    payload = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "audio/wav",
                             "data": base64.b64encode(wav_bytes).decode("ascii")}},
        ]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 512,
            "thinkingConfig": {"thinkingLevel": "minimal"},
        },
    }
    text = _gemini_post(payload, api_key, model)
    if not text or NO_SPEECH_TOKEN in text:
        return None
    return text

def gemini_translate_text(text, api_key, model, target_lang, prev_caption):
    """Streaming mode: translate the transcript-so-far. Called repeatedly as
    the utterance grows, so the translation self-corrects with context."""
    ctx = f"Previous caption (context only, do not repeat): {prev_caption}\n" if prev_caption else ""
    prompt = (
        "You translate live in-game voice chat captions.\n"
        f"{ctx}"
        f"Translate the following transcript into {target_lang}. It may be a "
        f"partial sentence still being spoken — translate what is there naturally. "
        f"Respond with ONLY the {target_lang} translation, no quotes, no labels.\n\n"
        f"{text}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 512,
            "thinkingConfig": {"thinkingLevel": "minimal"},
        },
    }
    return _gemini_post(payload, api_key, model)


def build_wav_bytes(samples_f32, sr=TARGET_SR):
    pcm = np.clip(samples_f32, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# ── VAD SEGMENTER (chunked mode) ──────────────────────────────────
class Segmenter:
    def __init__(self):
        self.noise_floor = 0.003
        self.preroll = deque(maxlen=max(1, int(VAD_PREROLL_S * 1000 / FRAME_MS)))
        self.voiced_run = 0
        self.silence_run = 0
        self.in_speech = False
        self.buf = []
        self.voiced_total = 0

    def _is_voice(self, rms):
        return rms > max(VAD_ABS_FLOOR, self.noise_floor * VAD_SNR_MULT)

    def feed(self, frame):
        rms = float(np.sqrt(np.mean(frame * frame) + 1e-12))
        voiced = self._is_voice(rms)
        if not voiced:
            self.noise_floor = 0.97 * self.noise_floor + 0.03 * rms

        if not self.in_speech:
            self.preroll.append(frame)
            if voiced:
                self.voiced_run += 1
                if self.voiced_run >= VAD_START_FRAMES:
                    self.in_speech = True
                    self.buf = list(self.preroll)
                    self.silence_run = 0
                    self.voiced_total = self.voiced_run
            else:
                self.voiced_run = 0
            return None

        self.buf.append(frame)
        if voiced:
            self.silence_run = 0
            self.voiced_total += 1
        else:
            self.silence_run += 1

        dur = len(self.buf) * FRAME_MS / 1000.0
        ended = self.silence_run * FRAME_MS / 1000.0 >= VAD_END_SILENCE_S
        if ended or dur >= VAD_MAX_UTTER_S:
            utter = np.concatenate(self.buf) if self.buf else None
            enough_voice = self.voiced_total * FRAME_MS / 1000.0 >= VAD_MIN_UTTER_S
            self.in_speech = False
            self.voiced_run = 0
            self.silence_run = 0
            self.voiced_total = 0
            self.buf = []
            self.preroll.clear()
            if utter is not None and enough_voice:
                return utter
        return None


# ── STREAM GATE (VAD gating for streaming mode) ──────────────────
class StreamGate:
    """Sits between capture and the Deepgram websocket. Streams audio only
    while sound is detected; during quiet stretches it sends keepalives
    instead, cutting billed audio (Deepgram bills per second SENT) by the
    fraction of the session that is actually silent."""

    def __init__(self, send_audio, send_keepalive, on_state=None):
        self.send_audio = send_audio          # (pcm_bytes) -> None
        self.send_keepalive = send_keepalive  # () -> None
        self.on_state = on_state or (lambda open_: None)
        self.noise_floor = 0.003
        self.preroll = deque(maxlen=max(1, int(GATE_PREROLL_S * 1000 / FRAME_MS)))
        self.open = False
        # frame-counted timers (30ms each) — deterministic, robust to
        # capture stalls, and unit-testable
        self.hangover_frames = int(GATE_HANGOVER_S * 1000 / FRAME_MS)
        self.keepalive_frames = int(GATE_KEEPALIVE_S * 1000 / FRAME_MS)
        self.silence_run = 0
        self.since_keepalive = 0
        self.open_run = 0
        self.locked = False
        self.lock_run = 0
        self.enabled_music_guard = True       # set from App checkbox
        self.max_cont_frames = int(GATE_MAX_CONT_S * 1000 / FRAME_MS)
        self.lock_max_frames = int(GATE_LOCK_MAX_S * 1000 / FRAME_MS)
        self.on_music = lambda: None          # set by App
        self.sent_frames = 0
        self.total_frames = 0

    def feed(self, frame):
        """frame: float32 mono @16k, FRAME_LEN samples."""
        self.total_frames += 1
        rms = float(np.sqrt(np.mean(frame * frame) + 1e-12))
        thresh = max(VAD_ABS_FLOOR, self.noise_floor * VAD_SNR_MULT)
        voiced = rms > thresh
        if not voiced:
            self.noise_floor = 0.97 * self.noise_floor + 0.03 * rms
            self.silence_run += 1
        else:
            self.silence_run = 0

        if self.locked:
            # music lock: reopen on a real pause (source stopped), or when
            # the safety valve expires (speech may be buried in the music)
            self.lock_run += 1
            if (self.silence_run >= self.hangover_frames
                    or self.lock_run >= self.lock_max_frames):
                self.locked = False
                self.lock_run = 0
            else:
                self.since_keepalive += 1
                if self.since_keepalive >= self.keepalive_frames:
                    self.since_keepalive = 0
                    self.send_keepalive()
                return

        if self.open:
            self._send(frame)
            self.sent_frames += 1
            self.open_run += 1
            if self.silence_run >= self.hangover_frames:
                self.open = False
                self.open_run = 0
                self.on_state(False)
            elif (self.enabled_music_guard
                  and self.open_run >= self.max_cont_frames):
                # sound with no conversational pauses for a full minute:
                # music / media, not voice chat. Stop paying for it.
                self.open = False
                self.open_run = 0
                self.locked = True
                self.lock_run = 0
                self.on_state(False)
                self.on_music()
        else:
            self.preroll.append(frame)
            if voiced:
                self.open = True
                self.on_state(True)
                for f in self.preroll:          # replay so words aren't clipped
                    self._send(f)
                    self.sent_frames += 1
                self.preroll.clear()
            else:
                self.since_keepalive += 1
                if self.since_keepalive >= self.keepalive_frames:
                    self.since_keepalive = 0
                    self.send_keepalive()

    def _send(self, frame):
        pcm = (np.clip(frame, -1, 1) * 32767.0).astype(np.int16).tobytes()
        self.send_audio(pcm)

    def sent_pct(self):
        return 100.0 * self.sent_frames / max(1, self.total_frames)


# ── DEEPGRAM STREAMER (streaming mode) ────────────────────────────
class DeepgramStreamer:
    """Feeds 16k mono int16 PCM to Deepgram; assembles interim/final
    transcript fragments into the utterance-so-far and reports updates.

    Callbacks (called from ws thread):
      on_update(utt_id, text_so_far, is_utterance_end)
      on_status(msg)
    """

    def __init__(self, api_key, source_lang, on_update, on_status,
                 on_segment=None):
        self.api_key = api_key
        self.source_lang = source_lang
        self.on_update = on_update
        self.on_status = on_status
        self.on_segment = on_segment or (lambda *a: None)
        self.ws = None
        self.connected = threading.Event()
        self.stop_flag = threading.Event()
        self.utt_id = 0
        self.finals = []
        self.cur_speaker = None
        self.seg_counter = 0
        self.thread = None
        # forced finalization (danmaku cadence)
        self.should_force = lambda: False     # set by App
        self.force_interval = DANMAKU_FINALIZE_S
        self.pending_interim = False
        self.last_final_at = 0.0
        self._force_thread = None

    def _split_speaker_runs(self, words):
        """Group consecutive words by speaker; merge 1-word jitter runs into
        the previous run. Returns [(speaker, text), ...]."""
        runs = []
        for w in words:
            spk = w.get("speaker")
            token = w.get("punctuated_word") or w.get("word") or ""
            if not token:
                continue
            if runs and runs[-1][0] == spk:
                runs[-1][1].append(token)
            else:
                runs.append([spk, [token]])
        # merge single-word runs (diarization jitter) into the previous run
        merged = []
        for spk, toks in runs:
            if merged and len(toks) < 2:
                merged[-1][1].extend(toks)
            else:
                merged.append([spk, toks])
        cjk = (self.source_lang or "").lower().startswith(("zh", "ja"))
        joiner = "" if cjk else " "
        return [(spk, joiner.join(toks)) for spk, toks in merged]

    def start(self):
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self._force_thread = threading.Thread(target=self._force_loop, daemon=True)
        self._force_thread.start()

    def _force_loop(self):
        """Danmaku cadence: if unfinalized speech has been pending too long,
        tell Deepgram to finalize it NOW instead of waiting for its own timer."""
        while not self.stop_flag.is_set():
            time.sleep(0.4)
            if (self.should_force() and self.pending_interim
                    and self.connected.is_set()
                    and time.time() - self.last_final_at >= self.force_interval):
                try:
                    self.ws.send(json.dumps({"type": "Finalize"}))
                    self.last_final_at = time.time()   # don't spam while it processes
                except Exception:
                    pass

    def stop(self):
        self.stop_flag.set()
        ws = self.ws
        if ws:
            try:
                ws.send(json.dumps({"type": "CloseStream"}))
                ws.close()
            except Exception:
                pass

    def send_audio(self, pcm_bytes):
        if self.connected.is_set() and self.ws:
            try:
                self.ws.send(pcm_bytes, opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception:
                pass  # reconnect loop will handle it

    def send_keepalive(self):
        if self.connected.is_set() and self.ws:
            try:
                self.ws.send(json.dumps({"type": "KeepAlive"}))
            except Exception:
                pass

    def _url(self):
        params = dict(DG_PARAMS)
        if self.source_lang:
            params["language"] = self.source_lang
        return DG_URL_BASE + "?" + urlencode(params)

    def _run(self):
        backoff = 1
        while not self.stop_flag.is_set():
            try:
                self.on_status("🔌 connecting to Deepgram…")
                self.ws = websocket.create_connection(
                    self._url(),
                    header={"Authorization": f"Token {self.api_key}"},
                    timeout=10,
                )
                self.ws.settimeout(30)
                self.connected.set()
                self.on_status("🎧 streaming — captions appear as people speak")
                backoff = 1
                self.finals = []
                while not self.stop_flag.is_set():
                    msg = self.ws.recv()
                    if not msg:
                        break
                    self._handle(json.loads(msg))
            except Exception as e:
                if self.stop_flag.is_set():
                    break
                self.connected.clear()
                self.on_status(f"⚠ Deepgram connection lost — retrying in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 15)
            finally:
                self.connected.clear()
                try:
                    if self.ws:
                        self.ws.close()
                except Exception:
                    pass
                self.ws = None
        self.on_status("Streaming stopped")

    def _handle(self, data):
        if data.get("type") == "UtteranceEnd":
            # fallback: endpointing missed the pause, force-finalize what we have
            full = " ".join(self.finals).strip()
            if full:
                print(f"[dbg] UtteranceEnd-fallback utt={self.utt_id} "
                      f"segs={len(self.finals)} text={full[:40]!r}")
                self.on_update(self.utt_id, full, True, self.cur_speaker)
                self.finals = []
                self.utt_id += 1
                self.cur_speaker = None
                self.seg_counter = 0
            return
        if data.get("type") != "Results":
            return
        alts = data.get("channel", {}).get("alternatives", [])
        transcript = alts[0].get("transcript", "") if alts else ""
        is_final = data.get("is_final", False)
        speech_final = data.get("speech_final", False)
        # diarization: majority speaker among this result's words
        words = alts[0].get("words", []) if alts else []
        spk = [w.get("speaker") for w in words if w.get("speaker") is not None]
        if spk and self.cur_speaker is None:
            # lock the speaker at utterance start; diarization jitter
            # mid-sentence must not change the caption color
            self.cur_speaker = max(set(spk), key=spk.count)

        if is_final:
            self.pending_interim = False
            self.last_final_at = time.time()
            if transcript.strip():
                self.finals.append(transcript.strip())
                # fragment finalized mid-utterance: report immediately.
                # If the fragment contains ONE speaker, use Deepgram's clean
                # formatted transcript; if speakers overlap inside it, split
                # into per-speaker runs so each voice gets its own message.
                runs = self._split_speaker_runs(words)
                distinct = {s for s, _ in runs if s is not None}
                if len(distinct) <= 1:
                    self.on_segment(self.utt_id, self.seg_counter,
                                    transcript.strip(), self.cur_speaker)
                    self.seg_counter += 1
                else:
                    for spk, text in runs:
                        if text.strip():
                            self.on_segment(self.utt_id, self.seg_counter,
                                            text.strip(), spk)
                            self.seg_counter += 1
            if speech_final:
                full = " ".join(self.finals).strip()
                if full:
                    print(f"[dbg] speech_final utt={self.utt_id} "
                          f"segs={len(self.finals)} text={full[:40]!r}")
                    self.on_update(self.utt_id, full, True, self.cur_speaker)
                self.finals = []
                self.utt_id += 1
                self.cur_speaker = None
                self.seg_counter = 0
                return
            if transcript.strip():
                self.on_update(self.utt_id, " ".join(self.finals).strip(), False,
                               self.cur_speaker)
        else:
            if transcript.strip():
                self.pending_interim = True
                so_far = " ".join(self.finals + [transcript.strip()]).strip()
                self.on_update(self.utt_id, so_far, False, self.cur_speaker)


# ── TRANSLATION COALESCER (streaming mode) ────────────────────────
class Translator:
    """One worker; always translates the NEWEST pending text for an utterance.
    Old pending updates are replaced, so we never queue up a backlog and the
    caption self-corrects to the latest state of the sentence."""

    def __init__(self, get_creds, on_result, on_status):
        self.get_creds = get_creds        # -> (api_key, model, target_lang)
        self.on_result = on_result        # (utt_id, translated, is_end)
        self.on_status = on_status
        self.lock = threading.Lock()
        self.pending = None               # coalescing slot: PARTIALS only
        self.finals = deque(maxlen=6)     # FIFO: finalized text is never dropped
                                          # by a newer partial (oldest drops only
                                          # if 6 finals back up)
        self.kick = threading.Event()
        self.stop_flag = threading.Event()
        self.prev_caption = ""
        self.cooldown_until = 0.0
        self.last_call = 0.0
        self.thread = None

    def start(self):
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        self.kick.set()

    def submit(self, utt_id, text, is_end, speaker=None):
        with self.lock:
            if is_end:
                self.finals.append((utt_id, text, True, speaker))
                # a queued final supersedes any pending partial of the same utt
                if self.pending and self.pending[0] == utt_id:
                    self.pending = None
            else:
                # partials coalesce: newest wins; never displace a final
                self.pending = (utt_id, text, False, speaker)
        self.kick.set()

    def _run(self):
        while not self.stop_flag.is_set():
            self.kick.wait(timeout=0.5)
            self.kick.clear()
            with self.lock:
                if self.finals:                       # finals first, in order
                    job = self.finals.popleft()
                else:
                    job, self.pending = self.pending, None
            if job is None:
                continue
            if time.time() < self.cooldown_until:
                # rate-limited: keep finals (front of queue), drop partials
                if job[2]:
                    with self.lock:
                        self.finals.appendleft(job)
                    time.sleep(0.5)
                continue
            utt_id, text, is_end, speaker = job
            # throttle partial retranslations to protect free-tier RPM;
            # finals always go through
            if not is_end:
                wait = MIN_PARTIAL_GAP_S - (time.time() - self.last_call)
                if wait > 0:
                    with self.lock:
                        if self.pending is None:      # nothing newer arrived
                            self.pending = job
                    time.sleep(min(wait, 0.4))
                    self.kick.set()
                    continue
            try:
                key, model, target = self.get_creds()
                self.last_call = time.time()
                out = gemini_translate_text(text, key, model, target, self.prev_caption)
                if out:
                    if is_end:
                        self.prev_caption = out
                    self.on_result(utt_id, out, is_end, speaker)
            except RateLimited:
                self.cooldown_until = time.time() + 10
                self.on_status("⚠ Gemini rate-limited — captions may lag ~10s")
            except Exception as e:
                self.on_status(f"⚠ translate error (update skipped)")


# ── AUDIO CAPTURE (WASAPI loopback) ───────────────────────────────
class LoopbackCapture:
    """Captures system audio; routes 16k mono float32 frames to on_frame.
    device_name=None -> default speakers' loopback; otherwise the named
    loopback device (e.g. VB-Cable) for custom per-app routing setups."""

    def __init__(self, on_frame, on_status, device_name=None):
        self.on_frame = on_frame
        self.on_status = on_status
        self.device_name = device_name
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        p = pyaudio.PyAudio()
        try:
            dev = None
            if self.device_name:
                try:
                    for lb in p.get_loopback_device_info_generator():
                        if lb["name"] == self.device_name:
                            dev = lb
                            break
                except Exception:
                    pass
                if dev is None:
                    self.on_status(f"⚠ device '{self.device_name}' not found — "
                                   f"using default speakers instead")
            if dev is None:
                try:
                    dev = p.get_default_wasapi_loopback()
                except Exception:
                    try:
                        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
                        speakers = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
                        for lb in p.get_loopback_device_info_generator():
                            if speakers["name"] in lb["name"]:
                                dev = lb
                                break
                    except Exception:
                        pass
            if dev is None:
                self.on_status("❌ No WASAPI loopback device found. Run: python -m pyaudiowpatch")
                return

            dev_sr = int(dev["defaultSampleRate"])
            channels = max(1, int(dev["maxInputChannels"]))
            frames_per_buffer = int(dev_sr * FRAME_MS / 1000)

            stream = p.open(format=pyaudio.paInt16, channels=channels, rate=dev_sr,
                            input=True, input_device_index=dev["index"],
                            frames_per_buffer=frames_per_buffer)
            self.on_status(f"🎧 Capturing ALL system audio — {dev['name']} "
                           f"({dev_sr} Hz, {channels}ch)")

            ratio = TARGET_SR / dev_sr
            carry = np.zeros(0, dtype=np.float32)
            while not self.stop_event.is_set():
                raw = stream.read(frames_per_buffer, exception_on_overflow=False)
                x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                if channels > 1:
                    x = x.reshape(-1, channels).mean(axis=1)
                if dev_sr != TARGET_SR:
                    n_out = int(round(len(x) * ratio))
                    if n_out <= 0:
                        continue
                    x = np.interp(np.linspace(0.0, len(x) - 1.0, n_out),
                                  np.arange(len(x), dtype=np.float32), x
                                  ).astype(np.float32)
                carry = np.concatenate([carry, x])
                while len(carry) >= FRAME_LEN:
                    self.on_frame(carry[:FRAME_LEN])
                    carry = carry[FRAME_LEN:]

            stream.stop_stream()
            stream.close()
        except Exception as e:
            self.on_status(f"❌ Audio error: {e}")
        finally:
            p.terminate()


# ── PER-APP CAPTURE (WASAPI process loopback via ProcTap) ────────
PROC_SR = 48000   # ProcTap Windows backend: 48 kHz stereo float32

DEFAULT_DEVICE_LABEL = "(Default speakers)"

# WASAPI process loopback requires Windows build 20348+ (Win11 / Server 2022).
# On older builds (e.g. Win10 22H2 = 19045) the native layer does NOT error —
# it silently captures ALL system audio, violating the user's app selection.
# Verified empirically on 19045. So the feature is version-gated hard.
MIN_BUILD_PROCESS_LOOPBACK = 20348

def win_build():
    try:
        return sys.getwindowsversion().build
    except Exception:
        return 0

def process_loopback_supported():
    return sys.platform == "win32" and win_build() >= MIN_BUILD_PROCESS_LOOPBACK

def list_loopback_devices():
    """Names of all WASAPI loopback capture devices (one per output device)."""
    if not HAS_PAW:
        return []
    names = []
    try:
        p = pyaudio.PyAudio()
        try:
            for lb in p.get_loopback_device_info_generator():
                names.append(lb["name"])
        finally:
            p.terminate()
    except Exception:
        pass
    return names


def list_audio_apps():
    """Apps that currently have a Windows audio session: [(pid, name), ...]"""
    if not HAS_PYCAW:
        return []
    apps = []
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process is None:
                continue
            try:
                apps.append((s.Process.pid, s.Process.name()))
            except Exception:
                continue
    except Exception:
        return []
    # dedupe by (pid), keep stable order
    seen, out = set(), []
    for pid, name in apps:
        if pid not in seen:
            seen.add(pid)
            out.append((pid, name))
    return out


class _SourceConverter:
    """Per-process: ProcTap raw bytes (assumed float32 stereo 48k on Windows)
    -> float32 mono 16k samples appended to a shared per-source buffer."""

    def __init__(self):
        self.carry = np.zeros(0, dtype=np.float32)
        self.buf = deque()          # converted sample arrays
        self.lock = threading.Lock()

    def push_raw(self, pcm_bytes):
        x = np.frombuffer(pcm_bytes, dtype=np.float32)
        if x.size == 0:
            return
        if x.size % 2 == 0:                      # stereo -> mono
            x = x.reshape(-1, 2).mean(axis=1)
        n_out = int(round(len(x) * TARGET_SR / PROC_SR))
        if n_out <= 0:
            return
        y = np.interp(np.linspace(0.0, len(x) - 1.0, n_out),
                      np.arange(len(x), dtype=np.float32), x).astype(np.float32)
        with self.lock:
            self.buf.append(y)

    def pull(self, n):
        """Take up to n samples; zero-pad if this source is starved."""
        out = np.zeros(n, dtype=np.float32)
        got = 0
        with self.lock:
            while got < n and self.buf:
                chunk = self.buf[0]
                take = min(n - got, len(chunk))
                out[got:got + take] = chunk[:take]
                if take == len(chunk):
                    self.buf.popleft()
                else:
                    self.buf[0] = chunk[take:]
                got += take
        return out


class AppSourceCapture:
    """Captures ONLY the selected processes (include mode) and mixes them
    into one 16k mono stream of 30ms frames -> on_frame."""

    def __init__(self, pid_names, on_frame, on_status):
        self.pid_names = pid_names            # [(pid, name), ...]
        self.on_frame = on_frame
        self.on_status = on_status
        self.stop_event = threading.Event()
        self.captures = []                    # (ProcessAudioCapture ctx, name)
        self.sources = []                     # _SourceConverter per pid
        self.thread = None

    def start(self):
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        started = []
        try:
            for pid, name in self.pid_names:
                conv = _SourceConverter()
                try:
                    # v1.0.3 API: output is guaranteed 48kHz stereo float32;
                    # callback is (pcm_bytes, num_frames)
                    cap = ProcessAudioCapture(
                        pid,
                        on_data=lambda pcm, frames, c=conv: c.push_raw(pcm))
                    cap.start()
                    started.append((cap, name))
                    self.sources.append(conv)
                except Exception as e:
                    self.on_status(f"⚠ couldn't capture '{name}' (pid {pid}): {e}")
            if not started:
                self.on_status("❌ No app captures could start — check apps are "
                               "running & playing audio, or use All system audio")
                return
            self.captures = started
            self.on_status("🎧 Capturing ONLY these apps: " +
                           ", ".join(n for _, n in started) +
                           " — everything else is ignored")

            # mixer: every 30ms pull one frame from each source, sum, emit
            period = FRAME_MS / 1000.0
            next_t = time.monotonic() + period
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now < next_t:
                    time.sleep(min(period, next_t - now))
                    continue
                next_t += period
                mixed = np.zeros(FRAME_LEN, dtype=np.float32)
                for s in self.sources:
                    mixed += s.pull(FRAME_LEN)
                np.clip(mixed, -1.0, 1.0, out=mixed)
                self.on_frame(mixed)
        except Exception as e:
            self.on_status(f"❌ App-capture error: {e}")
        finally:
            for cap, _ in self.captures:
                try:
                    cap.stop()
                    cap.close()
                except Exception:
                    pass
            self.captures = []


# ── CAPTION DISPLAYS (bar / chatbox / danmaku) ────────────────────
class _OverlayBase:
    """Shared: borderless topmost window, color-key transparency,
    click-through toggle, drag support."""

    def __init__(self, root):
        self.root = root
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=TRANS_KEY)
        try:
            self.win.attributes("-transparentcolor", TRANS_KEY)
        except tk.TclError:
            pass
        self._clickthrough = False
        self._drag_start = None

    def _hwnd(self):
        if sys.platform != "win32":
            return None
        hwnd = ctypes.windll.user32.GetParent(self.win.winfo_id())
        return hwnd or self.win.winfo_id()

    def set_clickthrough(self, enabled):
        self._clickthrough = enabled
        hwnd = self._hwnd()
        if not hwnd:
            return
        GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = -20, 0x80000, 0x20, 0x80
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW
        style = style | WS_EX_TRANSPARENT if enabled else style & ~WS_EX_TRANSPARENT
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

    def _bind_drag(self, widget):
        widget.bind("<ButtonPress-1>", self._drag_begin)
        widget.bind("<B1-Motion>", self._drag_move)

    def _drag_begin(self, e):
        self._drag_start = (e.x_root - self.win.winfo_x(),
                            e.y_root - self.win.winfo_y())

    def _drag_move(self, e):
        if self._clickthrough or not self._drag_start:
            return
        dx, dy = self._drag_start
        self.win.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass


class BarOverlay(_OverlayBase):
    """Classic bottom caption bar: committed line + live revising line."""

    def __init__(self, root, font_size=18):
        super().__init__(root)
        self.box = tk.Frame(self.win, bg=CAPTION_BG)
        self.prev_lbl = tk.Label(self.box, text="", bg=CAPTION_BG, fg=CAPTION_FG,
                                 font=(CAPTION_FONT, font_size), justify="center")
        self.live_lbl = tk.Label(self.box, text="", bg=CAPTION_BG, fg=CAPTION_LIVE_FG,
                                 font=(CAPTION_FONT, font_size), justify="center")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.wraplength = int(sw * 0.55)
        for lbl in (self.prev_lbl, self.live_lbl):
            lbl.configure(wraplength=self.wraplength)
            self._bind_drag(lbl)
        self.win.geometry(f"+{sw // 2 - self.wraplength // 2}+{int(sh * 0.80)}")
        self.prev_text = ""
        self.live_text = ""
        self._hide_job = None
        self.win.update_idletasks()
        self.set_clickthrough(True)

    def set_font_size(self, size):
        for lbl in (self.prev_lbl, self.live_lbl):
            lbl.configure(font=(CAPTION_FONT, size))

    def update_live(self, text, speaker=None):
        self.live_text = text
        self.live_lbl.configure(fg=speaker_color(speaker) if speaker is not None
                                else CAPTION_LIVE_FG)
        self._render()
        self._reschedule_hide()

    def commit_live(self, text=None, speaker=None, cont=False, group=None):
        if text is not None:
            self.live_text = text
        if self.live_text:
            self.prev_text = self.live_text
            self.prev_lbl.configure(fg=speaker_color(speaker))
        self.live_text = ""
        self._render()
        self._reschedule_hide()

    def _render(self):
        show_prev, show_live = bool(self.prev_text), bool(self.live_text)
        self.prev_lbl.configure(text=self.prev_text)
        self.live_lbl.configure(text=self.live_text + (" …" if show_live else ""))
        self.prev_lbl.pack_forget()
        self.live_lbl.pack_forget()
        if show_prev:
            self.prev_lbl.pack(padx=18, pady=(10, 2 if show_live else 10))
        if show_live:
            self.live_lbl.pack(padx=18, pady=(2 if show_prev else 10, 10))
        if show_prev or show_live:
            if not self.box.winfo_ismapped():
                self.box.pack()
            self.win.attributes("-topmost", True)
        else:
            self.box.pack_forget()

    def _reschedule_hide(self):
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
        self._hide_job = self.root.after(CAPTION_HIDE_AFTER_S * 1000, self._hide)

    def _hide(self):
        if self.live_text:
            self._reschedule_hide()
            return
        self.prev_text = ""
        self._render()
        self._hide_job = None


class ChatBoxOverlay(_OverlayBase):
    """Semi-transparent panel: last N messages, one color per speaker,
    plus a live (still-revising) line at the bottom."""

    N_MESSAGES = 5
    MSG_TTL_S = 14

    def __init__(self, root, font_size=13):
        super().__init__(root)
        self.win.configure(bg=CAPTION_BG)
        try:
            self.win.attributes("-alpha", 0.85)
        except tk.TclError:
            pass
        self.font_size = font_size
        self.frame = tk.Frame(self.win, bg=CAPTION_BG, padx=10, pady=8)
        self.frame.pack()
        self._bind_drag(self.frame)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.wrap = 360
        self.win.geometry(f"+{24}+{int(sh * 0.55)}")
        self.messages = deque()          # (speaker, text, expires_at)
        self.live = None                 # (speaker, text)
        self.labels = []
        self.win.update_idletasks()
        self.set_clickthrough(True)
        self._tick()

    def set_font_size(self, size):
        self.font_size = size
        self._render()

    def update_live(self, text, speaker=None):
        self.live = (speaker, text)
        self._render()

    def commit_live(self, text=None, speaker=None, cont=False, group=None):
        if text is None and self.live:
            speaker, text = self.live
        self.live = None
        if text:
            self.messages.append((speaker, text, time.time() + self.MSG_TTL_S))
            while len(self.messages) > self.N_MESSAGES:
                self.messages.popleft()
        self._render()

    def _render(self):
        for lbl in self.labels:
            lbl.destroy()
        self.labels = []
        rows = list(self.messages)
        if self.live:
            rows.append((self.live[0], self.live[1] + " …", None))
        for spk, text, _ in rows:
            prefix = f"S{(spk or 0) + 1} › " if spk is not None else ""
            lbl = tk.Label(self.frame, text=prefix + text, bg=CAPTION_BG,
                           fg=speaker_color(spk), font=(CAPTION_FONT, self.font_size),
                           wraplength=self.wrap, justify="left", anchor="w")
            lbl.pack(anchor="w", pady=1)
            self._bind_drag(lbl)
            self.labels.append(lbl)
        if rows:
            self.win.attributes("-topmost", True)
        self.win.update_idletasks()

    def _tick(self):
        now = time.time()
        before = len(self.messages)
        while self.messages and self.messages[0][2] and self.messages[0][2] < now:
            self.messages.popleft()
        if len(self.messages) != before:
            self._render()
        self.root.after(1000, self._tick)


class DanmakuOverlay(_OverlayBase):
    """Bilibili-style sliding captions: finalized messages glide right-to-left
    across the top of the screen in lanes, colored per speaker."""

    LANES = 3
    TICK_MS = 30

    def __init__(self, root, font_size=16):
        super().__init__(root)
        self.font_size = font_size
        self.sw = root.winfo_screenwidth()
        self.row_h = font_size + 16
        h = self.LANES * self.row_h + 8
        self.canvas = tk.Canvas(self.win, width=self.sw, height=h, bg=TRANS_KEY,
                                highlightthickness=0)
        self.canvas.pack()
        self.win.geometry(f"{self.sw}x{h}+0+30")
        self.items = []                  # dicts: ids, x, speed, lane, width
        self.lane_tail = [0.0] * self.LANES   # x of last message's right edge
        self.pending = deque()
        self.group_lane = {}             # utterance group -> lane (continuity)
        self.win.update_idletasks()
        self.set_clickthrough(True)
        self._anim()

    def set_font_size(self, size):
        self.font_size = size

    def update_live(self, text, speaker=None):
        pass                              # danmaku shows finalized lines only

    def commit_live(self, text=None, speaker=None, cont=False, group=None):
        if text:
            if cont:
                text = "… " + text
            self.pending.append((speaker, text, cont, group))

    def _spawn(self, speaker, text, cont=False, group=None):
        lane = None
        # continuation fragments follow their sentence's lane when possible
        if cont and group in self.group_lane:
            pref = self.group_lane[group]
            if self.lane_tail[pref] < self.sw - 60:
                lane = pref
        if lane is None:
            for i in range(self.LANES):
                if self.lane_tail[i] < self.sw - 60:
                    lane = i
                    break
        if lane is None:
            return False
        if group is not None:
            self.group_lane[group] = lane
            if len(self.group_lane) > 30:          # prune old groups
                for k in list(self.group_lane)[:-20]:
                    del self.group_lane[k]
        y = 4 + lane * self.row_h + self.row_h // 2
        color = speaker_color(speaker)
        font = (CAPTION_FONT, self.font_size, "bold")
        shadow = self.canvas.create_text(self.sw + 2, y + 2, text=text, anchor="w",
                                         font=font, fill="#101010")
        main = self.canvas.create_text(self.sw, y, text=text, anchor="w",
                                       font=font, fill=color)
        bbox = self.canvas.bbox(main)
        width = (bbox[2] - bbox[0]) if bbox else 200
        travel = self.sw + width
        duration_s = max(7.0, min(16.0, 5.0 + len(text) * 0.12))
        speed = travel / (duration_s * 1000 / self.TICK_MS)
        self.items.append({"ids": (shadow, main), "x": float(self.sw),
                           "speed": speed, "lane": lane, "width": width})
        return True

    def _anim(self):
        while self.pending and self._spawn(*self.pending[0]):
            self.pending.popleft()
        self.lane_tail = [0.0] * self.LANES
        alive = []
        for it in self.items:
            it["x"] -= it["speed"]
            for i in it["ids"]:
                self.canvas.coords(i, it["x"] + (2 if i == it["ids"][0] else 0),
                                   self.canvas.coords(i)[1])
            right = it["x"] + it["width"]
            if right > 0:
                alive.append(it)
                self.lane_tail[it["lane"]] = max(self.lane_tail[it["lane"]], right)
            else:
                for i in it["ids"]:
                    self.canvas.delete(i)
        self.items = alive
        if self.items or self.pending:
            self.win.attributes("-topmost", True)
        self.root.after(self.TICK_MS, self._anim)


CAPTION_STYLES = {"Bar (bottom)": BarOverlay,
                  "Chat box": ChatBoxOverlay,
                  "Danmaku (sliding)": DanmakuOverlay}


class CaptionDisplay:
    """Owns the active display; rebuilds it when the style changes."""

    def __init__(self, root, style, font_size):
        self.root = root
        self.font_size = font_size
        self.impl = None
        self.set_style(style)

    def set_style(self, style):
        if self.impl:
            self.impl.destroy()
        cls = CAPTION_STYLES.get(style, BarOverlay)
        self.impl = cls(self.root, self.font_size)

    def update_live(self, text, speaker=None):
        self.impl.update_live(text, speaker)

    def commit_live(self, text=None, speaker=None, cont=False, group=None):
        self.impl.commit_live(text, speaker, cont, group)

    def set_clickthrough(self, enabled):
        self.impl.set_clickthrough(enabled)

    def set_font_size(self, size):
        self.font_size = size
        self.impl.set_font_size(size)


# ── CONTROL PANEL / APP ───────────────────────────────────────────
class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME}  v{APP_VERSION}")
        root.geometry("600x560")
        root.configure(bg=BG)

        cfg = load_config()
        g_key, d_key = migrate_secrets(cfg)      # keys live in Credential Manager
        saved_model = cfg.get("model", "gemini-3.1-flash-lite")
        if saved_model not in RPM_LIMITS:
            saved_model = "gemini-3.1-flash-lite"
        # SECURITY: stored keys are NEVER loaded into the UI. Fields stay
        # empty; typing replaces the stored key. Without keyring (fallback),
        # keys still prefill from config as before.
        self.gemini_key  = tk.StringVar(value="" if HAS_KEYRING else g_key)
        self.dg_key      = tk.StringVar(value="" if HAS_KEYRING else d_key)
        self.model       = tk.StringVar(value=saved_model)
        self.target_lang = tk.StringVar(value=cfg.get("target_lang", "English"))
        self.source_lang = tk.StringVar(value=cfg.get("source_lang", "multi"))
        self.mode        = tk.StringVar(value=cfg.get("mode", "streaming"))
        self.source_mode = tk.StringVar(value=cfg.get("source_mode", "system"))
        self.capture_dev = tk.StringVar(value=cfg.get("capture_device", DEFAULT_DEVICE_LABEL))
        self.selected_app_names = list(cfg.get("selected_apps", []))
        self.font_size   = tk.IntVar(value=cfg.get("font_size", 18))
        self.move_mode   = tk.BooleanVar(value=False)
        self.music_guard = tk.BooleanVar(value=cfg.get("music_guard", True))
        self.status      = tk.StringVar(value=f"Idle — press START   (v{APP_VERSION})")

        self.running = False
        self.capture = None
        self.streamer = None
        self.gate = None
        self.translator = None
        self.segmenter = None
        self.ui_queue = queue.Queue()
        self.inflight = 0
        self.inflight_lock = threading.Lock()
        self.req_times = deque()
        self.rate_cooldown_until = 0.0
        self.recent_captions = deque(maxlen=3)
        self.dropped = 0
        self.current_utt = -1
        self.last_live_at = 0.0

        self.caption_style = tk.StringVar(value=cfg.get("caption_style", "Bar (bottom)"))
        if self.caption_style.get() not in CAPTION_STYLES:
            self.caption_style.set("Bar (bottom)")
        self.overlay = CaptionDisplay(root, self.caption_style.get(), self.font_size.get())
        self._build_ui()
        self._poll()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- UI -----------------------------------------------------------
    def _row(self, label):
        f = tk.Frame(self.root, bg=BG)
        f.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(f, text=label, bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI", 10)).pack(anchor="w")
        return f

    def _key_entry(self, parent, var):
        e = tk.Entry(parent, textvariable=var, show="•", bg=SURFACE2, fg=TEXT,
                     insertbackground=TEXT, relief="flat", font=("Segoe UI", 11))
        e.pack(side="left", fill="x", expand=True, ipady=5)
        tk.Button(parent, text="Show", bg=SURFACE2, fg=TEXT_MUTED, relief="flat",
                  bd=0, padx=8,
                  command=lambda: e.configure(show="" if e.cget("show") == "•" else "•")
                  ).pack(side="left", padx=(6, 0))
        return e

    def _build_ui(self):
        tk.Label(self.root, text="UniversalSubs", bg=BG, fg=TEXT,
                 font=("Segoe UI", 15, "bold")).pack(anchor="w", padx=16, pady=(14, 0))
        tk.Label(self.root, text="Any language in, your language out — live, over any game",
                 bg=BG, fg=TEXT_MUTED, font=("Segoe UI", 10)).pack(anchor="w", padx=16)

        f = self._row("Engine")
        tk.Radiobutton(f, text="Streaming — live captions while they talk (Deepgram + Gemini)",
                       variable=self.mode, value="streaming", bg=BG, fg=TEXT,
                       selectcolor=SURFACE2, activebackground=BG,
                       font=("Segoe UI", 10)).pack(anchor="w")
        tk.Radiobutton(f, text="Chunked — caption after each sentence (Gemini only)",
                       variable=self.mode, value="chunked", bg=BG, fg=TEXT,
                       selectcolor=SURFACE2, activebackground=BG,
                       font=("Segoe UI", 10)).pack(anchor="w")

        g_saved = bool(get_secret("gemini"))
        d_saved = bool(get_secret("deepgram"))
        f = self._row("Gemini API key" + (" — ✓ saved in Credential Manager; "
                      "leave blank to keep, type to replace" if g_saved
                      else " (aistudio.google.com/app/apikey)"))
        self._key_entry(f, self.gemini_key)

        f = self._row("Deepgram API key" + (" — ✓ saved in Credential Manager; "
                      "leave blank to keep, type to replace" if d_saved
                      else " (deepgram.com — needed for Streaming mode)"))
        self._key_entry(f, self.dg_key)

        f = self._row("Model  /  Translate into  /  Spoken lang (zh-CN, zh-TW, ja… — multi = EU langs+JP, no Chinese)")
        m = tk.OptionMenu(f, self.model, "gemini-3.1-flash-lite", "gemini-3.5-flash")
        m.configure(bg=SURFACE2, fg=TEXT, relief="flat", highlightthickness=0,
                    activebackground=SURFACE)
        m["menu"].configure(bg=SURFACE2, fg=TEXT)
        m.pack(side="left")
        tk.Entry(f, textvariable=self.target_lang, bg=SURFACE2, fg=TEXT,
                 insertbackground=TEXT, relief="flat", width=12,
                 font=("Segoe UI", 11)).pack(side="left", padx=(10, 0), ipady=5)
        tk.Entry(f, textvariable=self.source_lang, bg=SURFACE2, fg=TEXT,
                 insertbackground=TEXT, relief="flat", width=8,
                 font=("Segoe UI", 11)).pack(side="left", padx=(10, 0), ipady=5)

        f = self._row("Audio source")
        tk.Radiobutton(f, text="All system audio", variable=self.source_mode,
                       value="system", bg=BG, fg=TEXT, selectcolor=SURFACE2,
                       activebackground=BG, font=("Segoe UI", 10)).pack(side="left")
        apps_label = ("Selected apps only" if process_loopback_supported()
                      else "Selected apps only (needs Win11)")
        tk.Radiobutton(f, text=apps_label, variable=self.source_mode,
                       value="apps", bg=BG, fg=TEXT, selectcolor=SURFACE2,
                       activebackground=BG, font=("Segoe UI", 10),
                       command=self._apps_mode_blocked).pack(side="left", padx=(10, 0))
        tk.Button(f, text="Choose apps…", bg=SURFACE2, fg=TEXT_MUTED, relief="flat",
                  bd=0, padx=8, command=self._choose_apps).pack(side="left", padx=(10, 0))
        tk.Checkbutton(f, text="Music guard", variable=self.music_guard,
                       bg=BG, fg=TEXT, selectcolor=SURFACE2, activebackground=BG,
                       font=("Segoe UI", 10)).pack(side="left", padx=(10, 0))
        self.apps_lbl = tk.Label(self.root, text=self._apps_summary(), bg=BG,
                                 fg=TEXT_MUTED, font=("Segoe UI", 9))
        self.apps_lbl.pack(anchor="w", padx=16)

        f = self._row("Capture device — pick 'CABLE Input' here if you route apps "
                      "through VB-Cable (Win10 per-app setup)")
        self.dev_combo = ttk.Combobox(f, textvariable=self.capture_dev,
                                      state="readonly", width=52)
        self.dev_combo["values"] = [DEFAULT_DEVICE_LABEL] + list_loopback_devices()
        self.dev_combo.configure(postcommand=lambda: self.dev_combo.configure(
            values=[DEFAULT_DEVICE_LABEL] + list_loopback_devices()))
        self.dev_combo.pack(side="left")

        f = self._row("Caption style")
        sm = tk.OptionMenu(f, self.caption_style, *CAPTION_STYLES.keys(),
                           command=lambda _v: self._apply_style())
        sm.configure(bg=SURFACE2, fg=TEXT, relief="flat", highlightthickness=0,
                     activebackground=SURFACE)
        sm["menu"].configure(bg=SURFACE2, fg=TEXT)
        sm.pack(side="left")

        f = self._row("Overlay")
        tk.Checkbutton(f, text="Move mode (drag caption)", variable=self.move_mode,
                       command=self._toggle_move, bg=BG, fg=TEXT, selectcolor=SURFACE2,
                       activebackground=BG, font=("Segoe UI", 10)).pack(side="left")
        tk.Spinbox(f, from_=10, to=40, textvariable=self.font_size, width=4,
                   command=self._apply_font, bg=SURFACE2, fg=TEXT, relief="flat",
                   buttonbackground=SURFACE2).pack(side="left", padx=(12, 0))
        tk.Button(f, text="Test caption", bg=SURFACE2, fg=TEXT_MUTED, relief="flat",
                  bd=0, padx=8,
                  command=lambda: (self.overlay.update_live("live line updating…", 0),
                                   self.root.after(1000, lambda:
                                       self.overlay.commit_live("Speaker one — 说话的人一", 0)),
                                   self.root.after(1800, lambda:
                                       self.overlay.commit_live("Speaker two — 说话的人二", 1)))
                  ).pack(side="left", padx=(12, 0))

        self.start_btn = tk.Button(self.root, text="▶  START CAPTIONING",
                                   bg=ACCENT, fg="white", relief="flat", bd=0,
                                   font=("Segoe UI", 13, "bold"), padx=28, pady=10,
                                   cursor="hand2", command=self._toggle)
        self.start_btn.pack(pady=14)

        tk.Label(self.root, textvariable=self.status, bg=BG, fg=TEXT_MUTED,
                 font=("Segoe UI", 10), wraplength=560, justify="left"
                 ).pack(anchor="w", padx=16)
        tk.Label(self.root,
                 text="Tips: games in BORDERLESS WINDOWED • streaming mode bills your\n"
                      "Deepgram credit for the whole session while START is on • audio is\n"
                      "sent to Deepgram + Google while running",
                 bg=BG, fg="#4a4845", font=("Segoe UI", 9), justify="left"
                 ).pack(anchor="w", padx=16, pady=(8, 0))

    def _eff_gemini(self):
        return self.gemini_key.get().strip() or get_secret("gemini")

    def _eff_dg(self):
        return self.dg_key.get().strip() or get_secret("deepgram")

    def _apps_summary(self):
        if not self.selected_app_names:
            return "No apps selected yet — click Choose apps…"
        return "Selected: " + ", ".join(self.selected_app_names)

    def _apps_mode_blocked(self):
        if process_loopback_supported():
            return False
        messagebox.showwarning(
            "Not available on this Windows",
            f"Per-app capture needs Windows build {MIN_BUILD_PROCESS_LOOPBACK}+ "
            f"(Windows 11).\nThis PC is build {win_build()} — on this build "
            f"Windows silently captures ALL audio instead of just the selected "
            f"apps, so the feature is disabled to avoid misleading you.\n\n"
            f"Windows 10 alternative: route apps with VB-Cable and pick the "
            f"CABLE device under 'Capture device' (see README).")
        self.source_mode.set("system")
        return True

    def _choose_apps(self):
        if self._apps_mode_blocked():
            return
        if not HAS_PYCAW or not HAS_PROCTAP:
            messagebox.showerror("Missing dependency",
                                 "Per-app capture needs 'proctap' and 'pycaw'.\n"
                                 "Run install_and_run.bat again.")
            return
        apps = list_audio_apps()
        win = tk.Toplevel(self.root)
        win.title("Choose apps to caption")
        win.configure(bg=BG)
        win.geometry("380x420")
        tk.Label(win, text="Apps with an active audio session\n"
                           "(app must be running & have played sound)",
                 bg=BG, fg=TEXT_MUTED, font=("Segoe UI", 9), justify="left"
                 ).pack(anchor="w", padx=12, pady=(10, 4))
        vars_ = []
        box = tk.Frame(win, bg=BG)
        box.pack(fill="both", expand=True, padx=12)
        if not apps:
            tk.Label(box, text="Nothing found — play some audio first,\n"
                               "then reopen this window.",
                     bg=BG, fg=TEXT, font=("Segoe UI", 10)).pack(anchor="w")
        for pid, name in apps:
            v = tk.BooleanVar(value=name in self.selected_app_names)
            vars_.append((v, name))
            tk.Checkbutton(box, text=f"{name}   (pid {pid})", variable=v,
                           bg=BG, fg=TEXT, selectcolor=SURFACE2,
                           activebackground=BG, font=("Segoe UI", 10)
                           ).pack(anchor="w")
        def apply():
            self.selected_app_names = [n for v, n in vars_ if v.get()]
            self.apps_lbl.configure(text=self._apps_summary())
            if self.selected_app_names and process_loopback_supported():
                # choosing apps means the user wants apps mode — switch the
                # radio so START doesn't silently capture everything
                self.source_mode.set("apps")
            win.destroy()
        tk.Button(win, text="Save selection", bg=ACCENT, fg="white", relief="flat",
                  bd=0, padx=16, pady=6, command=apply).pack(pady=10)

    def _resolve_selected_pids(self):
        """Names saved in config -> current (pid, name) pairs."""
        current = list_audio_apps()
        chosen = []
        missing = []
        for name in self.selected_app_names:
            hits = [(pid, n) for pid, n in current if n == name]
            if hits:
                chosen.extend(hits)
            else:
                missing.append(name)
        return chosen, missing

    def _toggle_move(self):
        self.overlay.set_clickthrough(not self.move_mode.get())
        if self.move_mode.get():
            self.overlay.commit_live("Drag me, then turn Move mode off")

    def _apply_font(self):
        self.overlay.set_font_size(self.font_size.get())

    def _apply_style(self):
        self.overlay.set_style(self.caption_style.get())
        self.overlay.set_clickthrough(not self.move_mode.get())

    # -- start/stop -----------------------------------------------------
    def _toggle(self):
        if self.running:
            self._stop_all()
            return
        if not HAS_PAW:
            messagebox.showerror("Missing dependency",
                                 "pyaudiowpatch is not installed.\nRun install_and_run.bat")
            return
        if not self._eff_gemini():
            messagebox.showwarning("No Gemini key",
                                   "Enter your free Gemini API key first.\n"
                                   "https://aistudio.google.com/app/apikey")
            return
        streaming = self.mode.get() == "streaming"
        if streaming and not HAS_WS:
            messagebox.showerror("Missing dependency",
                                 "websocket-client is not installed.\nRun install_and_run.bat")
            return
        if streaming and not self._eff_dg():
            messagebox.showwarning("No Deepgram key",
                                   "Streaming mode needs a free Deepgram API key.\n"
                                   "Sign up at https://deepgram.com ($200 credit, no card)\n\n"
                                   "Or switch to Chunked mode (Gemini only).")
            return

        # secrets -> Windows Credential Manager; config file stays key-free.
        # Only newly TYPED keys are stored; fields are then cleared so stored
        # keys never linger in the UI.
        if self.gemini_key.get().strip():
            set_secret("gemini", self.gemini_key.get().strip())
            if HAS_KEYRING:
                self.gemini_key.set("")
        if self.dg_key.get().strip():
            set_secret("deepgram", self.dg_key.get().strip())
            if HAS_KEYRING:
                self.dg_key.set("")
        cfg = {"model": self.model.get(),
               "target_lang": self.target_lang.get().strip() or "English",
               "source_lang": self.source_lang.get().strip() or "multi",
               "mode": self.mode.get(),
               "source_mode": self.source_mode.get(),
               "capture_device": self.capture_dev.get(),
               "music_guard": self.music_guard.get(),
               "caption_style": self.caption_style.get(),
               "selected_apps": self.selected_app_names,
               "font_size": self.font_size.get()}
        if not HAS_KEYRING:
            # fallback: plain-text config (old behavior) with a warning
            cfg["api_key"] = self._eff_gemini()
            cfg["deepgram_key"] = self._eff_dg()
            self._status_async("⚠ keyring not installed — keys stored in plain "
                               "text. Run install_and_run.bat to fix.")
        save_config(cfg)

        self.running = True
        self.dropped = 0
        self.start_btn.configure(text="⏹  STOP", bg=ACCENT_RED)

        # pick capture backend
        frame_handler = None  # set below
        use_apps = self.source_mode.get() == "apps"
        if use_apps and self._apps_mode_blocked():
            self.running = False
            self.start_btn.configure(text="▶  START CAPTIONING", bg=ACCENT)
            return
        if use_apps:
            if not (HAS_PROCTAP and HAS_PYCAW):
                messagebox.showerror("Missing dependency",
                                     "Per-app capture needs 'proctap' and 'pycaw'.\n"
                                     "Run install_and_run.bat, or use All system audio.")
                self.running = False
                self.start_btn.configure(text="▶  START CAPTIONING", bg=ACCENT)
                return
            pids, missing = self._resolve_selected_pids()
            if missing:
                self._status_async("⚠ not running / no audio session: " + ", ".join(missing))
            if not pids:
                messagebox.showwarning(
                    "No apps to capture",
                    "None of your selected apps have an active audio session.\n"
                    "Start them (and let them play sound), or Choose apps… again.")
                self.running = False
                self.start_btn.configure(text="▶  START CAPTIONING", bg=ACCENT)
                return
            self._app_pids = pids

        if streaming:
            self.translator = Translator(
                get_creds=lambda: (self._eff_gemini(), self.model.get(),
                                   self.target_lang.get().strip() or "English"),
                on_result=lambda uid, text, end, spk=None: self.ui_queue.put(
                    ("live_final" if end else "live", (uid, text, spk))),
                on_status=self._status_async)
            self.translator.start()
            self.streamer = DeepgramStreamer(
                api_key=self._eff_dg(),
                source_lang=self.source_lang.get().strip() or "multi",
                on_update=self._on_transcript_update,
                on_status=self._status_async)
            self.streamer.start()
            self.gate = StreamGate(
                send_audio=self.streamer.send_audio,
                send_keepalive=self.streamer.send_keepalive,
                on_state=lambda open_: self.ui_queue.put(("gate", open_)))
            self.gate.enabled_music_guard = self.music_guard.get()
            self.gate.on_music = lambda: self.ui_queue.put(
                ("status", "🎵 continuous audio 60s — sounds like music. Pausing "
                           "streaming to save credit (auto-resumes on a pause, or in "
                           "3 min). If people talk over constant music, untick "
                           "Music guard."))
            frame_handler = self._frame_to_stream
        else:
            self.segmenter = Segmenter()
            frame_handler = self._frame_to_segmenter
        if use_apps:
            self.capture = AppSourceCapture(self._app_pids, frame_handler,
                                            self._status_async)
        else:
            dev = self.capture_dev.get()
            dev = None if (not dev or dev == DEFAULT_DEVICE_LABEL) else dev
            self.capture = LoopbackCapture(frame_handler, self._status_async,
                                           device_name=dev)
        self.capture.start()

    def _stop_all(self):
        self.running = False
        for x in (self.capture, self.streamer, self.translator):
            if x:
                x.stop()
        self.capture = self.streamer = self.translator = None
        self.gate = None
        self.start_btn.configure(text="▶  START CAPTIONING", bg=ACCENT)
        self.status.set("Stopped")

    def _on_close(self):
        self._stop_all()
        self.root.destroy()

    # -- streaming path ---------------------------------------------------
    def _status_async(self, msg):
        """Thread-safe status update (called from audio/ws/translator threads)."""
        self.ui_queue.put(("status", msg))

    def _frame_to_stream(self, frame):
        if not self.running or not self.gate:
            return
        self.gate.feed(frame)

    def _danmaku_active(self):
        return self.caption_style.get() == "Danmaku (sliding)"

    def _on_transcript_update(self, utt_id, text_so_far, is_end, speaker=None):
        if not (self.running and self.translator):
            return
        # Danmaku shows complete sentences only — skip partial translations
        # (it never displays them), submit just the finished utterance.
        if self._danmaku_active() and not is_end:
            return
        if is_end:
            print(f"[dbg] FINAL utt={utt_id} spk={speaker} "
                  f"text={text_so_far[:40]!r}")
        self.translator.submit(utt_id, text_so_far, is_end, speaker)

    # -- chunked path -------------------------------------------------------
    def _frame_to_segmenter(self, frame):
        if not self.running or not self.segmenter:
            return
        utter = self.segmenter.feed(frame)
        if utter is not None:
            self._on_utterance(utter)

    def _on_utterance(self, samples):
        now = time.time()
        if now < self.rate_cooldown_until:
            return self._drop("cooldown")
        limit = RPM_LIMITS.get(self.model.get(), 13)
        while self.req_times and now - self.req_times[0] > 60:
            self.req_times.popleft()
        if len(self.req_times) >= limit:
            return self._drop("RPM budget")
        with self.inflight_lock:
            if self.inflight >= MAX_INFLIGHT:
                return self._drop("backlog")
            self.inflight += 1
        self.req_times.append(now)
        threading.Thread(target=self._chunk_worker, args=(samples,), daemon=True).start()

    def _drop(self, why):
        self.dropped += 1
        self.ui_queue.put(("status", f"⚠ dropped chunk ({why}) — total {self.dropped}"))

    def _chunk_worker(self, samples):
        try:
            self.ui_queue.put(("status", f"🌐 translating {len(samples)/TARGET_SR:.1f}s of speech…"))
            wav = build_wav_bytes(samples)
            text = gemini_caption_audio(wav, self._eff_gemini(), self.model.get(),
                                        self.target_lang.get().strip() or "English",
                                        list(self.recent_captions))
            if text:
                self.recent_captions.append(text)
                self.ui_queue.put(("caption", text))
                self.ui_queue.put(("status", "✓ caption shown — listening…"))
        except RateLimited:
            self.rate_cooldown_until = time.time() + 15
            self.ui_queue.put(("status", "⚠ 429 rate-limited — pausing 15s"))
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            if code == 404:
                self.ui_queue.put(("status", f"⚠ model '{self.model.get()}' not found (404) — "
                                             f"try the other model in the dropdown"))
            else:
                self.ui_queue.put(("status", f"⚠ API error {code}: {self._redact(e)}"))
        except Exception as e:
            self.ui_queue.put(("status", f"⚠ error (chunk skipped): {self._redact(e)}"))
        finally:
            with self.inflight_lock:
                self.inflight -= 1

    def _redact(self, err):
        msg = str(err)
        for key in (self._eff_gemini(), self._eff_dg()):
            if key:
                msg = msg.replace(key, "•••KEY•••")
        return msg

    # -- UI pump -------------------------------------------------------------
    def _poll(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "caption":                     # chunked mode
                    self.overlay.commit_live(payload)
                elif kind == "live":                      # streaming partial
                    uid, text, spk = payload
                    if uid >= self.current_utt:
                        self.current_utt = uid
                        self.overlay.update_live(text, spk)
                        self.last_live_at = time.time()
                elif kind == "live_final":                # streaming utterance end
                    uid, text, spk = payload
                    if uid >= self.current_utt:
                        self.current_utt = uid
                        # fragment ids encode utt*1000+seg (danmaku path);
                        # seg>0 marks a continuation of the same sentence
                        self.overlay.commit_live(
                            text, spk,
                            cont=(uid >= 1000 and uid % 1000 > 0),
                            group=(uid // 1000 if uid >= 1000 else None))
                        self.last_live_at = 0.0
                elif kind == "gate":
                    pct = f" · {self.gate.sent_pct():.0f}% of audio sent" if self.gate else ""
                    self.status.set(("🎙 voice detected — streaming" if payload
                                     else "💤 quiet — gate closed (not billed)") + pct)
                elif kind == "status":
                    self.status.set(payload)
        except queue.Empty:
            pass
        # watchdog: a live line with no updates for a while is done — commit it.
        # (last_live_at is nonzero only while an uncommitted live line exists)
        if (self.last_live_at
                and time.time() - self.last_live_at > LIVE_STALE_COMMIT_S):
            self.overlay.commit_live()
            self.last_live_at = 0.0
        self.root.after(80, self._poll)


# ── ENTRY ─────────────────────────────────────────────────────────
def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
