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
