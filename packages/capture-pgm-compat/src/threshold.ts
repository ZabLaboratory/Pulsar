/**
 * Derives an acceptance threshold from the SEPARATION between an observed
 * "healthy" population and an observed "degraded" population on one axis,
 * rather than a bare constant. Mirrors pgm-correlator's correlator.ts
 * ThresholdDerivation posture: the derivation is data, not a decree, and an
 * overlap between the two populations is reported as a real result -- it
 * means this axis, on its own, does not discriminate for the sample given.
 *
 * Caller discipline that matters here: pass the degraded population that is
 * ACTUALLY expected to fail on this specific axis, not every degraded
 * scenario indiscriminately. A "frozen" sample is, by design, spatially
 * healthy (real detail, just static) -- pooling it into a spatial-axis
 * threshold's degraded population pulls that threshold toward the healthy
 * sample itself and can produce a false negative on the healthy case. Each
 * axis should be validated against the scenario(s) it exists to catch (see
 * capture-pgm-compat's frame-health.test.ts and live-capture-compat.test.ts
 * for the concrete split: spatial vs "black", temporal vs "black"+"frozen").
 */
export interface SeparationThreshold {
  method: "midpoint of observed healthy/degraded separation";
  healthyMin: number;
  healthyMax: number;
  degradedMin: number;
  degradedMax: number;
  /** True only when every degraded sample is strictly below every healthy
   *  sample (or the axis is inverted, see `direction`) -- i.e. a single
   *  threshold value actually separates the two populations with no
   *  ambiguous overlap region. */
  separated: boolean;
  /** "higher-is-healthy" (spatialStddev, temporalDiff) or
   *  "lower-is-healthy" -- not used by this package's axes today, kept
   *  explicit so a future caller can't silently misread the sign. */
  direction: "higher-is-healthy" | "lower-is-healthy";
  threshold: number;
}

export function deriveSeparationThreshold(
  healthyValues: number[],
  degradedValues: number[],
  direction: "higher-is-healthy" | "lower-is-healthy" = "higher-is-healthy",
): SeparationThreshold {
  if (healthyValues.length === 0 || degradedValues.length === 0) {
    throw new Error(
      `deriveSeparationThreshold needs at least one sample per population ` +
        `(got healthy=${healthyValues.length}, degraded=${degradedValues.length})`,
    );
  }
  const healthyMin = Math.min(...healthyValues);
  const healthyMax = Math.max(...healthyValues);
  const degradedMin = Math.min(...degradedValues);
  const degradedMax = Math.max(...degradedValues);

  const separated =
    direction === "higher-is-healthy" ? degradedMax < healthyMin : degradedMin > healthyMax;

  const threshold =
    direction === "higher-is-healthy" ? (degradedMax + healthyMin) / 2 : (degradedMin + healthyMax) / 2;

  return {
    method: "midpoint of observed healthy/degraded separation",
    healthyMin,
    healthyMax,
    degradedMin,
    degradedMax,
    separated,
    direction,
    threshold,
  };
}

export function passesThreshold(value: number, t: SeparationThreshold): boolean {
  return t.direction === "higher-is-healthy" ? value >= t.threshold : value <= t.threshold;
}

export interface MaterialSeparationOptions {
  /** Minimum required healthyValue / (degradedMax floored by epsilon)
   *  ratio. Default 10 -- one order of magnitude. Comfortably below every
   *  margin actually observed against a real Pulsar/CEF recording
   *  (spatial: black ~0 vs healthy ~48; temporal: ~4386x, healthy ~4.386
   *  vs frozen ~0.001 -- see capture-pgm-compat's PR #233 review), so it
   *  won't flag a healthy CEF encode's normal noise as a failure, while
   *  still catching the case a bare `separated` boolean or a
   *  single-sample `deriveSeparationThreshold` midpoint cannot: with one
   *  sample per population, that midpoint sits between them BY
   *  CONSTRUCTION, so `passesThreshold(healthyValue, ...)` is
   *  tautologically true regardless of the healthy sample's absolute
   *  magnitude -- it would stay green even if the healthy source itself
   *  degraded (e.g. temporalDiffAvg collapsing from 4.386 to 0.002), as
   *  long as it stayed marginally above the (equally collapsing)
   *  threshold. */
  minRatio?: number;
  /** Floor added under the degraded population's max before dividing, so
   *  a degraded value of exactly 0 (observed for a real solid-black
   *  recording -- CEF's flat output compresses losslessly with x264 at
   *  this bitrate, no residual noise) doesn't turn the ratio into a
   *  division that trivially "passes" independent of whether
   *  healthyValue is itself materially non-zero. 0.05 sits comfortably
   *  above the near-zero real values observed for "black" (0.000) and
   *  well below "frozen"'s real temporal value (0.001 is BELOW this --
   *  intentional, frozen must still fail the temporal check) and every
   *  healthy value observed (~4.386 temporal, ~48-81 spatial). */
  epsilon?: number;
}

export interface MaterialSeparationResult {
  healthyValue: number;
  degradedMax: number;
  ratio: number;
  minRatio: number;
  /** true only when healthyValue is at least minRatio times the
   *  (epsilon-floored) degraded population's max -- an absolute,
   *  data-independent bar, not a threshold derived from (and therefore
   *  tautologically satisfied by) the very samples being judged. */
  material: boolean;
}

export function checkMaterialSeparation(
  healthyValue: number,
  degradedValues: number[],
  options: MaterialSeparationOptions = {},
): MaterialSeparationResult {
  if (degradedValues.length === 0) {
    throw new Error("checkMaterialSeparation needs at least one degraded sample");
  }
  const minRatio = options.minRatio ?? 10;
  const epsilon = options.epsilon ?? 0.05;
  const degradedMax = Math.max(...degradedValues, epsilon);
  const ratio = healthyValue / degradedMax;
  return { healthyValue, degradedMax, ratio, minRatio, material: ratio >= minRatio };
}
