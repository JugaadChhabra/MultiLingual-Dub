/* AutoDub — shared optional sound.
   A pentatonic blip on selection, a chord on completion. Both surfaces used an
   identical copy of this; the only real difference was how the AudioContext gets
   unlocked, which is now a flag.

   Usage:
     const sfx = AutoDubAudio.create({ enabled: false });   // Studio: button-gated
     sfx.setEnabled(true); sfx.note(3);

     const sfx = AutoDubAudio.create({ enabled: true });    // Video Studio: always on
     sfx.unlockOnFirstGesture();                            // autoplay policy
     sfx.chord();

   Every call is best-effort: if the AudioContext is unavailable or blocked, the
   helpers no-op rather than throwing into the caller. */
(() => {
  "use strict";

  const PENTA = [261.63, 293.66, 329.63, 392.0, 440.0, 523.25, 587.33, 659.25, 783.99, 880.0, 1046.5];

  function create(opts) {
    let on = Boolean(opts && opts.enabled);
    let actx = null;

    function ensure() {
      try {
        actx = actx || new (window.AudioContext || window.webkitAudioContext)();
        return actx;
      } catch (e) { return null; }
    }

    function tone(freq, dur, type, gain) {
      if (!on) return;
      const a = ensure(); if (!a) return;
      try {
        const o = a.createOscillator(), g = a.createGain();
        o.type = type; o.frequency.value = freq;
        o.connect(g); g.connect(a.destination);
        const now = a.currentTime;
        g.gain.setValueAtTime(gain, now);
        g.gain.exponentialRampToValueAtTime(0.0001, now + dur);
        o.start(now); o.stop(now + dur);
      } catch (e) {}
    }

    return {
      enabled() { return on; },
      setEnabled(v) {
        on = Boolean(v);
        if (on) { const a = ensure(); if (a) a.resume(); }
      },
      // browsers refuse to start audio before a user gesture
      unlockOnFirstGesture() {
        ["pointerdown", "keydown"].forEach((e) =>
          addEventListener(e, () => { const a = ensure(); if (a) a.resume(); }, { once: true }));
      },
      note(i) { tone(PENTA[i % PENTA.length], 0.17, "sine", 0.05); },
      chord() { [0, 2, 4].forEach((n, k) => setTimeout(() => tone(PENTA[n+4], 0.55, "sine", 0.04), k*95)); },
    };
  }

  window.AutoDubAudio = { create };
})();
