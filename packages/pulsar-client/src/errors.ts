export interface PulsarPrismErrorEnvelope {
  schemaVersion: 1;
  code: string;
  message: string;
  severity: "warning" | "error";
  domain: "scene" | "broadcast" | "service" | "system" | "operator";
  source: string;
  context: Record<string, unknown>;
  details: Record<string, unknown>;
  requestId?: string;
}

/**
 * Thrown when a Pulsar vendor request returns a typed `error` field.
 * Distinguishes server-side validation failures from transport-level
 * obs-websocket failures (which still bubble up as the underlying lib's
 * exceptions).
 */
export class PulsarVendorError extends Error {
  readonly prism: PulsarPrismErrorEnvelope;

  constructor(
    public readonly requestType: string,
    public readonly serverMessage: string,
    envelope?: Partial<PulsarPrismErrorEnvelope>,
  ) {
    super(`pulsar:${requestType} rejected: ${serverMessage}`);
    this.name = "PulsarVendorError";
    this.prism = {
      schemaVersion: 1,
      code: envelope?.code ?? `PULSAR_${requestType.toUpperCase()}_REFUSED`,
      message: envelope?.message ?? serverMessage,
      severity: envelope?.severity ?? "warning",
      domain: envelope?.domain ?? "broadcast",
      source: envelope?.source ?? "pulsar.vendor",
      context: { action: requestType, ...(envelope?.context ?? {}) },
      details: envelope?.details ?? {},
      ...(envelope?.requestId ? { requestId: envelope.requestId } : {}),
    };
  }
}

/**
 * Thrown when the user calls a method requiring a connection while the
 * client is not connected.
 */
export class PulsarNotConnectedError extends Error {
  readonly prism: PulsarPrismErrorEnvelope;

  constructor() {
    super("PulsarClient is not connected; call connect() first");
    this.name = "PulsarNotConnectedError";
    this.prism = {
      schemaVersion: 1,
      code: "PULSAR_NOT_CONNECTED",
      message: "PulsarClient is not connected; call connect() first",
      severity: "error",
      domain: "service",
      source: "pulsar.client",
      context: { action: "client.call" },
      details: {},
    };
  }
}

export class PulsarRuntimeError extends Error {
  readonly prism: PulsarPrismErrorEnvelope;

  constructor(code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "PulsarRuntimeError";
    this.prism = {
      schemaVersion: 1,
      code,
      message,
      severity: "error",
      domain: "service",
      source: "pulsar.runtime",
      context: {},
      details,
    };
  }
}
