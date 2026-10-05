"""
mixer.py — Multi-track mixdown.

Mixes any number of tracks (each an audio file with gain, pan, mute flag and
optional sample-accurate volume/pan automation) into a single stereo WAV.  Tracks may have different sample rates and lengths;
the mixer resamples on the fly with a seamless streaming resampler and pads
shorter tracks with silence.  Memory stays bounded because every track is read
a fixed-size chunk at a time.
"""

from __future__ import annotations

import math
import os
from typing import Dict, List, Optional, Sequence, Tuple

from . import audio_io, dsp


class AutomationCurve:
    """Sample-accurate piecewise-linear automation curve.

    Nodes are ``{"time": seconds, "value": float}``.  Values before the first
    node and after the last node are held constant, so an automation lane can
    cover only part of a track without affecting the remainder.
    """

    def __init__(self, nodes: Sequence[Dict], sr: int, default: float,
                 minimum: float, maximum: float):
        values_by_frame: Dict[int, float] = {}
        for node in nodes or []:
            try:
                time = float(node.get("time"))
                value = float(node.get("value"))
            except (TypeError, ValueError, AttributeError):
                continue
            if not math.isfinite(time) or not math.isfinite(value):
                continue
            frame = max(0, int(round(time * sr)))
            values_by_frame[frame] = max(minimum, min(maximum, value))
        self.nodes: List[Tuple[int, float]] = sorted(values_by_frame.items())
        self.default = max(minimum, min(maximum, default))
        self.minimum = minimum
        self.maximum = maximum
        self.position = 0
        self.index = 0

    def take(self, n: int) -> List[float]:
        """Return the next ``n`` output-frame values and advance the cursor."""
        if n <= 0:
            return []
        if not self.nodes:
            self.position += n
            return [self.default] * n

        out = [0.0] * n
        first_frame, first_value = self.nodes[0]
        last_frame, last_value = self.nodes[-1]
        idx = self.index

        for offset in range(n):
            frame = self.position + offset
            if frame <= first_frame:
                out[offset] = first_value
                continue
            if frame >= last_frame:
                out[offset] = last_value
                # Keep the cursor useful if another chunk follows.
                idx = len(self.nodes) - 1
                continue
            while idx + 1 < len(self.nodes) and frame >= self.nodes[idx + 1][0]:
                idx += 1
            f0, v0 = self.nodes[idx]
            f1, v1 = self.nodes[idx + 1]
            ratio = (frame - f0) / (f1 - f0)
            out[offset] = v0 + (v1 - v0) * ratio

        self.index = idx
        self.position += n
        return out


def _pan_gains(pan: float) -> tuple:
    """Constant-power pan gains for a mono source (pan in [-1, 1])."""
    pan = max(-1.0, min(1.0, pan))
    angle = (pan + 1.0) * math.pi / 4.0
    return math.cos(angle), math.sin(angle)


def _balance_gains(pan: float, gain: float) -> tuple:
    """Stereo balance gains: pan < 0 attenuates right, pan > 0 attenuates left."""
    pan = max(-1.0, min(1.0, pan))
    if pan <= 0:
        return gain, gain * (1.0 + pan)
    return gain * (1.0 - pan), gain


def mixdown(tracks: Sequence[Dict], out_path: str, target_sr: Optional[int] = None,
            master_gain: float = 1.0, chunk: int = 1 << 15) -> Dict:
    """Mix ``tracks`` into ``out_path``.

    Each track is a dict: ``{"path", "gain", "pan", "muted", "automation"}``.
    ``automation`` may contain ``volume`` and ``pan`` node lists; a non-empty
    lane overrides the corresponding static gain/pan value sample by sample.
    """
    active = [t for t in tracks if t.get("path") and not t.get("muted")
              and os.path.isfile(t["path"])]
    if not active:
        raise ValueError("no active tracks to mix")

    readers = []
    for t in active:
        r = audio_io.WavReader(t["path"])
        readers.append((r, t))

    sr = target_sr or max(r.sr for r, _ in readers)
    # Streaming resamplers for tracks that need rate conversion.
    resamplers: List[Optional[dsp.StreamingResampler]] = []
    for r, _ in readers:
        resamplers.append(dsp.StreamingResampler(r.sr, sr) if r.sr != sr else None)

    # Automation nodes are authored against each source file's timeline, so each
    # curve uses that reader's sample rate before being resampled like audio.
    automation: List[Tuple[AutomationCurve, AutomationCurve]] = []
    for r, t in readers:
        lanes = t.get("automation") or {}
        automation.append((
            AutomationCurve(lanes.get("volume", []), r.sr, t.get("gain", 1.0), 0.0, 2.0),
            AutomationCurve(lanes.get("pan", []), r.sr, t.get("pan", 0.0), -1.0, 1.0),
        ))

    automation_resamplers: List[Tuple[Optional[dsp.StreamingResampler], Optional[dsp.StreamingResampler]]] = []
    for (gain_curve, pan_curve), (r, _) in zip(automation, readers):
        gain_rs = dsp.StreamingResampler(r.sr, sr) if r.sr != sr and gain_curve.nodes else None
        pan_rs = dsp.StreamingResampler(r.sr, sr) if r.sr != sr and pan_curve.nodes else None
        automation_resamplers.append((gain_rs, pan_rs))

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
                rs = resamplers[i]
                # Read enough source samples to yield ~chunk output frames.
                src_frames = max(1, int(chunk * r.sr / sr)) if rs else chunk
                raw = r.read_chunk(src_frames)
                source_eof = raw is None
                if source_eof:
                    # Drain a resampler's trailing samples, if any.
                    if rs is not None:
                        tail = [rs.pull(chunk) for _ in range(r.channels)]
                        if any(tail):
                            raw = tail
                        else:
                            done[i] = True
                            continue
                    else:
                        done[i] = True
                        continue

                gain_curve, pan_curve = automation[i]
                gain_auto_rs, pan_auto_rs = automation_resamplers[i]
                raw_n = min(len(x) for x in raw)
                if rs is not None:
                    out_ch = []
                    for c, ch_data in enumerate(raw):
                        rs.push(ch_data)
                        out_ch.append(rs.pull(chunk))

                    def _resample_automation(curve: AutomationCurve,
                                             auto_rs: Optional[dsp.StreamingResampler],
                                             source_frames: int,
                                             output_frames: int) -> Optional[List[float]]:
                        if not curve.nodes:
                            return None
                        src_values = curve.take(source_frames)
                        auto_rs.push(src_values)
                        values = auto_rs.pull(chunk)
                        if source_frames == 0:
                            while len(values) < output_frames:
                                more = auto_rs.flush(chunk)
                                if not more:
                                    break
                                values.extend(more)
                        if len(values) < output_frames:
                            values.extend(curve.nodes[-1][1] for _ in range(output_frames - len(values)))
                        return values[:output_frames]

                    gain_env = _resample_automation(gain_curve, gain_auto_rs,
                                                    0 if source_eof else raw_n, chunk)
                    pan_env = _resample_automation(pan_curve, pan_auto_rs,
                                                   0 if source_eof else raw_n, chunk)
                else:
                    out_ch = [list(x) for x in raw]
                    gain_env = gain_curve.take(raw_n) if gain_curve.nodes else None
                    pan_env = pan_curve.take(raw_n) if pan_curve.nodes else None

                gain = t.get("gain", 1.0)
                pan = t.get("pan", 0.0)
                n = min(len(x) for x in out_ch)
                gain_env = gain_env[:n] if gain_env is not None else None
                pan_env = pan_env[:n] if pan_env is not None else None
                actual = max(actual, n)
                if len(out_ch) == 1:
                    mono = out_ch[0]
                    if gain_env is None and pan_env is None:
                        lg, rg = _pan_gains(pan)
                        for j in range(n):
                            out_l[j] += mono[j] * gain * lg
                            out_r[j] += mono[j] * gain * rg
                    else:
                        for j in range(n):
                            gj = gain_env[j] if gain_env is not None else gain
                            pj = pan_env[j] if pan_env is not None else pan
                            lg, rg = _pan_gains(pj)
                            out_l[j] += mono[j] * gj * lg
                            out_r[j] += mono[j] * gj * rg
                else:
                    left, right = out_ch[0], out_ch[1]
                    if gain_env is None and pan_env is None:
                        lg, rg = _balance_gains(pan, gain)
                        for j in range(n):
                            out_l[j] += left[j] * lg
                            out_r[j] += right[j] * rg
                    else:
                        for j in range(n):
                            gj = gain_env[j] if gain_env is not None else gain
                            pj = pan_env[j] if pan_env is not None else pan
                            lg, rg = _balance_gains(pj, gj)
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

    return {
        "tracks": len(active),
        "sr": sr,
        "duration": total_frames / sr if sr else 0.0,
        "frames": total_frames,
    }
