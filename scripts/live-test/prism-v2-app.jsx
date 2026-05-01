/* eslint-disable */
// Pulsar live broadcast scene -- React app loaded by test-scene.html.
//
// Edit this file freely. The shell (test-scene.html) provides:
//   - window.__pulsarTelemetry  : hot-ref filled by the WS adapter
//   - window.__pulsarSfx        : { whoosh, chime, droneStart, blip, tick }
//
// Scenes ordered along a single timeline. Scene 0 is the Apple-style
// "Introducing Pulsar" intro -- runs once per session, skipped on every
// subsequent loop so the broadcast doesn't keep flashing the title card.

const { useState, useEffect, useRef, useMemo } = React;

// ─────────────────────────────────────────────────────────────────────────────
// PRIMITIVES
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const lerp  = (a, b, t) => a + (b - a) * t;
const ease = {
  inOutCubic: t => t < 0.5 ? 4*t*t*t : 1 - Math.pow(-2*t + 2, 3)/2,
  outCubic:   t => 1 - Math.pow(1 - t, 3),
  outQuart:   t => 1 - Math.pow(1 - t, 4),
  outExpo:    t => t === 1 ? 1 : 1 - Math.pow(2, -10 * t),
};

// SFX dispatcher -- short-circuits to a no-op if window.__pulsarSfx is
// missing (Web Audio API unavailable / blocked). Keeps the sound calls
// inside scenes terse.
const sfx = (name, ...args) => {
  const k = (window.__pulsarSfx || {})[name];
  if (typeof k === 'function') k(...args);
};

// ─────────────────────────────────────────────────────────────────────────────
// TIMELINE
// Scene 0 is the new "Introducing Pulsar" intro. It runs once at the
// beginning of the loop ; subsequent loop iterations skip it so the
// sustained broadcast doesn't keep flashing the title card every cycle.
const SCENES = [
  { id: 's0', start: 0,  end: 7,  label: '00 / Intro',        intro: true  },
  { id: 's1', start: 7,  end: 19, label: '01 / Boot' },
  { id: 's2', start: 19, end: 33, label: '02 / Pipeline' },
  { id: 's3', start: 33, end: 47, label: '03 / Destinations' },
  { id: 's4', start: 47, end: 61, label: '04 / Licence' },
  { id: 's5', start: 61, end: 71, label: '05 / Validated' },
];
const TOTAL = SCENES[SCENES.length - 1].end;

function useTimeline() {
  const [t, setT] = useState(0);
  useEffect(() => {
    let raf, last = performance.now(), introDone = false, lastScene = -1;
    const tick = (now) => {
      const dt = (now - last) / 1000;
      last = now;
      setT(prev => {
        if (window.__seekT != null) return window.__seekT % TOTAL;
        let next = prev + dt;
        if (!introDone && next >= SCENES[0].end) introDone = true;
        if (next >= TOTAL) {
          // Loop : restart at the end of the intro so it never replays.
          next = introDone ? SCENES[1].start + (next - TOTAL) : 0;
        }
        // Fire a swoop on every scene-to-scene transition (skips intro
        // since it has its own whoosh).
        const nowScene = SCENES.findIndex(s => next >= s.start && next < s.end);
        if (nowScene !== lastScene) {
          if (lastScene >= 0 && nowScene > 0) sfx('swoop');
          lastScene = nowScene;
        }
        return next;
      });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return t;
}

// ─────────────────────────────────────────────────────────────────────────────
// SCENE TIMING — soft fade + tiny slide
function useSceneTiming(sceneT, dur, opts = {}) {
  const fadeIn  = opts.in  ?? 0.9;
  const fadeOut = opts.out ?? 0.7;
  const inP  = clamp(sceneT / fadeIn, 0, 1);
  const outP = 1 - clamp((sceneT - (dur - fadeOut)) / fadeOut, 0, 1);
  const op   = ease.outQuart(inP) * ease.outQuart(outP);
  return { inP, outP, op, enterY: (1 - ease.outCubic(inP)) * 18 };
}

// ─────────────────────────────────────────────────────────────────────────────
// TELEMETRY HOOK — reads window.__pulsarTelemetry every frame
function useTelemetry() {
  const [snap, setSnap] = useState(() => ({ ...(window.__pulsarTelemetry || {}) }));
  useEffect(() => {
    let raf;
    const tick = () => {
      setSnap({ ...(window.__pulsarTelemetry || {}) });
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return snap;
}

const fmtNum = (n) => (n === null || n === undefined || Number.isNaN(n))
  ? '—' : Math.round(n).toLocaleString('en-US');
const fmtPct = (n) => (n === null || n === undefined || Number.isNaN(n))
  ? '—' : n.toFixed(1);

// ─────────────────────────────────────────────────────────────────────────────
// HUD — top-right telemetry panel
// Constrained to a 360 px column on the far right ; scene content keeps
// maxWidth ≤ 1080 so they never overlap regardless of telemetry text.

function HUD({ visible }) {
  const tel = useTelemetry();
  const sec = Math.max(0, Math.floor((Date.now() - (tel.sessionStartedAt || Date.now())) / 1000));
  const mm  = String(Math.floor(sec / 60)).padStart(2, '0');
  const ss  = String(sec % 60).padStart(2, '0');
  const dropPct = tel.totalFrames > 0 ? (100 * (tel.droppedFrames || 0) / tel.totalFrames) : 0;
  const live = !!tel.streaming;

  // Lit a "warm" tint on bitrate value when adaptive recently scaled down.
  const recentlyAdjusted = tel.bitrateKbps !== null && tel.bitrateTargetKbps !== null
    && Math.abs(tel.bitrateKbps - tel.bitrateTargetKbps) > 50;

  const labelStyle = {
    fontFamily: 'var(--sf-mono)', fontSize: 10,
    letterSpacing: '0.18em', textTransform: 'uppercase',
    color: 'var(--fg-faint)',
  };
  const valueStyle = {
    fontFamily: 'var(--sf-mono)', fontSize: 18, fontWeight: 500,
    fontVariantNumeric: 'tabular-nums',
    color: 'var(--fg)', lineHeight: 1.1,
    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
  };
  const unitStyle = { fontSize: 11, color: 'var(--fg-mute)', marginLeft: 4, fontWeight: 400 };

  const Stat = ({ label, value, valueStyle: vs }) => (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
      <div style={labelStyle}>{label}</div>
      <div style={{ ...valueStyle, ...(vs || {}) }}>{value}</div>
    </div>
  );

  return (
    <div style={{
      position: 'absolute', top: 32, right: 32, zIndex: 1000,
      width: 360,
      padding: '20px 22px',
      background: 'rgba(20, 20, 22, 0.72)',
      backdropFilter: 'blur(28px) saturate(180%)',
      WebkitBackdropFilter: 'blur(28px) saturate(180%)',
      border: '1px solid var(--hairline-strong)',
      borderRadius: 16,
      fontFamily: 'var(--sf-text)',
      color: 'var(--fg)',
      letterSpacing: '-0.01em',
      opacity: visible ? 1 : 0,
      transform: `translateY(${visible ? 0 : -6}px)`,
      transition: 'opacity 0.6s ease, transform 0.6s ease',
      pointerEvents: 'none',
    }}>
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        marginBottom: 16,
      }}>
        <div style={{
          fontFamily: 'var(--sf-display)', fontSize: 12, fontWeight: 600,
          letterSpacing: '0.08em', textTransform: 'uppercase',
          color: 'var(--fg-mute)',
        }}>Twitch broadcast</div>
        <div style={{
          display: 'inline-flex', alignItems: 'center', gap: 6,
          fontFamily: 'var(--sf-mono)', fontSize: 10,
          letterSpacing: '0.1em', textTransform: 'uppercase',
          color: live ? 'var(--accent)' : 'var(--fg-faint)',
        }}>
          <span style={{
            width: 6, height: 6, borderRadius: 999,
            background: live ? 'var(--accent)' : 'var(--fg-faint)',
            boxShadow: live ? '0 0 6px var(--accent)' : 'none',
            animation: live ? 'pulseDot 1.2s ease-in-out infinite' : 'none',
          }}/>
          {live ? 'Live' : 'Idle'}
        </div>
      </div>

      {/* 4×2 stat grid */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(2, 1fr)',
        gap: '14px 24px',
      }}>
        <Stat label="Bitrate"
              value={<>{fmtNum(tel.bitrateKbps)}<span style={unitStyle}>kbps</span></>}
              valueStyle={recentlyAdjusted ? { color: 'var(--accent)' } : null}/>
        <Stat label="Target"
              value={<>{fmtNum(tel.bitrateTargetKbps)}<span style={unitStyle}>kbps</span></>}/>
        <Stat label="Resolution" value={tel.resolution || '—'}/>
        <Stat label="Frame rate"
              value={<>{fmtNum(tel.fps)}<span style={unitStyle}>fps</span></>}/>
        <Stat label="Frames" value={fmtNum(tel.totalFrames)}/>
        <Stat label="Dropped"
              value={<>{fmtNum(tel.droppedFrames)}<span style={unitStyle}>{dropPct.toFixed(2)}%</span></>}
              valueStyle={dropPct > 5 ? { color: 'var(--warn)' } : null}/>
        <Stat label="CPU"
              value={<>{fmtPct(tel.cpuPct)}<span style={unitStyle}>%</span></>}/>
        <Stat label="Memory"
              value={<>{fmtNum(tel.memMB)}<span style={unitStyle}>MB</span></>}/>
      </div>

      {/* Footer : destinations + uptime */}
      <div style={{
        marginTop: 16, paddingTop: 14,
        borderTop: '1px solid var(--hairline)',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        fontFamily: 'var(--sf-mono)', fontSize: 10,
        color: 'var(--fg-faint)',
      }}>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
          {(tel.destinations || []).slice(0, 3).map((d, i) => {
            const active = d.state === 'active' || d.active;
            return (
              <span key={i} style={{
                display: 'inline-flex', alignItems: 'center', gap: 5,
                padding: '3px 7px',
                border: '1px solid ' + (active ? 'rgba(48,209,88,0.35)' : 'var(--hairline)'),
                borderRadius: 6,
                color: active ? 'var(--ok)' : 'var(--fg-mute)',
              }}>
                {active && <span style={{ width: 4, height: 4, borderRadius: 999, background: 'var(--ok)' }}/>}
                {d.kind || 'rtmp'}
              </span>
            );
          })}
          {(!tel.destinations || tel.destinations.length === 0) && (
            <span style={{ color: 'var(--fg-faint)' }}>no destinations</span>
          )}
        </div>
        <div style={{ fontVariantNumeric: 'tabular-nums' }}>{mm}:{ss}</div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// SCENE CHIP — bottom-left, hidden during intro
function SceneChip({ label, op, hidden }) {
  if (hidden) return null;
  return (
    <div style={{
      position: 'absolute', bottom: 28, left: 32, zIndex: 900,
      opacity: op,
      display: 'flex', alignItems: 'center', gap: 10,
      fontFamily: 'var(--sf-mono)', fontSize: 11,
      letterSpacing: '0.18em', textTransform: 'uppercase',
      color: 'var(--fg-mute)',
    }}>
      <span style={{ width: 18, height: 1, background: 'var(--hairline-strong)' }}/>
      {label}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// PROGRESS BAR — hairline at top of frame, hidden during intro
function Progress({ t, hidden }) {
  if (hidden) return null;
  // Progress over the looping section only (skip the intro slot).
  const loopStart = SCENES[1].start;
  const loopDur   = TOTAL - loopStart;
  const tt = clamp((t - loopStart) / loopDur, 0, 1);
  return (
    <div style={{
      position: 'absolute', top: 0, left: 0, right: 0, zIndex: 800,
      height: 2, pointerEvents: 'none',
    }}>
      <div style={{
        height: '100%', width: (tt * 100) + '%',
        background: 'linear-gradient(90deg, transparent, var(--accent) 80%)',
      }}/>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// SHARED PRIMITIVES
const Eyebrow = ({ children, style }) => (
  <div style={{
    fontFamily: 'var(--sf-mono)', fontSize: 11, fontWeight: 500,
    letterSpacing: '0.22em', textTransform: 'uppercase',
    color: 'var(--accent)',
    ...style,
  }}>{children}</div>
);

const Headline = ({ children, size = 88, style }) => (
  <div style={{
    fontFamily: 'var(--sf-display)',
    fontSize: size, fontWeight: 600,
    letterSpacing: '-0.045em',
    lineHeight: 1.02,
    color: 'var(--fg)',
    ...style,
  }}>{children}</div>
);

const Subhead = ({ children, style }) => (
  <div style={{
    fontFamily: 'var(--sf-text)', fontSize: 22, fontWeight: 400,
    letterSpacing: '-0.01em', lineHeight: 1.45,
    color: 'var(--fg-mute)',
    maxWidth: 760,
    ...style,
  }}>{children}</div>
);

function WordReveal({ text, delay = 0, sceneT, perWord = 0.06, style }) {
  const words = String(text || '').split(' ');
  return (
    <span style={{ display: 'inline' }}>
      {words.map((w, i) => {
        const lp = clamp((sceneT - delay - i * perWord) / 0.5, 0, 1);
        return (
          <span key={i} style={{
            display: 'inline-block',
            opacity: ease.outQuart(lp),
            transform: `translateY(${(1 - ease.outQuart(lp)) * 14}px)`,
            marginRight: '0.28em',
            ...style,
          }}>{w}</span>
        );
      })}
    </span>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// SCENE 0 — INTRO  ("Introducing Pulsar", Apple-style letter reveal)
// Per-letter cascade : opacity 0→1, blur 8px→0, translateY 14px→0.
// Then "Pulsar" interpolates white → SF System Orange over ~1 s.
// SFX : whoosh on first letter, chime on warm transition, drone ramps in.

function Scene0({ sceneT, dur }) {
  const STAGGER    = 0.08;
  const REVEAL_DUR = 0.9;
  const word1 = 'Introducing';
  const word2 = 'Pulsar';
  const totalLetters    = word1.length + word2.length;
  const lastLetterStart = (totalLetters - 1) * STAGGER;
  const revealEnd       = lastLetterStart + REVEAL_DUR;
  const warmStart       = revealEnd + 0.4;
  const warmDur         = 1.0;
  const warmP    = ease.outQuart(clamp((sceneT - warmStart) / warmDur, 0, 1));
  const fadeOutP = clamp((sceneT - (dur - 1.0)) / 1.0, 0, 1);

  // Each cue fires exactly once per intro pass (refs survive React renders).
  const firedWhoosh = useRef(false);
  const firedChime  = useRef(false);
  useEffect(() => {
    if (sceneT > 0.05 && !firedWhoosh.current) {
      sfx('whoosh', 2.4); firedWhoosh.current = true;
    }
    if (sceneT > warmStart && !firedChime.current) {
      sfx('chime', 523.25, 2.4); firedChime.current = true;
    }
  }, [sceneT]);

  const renderWord = (word, baseIndex, warm) => {
    return [...word].map((ch, i) => {
      const idx = baseIndex + i;
      const lp = clamp((sceneT - idx * STAGGER) / REVEAL_DUR, 0, 1);
      const e  = ease.outQuart(lp);
      const color = warm
        ? `rgb(${lerp(245, 255, warmP)}, ${lerp(245, 159, warmP)}, ${lerp(247, 10, warmP)})`
        : 'var(--fg)';
      return (
        <span key={i} style={{
          display: 'inline-block',
          opacity: e,
          transform: `translateY(${(1 - e) * 14}px)`,
          filter: `blur(${(1 - e) * 8}px)`,
          color,
          transition: 'color 0.6s ease',
        }}>{ch}</span>
      );
    });
  };

  return (
    <div style={{
      position: 'absolute', inset: 0,
      background: '#000',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      opacity: 1 - fadeOutP,
    }}>
      {/* Halo glow once Pulsar warms. */}
      <div style={{
        position: 'absolute', inset: 0,
        background: 'radial-gradient(circle at 50% 50%, rgba(255,159,10,0.18) 0%, rgba(255,159,10,0.05) 30%, transparent 55%)',
        opacity: warmP,
        pointerEvents: 'none',
      }}/>
      <h1 style={{
        margin: 0, padding: 0,
        fontFamily: 'var(--sf-display)',
        fontWeight: 200,
        fontSize: 'clamp(72px, 8vw, 144px)',
        letterSpacing: '-0.035em',
        whiteSpace: 'nowrap',
        zIndex: 1,
      }}>
        <span style={{ display: 'inline-block' }}>
          {renderWord(word1, 0, false)}
        </span>
        <span style={{ display: 'inline-block', marginLeft: '0.35em' }}>
          {renderWord(word2, word1.length, true)}
        </span>
      </h1>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// SCENE 1 — Boot (typography hero)
function Scene1({ sceneT, dur }) {
  const { op, enterY } = useSceneTiming(sceneT, dur);
  const bootLines = [
    { t: 5.0, text: '$ pulsar.exe --headless' },
    { t: 5.6, text: '> libobs 32.1.2 · obs-ws v5' },
    { t: 6.2, text: '> binding 127.0.0.1:auto' },
    { t: 6.8, text: '> issuing session JWT' },
    { t: 7.4, text: 'PULSAR_READY 51847 ey…' },
  ];
  // Soft tick on each new boot line lighting up.
  const ticked = useRef(0);
  useEffect(() => {
    const i = bootLines.findIndex(l => sceneT < l.t);
    const next = i === -1 ? bootLines.length : i;
    if (next > ticked.current) { ticked.current = next; sfx('click'); }
  }, [sceneT]);

  return (
    <div style={{ position: 'absolute', inset: 0, opacity: op }}>
      {/* Hero block. maxWidth 1080 keeps it left of the HUD column. */}
      <div style={{ position: 'absolute', left: 120, top: 220, maxWidth: 1080 }}>
        <div style={{ opacity: clamp(sceneT/0.6, 0, 1), transform: `translateY(${enterY}px)` }}>
          <Eyebrow>Pulsar · Streaming Service</Eyebrow>
        </div>
        <div style={{ marginTop: 22 }}>
          <Headline size={120}>
            <WordReveal sceneT={sceneT} delay={0.4} perWord={0.08} text="The headless"/>
            <br/>
            <WordReveal sceneT={sceneT} delay={0.7} perWord={0.08} text="streaming engine."/>
          </Headline>
        </div>
        <div style={{
          marginTop: 32,
          opacity: clamp((sceneT - 2.4)/0.8, 0, 1),
          transform: `translateY(${(1 - clamp((sceneT - 2.4)/0.8, 0, 1)) * 12}px)`,
        }}>
          <Subhead>
            A forked, headless libobs that Prism Studio spawns at boot — speaking obs-websocket v5 over loopback, nothing else.
          </Subhead>
        </div>
      </div>
      {/* Boot transcript bottom-left, constrained to 560 px so it never
          reaches the HUD column or the SceneChip area. */}
      <div style={{
        position: 'absolute', left: 120, bottom: 90,
        fontFamily: 'var(--sf-mono)', fontSize: 13, lineHeight: 1.9,
        color: 'var(--fg-mute)',
        maxWidth: 560,
      }}>
        {bootLines.map((l, i) => {
          const lp = clamp((sceneT - l.t) / 0.4, 0, 1);
          const isReady = i === bootLines.length - 1;
          return (
            <div key={i} style={{
              opacity: lp,
              color: isReady ? 'var(--accent)' : 'var(--fg-mute)',
              fontWeight: isReady ? 500 : 400,
            }}>{l.text}</div>
          );
        })}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// SCENE 2 — Pipeline (encode-once / fan-out-N wireframe)
function Scene2({ sceneT, dur }) {
  const { op, enterY } = useSceneTiming(sceneT, dur);
  const sources = [
    { label: 'WASAPI · mic',     d: 1.6 },
    { label: 'WASAPI · desktop', d: 1.85 },
    { label: 'Window capture',   d: 2.1 },
    { label: 'Game · DLL hook',  d: 2.35 },
  ];
  const dests = [
    { label: 'twitch',      d: 5.6 },
    { label: 'rtmp_custom', d: 5.85 },
    { label: 'vod_local',   d: 6.1 },
  ];
  const cx = 800, cy = 540;

  // Tick on each connector that lights up.
  const tickedSrc = useRef(0), tickedDst = useRef(0);
  useEffect(() => {
    const ns = sources.filter(s => sceneT > s.d + 0.3).length;
    if (ns > tickedSrc.current) { tickedSrc.current = ns; sfx('click'); }
    const nd = dests.filter(d => sceneT > d.d + 0.3).length;
    if (nd > tickedDst.current) { tickedDst.current = nd; sfx('click'); }
  }, [sceneT]);

  return (
    <div style={{ position: 'absolute', inset: 0, opacity: op }}>
      <div style={{
        position: 'absolute', left: 120, top: 130, maxWidth: 1000,
        opacity: clamp(sceneT/0.6, 0, 1), transform: `translateY(${enterY}px)`,
      }}>
        <Eyebrow>Pipeline</Eyebrow>
        <div style={{ marginTop: 16 }}>
          <Headline size={68}>Encode once. Fan out N.</Headline>
        </div>
        <div style={{ marginTop: 18, opacity: clamp((sceneT-1.0)/0.6, 0, 1) }}>
          <Subhead>One x264 + AAC pair — CBR 6000 / 160, keyint 2s — feeds every destination at once.</Subhead>
        </div>
      </div>

      <svg width="1600" height="900" style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
        {sources.map((s, i) => {
          const sx = 200 + 110, sy = 470 + i * 56;
          const ex = cx - 130,  ey = cy;
          const lp = clamp((sceneT - s.d - 0.3) / 0.6, 0, 1);
          return (
            <path key={i}
              d={`M ${sx} ${sy} C ${sx + 100} ${sy}, ${ex - 100} ${ey}, ${ex} ${ey}`}
              stroke="rgba(255,255,255,0.18)" strokeWidth="1" fill="none"
              strokeDasharray="600" strokeDashoffset={(1 - lp) * 600}
            />
          );
        })}
        {dests.map((d, i) => {
          const sx = cx + 130, sy = cy;
          const ex = 1180,      ey = 484 + i * 64;
          const lp = clamp((sceneT - d.d - 0.3) / 0.6, 0, 1);
          return (
            <path key={i}
              d={`M ${sx} ${sy} C ${sx + 100} ${sy}, ${ex - 100} ${ey}, ${ex} ${ey}`}
              stroke="rgba(255, 159, 10, 0.45)" strokeWidth="1" fill="none"
              strokeDasharray="500" strokeDashoffset={(1 - lp) * 500}
            />
          );
        })}
      </svg>

      <div style={{ position: 'absolute', left: 200, top: 460 }}>
        {sources.map((s, i) => {
          const lp = clamp((sceneT - s.d) / 0.5, 0, 1);
          return (
            <div key={i} style={{
              width: 220, padding: '12px 18px',
              marginBottom: 12,
              border: '1px solid var(--hairline-strong)',
              borderRadius: 12,
              background: 'rgba(255,255,255,0.02)',
              fontFamily: 'var(--sf-mono)', fontSize: 13,
              color: 'var(--fg)',
              opacity: ease.outQuart(lp),
              transform: `translateX(${(1 - ease.outQuart(lp)) * -16}px)`,
              display: 'flex', alignItems: 'center', gap: 10,
            }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--fg-faint)' }}/>
              {s.label}
            </div>
          );
        })}
      </div>

      <div style={{
        position: 'absolute',
        left: cx - 130, top: cy - 70,
        width: 260, height: 140,
        border: '1px solid var(--accent)',
        borderRadius: 16,
        background: 'rgba(255, 159, 10, 0.04)',
        opacity: clamp((sceneT - 3.6) / 0.7, 0, 1),
        transform: `scale(${0.96 + 0.04 * ease.outQuart(clamp((sceneT - 3.6) / 0.7, 0, 1))})`,
        display: 'flex', flexDirection: 'column',
        justifyContent: 'center', alignItems: 'center', gap: 10,
      }}>
        <div style={{
          fontFamily: 'var(--sf-mono)', fontSize: 11,
          letterSpacing: '0.2em', color: 'var(--accent)',
          textTransform: 'uppercase',
        }}>encoder</div>
        <div style={{
          fontFamily: 'var(--sf-display)', fontSize: 32, fontWeight: 600,
          letterSpacing: '-0.02em',
        }}>x264 · AAC</div>
        <div style={{
          fontFamily: 'var(--sf-mono)', fontSize: 12, color: 'var(--fg-mute)',
        }}>CBR 6000 · 1080p60</div>
      </div>

      <div style={{ position: 'absolute', left: 1180, top: 470 }}>
        {dests.map((d, i) => {
          const lp = clamp((sceneT - d.d) / 0.5, 0, 1);
          return (
            <div key={i} style={{
              width: 220, padding: '12px 18px',
              marginBottom: 16,
              border: '1px solid var(--hairline-strong)',
              borderRadius: 12,
              background: 'rgba(255,255,255,0.02)',
              fontFamily: 'var(--sf-mono)', fontSize: 13,
              color: 'var(--fg)',
              opacity: ease.outQuart(lp),
              transform: `translateX(${(1 - ease.outQuart(lp)) * 16}px)`,
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            }}>
              <span>{d.label}</span>
              <span style={{
                fontSize: 10, color: 'var(--ok)',
                opacity: clamp((sceneT - d.d - 0.4) / 0.4, 0, 1),
              }}>● live</span>
            </div>
          );
        })}
      </div>

      <div style={{
        position: 'absolute', bottom: 90, left: '50%', transform: 'translateX(-50%)',
        opacity: clamp((sceneT - 7.5) / 0.6, 0, 1),
        fontFamily: 'var(--sf-mono)', fontSize: 12,
        letterSpacing: '0.2em', textTransform: 'uppercase',
        color: 'var(--fg-faint)',
      }}>
        pulsar:CallVendorRequest · multi-stream
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// SCENE 3 — Destinations (carousel)
function Scene3({ sceneT, dur }) {
  const { op, enterY } = useSceneTiming(sceneT, dur);
  const kinds = [
    { kind: 'twitch',      tag: 'live · oauth',  bitrate: '6000',  target: 'rtmp://ingest.twitch.tv' },
    { kind: 'rtmp_custom', tag: 'live · key',    bitrate: '6000',  target: 'rtmp://your-server' },
    { kind: 'vod_local',   tag: 'recording',     bitrate: '12000', target: '~/Movies/Pulsar/' },
    { kind: 'youtube',     tag: 'deferred',      bitrate: '6000',  target: 'rtmps://a.youtube.com' },
  ];
  const slotDur = 2.4;
  const idx = Math.floor((sceneT - 1.6) / slotDur);
  const activeIdx = clamp(idx, 0, kinds.length - 1);

  // Tick on slot change.
  const lastIdx = useRef(-1);
  useEffect(() => {
    if (activeIdx !== lastIdx.current && sceneT > 1.6) {
      lastIdx.current = activeIdx;
      sfx('click');
    }
  }, [activeIdx, sceneT]);

  return (
    <div style={{ position: 'absolute', inset: 0, opacity: op }}>
      {/* Header constrained -- carousel sits to the right of header but
          left of the HUD column. */}
      <div style={{ position: 'absolute', left: 120, top: 200, maxWidth: 720,
                    opacity: clamp(sceneT/0.6, 0, 1),
                    transform: `translateY(${enterY}px)` }}>
        <Eyebrow>Destinations</Eyebrow>
        <div style={{ marginTop: 18 }}>
          <Headline size={92}>
            <WordReveal sceneT={sceneT} delay={0.3} text="One pipeline."/>
            <br/>
            <WordReveal sceneT={sceneT} delay={0.6} text="Many endpoints."/>
          </Headline>
        </div>
        <div style={{ marginTop: 22, opacity: clamp((sceneT - 1.2) / 0.6, 0, 1) }}>
          <Subhead>
            Each <code style={{ fontFamily: 'var(--sf-mono)', fontSize: 18, color: 'var(--accent)' }}>kind</code> declares its own credentials and ingest target. Add one, remove one, hot — never re-encode.
          </Subhead>
        </div>
      </div>

      {/* Carousel pinned LEFT of the HUD (HUD = right:32 width:360, so
          right offset = 32 + 360 + 32 = 424). */}
      <div style={{
        position: 'absolute', right: 424, top: 280,
        width: 480, height: 380,
      }}>
        {kinds.map((k, i) => {
          const slotStart = 1.6 + i * slotDur;
          const lp  = clamp((sceneT - slotStart) / 0.5, 0, 1);
          const out = 1 - clamp((sceneT - slotStart - slotDur) / 0.4, 0, 1);
          if (i !== activeIdx) return null;
          const op2 = ease.outQuart(lp) * out;
          return (
            <div key={i} style={{
              position: 'absolute', inset: 0,
              opacity: op2,
              transform: `translateY(${(1 - ease.outQuart(lp)) * 24}px)`,
              padding: '36px 40px',
              border: '1px solid var(--hairline-strong)',
              borderRadius: 24,
              background: 'rgba(255,255,255,0.02)',
              backdropFilter: 'blur(10px)',
              display: 'flex', flexDirection: 'column',
              justifyContent: 'space-between',
            }}>
              <div>
                <div style={{ fontFamily: 'var(--sf-mono)', fontSize: 12,
                              letterSpacing: '0.2em', textTransform: 'uppercase',
                              color: 'var(--accent)' }}>kind</div>
                <div style={{ fontFamily: 'var(--sf-display)', fontSize: 60,
                              fontWeight: 600, letterSpacing: '-0.03em', marginTop: 8 }}>
                  {k.kind}
                </div>
                <div style={{ fontFamily: 'var(--sf-mono)', fontSize: 14,
                              color: 'var(--fg-mute)', marginTop: 6 }}>{k.tag}</div>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                <div>
                  <div style={{ fontSize: 11, color: 'var(--fg-faint)',
                                letterSpacing: '0.18em', textTransform: 'uppercase',
                                marginBottom: 4 }}>Target</div>
                  <div style={{ fontFamily: 'var(--sf-mono)', fontSize: 16, color: 'var(--fg)' }}>{k.target}</div>
                </div>
                <div>
                  <div style={{ fontSize: 11, color: 'var(--fg-faint)',
                                letterSpacing: '0.18em', textTransform: 'uppercase',
                                marginBottom: 4 }}>Bitrate</div>
                  <div style={{ fontFamily: 'var(--sf-mono)', fontSize: 16, color: 'var(--fg)' }}>{k.bitrate} kb/s</div>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <div style={{
        position: 'absolute', right: 424, top: 700, width: 480,
        display: 'flex', justifyContent: 'center', gap: 10,
      }}>
        {kinds.map((_, i) => (
          <span key={i} style={{
            width: i === activeIdx ? 24 : 6, height: 6, borderRadius: 999,
            background: i === activeIdx ? 'var(--accent)' : 'var(--hairline-strong)',
            transition: 'all 0.4s cubic-bezier(0.4, 0, 0.2, 1)',
          }}/>
        ))}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// SCENE 4 — Licence
function Scene4({ sceneT, dur }) {
  const { op, enterY } = useSceneTiming(sceneT, dur);
  const branchP = ease.outQuart(clamp((sceneT - 2.0) / 1.5, 0, 1));
  const wsP     = ease.outQuart(clamp((sceneT - 4.0) / 1.0, 0, 1));
  const stampP  = ease.outCubic(clamp((sceneT - 8.5) / 0.7, 0, 1));

  // Chime when the "Mere aggregation" stamp lands.
  const stampFired = useRef(false);
  useEffect(() => {
    if (sceneT > 8.5 && !stampFired.current) {
      stampFired.current = true;
      sfx('chime', 659.25, 1.6);
    }
  }, [sceneT]);

  const invariants = [
    'WebSocket-only IPC',
    'No FFI · no NAPI',
    'No shared memory',
    'Process boundary',
  ];

  return (
    <div style={{ position: 'absolute', inset: 0, opacity: op }}>
      <div style={{
        position: 'absolute', left: 120, top: 130, maxWidth: 880,
        opacity: clamp(sceneT/0.6, 0, 1), transform: `translateY(${enterY}px)`,
      }}>
        <Eyebrow>Licence</Eyebrow>
        <div style={{ marginTop: 16 }}>
          <Headline size={68}>Forking libobs — without inheriting it.</Headline>
        </div>
        <div style={{ marginTop: 18, opacity: clamp((sceneT - 1.0) / 0.6, 0, 1) }}>
          <Subhead>
            Pulsar inherits GPL-2.0. Prism stays under its own licence — the only IPC is WebSocket on loopback. Mere aggregation, not derivative work.
          </Subhead>
        </div>
      </div>

      <div style={{ position: 'absolute', left: 0, top: 540, width: 1600, height: 280 }}>
        <svg width="1600" height="280" style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
          <path d="M 430 140 L 620 140"
            stroke="var(--hairline-strong)" strokeWidth="1" fill="none"
            strokeDasharray="200" strokeDashoffset={(1 - branchP) * 200}/>
          <path d="M 960 140 L 1100 140"
            stroke="var(--accent)" strokeWidth="1" fill="none"
            strokeDasharray="6 8" opacity={wsP * 0.85}/>
          <circle cx="960"  cy="140" r="4" fill="var(--accent)" opacity={wsP}/>
          <circle cx="1100" cy="140" r="4" fill="var(--accent)" opacity={wsP}/>
        </svg>

        <div style={{
          position: 'absolute', left: 970, top: 90, width: 130,
          textAlign: 'center',
          fontFamily: 'var(--sf-mono)', fontSize: 11,
          letterSpacing: '0.18em', textTransform: 'uppercase',
          color: 'var(--accent)', opacity: wsP,
        }}>WebSocket · loopback</div>
        <div style={{
          position: 'absolute', left: 970, top: 168, width: 130,
          textAlign: 'center',
          fontFamily: 'var(--sf-mono)', fontSize: 10,
          letterSpacing: '0.16em', textTransform: 'uppercase',
          color: 'var(--fg-faint)', opacity: wsP,
        }}>dumpbin /exports → ∅</div>

        {/* obs-studio card */}
        <div style={{
          position: 'absolute', left: 210, top: 70, width: 220, height: 140,
          padding: '20px 22px',
          border: '1px solid var(--hairline-strong)',
          borderRadius: 16,
          background: 'rgba(255,255,255,0.02)',
          display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
        }}>
          <div>
            <div style={{ fontFamily: 'var(--sf-mono)', fontSize: 10,
                          letterSpacing: '0.22em', textTransform: 'uppercase',
                          color: 'var(--fg-faint)' }}>Upstream</div>
            <div style={{ fontFamily: 'var(--sf-display)', fontSize: 26,
                          fontWeight: 600, letterSpacing: '-0.02em', marginTop: 8 }}>
              obs-studio
            </div>
            <div style={{ fontFamily: 'var(--sf-mono)', fontSize: 12,
                          color: 'var(--fg-mute)', marginTop: 4 }}>v32.1.2</div>
          </div>
          <div style={{ fontFamily: 'var(--sf-mono)', fontSize: 10,
                        letterSpacing: '0.14em', color: 'var(--fg-mute)' }}>
            GPL-2.0-or-later
          </div>
        </div>

        {/* pulsar card */}
        <div style={{
          position: 'absolute', left: 620, top: 50, width: 340, height: 180,
          padding: '22px 26px',
          border: '1px solid var(--hairline-strong)',
          borderRadius: 16,
          background: 'rgba(255,255,255,0.02)',
          opacity: clamp((sceneT - 3.0) / 0.5, 0, 1),
          transform: `translateY(${(1 - clamp((sceneT - 3.0) / 0.5, 0, 1)) * 8}px)`,
          display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
        }}>
          <div>
            <div style={{ fontFamily: 'var(--sf-mono)', fontSize: 10,
                          letterSpacing: '0.22em', textTransform: 'uppercase',
                          color: 'var(--fg-faint)' }}>Fork</div>
            <div style={{ fontFamily: 'var(--sf-display)', fontSize: 32,
                          fontWeight: 600, letterSpacing: '-0.02em', marginTop: 8 }}>
              pulsar.exe
            </div>
            <div style={{ fontFamily: 'var(--sf-mono)', fontSize: 12,
                          color: 'var(--fg-mute)', marginTop: 8, lineHeight: 1.6 }}>
              upstream/ · patches/*.diff<br/>
              plugins/pulsar-headless<br/>
              plugins/pulsar-multi-stream
            </div>
          </div>
          <div style={{ fontFamily: 'var(--sf-mono)', fontSize: 10,
                        letterSpacing: '0.14em', color: 'var(--fg-mute)' }}>
            GPL-2.0 · copyleft inherited
          </div>
        </div>

        {/* prism card -- shifted left so it doesn't reach the HUD column. */}
        <div style={{
          position: 'absolute', left: 1100, top: 40, width: 320, height: 200,
          padding: '22px 26px',
          border: '1px solid var(--accent)',
          borderRadius: 16,
          background: 'rgba(255, 159, 10, 0.04)',
          opacity: clamp((sceneT - 5.5) / 0.6, 0, 1),
          transform: `translateY(${(1 - clamp((sceneT - 5.5) / 0.6, 0, 1)) * 10}px)`,
          display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
        }}>
          <div>
            <div style={{ fontFamily: 'var(--sf-mono)', fontSize: 10,
                          letterSpacing: '0.22em', textTransform: 'uppercase',
                          color: 'var(--accent)' }}>Consumer</div>
            <div style={{ fontFamily: 'var(--sf-display)', fontSize: 32,
                          fontWeight: 600, letterSpacing: '-0.02em', marginTop: 8 }}>
              Prism Studio
            </div>
            <div style={{ fontSize: 13, color: 'var(--fg-mute)', marginTop: 8, lineHeight: 1.5 }}>
              Spawns <code style={{ fontFamily: 'var(--sf-mono)', color: 'var(--fg)' }}>pulsar.exe</code> at boot. Talks v5 + <code style={{ fontFamily: 'var(--sf-mono)', color: 'var(--fg)' }}>pulsar:*</code>.
            </div>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {invariants.map((inv, i) => {
              const lp = clamp((sceneT - 6.5 - i * 0.25) / 0.4, 0, 1);
              return (
                <span key={i} style={{
                  fontFamily: 'var(--sf-mono)', fontSize: 10,
                  padding: '4px 9px', borderRadius: 999,
                  border: '1px solid var(--hairline-strong)',
                  color: 'var(--fg-mute)',
                  opacity: lp,
                  transform: `scale(${0.9 + 0.1 * ease.outQuart(lp)})`,
                  transformOrigin: 'left center',
                  letterSpacing: '0.04em',
                }}>{inv}</span>
              );
            })}
          </div>
        </div>

        {/* MERE AGGREGATION stamp */}
        <div style={{
          position: 'absolute', left: 1240, top: -10,
          opacity: stampP,
          transform: `scale(${0.92 + 0.08 * stampP}) rotate(${(1 - stampP) * 6}deg)`,
          transformOrigin: 'center',
          padding: '6px 12px',
          border: '1px solid var(--accent)',
          borderRadius: 999,
          fontFamily: 'var(--sf-mono)', fontSize: 10,
          letterSpacing: '0.16em', textTransform: 'uppercase',
          color: 'var(--accent)',
          background: 'rgba(0, 0, 0, 0.6)',
        }}>✓ Mere aggregation</div>
      </div>

      <div style={{
        position: 'absolute', bottom: 80, left: 120,
        opacity: clamp((sceneT - 10.5) / 0.6, 0, 1),
        fontFamily: 'var(--sf-mono)', fontSize: 12,
        letterSpacing: '0.2em', textTransform: 'uppercase',
        color: 'var(--fg-faint)',
      }}>CONSUMER-AUDIT.md · enforced in CI</div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// SCENE 5 — E2E checklist
function Scene5({ sceneT, dur }) {
  const { op, enterY } = useSceneTiming(sceneT, dur);
  const checks = [
    { l: 'pulsar.exe boot · libobs 32.1.2',                d: 1.6 },
    { l: 'WS handshake · v5 challenge / response',         d: 2.0 },
    { l: 'WASAPI graph · mic + desktop + per-process',     d: 2.4 },
    { l: 'x264 + AAC · CBR 6000 / 1080p60',                d: 2.8 },
    { l: 'pulsar:CallVendorRequest · destinations × 3',    d: 3.2 },
    { l: 'Twitch ingest · 30s · 0 dropped frames',         d: 3.6 },
    { l: 'Adaptive bitrate worker · BitrateAdjusted',      d: 4.0 },
    { l: 'CONSUMER-AUDIT · dumpbin /exports → ∅',          d: 4.4 },
  ];
  // Blip on each new green-check landing.
  const lit = useRef(0);
  useEffect(() => {
    const n = checks.filter(c => sceneT > c.d).length;
    if (n > lit.current) { lit.current = n; sfx('blip'); }
  }, [sceneT]);

  return (
    <div style={{ position: 'absolute', inset: 0, opacity: op }}>
      <div style={{ position: 'absolute', left: 120, top: 160,
                    opacity: clamp(sceneT/0.6, 0, 1),
                    transform: `translateY(${enterY}px)` }}>
        <Eyebrow>End-to-end</Eyebrow>
        <div style={{ marginTop: 18 }}>
          <Headline size={120}>
            <WordReveal sceneT={sceneT} delay={0.3} perWord={0.1} text="Validated."/>
          </Headline>
        </div>
        <div style={{ marginTop: 22, opacity: clamp((sceneT - 0.9) / 0.6, 0, 1) }}>
          <Subhead>Eight checks. All green. End to end.</Subhead>
        </div>
      </div>

      <div style={{ position: 'absolute', left: 120, top: 460, width: 880 }}>
        {checks.map((c, i) => {
          const lp = clamp((sceneT - c.d) / 0.4, 0, 1);
          const e  = ease.outQuart(lp);
          return (
            <div key={i} style={{
              display: 'flex', alignItems: 'center', gap: 18,
              padding: '14px 0',
              borderBottom: '1px solid var(--hairline)',
              opacity: e, transform: `translateX(${(1 - e) * -12}px)`,
            }}>
              <div style={{
                width: 22, height: 22, borderRadius: '50%',
                background: 'var(--ok)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: '#000', fontSize: 12, fontWeight: 700,
                flexShrink: 0,
              }}>✓</div>
              <div style={{
                flex: 1,
                fontFamily: 'var(--sf-text)', fontSize: 17, fontWeight: 400,
                color: 'var(--fg)', letterSpacing: '-0.01em',
              }}>{c.l}</div>
              <div style={{
                fontFamily: 'var(--sf-mono)', fontSize: 11,
                letterSpacing: '0.2em', textTransform: 'uppercase',
                color: 'var(--fg-mute)',
              }}>Pass</div>
            </div>
          );
        })}
      </div>

      {/* Bottom-right loop hint, sits below the HUD column. */}
      <div style={{
        position: 'absolute', bottom: 80, right: 32,
        opacity: clamp((sceneT - 6.5) / 0.6, 0, 1),
        fontFamily: 'var(--sf-mono)', fontSize: 12,
        letterSpacing: '0.2em', textTransform: 'uppercase',
        color: 'var(--fg-faint)',
      }}>↻ pulsar:ServiceReady</div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// ROUTER + APP
function SceneRouter({ t }) {
  let active = 0;
  for (let i = 0; i < SCENES.length; i++) {
    if (t >= SCENES[i].start && t < SCENES[i].end) { active = i; break; }
    if (i === SCENES.length - 1 && t >= SCENES[i].start) active = i;
  }
  const scene = SCENES[active];
  const sceneT = t - scene.start;
  const dur = scene.end - scene.start;
  const Comp = [Scene0, Scene1, Scene2, Scene3, Scene4, Scene5][active];
  const { op } = useSceneTiming(sceneT, dur, { in: 0.8, out: 0.6 });
  return (
    <>
      <Comp t={t} sceneT={sceneT} dur={dur}/>
      <SceneChip label={scene.label} op={op} hidden={!!scene.intro}/>
    </>
  );
}

function App() {
  const t = useTimeline();
  const inIntro = t < SCENES[0].end;

  const [scale, setScale] = useState(1);
  useEffect(() => {
    const measure = () => {
      const W = 1600, H = 900;
      setScale(Math.min(window.innerWidth / W, window.innerHeight / H));
    };
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, []);

  return (
    <div style={{
      position: 'absolute', inset: 0,
      background: '#000',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      overflow: 'hidden',
    }}>
      <div style={{
        width: 1600, height: 900,
        position: 'relative',
        transform: `scale(${scale})`,
        transformOrigin: 'center',
        flexShrink: 0,
        background: '#000',
        overflow: 'hidden',
      }}>
        <SceneRouter t={t}/>
        <Progress t={t} hidden={inIntro}/>
        <HUD visible={!inIntro}/>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
