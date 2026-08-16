import type {
  CorrelationMatch,
  CorrelationRecord,
  StateEvent,
  ThresholdDerivation,
  VisualEvent,
} from "./types.js";

export interface CorrelateOptions {
  /** Generous window used ONLY to gather a candidate-latency sample from
   *  which the acceptance threshold is derived below. This is NOT the
   *  final acceptance threshold. */
  candidateCeilingMs?: number;
  /** How far a visual event may appear to PRECEDE its state event and
   *  still count as a candidate -- an allowance for clock skew between the
   *  process that timestamps state events and the one whose encoder clock
   *  timestamps the recording, not a claim that a visual can precede its
   *  cause by more than this. */
  clockSkewAllowanceMs?: number;
  /** Percentile of the observed candidate-latency distribution used to
   *  derive the final acceptance threshold. */
  percentile?: number;
  /** Used only when fewer than `minSampleForDerivation` candidates exist
   *  to compute a distribution from. A stated placeholder, never silently
   *  substituted without being recorded in the returned ThresholdDerivation. */
  fallbackThresholdMs?: number;
  minSampleForDerivation?: number;
}

const DEFAULTS: Required<CorrelateOptions> = {
  candidateCeilingMs: 5000,
  clockSkewAllowanceMs: 200,
  percentile: 0.95,
  fallbackThresholdMs: 1000,
  minSampleForDerivation: 5,
};

function percentileOf(sortedAsc: number[], p: number): number {
  if (sortedAsc.length === 0) return 0;
  const idx = Math.min(sortedAsc.length - 1, Math.max(0, Math.ceil(p * sortedAsc.length) - 1));
  return sortedAsc[idx]!;
}

interface GreedyResult {
  matches: CorrelationMatch[];
  unmatchedState: StateEvent[];
  unmatchedVisual: VisualEvent[];
}

/** Greedy nearest-latency one-to-one matching within
 *  `[-clockSkewAllowanceMs, windowMs]`. Deterministic: state events are
 *  walked earliest-first, so an ambiguous shared visual event goes to
 *  whichever state event actually precedes it. */
function greedyMatch(
  stateEvents: StateEvent[],
  visualEvents: VisualEvent[],
  windowMs: number,
  skewMs: number,
): GreedyResult {
  const usedVisual = new Set<number>();
  const matches: CorrelationMatch[] = [];
  const unmatchedState: StateEvent[] = [];

  const orderedState = [...stateEvents].sort((a, b) => a.receivedAtMs - b.receivedAtMs);

  for (const state of orderedState) {
    let bestIdx = -1;
    let bestAbsLatency = Infinity;
    for (let i = 0; i < visualEvents.length; i++) {
      if (usedVisual.has(i)) continue;
      const visual = visualEvents[i]!;
      const latency = visual.atMs - state.receivedAtMs;
      if (latency < -skewMs || latency > windowMs) continue;
      const absLatency = Math.abs(latency);
      if (absLatency < bestAbsLatency) {
        bestAbsLatency = absLatency;
        bestIdx = i;
      }
    }
    if (bestIdx === -1) {
      unmatchedState.push(state);
      continue;
    }
    usedVisual.add(bestIdx);
    const visual = visualEvents[bestIdx]!;
    matches.push({ category: "matched", state, visual, latencyMs: visual.atMs - state.receivedAtMs });
  }

  const unmatchedVisual = visualEvents.filter((_, i) => !usedVisual.has(i));
  return { matches, unmatchedState, unmatchedVisual };
}

/**
 * Correlates a state-event stream (Orion LSDP identity, observed live)
 * against a visual-event stream (real recorded PGM, ffprobe/ffmpeg) by
 * time. Two passes:
 *
 *  1. A generous-ceiling match gathers a sample of candidate latencies.
 *  2. The acceptance threshold is DERIVED from that sample's distribution
 *     (a percentile), not decreed as a constant -- unless the sample is
 *     too small, in which case a documented fallback is used and flagged
 *     as such in the returned `ThresholdDerivation`.
 *
 * Records fall into exactly three categories -- `matched`,
 * `state_without_visual`, `visual_without_state` -- and the latter two are
 * expected outcomes (see types.ts), never counted or reported as errors.
 */
export function correlate(
  stateEvents: StateEvent[],
  visualEvents: VisualEvent[],
  options: CorrelateOptions = {},
): { records: CorrelationRecord[]; threshold: ThresholdDerivation } {
  const opts = { ...DEFAULTS, ...options };

  const pass1 = greedyMatch(stateEvents, visualEvents, opts.candidateCeilingMs, opts.clockSkewAllowanceMs);
  const candidateLatencies = pass1.matches.map((m) => m.latencyMs).sort((a, b) => a - b);

  let derivedThresholdMs: number;
  let fallbackUsed = false;
  let fallbackReasonIfUsed: string | undefined;
  let distributionMs: ThresholdDerivation["distributionMs"] = null;

  if (candidateLatencies.length >= opts.minSampleForDerivation) {
    distributionMs = {
      min: candidateLatencies[0]!,
      p50: percentileOf(candidateLatencies, 0.5),
      p90: percentileOf(candidateLatencies, 0.9),
      p95: percentileOf(candidateLatencies, 0.95),
      max: candidateLatencies[candidateLatencies.length - 1]!,
    };
    derivedThresholdMs = percentileOf(candidateLatencies, opts.percentile);
  } else {
    fallbackUsed = true;
    fallbackReasonIfUsed =
      `Only ${candidateLatencies.length} candidate pair(s) observed within the ` +
      `${opts.candidateCeilingMs}ms ceiling, below the minimum of ${opts.minSampleForDerivation} ` +
      `required to derive a threshold from a measured distribution. Falling back to the ` +
      `documented default of ${opts.fallbackThresholdMs}ms -- this is a stated placeholder, not ` +
      `a measurement, and must be re-derived once a session with more state/visual events is ` +
      `available.`;
    derivedThresholdMs = opts.fallbackThresholdMs;
  }

  const pass2 = greedyMatch(stateEvents, visualEvents, derivedThresholdMs, opts.clockSkewAllowanceMs);

  const records: CorrelationRecord[] = [
    ...pass2.matches,
    ...pass2.unmatchedState.map(
      (state) =>
        ({
          category: "state_without_visual" as const,
          state,
          reason:
            `No visual change detected within the derived threshold of ${derivedThresholdMs}ms ` +
            `after this state event. Expected when the delta/snapshot did not change anything ` +
            `rendered (identical value, an off-screen leaf, a non-visual leaf) -- not necessarily ` +
            `a correlation failure.`,
        }) satisfies CorrelationRecord,
    ),
    ...pass2.unmatchedVisual.map(
      (visual) =>
        ({
          category: "visual_without_state" as const,
          visual,
          reason:
            `No state event within the derived threshold of ${derivedThresholdMs}ms before this ` +
            `visual change. Expected for a running animation, a playing video source, or a ` +
            `scene/output transition that is not itself an LSDP delta -- not necessarily a ` +
            `correlation failure.`,
        }) satisfies CorrelationRecord,
    ),
  ];

  const threshold: ThresholdDerivation = {
    method: fallbackUsed
      ? "fallback (insufficient sample)"
      : `${Math.round(opts.percentile * 100)}th percentile of observed candidate-pair latency`,
    sampleSize: candidateLatencies.length,
    ...(fallbackUsed ? {} : { percentileUsed: opts.percentile }),
    distributionMs,
    derivedThresholdMs,
    fallbackUsed,
    ...(fallbackReasonIfUsed ? { fallbackReasonIfUsed } : {}),
  };

  return { records, threshold };
}
