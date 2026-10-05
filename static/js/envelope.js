/* envelope.js — automation-envelope editor for mixer tracks.
 *
 * Each track gets an EnvelopeEditor: a horizontally scrollable canvas showing
 * the track's waveform as a backdrop with the volume or pan envelope on top.
 * Click empty space to add a node, drag nodes to move them, right-click or
 * double-click a node to delete it.  The editor edits the envelope object
 * inside the project JSON in place; the mixer page decides when to persist.
 */

const ENVELOPE_MODES = {
  volume: { label: "音量", lo: 0, hi: 2, center: 1, color: "#58a6ff",
            unit: "×", centerLabel: "1.00" },
  pan:    { label: "声像", lo: -1, hi: 1, center: 0, color: "#bc8cff",
            unit: "", centerLabel: "中" },
};

const ENV_PAD_L = 46, ENV_RULER_H = 22, ENV_PLOT_H = 132;

class EnvelopeEditor {
  constructor(track, fileEntry, duration, onChange) {
    this.track = track;
    this.file = fileEntry;
    this.duration = Math.max(duration, 0.1);
    this.onChange = onChange || (() => {});
    this.mode = "volume";
    this.wave = null;
    this.zoom = 1;
    this.drag = null;            // {node} while a node grab/drag is active
    this.hoverNode = null;
    this._wavePromise = null;
    this._onMove = e => this.onMouseMove(e);
    this._onUp = e => this.onMouseUp(e);
    this._onResize = () => { if (this.root.isConnected) this.draw(); };
    this.build();
    this.loadWave();
    this.draw();
  }

  // ------------------------------------------------------------ DOM setup
  build() {
    const root = document.createElement("div");
    root.className = "env-editor";
    root.innerHTML = `
      <div class="env-toolbar">
        <div class="seg" data-seg>
          <button data-mode="volume" class="seg-on">音量</button>
          <button data-mode="pan">声像</button>
        </div>
        <label class="field inline env-check"><input type="checkbox" data-env-enable/> 启用自动化</label>
        <button class="btn-sm" data-env-clear>清空节点</button>
        <span class="spacer"></span>
        <button class="btn-sm" data-zoom-out>－</button>
        <span class="mono small" data-zoom-label>1×</span>
        <button class="btn-sm" data-zoom-in>＋</button>
        <button class="btn-sm" data-zoom-fit>适配</button>
      </div>
      <div class="env-scroll" data-scroll>
        <div class="env-inner" data-inner>
          <canvas data-ruler></canvas>
          <canvas data-plot></canvas>
        </div>
      </div>
      <div class="env-readout-wrap mono small muted" data-readout></div>
      <div class="env-legend small muted">
        <span><span class="dot" style="background:var(--accent)"></span>波形</span>
        <span><span class="dot" data-curve-dot></span>${this.spec().label}曲线 / 节点</span>
        <span>单击空白添加 · 拖动调整 · 右键或双击节点删除</span>
      </div>`;
    this.root = root;
    this.scroll = root.querySelector("[data-scroll]");
    this.inner = root.querySelector("[data-inner]");
    this.ruler = root.querySelector("[data-ruler]");
    this.plot = root.querySelector("[data-plot]");
    this.readout = root.querySelector("[data-readout]");
    this.zoomLabel = root.querySelector("[data-zoom-label]");

    root.querySelector("[data-seg]").addEventListener("click", e => {
      const btn = e.target.closest("button[data-mode]");
      if (!btn) return;
      this.setMode(btn.dataset.mode);
    });
    root.querySelector("[data-env-enable]").addEventListener("change", e => {
      this.env().enabled = e.target.checked;
      if (e.target.checked && !this.env().nodes.length) {
        // Seed with sensible start/end nodes matching the current fixed value.
        const fixed = this.mode === "volume" ? (this.track.gain ?? 1) : (this.track.pan ?? 0);
        this.env().nodes = [
          { t: 0, v: +fixed.toFixed(3) },
          { t: +this.duration.toFixed(3), v: +fixed.toFixed(3) },
        ];
      }
      this.onChange(true);
      this.draw();
    });
    root.querySelector("[data-env-clear]").addEventListener("click", () => {
      this.env().nodes = [];
      this.onChange(true);
      this.draw();
    });
    root.querySelector("[data-zoom-in]").addEventListener("click", () => this.setZoom(this.zoom * 2));
    root.querySelector("[data-zoom-out]").addEventListener("click", () => this.setZoom(this.zoom / 2));
    root.querySelector("[data-zoom-fit]").addEventListener("click", () => this.setZoom(1));
    window.addEventListener("resize", this._onResize);

    this.plot.addEventListener("mousedown", e => this.onMouseDown(e));
    window.addEventListener("mousemove", this._onMove);
    window.addEventListener("mouseup", this._onUp);
    this.plot.addEventListener("contextmenu", e => { e.preventDefault(); this.onContextMenu(e); });
    this.plot.addEventListener("dblclick", e => this.onDoubleClick(e));
  }

  destroy() {
    window.removeEventListener("mousemove", this._onMove);
    window.removeEventListener("mouseup", this._onUp);
    window.removeEventListener("resize", this._onResize);
    this.root.remove();
  }

  // ------------------------------------------------------------ state
  spec() { return ENVELOPE_MODES[this.mode]; }

  setTrack(track) {
    this.track = track;
    this.drag = null;
    this.draw();
  }

  env() {
    const tr = this.track;
    tr.envelopes = tr.envelopes || {};
    if (!tr.envelopes[this.mode]) tr.envelopes[this.mode] = { enabled: false, nodes: [] };
    return tr.envelopes[this.mode];
  }

  setMode(mode) {
    if (!ENVELOPE_MODES[mode] || mode === this.mode) return;
    this.mode = mode;
    this.root.querySelectorAll("[data-mode]").forEach(b =>
      b.classList.toggle("seg-on", b.dataset.mode === mode));
    this.root.querySelector("[data-curve-dot]").style.background = this.spec().color;
    this.syncToolbar();
    this.draw();
  }

  syncToolbar() {
    const e = this.env();
    this.root.querySelector("[data-env-enable]").checked = !!e.enabled;
    this.zoomLabel.textContent = this.zoom + "×";
  }

  setZoom(z) {
    this.zoom = Math.max(1, Math.min(16, +z.toFixed(3)));
    this.zoomLabel.textContent = this.zoom + "×";
    this.draw();
  }

  async loadWave() {
    if (!this.file) return;
    if (this._wavePromise) return this._wavePromise;
    const points = Math.min(8000, Math.max(1000, Math.round(this.duration * 400)));
    this._wavePromise = API.get(`/api/audio/${this.file.id}/waveform?points=${points}`)
      .then(w => { this.wave = w; this.draw(); })
      .catch(() => { this.wave = null; });
    return this._wavePromise;
  }

  // ------------------------------------------------------------ geometry
  innerWidth() {
    const visible = Math.max(200, this.scroll.clientWidth - ENV_PAD_L - 4);
    const fit = Math.max(200, Math.round(this.duration * 80));
    return Math.round(Math.max(visible, fit) * this.zoom);
  }

  resize() {
    const w = this.innerWidth();
    this.inner.style.width = (w + ENV_PAD_L) + "px";
    this.ruler.style.width = (w + ENV_PAD_L) + "px";
    this.ruler.style.height = ENV_RULER_H + "px";
    this.plot.style.width = (w + ENV_PAD_L) + "px";
    this.plot.style.height = ENV_PLOT_H + "px";
    return w;
  }

  xToTime(x) { return Math.max(0, Math.min(this.duration, x / this.innerWidth() * this.duration)); }
  timeToX(t) { return t / this.duration * this.innerWidth(); }
  // x relative to the plot canvas (includes the axis pad on the left).
  plotXToTime(x) { return this.xToTime(x - ENV_PAD_L); }
  yToValue(y) {
    const s = this.spec();
    return s.hi + (s.lo - s.hi) * (y / ENV_PLOT_H);
  }
  valueToY(v) {
    const s = this.spec();
    return (s.hi - v) / (s.hi - s.lo) * ENV_PLOT_H;
  }

  hitNode(px, y) {
    if (!this.env().enabled) return null;
    const x = px - ENV_PAD_L;
    const tol = 7;
    let best = null, bestD = tol;
    for (const nd of this.env().nodes) {
      const dx = this.timeToX(nd.t) - x, dy = this.valueToY(nd.v) - y;
      const d = Math.hypot(dx, dy);
      if (d <= bestD) { bestD = d; best = nd; }
    }
    return best;
  }

  // ------------------------------------------------------------ mouse
  onMouseDown(e) {
    if (e.button === 2) return; // contextmenu handles removal
    if (e.detail >= 2) return;  // dblclick is a delete gesture, not two adds
    if (!this.env().enabled) { toast("先勾选「启用自动化」", "warn"); return; }
    const rect = this.plot.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    const hit = this.hitNode(x, y);
    if (hit) {
      this.drag = { node: hit };
    } else {
      const nd = { t: +this.plotXToTime(x).toFixed(4), v: +this.clampVal(this.yToValue(y)).toFixed(3) };
      this.env().nodes.push(nd);
      this.sortNodes();
      this.drag = { node: nd };
      this.onChange(false);
      this.draw();
    }
  }

  onDoubleClick(e) {
    if (!this.env().enabled) return;
    const rect = this.plot.getBoundingClientRect();
    const hit = this.hitNode(e.clientX - rect.left, e.clientY - rect.top);
    if (hit) {
      const nodes = this.env().nodes;
      nodes.splice(nodes.indexOf(hit), 1);
      this.onChange(true);
      this.draw();
    }
  }

  onMouseMove(e) {
    const rect = this.plot.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    if (this.drag) {
      // Dragging continues (and clamps) even when the pointer leaves the plot.
      const nd = this.drag.node;
      nd.t = +this.plotXToTime(x).toFixed(4);
      nd.v = +this.clampVal(this.yToValue(y)).toFixed(3);
      // Keep ordered for rendering without reordering object refs mid-drag.
      this.sortNodes();
      this.readout.textContent =
        `${this.spec().label}: ${fmtTime(nd.t)}  →  ${this.formatVal(nd.v)}${this.spec().unit}`;
      this.onChange(false);
      this.draw();
      return;
    }
    if (x < 0 || x > rect.width || y < 0 || y > rect.height) {
      this.plot.style.cursor = "";
      return;
    }
    const hit = this.hitNode(x, y);
    this.hoverNode = hit;
    this.plot.style.cursor = hit ? "grab" : (this.env().enabled ? "crosshair" : "not-allowed");
  }

  onMouseUp() {
    if (!this.drag) return;
    this.drag = null;
    this.onChange(true);  // persist (project version) once per finished gesture
    this.draw();
  }

  onContextMenu(e) {
    if (!this.env().enabled) return;
    const rect = this.plot.getBoundingClientRect();
    const hit = this.hitNode(e.clientX - rect.left, e.clientY - rect.top);
    if (hit) {
      const nodes = this.env().nodes;
      nodes.splice(nodes.indexOf(hit), 1);
      this.onChange(true);
      this.draw();
    }
  }

  clampVal(v) {
    const s = this.spec();
    return Math.max(s.lo, Math.min(s.hi, v));
  }

  formatVal(v) {
    return this.mode === "volume" ? v.toFixed(2) : (v === 0 ? "0.00 中" : v.toFixed(2));
  }

  sortNodes() {
    this.env().nodes.sort((a, b) => a.t - b.t);
  }

  // ------------------------------------------------------------ rendering
  draw() {
    const w = this.resize();
    this.drawRuler(w);
    this.drawPlot(w, ENV_PLOT_H);
    this.syncToolbar();
  }

  drawRuler(w) {
    const { ctx, h } = setupCanvas(this.ruler);
    ctx.clearRect(0, 0, w + ENV_PAD_L, h);
    ctx.fillStyle = "rgba(139,148,158,0.75)";
    ctx.font = "10px sans-serif";
    ctx.textBaseline = "alphabetic";
    ctx.strokeStyle = "rgba(139,148,158,0.25)";
    const step = this.tickStep(w);
    for (let t = 0; t <= this.duration + 1e-9; t += step) {
      const x = ENV_PAD_L + this.timeToX(t);
      ctx.beginPath(); ctx.moveTo(x, h - 6); ctx.lineTo(x, h); ctx.stroke();
      ctx.fillText(fmtTime(t), x + 3, h - 8);
    }
  }

  tickStep(w) {
    const targetPx = 90;
    const targetS = targetPx / w * this.duration;
    const nice = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
    return nice.find(s => s >= targetS) || 600;
  }

  drawPlot(w, h) {
    const s = this.spec();
    const { ctx } = setupCanvas(this.plot);
    ctx.clearRect(0, 0, w + ENV_PAD_L, h);

    // axis labels
    ctx.fillStyle = "rgba(139,148,158,0.7)";
    ctx.font = "10px sans-serif";
    ctx.textBaseline = "middle";
    ctx.fillText(s.hi.toFixed(1), 6, 8);
    ctx.fillText(s.centerLabel, 6, h / 2);
    ctx.fillText(s.lo.toFixed(1), 6, h - 8);
    ctx.fillStyle = "rgba(139,148,158,0.55)";
    ctx.fillText(s.label + (s.unit ? " " + s.unit : ""), 6, h / 2 - 16);

    ctx.save();
    ctx.beginPath();
    ctx.rect(ENV_PAD_L, 0, w, h);
    ctx.clip();

    // gridlines
    ctx.strokeStyle = "rgba(139,148,158,0.12)";
    ctx.beginPath();
    for (const frac of [0.25, 0.5, 0.75]) {
      const y = h * frac;
      ctx.moveTo(ENV_PAD_L, y); ctx.lineTo(ENV_PAD_L + w, y);
    }
    ctx.stroke();
    // centre (value 0 for pan, 1.0 for volume)
    ctx.strokeStyle = "rgba(139,148,158,0.35)";
    ctx.beginPath();
    ctx.moveTo(ENV_PAD_L, this.valueToY(s.center));
    ctx.lineTo(ENV_PAD_L + w, this.valueToY(s.center));
    ctx.stroke();

    // waveform backdrop
    if (this.wave && this.wave.mins) {
      const mins = this.wave.mins, maxs = this.wave.maxs;
      const step = w / mins.length;
      ctx.strokeStyle = "rgba(88,166,255,0.35)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let i = 0; i < mins.length; i++) {
        const x = ENV_PAD_L + i * step;
        ctx.moveTo(x, h / 2 - (maxs[i] || 0) * h * 0.46);
        ctx.lineTo(x, h / 2 - (mins[i] || 0) * h * 0.46);
      }
      ctx.stroke();
    }

    const env = this.env();
    if (env.enabled && env.nodes.length) {
      this.drawCurve(ctx, env.nodes, w, h, s.color);
    } else {
      // dashed line at the fixed value so the user sees what is applied
      const fixed = this.mode === "volume" ? (this.track.gain ?? 1) : (this.track.pan ?? 0);
      ctx.strokeStyle = "rgba(139,148,158,0.5)";
      ctx.setLineDash([5, 4]);
      const y = this.valueToY(this.clampVal(fixed));
      ctx.beginPath(); ctx.moveTo(ENV_PAD_L, y); ctx.lineTo(ENV_PAD_L + w, y); ctx.stroke();
      ctx.setLineDash([]);
      if (!env.enabled) {
        ctx.fillStyle = "rgba(139,148,158,0.85)";
        ctx.font = "12px sans-serif";
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText("未启用自动化", ENV_PAD_L + w / 2, h / 2 + 18);
        ctx.textAlign = "start";
      }
    }
    ctx.restore();
  }

  drawCurve(ctx, nodes, w, h, color) {
    const X = nd => ENV_PAD_L + this.timeToX(nd.t);
    // held lead-in/trailing-out horizontal segments
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.8;
    ctx.beginPath();
    ctx.moveTo(ENV_PAD_L, this.valueToY(nodes[0].v));
    ctx.lineTo(X(nodes[0]), this.valueToY(nodes[0].v));
    for (const nd of nodes) ctx.lineTo(X(nd), this.valueToY(nd.v));
    ctx.lineTo(ENV_PAD_L + w, this.valueToY(nodes[nodes.length - 1].v));
    ctx.stroke();

    for (const nd of nodes) {
      const x = X(nd), y = this.valueToY(nd.v);
      const sel = nd === this.hoverNode || (this.drag && this.drag.node === nd);
      ctx.beginPath();
      ctx.arc(x, y, sel ? 6 : 4.2, 0, Math.PI * 2);
      ctx.fillStyle = "#e6edf3";
      ctx.fill();
      ctx.lineWidth = sel ? 2.4 : 1.6;
      ctx.strokeStyle = color;
      ctx.stroke();
    }
  }
}

window.EnvelopeEditor = EnvelopeEditor;
window.ENVELOPE_MODES = ENVELOPE_MODES;
