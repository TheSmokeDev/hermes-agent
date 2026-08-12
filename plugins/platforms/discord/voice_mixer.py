from __future__ import annotations

"""
Continuous PCM audio mixer for Discord voice channels.

discord.py (Rapptz) ships no audio mixer: ``VoiceClient.play()`` accepts a
single :class:`discord.AudioSource` and raises ``ClientException`` if called
while already playing.  One opus stream per connection, one source feeding it.

This module adds software mixing *upstream* of that single stream.  A
:class:`VoiceMixer` is itself a ``discord.AudioSource`` that discord.py polls
every 20 ms via :meth:`read`.  Internally it sums the 20 ms PCM frames of any
number of child sources, clamps to int16, and returns one blended frame.
discord.py never knows several streams were combined underneath — it just
encodes and sends the single mixed frame.

This gives us, for one voice connection at once:

  * an always-on low-volume **ambient/idle loop** (the "thinking" sound),
  * a **speech** channel (TTS replies, verbal acknowledgements) that plays
    *over* the ambient bed, automatically **ducking** the ambient gain down
    while speech is active and restoring it when speech ends — the smooth
    Grok-voice-mode feel, instead of stop-and-swap.

Design notes
------------
* The mixer is installed **once** per guild on join (``vc.play(mixer)``) and
  runs continuously until the bot leaves.  Children come and go; the mixer
  itself never stops, so there is no ``is_playing()`` race between an
  acknowledgement and the final reply.
* Frame format is Discord-native: 48 kHz, 2 channels, signed 16-bit LE,
  20 ms per frame == ``discord.opus.Encoder.FRAME_SIZE`` bytes
  (3840 = 960 samples * 2 channels * 2 bytes).
* Mixing is a single vectorised int32 add + clip per 20 ms frame (numpy,
  already a core dependency).  CPU cost is negligible.
* :meth:`read` is called from discord.py's audio sender **thread**, while
  children are added/removed from the asyncio event loop thread, so all
  shared state is guarded by a plain ``threading.Lock``.

The mixer NEVER touches the inbound receive path: it only produces the bot's
*outgoing* stream.  The :class:`VoiceReceiver` decodes incoming SSRCs only, so
the mixer's output cannot echo back into transcription.
"""

import asyncio
from collections import deque
from dataclasses import dataclass
import logging
import struct
import threading
from typing import TYPE_CHECKING, List, Optional

import discord

try:
    from .ffmpeg_utils import resolve_ffmpeg_executable
except ImportError:
    from ffmpeg_utils import resolve_ffmpeg_executable

if TYPE_CHECKING:  # numpy is an optional ("voice" extra) dep — never import at runtime top-level
    import numpy as np

logger = logging.getLogger(__name__)


def _require_numpy():
    """Import numpy lazily.

    numpy ships in the optional ``voice`` extra, not the base install, so this
    module must import cleanly without it (the Discord adapter imports this
    file unconditionally).  Callers that actually mix audio call this; if the
    voice extra isn't installed they get a clear error instead of a top-level
    ImportError that would break the whole adapter import.
    """
    import numpy as np  # noqa: PLC0415 — intentional lazy import
    return np

# Discord-native frame geometry (matches discord.opus.Encoder).
SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2                       # bytes per sample (s16)
FRAME_LENGTH_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_LENGTH_MS // 1000   # 960
FRAME_SIZE = SAMPLES_PER_FRAME * CHANNELS * SAMPLE_WIDTH    # 3840 bytes
BYTES_PER_MS = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH // 1000  # 192
SILENCE_FRAME = b"\x00" * FRAME_SIZE
NATIVE_ID_MAX_CHARS = 512  # Compact bound compatible with provider-generated IDs.


@dataclass(frozen=True, slots=True, eq=False)
class NativePlaybackReceipt:
    lease_id: str
    response_id: str
    turn_marker: str
    generation: int
    accepted_provider_bytes: int
    lease_frames: int
    interrupted: bool


class NativePCMLease:
    """One identity-bound native PCM stream owned by a VoiceMixer."""

    def __init__(self, mixer, lease_id, response_id, turn_marker, generation, loop):
        self._mixer = mixer
        self.lease_id = lease_id
        self.response_id = response_id
        self.turn_marker = turn_marker
        self.generation = generation
        self._loop = loop
        self._closed = False
        self._terminal = False
        self._interrupted = False
        self._carry = b""
        self._last_sample = None
        self._converted = bytearray()
        self._converted_provider_bytes = 0
        self._frames = deque()
        self._accepted = 0
        self._frame_count = 0
        self._space_waiters = deque()
        self._drained = loop.create_future()
        self._write_lock = asyncio.Lock()
        self._terminal_lock = asyncio.Lock()
        self._close_task = None
        self._write_reserved = False
        self.max_write_bytes = (mixer._native_frame_capacity + 1) * 960
        self._receipt = None

    @staticmethod
    def _midpoint(left, right):
        total = left + right
        return total // 2 if total >= 0 else -((-total) // 2)

    async def write_pcm(self, data):
        if type(data) is not bytes:
            raise TypeError("PCM data must be exact bytes")
        if len(data) > self.max_write_bytes:
            raise ValueError("PCM write exceeds bounded per-call capacity")
        with self._mixer._lock:
            if self._write_reserved:
                raise RuntimeError("native playback write already in flight")
            if self._terminal or self._closed:
                raise RuntimeError("native playback lease is terminal")
            self._write_reserved = True
        task = self._loop.create_task(self._write_pcm(bytes(data)))
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                with self._mixer._lock:
                    self._write_reserved = False
            else:
                task.add_done_callback(self._release_write_reservation)

    def _release_write_reservation(self, task):
        task.exception() if not task.cancelled() else None
        with self._mixer._lock:
            self._write_reserved = False

    async def _write_pcm(self, data):
        async with self._write_lock:
            if self._terminal or self._closed:
                raise RuntimeError("native playback lease is terminal")
            raw = self._carry + data
            carry_bytes = len(self._carry)
            self._carry = raw[-1:] if len(raw) % 2 else b""
            usable = raw[:-1] if self._carry else raw
            provider_bytes = len(usable) - carry_bytes
            for index, (sample,) in enumerate(struct.iter_unpack("<h", usable)):
                if self._last_sample is None:
                    first = sample
                else:
                    first = self._midpoint(self._last_sample, sample)
                self._converted.extend(struct.pack("<hhhh", first, first, sample, sample))
                self._converted_provider_bytes += min(2, max(0, provider_bytes - index * 2))
                self._last_sample = sample
                await self._enqueue_complete_frames()
            if self._carry:
                self._converted_provider_bytes += 1

    async def _enqueue_complete_frames(self):
        while len(self._converted) >= FRAME_SIZE:
            await self._wait_for_space()
            with self._mixer._lock:
                if self._terminal and self._interrupted:
                    raise RuntimeError("native playback lease was interrupted")
                self._frames.append(bytes(self._converted[:FRAME_SIZE]))
                del self._converted[:FRAME_SIZE]
                accepted = min(960, self._converted_provider_bytes)
                self._converted_provider_bytes -= accepted
                self._accepted += accepted
                self._frame_count += 1

    async def _wait_for_space(self):
        while True:
            with self._mixer._lock:
                outstanding = len(self._frames) + (self._mixer._native_prior_lease is self)
                if outstanding < self._mixer._native_frame_capacity:
                    return
                waiter = self._loop.create_future()
                self._space_waiters.append(waiter)
            await waiter

    async def finish_and_wait(self, timeout):
        async with self._write_lock:
            if self._carry:
                raise ValueError("odd total provider PCM byte count")
            if not self._terminal:
                self._last_sample = None
                if self._converted:
                    self._converted.extend(b"\x00" * (FRAME_SIZE - len(self._converted)))
                await self._enqueue_complete_frames()
                self._terminal = True
                self._maybe_complete()
        return await asyncio.wait_for(asyncio.shield(self._drained), timeout)

    async def interrupt_and_wait(self, timeout):
        with self._mixer._lock:
            self._terminal = True
            self._interrupted = True
            self._carry = b""
            self._last_sample = None
            self._converted.clear()
            self._converted_provider_bytes = 0
            self._frames.clear()
            self._wake_space_locked()
        self._maybe_complete()
        return await asyncio.wait_for(asyncio.shield(self._drained), timeout)

    def _wake_space_locked(self):
        while self._space_waiters:
            waiter = self._space_waiters.popleft()
            self._loop.call_soon_threadsafe(_set_future_result, waiter, None)

    def _ack_prior_frame(self):
        with self._mixer._lock:
            self._wake_space_locked()
        self._maybe_complete()

    def _take_frame_locked(self):
        if not self._frames:
            return None
        frame = self._frames.popleft()
        return frame

    def _maybe_complete(self):
        with self._mixer._lock:
            done = (
                self._terminal
                and not self._frames
                and self._mixer._native_prior_lease is not self
            )
        if done:
            with self._mixer._lock:
                if self._receipt is None:
                    self._receipt = NativePlaybackReceipt(
                        self.lease_id, self.response_id, self.turn_marker, self.generation,
                        self._accepted, self._frame_count, self._interrupted,
                    )
                receipt = self._receipt
            self._loop.call_soon_threadsafe(_set_future_result, self._drained, receipt)

    def _validate_receipt(self, receipt):
        if receipt is not self._receipt:
            raise ValueError("receipt was not minted by this lease")
        return receipt

    async def close(self):
        async with self._terminal_lock:
            if self._closed:
                return
            if self._close_task is None:
                self._close_task = self._loop.create_task(self._close())
            task = self._close_task
        await asyncio.shield(task)

    async def _close(self):
        with self._mixer._lock:
            self._terminal = True
            self._interrupted = True
            self._carry = b""
            self._last_sample = None
            self._converted.clear()
            self._converted_provider_bytes = 0
            self._frames.clear()
            if self._mixer._native_prior_lease is self:
                self._mixer._native_prior_lease = None
            self._wake_space_locked()
        self._maybe_complete()
        await asyncio.shield(self._drained)
        self._mixer._close_native_lease(self)
        self._closed = True


def _set_future_result(future, value):
    if not future.done():
        future.set_result(value)


class MixerChild:
    """A single audio stream feeding into :class:`VoiceMixer`.

    Wraps raw 48 kHz / stereo / s16le PCM bytes.  ``read_frame`` hands back one
    20 ms frame at a time, optionally looping, with a per-child gain applied.
    """

    __slots__ = (
        "name", "_pcm", "_pos", "loop", "gain",
        "is_speech", "fade_frames", "_fade_done", "_finished",
    )

    def __init__(
        self,
        name: str,
        pcm: bytes,
        *,
        loop: bool = False,
        gain: float = 1.0,
        is_speech: bool = False,
        fade_in_ms: int = 0,
    ):
        # Pad to a whole number of frames so looping is seamless and the final
        # partial frame doesn't click.
        remainder = len(pcm) % FRAME_SIZE
        if remainder:
            pcm = pcm + b"\x00" * (FRAME_SIZE - remainder)
        self.name = name
        self._pcm = pcm
        self._pos = 0
        self.loop = loop
        self.gain = float(gain)
        self.is_speech = is_speech
        # Linear fade-in over N frames avoids a click when a loud child starts.
        self.fade_frames = max(0, fade_in_ms // FRAME_LENGTH_MS)
        self._fade_done = 0
        self._finished = False

    @property
    def finished(self) -> bool:
        return self._finished

    def read_frame(self) -> Optional[list[float]]:
        """Return the next frame as signed samples, or None if done."""
        if self._finished:
            return None
        if self._pos >= len(self._pcm):
            if self.loop and self._pcm:
                self._pos = 0
            else:
                self._finished = True
                return None

        chunk = self._pcm[self._pos:self._pos + FRAME_SIZE]
        self._pos += FRAME_SIZE
        if len(chunk) < FRAME_SIZE:
            chunk = chunk + b"\x00" * (FRAME_SIZE - len(chunk))

        samples = list(struct.unpack("<1920h", chunk))

        gain = self.gain
        if self.fade_frames and self._fade_done < self.fade_frames:
            self._fade_done += 1
            gain *= self._fade_done / self.fade_frames

        if gain != 1.0:
            samples = [sample * gain for sample in samples]
        return samples


class VoiceMixer(discord.AudioSource):
    """A continuous ``discord.AudioSource`` that mixes N child streams.

    Use :meth:`set_ambient` to install/replace the looping idle bed and
    :meth:`play_speech` to layer a one-shot clip over it (ducking the ambient
    while it plays).  Both are safe to call from the asyncio loop thread while
    discord.py drains :meth:`read` from its sender thread.
    """

    # discord.AudioSource subclasses set is_opus()==False to receive PCM.
    def is_opus(self) -> bool:  # pragma: no cover - trivial
        return False

    def __init__(
        self,
        *,
        ambient_gain: float = 0.18,
        duck_gain: float = 0.06,
        speech_gain: float = 1.0,
        duck_release_ms: int = 400,
        native_frame_capacity: int = 8,
    ):
        if type(native_frame_capacity) is not int or native_frame_capacity <= 0:
            raise ValueError("native_frame_capacity must be a positive exact int")
        self._lock = threading.Lock()
        self._ambient: Optional[MixerChild] = None
        self._speech: List[MixerChild] = []
        self._ambient_gain = float(ambient_gain)
        self._duck_gain = float(duck_gain)
        self._speech_gain = float(speech_gain)
        # When speech ends, ramp the ambient back up over this many frames
        # instead of jumping, so the bed swells back smoothly.
        self._duck_release_frames = max(1, duck_release_ms // FRAME_LENGTH_MS)
        self._duck_release_left = 0
        self._closed = False
        # Tracks whether speech is currently active, for external callers that
        # want to avoid double-ducking or know when a reply is mid-flight.
        self._speech_active = False
        self._native_frame_capacity = native_frame_capacity
        self._native_lease = None
        self._native_prior_lease = None

    def acquire_native_playback(self, lease_id, response_id, turn_marker, generation):
        """Acquire the mixer's single host-owned native playback lane."""
        identifiers = (lease_id, response_id, turn_marker)
        if any(type(value) is not str for value in identifiers):
            raise TypeError("native playback identifiers must be exact strings")
        identifiers = tuple(value.strip() for value in identifiers)
        if any(not value for value in identifiers):
            raise ValueError("native playback identifiers must be nonblank")
        if any(len(value) > NATIVE_ID_MAX_CHARS for value in identifiers):
            raise ValueError(f"native playback identifiers exceed {NATIVE_ID_MAX_CHARS} characters")
        if type(generation) is not int or generation <= 0:
            raise TypeError("generation must be a positive exact int")
        lease_id, response_id, turn_marker = identifiers
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("native playback acquisition requires a running loop") from exc
        with self._lock:
            if self._closed:
                raise RuntimeError("voice mixer is closed")
            if self._native_lease is not None:
                raise RuntimeError("native playback lease already active")
            lease = NativePCMLease(self, lease_id, response_id, turn_marker, generation, loop)
            self._native_lease = lease
            return lease

    def _close_native_lease(self, lease):
        with self._lock:
            if self._native_lease is lease:
                self._native_lease = None

    def validate_native_receipt(self, receipt, lease):
        with self._lock:
            if self._native_lease is not lease:
                raise ValueError("lease is not active on this mixer")
        return lease._validate_receipt(receipt)

    # ------------------------------------------------------------------
    # Ambient (idle / "thinking") bed
    # ------------------------------------------------------------------

    def set_ambient(self, pcm: Optional[bytes], *, gain: Optional[float] = None) -> None:
        """Install (or clear, with ``pcm=None``) the looping ambient bed."""
        with self._lock:
            if gain is not None:
                self._ambient_gain = float(gain)
            if not pcm:
                self._ambient = None
                return
            self._ambient = MixerChild(
                "ambient", pcm, loop=True,
                gain=self._effective_ambient_gain(), fade_in_ms=200,
            )

    def _effective_ambient_gain(self) -> float:
        return self._duck_gain if self._speech_active else self._ambient_gain

    # ------------------------------------------------------------------
    # Speech (TTS replies, verbal acks) layered over the ambient bed
    # ------------------------------------------------------------------

    def play_speech(self, pcm: bytes, *, gain: Optional[float] = None,
                    fade_in_ms: int = 40) -> None:
        """Layer a one-shot speech clip over the ambient bed (ducks ambient)."""
        if not pcm:
            return
        with self._lock:
            child = MixerChild(
                "speech", pcm, loop=False,
                gain=self._speech_gain if gain is None else float(gain),
                is_speech=True, fade_in_ms=fade_in_ms,
            )
            self._speech.append(child)
            self._speech_active = True
            self._duck_release_left = 0
            if self._ambient is not None:
                self._ambient.gain = self._duck_gain

    @property
    def speech_active(self) -> bool:
        with self._lock:
            return self._speech_active

    def stop_speech(self) -> None:
        """Drop any in-flight speech immediately and release the duck."""
        with self._lock:
            self._speech.clear()
            self._begin_duck_release_locked()

    def _begin_duck_release_locked(self) -> None:
        self._speech_active = False
        self._duck_release_left = self._duck_release_frames

    # ------------------------------------------------------------------
    # AudioSource interface — called from discord.py's sender thread
    # ------------------------------------------------------------------

    def read(self) -> bytes:
        """Return one 20 ms mixed PCM frame (always FRAME_SIZE bytes).

        Returning a non-empty frame keeps discord.py's player alive; we never
        return b"" because that would stop the single underlying stream and we
        want the mixer to run continuously for the lifetime of the connection.
        """
        prior = None
        with self._lock:
            if self._native_prior_lease is not None:
                prior = self._native_prior_lease
                self._native_prior_lease = None
        if prior is not None:
            prior._ack_prior_frame()

        with self._lock:
            if self._closed:
                return SILENCE_FRAME

            native = None
            if self._native_lease is not None:
                native = self._native_lease._take_frame_locked()
                if native is not None:
                    self._native_prior_lease = self._native_lease

            acc: Optional[list[float]] = None

            # Speech children (drop exhausted ones; release duck when last ends)
            if self._speech:
                still_live: List[MixerChild] = []
                for child in self._speech:
                    frame = child.read_frame()
                    if frame is None:
                        continue
                    acc = frame if acc is None else [a + b for a, b in zip(acc, frame)]
                    still_live.append(child)
                self._speech = still_live
                if not self._speech and self._speech_active:
                    self._begin_duck_release_locked()

            # Ambient bed — ramp gain back up during duck-release.
            if self._ambient is not None:
                if self._duck_release_left > 0 and not self._speech_active:
                    self._duck_release_left -= 1
                    frac = 1.0 - (self._duck_release_left / self._duck_release_frames)
                    self._ambient.gain = (
                        self._duck_gain
                        + (self._ambient_gain - self._duck_gain) * frac
                    )
                elif not self._speech_active and self._duck_release_left == 0:
                    self._ambient.gain = self._ambient_gain
                amb = self._ambient.read_frame()
                if amb is not None:
                    acc = amb if acc is None else [a + b for a, b in zip(acc, amb)]

            if acc is None:
                return native if native is not None else SILENCE_FRAME

            mixed_samples = [max(-32768, min(32767, int(sample))) for sample in acc]
            if native is None:
                return struct.pack("<1920h", *mixed_samples)
            native_samples = struct.unpack("<1920h", native)
            return struct.pack(
                "<1920h",
                *(max(-32768, min(32767, a + b)) for a, b in zip(native_samples, mixed_samples)),
            )

    def cleanup(self) -> None:  # called by discord.py when playback stops
        lease = None
        with self._lock:
            self._closed = True
            self._ambient = None
            self._speech.clear()
            lease = self._native_lease
            self._native_lease = None
            self._native_prior_lease = None
            if lease is not None:
                lease._terminal = True
                lease._interrupted = True
                lease._carry = b""
                lease._last_sample = None
                lease._converted.clear()
                lease._converted_provider_bytes = 0
                lease._frames.clear()
                lease._wake_space_locked()
        if lease is not None:
            lease._maybe_complete()


# ----------------------------------------------------------------------
# PCM helpers
# ----------------------------------------------------------------------

def decode_to_pcm(path: str, *, timeout: float = 30.0) -> Optional[bytes]:
    """Decode any audio file to 48 kHz / stereo / s16le PCM via ffmpeg.

    Returns the raw PCM bytes, or None on failure.  ffmpeg is already a hard
    requirement of the voice path (see ``VoiceReceiver.pcm_to_wav``).
    """
    import subprocess

    try:
        proc = subprocess.run(
            [
                resolve_ffmpeg_executable(), "-y", "-loglevel", "error",
                "-i", path,
                "-f", "s16le",
                "-ar", str(SAMPLE_RATE),
                "-ac", str(CHANNELS),
                "pipe:1",
            ],
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("decode_to_pcm failed for %s: %s", path, e)
        return None
    if proc.returncode != 0:
        logger.warning(
            "ffmpeg decode failed for %s (rc=%d): %s",
            path, proc.returncode, (proc.stderr or b"").decode("utf-8", "replace")[:200],
        )
        return None
    return proc.stdout or None


def synth_ambient_pcm(seconds: float = 4.0) -> bytes:
    """Synthesise a subtle looping ambient bed (no asset file required).

    A soft, slowly-pulsing low pad: two detuned sine partials with a gentle
    tremolo, plus a touch of filtered noise.  Designed to loop seamlessly
    (whole number of cycles, zero-crossing endpoints) and sit quietly under
    speech.  Mono content duplicated to stereo.
    """
    np = _require_numpy()
    n = int(SAMPLE_RATE * seconds)
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE

    # Choose base frequencies that complete whole cycles over the loop so the
    # wrap point is click-free.
    def _whole_cycle_freq(target: float) -> float:
        cycles = max(1, round(target * seconds))
        return cycles / seconds

    f1 = _whole_cycle_freq(110.0)
    f2 = _whole_cycle_freq(110.5)
    trem = _whole_cycle_freq(0.5)   # ~0.5 Hz tremolo

    pad = (
        0.55 * np.sin(2 * np.pi * f1 * t)
        + 0.45 * np.sin(2 * np.pi * f2 * t)
    )
    tremolo = 0.6 + 0.4 * (0.5 * (1 + np.sin(2 * np.pi * trem * t)))
    signal = pad * tremolo

    # Smooth filtered noise for air, kept very low.
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(n)
    kernel = np.ones(64) / 64.0
    noise = np.convolve(noise, kernel, mode="same")
    signal = signal + 0.08 * noise

    # Normalise to a modest peak (mixer applies the real ambient gain on top).
    peak = float(np.max(np.abs(signal))) or 1.0
    signal = (signal / peak) * 0.5

    mono16 = (signal * 32767.0).astype(np.int16)
    stereo16 = np.repeat(mono16[:, None], CHANNELS, axis=1).reshape(-1)
    return stereo16.tobytes()
