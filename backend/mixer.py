"""
mixer.py — Multi-track mixdown.

Mixes any number of tracks (each an audio file with a gain, pan and mute flag)
into a single stereo WAV.  Tracks may have different sample rates and lengths;
the mixer resamples on the fly with a seamless streaming resampler and pads
shorter tracks with silence.  Memory stays bounded because every track is read
a fixed-size chunk at a time.

Automation
----------
A track may carry ``volume_env`` / ``pan_env`` envelopes (normalised dicts as
produced by :func:`backend.automation.normalize_envelope`).  When an envelope
is active it *replaces* the track's fixed gain/pan and varies it sample by
sample along the output timeline (see ``backend/automation``).  Envelopes are
evaluated lazily per chunk, only for tracks that need them, so dense
automation is cheap.

Resampling alignment
--------------------
Each channel owns its own :class:`dsp.StreamingResampler` (a single shared
instance would interleave the channels' samples).  For every output chunk the
mixer reads exactly the source frames needed to synthesise that chunk
(``need*ratio`` plus one interpolation-base frame, accounting for samples
already buffered), pushes them and pulls the chunk once.  The input buffer
therefore holds a constant one-frame carry between chunks and the track — and
any envelope indexed by output frame — stays sample-aligned at every boundary.
"""

from __future__ import annotations

import math
import os
from typing import Dict, List, Optional, Sequence, Tuple

from . import audio_io, automation, dsp


def _pan_gains(pan: float) -> Tuple[float, float]:
    """Constant-power pan gains for a mono source (pan in [-1, 1])."""
    pan = max(-1.0, min(1.0, pan))
    angle = (pan + 1.0) * math.pi / 4.0
    return math.cos(angle), math.sin(angle)


def _balance_gains(pan: float, gain: float) -> Tuple[float, float]:
    """Stereo balance gains: pan < 0 attenuates right, pan > 0 attenuates left."""
    pan = max(-1.0, min(1.0, pan))
    if pan <= 0:
        return gain, gain * (1.0 + pan)
    return gain * (1.0 - pan), gain


def _pull_chunk(reader: audio_io.WavReader,
                rss: List[dsp.StreamingResampler], sr: int,
                n_frames: int) -> Optional[List[List[float]]]:
    """Read/resample exactly one output chunk of up to ``n_frames`` frames.

    Per channel the source-rate → output-rate ratio is ``reader.sr / sr``.
    Returns per-channel output lists (the final flush chunk may be short), or
    ``None`` at EOF.
    """
    ratio = reader.sr / sr
    nch = reader.channels
    out_ch: List[List[float]] = [[] for _ in range(nch)]
    eof = False
    while min(len(x) for x in out_ch) < n_frames:
        need = n_frames - min(len(x) for x in out_ch)
        buffered = len(rss[0].buf)
        # ``need`` output samples consume int(need*ratio) source frames and
        # require one additional frame as the interpolation base.
        wanted = int(need * ratio) + 1 - buffered
        if wanted > 0:
            raw = reader.read_chunk(wanted)
            if raw is None:
                eof = True
                break
            for c, ch_data in enumerate(raw):
                rss[c].push(ch_data)
        pulls = [rss[c].pull(need) for c in range(nch)]
        got = min(len(p) for p in pulls)
        for c in range(nch):
            out_ch[c].extend(pulls[c][:got])
        if got == 0:
            eof = True
            break

    produced = min(len(x) for x in out_ch)
    if produced == 0:
        tails = [rss[c].flush(n_frames) for c in range(nch)]
        nt = min(len(x) for x in tails)
        if nt == 0:
            return None
        return [tails[c][:nt] for c in range(nch)]
    if max(len(x) for x in out_ch) > produced:
        out_ch = [x[:produced] for x in out_ch]
    return out_ch


def mixdown(tracks: Sequence[Dict], out_path: str, target_sr: Optional[int] = None,
            master_gain: float = 1.0, chunk: int = 1 << 15) -> Dict:
    """Mix ``tracks`` into ``out_path``.

    Each track is a dict: ``{"path", "gain", "pan", "muted",
    "volume_env", "pan_env"}``.  ``volume_env`` / ``pan_env`` are optional
    automation envelopes; when enabled they override the fixed value.
    """
    active = [t for t in tracks if t.get("path") and not t.get("muted")
              and os.path.isfile(t["path"])]
    if not active:
        raise ValueError("no active tracks to mix")

    readers: List[Tuple[audio_io.WavReader, Dict]] = []
    for t in active:
        readers.append((audio_io.WavReader(t["path"]), t))

    sr = target_sr or max(r.sr for r, _ in readers)
    # One streaming resampler PER CHANNEL for tracks needing rate conversion.
    resamplers: List[Optional[List[dsp.StreamingResampler]]] = [
        [dsp.StreamingResampler(r.sr, sr) for _ in range(r.channels)]
        if r.sr != sr else None for r, _ in readers
    ]

    # Per-track automation samplers (None unless the envelope is active).
    vol_samplers = [automation.sampler_for(t.get("volume_env"), "volume", sr)
                    for _, t in readers]
    pan_samplers = [automation.sampler_for(t.get("pan_env"), "pan", sr)
                    for _, t in readers]
    automated = [bool(vol_samplers[i] or pan_samplers[i])
                 for i in range(len(readers))]
    # Absolute output-frame position of each track's next samples.
    positions = [0] * len(readers)

    total_frames = 0
    with audio_io.WavWriter(out_path, sr, 2, 2) as w:
        done = [False] * len(readers)
        while not all(done):
            out_l = [0.0] * chunk
            out_r = [0.0] * chunk
            actual = 0
            for i, (r, t) in enumerate(readers):
                if done[i]:
                    continue

                if resamplers[i] is not None:
                    out_ch = _pull_chunk(r, resamplers[i], sr, chunk)
                else:
                    raw = r.read_chunk(chunk)
                    out_ch = [list(x) for x in raw] if raw is not None else None
                if out_ch is None:
                    done[i] = True
                    continue

                fixed_gain = t.get("gain", 1.0)
                fixed_pan = t.get("pan", 0.0)
                n = min(len(x) for x in out_ch)
                actual = max(actual, n)

                if automated[i]:
                    f0 = positions[i]
                    gains = [0.0] * n
                    pans = [0.0] * n
                    if vol_samplers[i] is not None:
                        vol_samplers[i].fill(gains, f0)
                    else:
                        for j in range(n):
                            gains[j] = fixed_gain
                    if pan_samplers[i] is not None:
                        pan_samplers[i].fill(pans, f0)
                    else:
                        for j in range(n):
                            pans[j] = fixed_pan
                    positions[i] = f0 + n

                    if len(out_ch) == 1:
                        mono = out_ch[0]
                        for j in range(n):
                            angle = (max(-1.0, min(1.0, pans[j])) + 1.0) * math.pi / 4.0
                            g = gains[j]
                            out_l[j] += mono[j] * g * math.cos(angle)
                            out_r[j] += mono[j] * g * math.sin(angle)
                    else:
                        left, right = out_ch[0], out_ch[1]
                        for j in range(n):
                            g = gains[j]
                            p = max(-1.0, min(1.0, pans[j]))
                            # Stereo balance: attenuate the opposite side.
                            lg, rg = (g, g * (1.0 + p)) if p <= 0 \
                                else (g * (1.0 - p), g)
                            out_l[j] += left[j] * lg
                            out_r[j] += right[j] * rg
                else:
                    positions[i] += n
                    if len(out_ch) == 1:
                        lg, rg = _pan_gains(fixed_pan)
                        mono = out_ch[0]
                        for j in range(n):
                            out_l[j] += mono[j] * fixed_gain * lg
                            out_r[j] += mono[j] * fixed_gain * rg
                    else:
                        lg, rg = _balance_gains(fixed_pan, fixed_gain)
                        left, right = out_ch[0], out_ch[1]
                        for j in range(n):
                            out_l[j] += left[j] * lg
                            out_r[j] += right[j] * rg

            if actual == 0:
                break
            # Master gain + soft clipping to guard against overload.
            total_frames += actual
            out_l = [math.tanh(x * master_gain) for x in out_l[:actual]]
            out_r = [math.tanh(x * master_gain) for x in out_r[:actual]]
            w.write_chunk([out_l, out_r])

    for r, _ in readers:
        r.close()

    n_vol = sum(1 for s in vol_samplers if s is not None)
    n_pan = sum(1 for s in pan_samplers if s is not None)
    return {
        "tracks": len(active),
        "sr": sr,
        "duration": total_frames / sr if sr else 0.0,
        "frames": total_frames,
        "automation": {"volume_envelopes": n_vol, "pan_envelopes": n_pan},
    }
