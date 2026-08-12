"""Tests for the Discord continuous voice mixer (ambient + ducked speech)
and the verbal-ack-before-tool-calls hook.

The mixer (plugins/platforms/discord/voice_mixer.py) is pure-PCM and has no
discord.py dependency, so its core is tested directly.  The adapter
integration (install on join, play routing, ack) is tested with the standard
``object.__new__(DiscordAdapter)`` helper used elsewhere in the voice suite.
"""

import asyncio
import os
import struct
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Only ambient synthesis needs NumPy. Core mixing and native playback do not.
try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised in an isolated import test
    np = None

# voice_mixer lives inside the discord plugin package dir; import by path the
# same way the adapter does.
_DISCORD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "plugins", "platforms", "discord",
)
if _DISCORD_DIR not in sys.path:
    sys.path.insert(0, _DISCORD_DIR)

import voice_mixer as vm  # noqa: E402


# =====================================================================
# Pure mixer unit tests
# =====================================================================

class TestVoiceMixerCore:
    def test_frame_geometry_matches_discord(self):
        # 20ms @ 48kHz stereo s16 == 3840 bytes (discord.opus.Encoder.FRAME_SIZE)
        assert vm.FRAME_SIZE == 3840
        assert vm.SAMPLES_PER_FRAME == 960
        assert len(vm.SILENCE_FRAME) == vm.FRAME_SIZE

    def test_empty_mixer_returns_silence_frames(self):
        mx = vm.VoiceMixer()
        for _ in range(5):
            frame = mx.read()
            assert len(frame) == vm.FRAME_SIZE
            assert frame == vm.SILENCE_FRAME

    def test_is_opus_false(self):
        # discord.py sends raw PCM when is_opus() is False.
        assert vm.VoiceMixer().is_opus() is False

    @pytest.mark.asyncio
    async def test_native_lease_requires_exact_identity_and_is_exclusive(self):
        mx = vm.VoiceMixer(native_frame_capacity=1)
        with pytest.raises(TypeError):
            mx.acquire_native_playback(str("lease"), "response", "turn", True)
        lease = mx.acquire_native_playback("lease", "response", "turn", 1)
        with pytest.raises(RuntimeError):
            mx.acquire_native_playback("other", "response", "turn", 2)
        await lease.close()
        replacement = mx.acquire_native_playback("other", "response", "turn", 2)
        await replacement.close()

    @pytest.mark.asyncio
    async def test_native_conversion_and_honest_sender_boundary_drain(self):
        mx = vm.VoiceMixer(native_frame_capacity=1)
        lease = mx.acquire_native_playback("lease", "response", "turn", 1)
        pcm = struct.pack("<hhh", 0, 2, -3)
        await lease.write_pcm(pcm)
        finishing = asyncio.create_task(lease.finish_and_wait(1))
        await asyncio.sleep(0)
        assert not finishing.done()
        frame = mx.read()
        assert struct.unpack("<12h", frame[:24]) == (0, 0, 0, 0, 1, 1, 2, 2, 0, 0, -3, -3)
        assert not finishing.done()
        mx.read()  # acknowledges the prior synchronous sender return
        receipt = await finishing
        assert receipt.accepted_provider_bytes == len(pcm)
        assert receipt.lease_frames == 1
        assert receipt.interrupted is False

    @pytest.mark.asyncio
    async def test_chunk_permutations_odd_eos_and_exact_geometry(self):
        pcm = struct.pack("<480h", *range(480))
        outputs = []
        for chunks in ([pcm], [pcm[:1], pcm[1:]], [pcm[i:i + 1] for i in range(len(pcm))]):
            mx = vm.VoiceMixer()
            lease = mx.acquire_native_playback("lease", "response", "turn", 1)
            for chunk in chunks:
                await lease.write_pcm(chunk)
            finishing = asyncio.create_task(lease.finish_and_wait(1))
            await asyncio.sleep(0)
            outputs.append(mx.read())
            mx.read()
            await finishing
        assert outputs[0] == outputs[1] == outputs[2]
        assert len(outputs[0]) == 3840

        mx = vm.VoiceMixer()
        lease = mx.acquire_native_playback("odd", "response", "turn", 2)
        await lease.write_pcm(b"\x00")
        with pytest.raises(ValueError):
            await lease.finish_and_wait(1)

    @pytest.mark.asyncio
    async def test_bounded_write_interrupt_and_receipt_identity(self):
        mx = vm.VoiceMixer(native_frame_capacity=1)
        lease = mx.acquire_native_playback("lease", "response", "turn", 1)
        payload = struct.pack("<960h", *range(960))
        writer = asyncio.create_task(lease.write_pcm(payload))
        await asyncio.sleep(0)
        assert not writer.done()
        first = mx.read()
        await asyncio.sleep(0)
        assert not writer.done()
        second = mx.read()
        await asyncio.wait_for(writer, 1)
        assert len(first) == len(second) == 3840
        interrupting = asyncio.create_task(lease.interrupt_and_wait(1))
        await asyncio.sleep(0)
        assert not interrupting.done()
        mx.read()
        receipt = await interrupting
        assert receipt.interrupted is True
        assert receipt.accepted_provider_bytes == len(payload)
        forged = vm.NativePlaybackReceipt(
            receipt.lease_id, receipt.response_id, receipt.turn_marker,
            receipt.generation, receipt.accepted_provider_bytes,
            receipt.lease_frames, receipt.interrupted,
        )
        assert mx.validate_native_receipt(receipt, lease) is receipt
        with pytest.raises(ValueError):
            mx.validate_native_receipt(forged, lease)
        with pytest.raises(RuntimeError):
            await lease.write_pcm(b"\x00\x00")
        await lease.close()
        await lease.close()

    @pytest.mark.skipif(np is None, reason="ambient synthesis requires optional NumPy")
    def test_ambient_loops_and_is_quiet(self):
        mx = vm.VoiceMixer(ambient_gain=0.2)
        amb = vm.synth_ambient_pcm(seconds=0.5)
        assert len(amb) % vm.FRAME_SIZE == 0  # frame-aligned for seamless loop
        mx.set_ambient(amb)
        peaks = [int(np.max(np.abs(np.frombuffer(mx.read(), dtype=np.int16))))
                 for _ in range(100)]  # 2s >> 0.5s loop
        # Produces audio after the fade-in and stays under the configured gain.
        assert any(p > 0 for p in peaks[10:])
        assert max(peaks) < int(32767 * 0.5)


# =====================================================================
# Adapter integration
# =====================================================================

def _make_adapter(fx_cfg=None):
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.config import Platform, PlatformConfig
    config = PlatformConfig(enabled=True, extra={})
    config.token = "fake-token"
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter.config = config
    adapter._client = MagicMock()
    adapter._voice_clients = {}
    adapter._voice_locks = {}
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._voice_timeout_tasks = {}
    adapter._voice_receivers = {}
    adapter._voice_listen_tasks = {}
    adapter._voice_mixers = {}
    adapter._ambient_pcm_cache = None
    adapter._voice_fx_cfg = fx_cfg if fx_cfg is not None else {
        "enabled": True, "ambient_enabled": True, "ambient_path": "",
        "ambient_gain": 0.18, "duck_gain": 0.06, "speech_gain": 1.0,
        "ack_enabled": True, "ack_phrases": ["One moment."],
    }
    return adapter


class TestVoiceMixerActive:


    def test_false_when_attr_missing(self):
        # Defensive getattr path (object.__new__ helper that forgot the attr).
        from plugins.platforms.discord.adapter import DiscordAdapter
        from gateway.config import Platform
        bare = object.__new__(DiscordAdapter)
        bare.platform = Platform.DISCORD
        assert bare.voice_mixer_active(111) is False


class TestPlayInVoiceChannelMixerPath:
    @pytest.mark.asyncio
    async def test_routes_through_mixer_when_present(self):
        adapter = _make_adapter()
        vc = MagicMock()
        vc.is_connected.return_value = True
        adapter._voice_clients[111] = vc

        # speech_active returns True once (so play_speech is observed) then
        # False so the wait loop exits promptly.
        class _Mixer:
            def __init__(self):
                self._polls = 0
                self.play_speech = MagicMock()

            @property
            def speech_active(self):
                self._polls += 1
                return self._polls <= 1

        mixer = _Mixer()
        adapter._voice_mixers[111] = mixer
        adapter._reset_voice_timeout = MagicMock()

        fake_pcm = b"\x00" * vm.FRAME_SIZE
        with patch.object(vm, "decode_to_pcm", return_value=fake_pcm):
            ok = await adapter.play_in_voice_channel(111, "/tmp/x.mp3")
        assert ok is True
        mixer.play_speech.assert_called_once()
        adapter._reset_voice_timeout.assert_called_once_with(111)
        # Legacy path must NOT have been used.
        vc.play.assert_not_called()


class TestLeadSilence:
    """Warm-up lead silence prepended to speech so the first word isn't clipped
    (issue #66827)."""

    def test_bytes_empty_when_unset(self):
        adapter = _make_adapter()  # default cfg has no lead_silence_ms
        assert adapter._lead_silence_bytes() == b""


    def test_bytes_length_matches_ms(self):
        adapter = _make_adapter({"lead_silence_ms": 200})
        lead = adapter._lead_silence_bytes()
        assert lead == b"\x00" * (vm.BYTES_PER_MS * 200)
        assert len(lead) == 200 * 192  # 48kHz stereo s16 -> 192 bytes/ms


class TestPlayAckInVoice:
    @pytest.mark.asyncio
    async def test_noop_when_ack_disabled(self):
        adapter = _make_adapter({"ack_enabled": False})
        adapter._voice_mixers[111] = MagicMock()
        assert await adapter.play_ack_in_voice(111) is False


