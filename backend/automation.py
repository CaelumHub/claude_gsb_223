"""
automation.py — Automation envelopes (volume / pan) for mixer tracks.

Envelope model
--------------
An envelope is a dict::

    {"enabled": bool, "nodes": [{"t": seconds, "v": value}, ...]}

* ``t`` is time in seconds measured from the start of the project timeline
  (which is identical to the mixer output frame index / output sample rate,
  even for tracks whose source sample rate differs).
* ``v`` depends on the mode:
    - ``"volume"``: linear gain multiplier, 0.0 … 2.0
    - ``"pan"``:    pan position, -1.0 (full left) … 1.0 (full right)

Nodes are joined with **straight lines (linear interpolation)**.  Before the
first node the first value is held; after the last node the last value is
held.  Ties in time are de-duplicated (the later node wins) and nodes are
always stored sorted by time.  An envelope that is disabled, or that has no
nodes, contributes nothing — the ordinary fixed track gain/pan is used.

Rendering precision
-------------------
:class:`EnvelopeSampler` emits the *exact* piecewise-linear value at every
output sample: inside a segment the ramp is ``value(frame) = a*frame + b``
advanced with one addition per sample, so there is no lookup table and no
quantisation of the curve itself.  Segment boundaries are crossed with
integer-frame comparisons, which makes the per-chunk cost
``O(chunk_frames + segments_crossed)`` — tens of thousands of densely packed
nodes cost essentially the same as two nodes, and values never overshoot a
node.  Sampler state (the current segment pointer) advances monotonically
across chunks, so arbitrarily long projects stream in bounded memory.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

# mode -> (minimum value, maximum value, value when there are no nodes)
RANGES: Dict[str, tuple] = {
    "volume": (0.0, 2.0, 1.0),
    "pan": (-1.0, 1.0, 0.0),
}


def normalize_nodes(raw: Optional[Sequence[Dict[str, Any]]], mode: str) -> List[Dict[str, float]]:
    """Clean user-supplied nodes: numeric coercion, clamping, sort + de-dupe.

    Non-numeric / non-finite / negative-time nodes are dropped silently (the
    UI never emits them, but persisted JSON may come from older clients).
    """
    if mode not in RANGES:
        raise ValueError(f"unknown envelope mode {mode!r}")
    lo, hi, _ = RANGES[mode]
    cleaned: List[Dict[str, float]] = []
    for nd in raw or []:
        try:
            t = float(nd["t"])
            v = float(nd["v"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(t) and math.isfinite(v)) or t < 0.0:
            continue
        cleaned.append({"t": t, "v": min(hi, max(lo, v))})
    cleaned.sort(key=lambda n: n["t"])
    # De-duplicate identical times, keeping the last node at that time.
    deduped: List[Dict[str, float]] = []
    for nd in cleaned:
        if deduped and nd["t"] == deduped[-1]["t"]:
            deduped[-1]["v"] = nd["v"]
        else:
            deduped.append(nd)
    return deduped


def normalize_envelope(spec: Optional[Dict[str, Any]], mode: str) -> Dict[str, Any]:
    """Return a canonical envelope dict for storage/serialisation."""
    spec = spec or {}
    return {
        "enabled": bool(spec.get("enabled")),
        "mode": mode,
        "nodes": normalize_nodes(spec.get("nodes"), mode),
    }


def is_active(env: Optional[Dict[str, Any]]) -> bool:
    """An envelope affects the mix only when enabled and with at least one node."""
    return bool(env) and bool(env.get("enabled")) and bool(env.get("nodes"))


def sampler_for(env: Optional[Dict[str, Any]], mode: str, sr: int) -> Optional["EnvelopeSampler"]:
    """Build a sampler for an envelope, or ``None`` when it does nothing."""
    if not is_active(env):
        return None
    return EnvelopeSampler(normalize_nodes(env.get("nodes"), mode), mode, sr)


class EnvelopeSampler:
    """Streams exact piecewise-linear envelope values per output frame."""

    def __init__(self, nodes: Sequence[Dict[str, float]], mode: str, sr: int):
        if mode not in RANGES:
            raise ValueError(f"unknown envelope mode {mode!r}")
        self.mode = mode
        self.sr = int(sr)
        lo, hi, default = RANGES[mode]
        self.lo, self.hi, self.default = lo, hi, default
        self.nodes = list(nodes)
        self._active = bool(self.nodes)
        # Frame to which each node snaps; the segment switch happens there so
        # the emitted value is exactly the node value at every boundary.
        self._bounds = [int(round(n["t"] * self.sr)) for n in self.nodes]
        self.seg = 0  # index of the segment currently being emitted

    def value_at(self, t: float) -> float:
        """Curve value at an arbitrary time in seconds (held beyond ends)."""
        if not self._active:
            return self.default
        if t <= self.nodes[0]["t"]:
            return self.nodes[0]["v"]
        if t >= self.nodes[-1]["t"]:
            return self.nodes[-1]["v"]
        # Binary search for the surrounding segment.
        lo_i, hi_i = 0, len(self.nodes) - 1
        while hi_i - lo_i > 1:
            mid = (lo_i + hi_i) // 2
            if t < self.nodes[mid]["t"]:
                hi_i = mid
            else:
                lo_i = mid
        n0, n1 = self.nodes[lo_i], self.nodes[hi_i]
        frac = (t - n0["t"]) / (n1["t"] - n0["t"])
        return n0["v"] + (n1["v"] - n0["v"]) * frac

    def fill(self, out: List[float], frame0: int) -> List[float]:
        """Fill pre-sized ``out`` with values for output frames ``frame0..``.

        Each node snaps to its nearest frame ``B = round(t*sr)``; segment
        ``[Bj, Bj+1)`` emits ``vj + (vj+1-vj)*(f-Bj)/(Bj+1-Bj)`` — a straight
        ramp in frame space that hits each node value *exactly* at its
        boundary frame and cannot overshoot.  Frames before the first / after
        the last node hold the end value.  Dense nodes that snap to the same
        frame collapse (the later one wins).  ``frame0`` must advance
        monotonically across calls, so the segment pointer moves in amortised
        O(1) per chunk.
        """
        n = len(out)
        if not self._active:
            for k in range(n):
                out[k] = self.default
            return out
        nodes = self.nodes
        m = len(nodes)
        if m == 1:
            v = nodes[0]["v"]
            for k in range(n):
                out[k] = v
            return out

        bounds = self._bounds
        seg = min(self.seg, m - 1)
        frame = int(frame0)
        i = 0

        # Leading hold: frames before the first node.
        if frame < bounds[0]:
            v = nodes[0]["v"]
            end = min(n, bounds[0] - frame0)
            while i < end:
                out[i] = v
                i += 1
            frame = frame0 + i
            if i >= n:
                self.seg = seg
                return out

        while i < n:
            # Advance to the segment that contains the current frame.
            while seg < m - 1 and frame >= bounds[seg + 1]:
                seg += 1
            if seg >= m - 1:
                # Trailing hold after the last node.
                v = nodes[-1]["v"]
                while i < n:
                    out[i] = v
                    i += 1
                break
            b0, b1 = bounds[seg], bounds[seg + 1]
            if b1 <= b0:
                # Nodes snapped to the same frame: the later one wins.
                seg += 1
                continue
            v0 = nodes[seg]["v"]
            delta = (nodes[seg + 1]["v"] - v0) / (b1 - b0)
            end_frame = min(frame0 + n, b1)
            v = v0 + delta * (frame - b0)  # == v0 exactly when frame == b0
            while frame < end_frame:
                out[i] = v
                v += delta
                i += 1
                frame += 1

        self.seg = seg
        return out
