/* AutoDub — shared dithered-canvas background.
   Both surfaces draw Bayer-dithered plus-sign cells over the same iridescent ramp
   and share the same pulse ("ping") model; only the intensity field differs:

     "linear" — a summed-sine horizontal sound wave (AutoDub Studio)
     "iris"   — concentric aperture rings around a focal point (Video Studio)

   Usage:
     const wave = AutoDubWave.create(document.querySelector("#wave"), "iris");
     wave.ping(rgb, 0.8, 900);          // colour, strength, duration, [position]
     wave.setEnergy(1);                 // 0 idle .. 1 running
     wave.setPlayhead(0.42);            // linear mode only; -1 to clear

   AutoDubWave.RAMP is the 96-step ramp, exported because the pages also use it to
   colour progress bars and per-item hues. */
(() => {
  "use strict";

  const STOPS = [[94,234,212],[125,211,252],[196,181,253],[253,230,138],[252,165,165],[240,171,252],[255,255,255]];
  const RAMP = [];
  for (let i = 0; i < 96; i++) {
    const f = i/95*(STOPS.length-1), a = Math.floor(f), b = Math.min(a+1, STOPS.length-1), t = f-a;
    RAMP.push(STOPS[a].map((c, k) => Math.round(c + (STOPS[b][k]-c)*t)));
  }

  const BAYER = [[0,8,2,10],[12,4,14,6],[3,11,1,9],[15,7,13,5]].map((r) => r.map((v) => v/16));
  const CELL = 9;
  const MAX_PULSES = 48;
  const MIX_FLOOR = 0.2;   // below this a pulse tints nothing (was .22 / .20)
  const reduce = matchMedia("(prefers-reduced-motion:reduce)").matches;

  function create(canvas, mode) {
    const ctx = canvas.getContext("2d");
    const iris = mode === "iris";
    let W = 0, H = 0, DPR = 1;
    let energy = 0, energyTarget = 0, playhead = -1;
    const pulses = [];

    function resize() {
      const r = canvas.getBoundingClientRect();
      DPR = Math.min(devicePixelRatio || 1, 2);
      W = r.width; H = r.height;
      canvas.width = W*DPR; canvas.height = H*DPR;
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    }

    // One cell: mix the ramp colour toward the strongest overlapping pulse, then
    // stamp a plus sign. Shared by both fields.
    function stamp(x, y, ci, alpha, size, pc, pcw) {
      const base = RAMP[Math.min(95, Math.max(0, ci))];
      let c = base;
      if (pc && pcw > MIX_FLOOR) {
        const m = Math.min(1, pcw);
        c = [base[0]+(pc[0]-base[0])*m|0, base[1]+(pc[1]-base[1])*m|0, base[2]+(pc[2]-base[2])*m|0];
      }
      ctx.fillStyle = `rgba(${c[0]},${c[1]},${c[2]},${alpha})`;
      ctx.fillRect(x-size, y-0.7, size*2, 1.4);
      ctx.fillRect(x-0.7, y-size, 1.4, size*2);
    }

    // Accumulate every live pulse at a 1-D coordinate. In linear mode the pulse is
    // a blob widening around its position; in iris mode it is a ring travelling
    // outward — hence the two envelopes.
    let pAdd = 0, pColor = null, pWeight = 0;
    function samplePulses(ts, coord) {
      pAdd = 0; pColor = null; pWeight = 0;
      for (const p of pulses) {
        const age = (ts - p.t0)/p.dur, env = Math.sin(age*Math.PI);
        let g;
        if (iris) { const r = 0.04 + age*0.95; g = Math.max(0, 1 - Math.abs(coord - r)/0.07); }
        else { const reach = 0.05 + age*0.14; g = Math.max(0, 1 - Math.abs(coord - p.p)/reach); }
        const amt = g*env*p.s;
        if (amt > 0) { pAdd += amt; if (amt > pWeight) { pWeight = amt; pColor = p.c; } }
      }
    }

    function fieldAmp(x, t) {
      const nx = x/W;
      let w = Math.sin(nx*9 + t*1.1)*0.5 + Math.sin(nx*23 - t*0.7)*0.28 + Math.sin(nx*41 + t*1.9)*0.16;
      w = Math.abs(w);
      const base = 0.10 + 0.05*Math.sin(nx*5 - t*0.5);
      return base + w*(0.30 + 0.66*energy);
    }

    function drawLinear(ts, t) {
      const mid = H*0.30;
      for (let cx = 0; cx < W; cx += CELL) {
        const x = cx + CELL/2, nx = x/W;
        samplePulses(ts, nx);
        const pa = pAdd, pc = pColor, pcw = pWeight;
        const amp = (fieldAmp(x, t) + pa*0.55) * H*0.32;
        const sweep = playhead >= 0 ? Math.max(0, 1 - Math.abs(nx - playhead)*7) : 0;
        const idleSweep = Math.max(0, 1 - Math.abs(nx - (((t*0.06)%1.3) - 0.15))*9) * 0.18 * (1 - energy);
        for (let cy = 0; cy < H; cy += CELL) {
          const y = cy + CELL/2;
          let I = 1 - Math.abs(y - mid)/amp;
          if (I <= 0) continue;
          I = Math.pow(I, 0.7)*(0.5 + 0.5*energy) + sweep*0.5 + idleSweep + pa*0.45;
          if (I < BAYER[(cx/CELL|0)%4][(cy/CELL|0)%4]*0.9) continue;
          stamp(x, y, Math.floor((nx*0.7 + I*0.5 + energy*0.15)*95),
                Math.min(0.72, (0.08 + I*0.36)*(1 + energy*0.7)),
                Math.max(1, 1.3 + I*2.1), pc, pcw);
        }
      }
    }

    function drawIris(ts, t) {
      const cx0 = W*0.40, cy0 = H*0.46;
      const maxR = Math.hypot(Math.max(cx0, W-cx0), Math.max(cy0, H-cy0)) || 1;
      const k = 6 + energy*7;        // ring frequency tightens as it focuses
      const spd = 0.5 - energy*2.4;  // rings drift inward while rendering
      for (let gx = 0; gx < W; gx += CELL) {
        for (let gy = 0; gy < H; gy += CELL) {
          const x = gx + CELL/2, y = gy + CELL/2;
          const dx = x - cx0, dy = y - cy0;
          const nr = Math.min(1, Math.hypot(dx, dy)/maxR), ang = Math.atan2(dy, dx);
          const ring = Math.abs(Math.sin(nr*k*Math.PI + t*spd));
          const blade = 0.58 + 0.42*Math.sin(ang*6 + t*0.25);   // 6 aperture blades
          const breath = 0.78 + 0.22*Math.sin(t*0.5 - nr*2.5);
          samplePulses(ts, nr);
          let I = (0.26 + 0.74*ring)*blade*breath*(0.62 + 0.5*energy);
          I *= (1 - Math.pow(nr, 1.7));                          // soft edge vignette
          I += pAdd*0.8;
          if (I < BAYER[(gx/CELL|0)%4][(gy/CELL|0)%4]*0.40 + 0.045) continue;
          stamp(x, y, Math.floor((nr*0.85 + I*0.25 + energy*0.1)*95),
                Math.min(0.72, (0.07 + I*0.4)*(1 + energy*0.5)),
                Math.max(1, 1.2 + I*1.9), pColor, pWeight);
        }
      }
    }

    const draw = iris ? drawIris : drawLinear;

    function frame(ts) {
      const t = ts/1000;
      energy += (energyTarget - energy) * 0.05;
      for (let i = pulses.length-1; i >= 0; i--) {
        if ((ts - pulses[i].t0)/pulses[i].dur >= 1) pulses.splice(i, 1);
      }
      ctx.clearRect(0, 0, W, H);
      draw(ts, t);
      if (!reduce) requestAnimationFrame(frame);
    }

    resize();
    addEventListener("resize", resize);
    addEventListener("load", resize);
    if (document.fonts) document.fonts.ready.then(resize);
    // frame() re-schedules itself unless reduced motion is on, in which case this
    // single call paints one static field
    requestAnimationFrame(frame);

    return {
      // colour, strength, duration, position (0..1 along the field; linear only)
      ping(c, s = 1, dur = 900, pos = 0.5) {
        if (reduce) return;
        pulses.push({ p: pos, c, s, t0: performance.now(), dur });
        if (pulses.length > MAX_PULSES) pulses.shift();
      },
      setEnergy(v) { energyTarget = v; },
      setPlayhead(v) { playhead = v; },
      resize,
    };
  }

  window.AutoDubWave = { RAMP, create };
})();
