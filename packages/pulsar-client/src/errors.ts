/**
 * Thrown when a Pulsar vendor request returns a typed `error` field.
 * Distinguishes server-side validation failures from transport-level
 * obs-websocket failures (which still bubble up as the underlying lib's
 * exceptions).
 */
export class PulsarVendorError extends Error {
  constructor(
    public readonly requestType: string,
    public readonly serverMessage: string,
  ) {
    super(`pulsar:${requestType} rejected: ${serverMessage}`);
    this.name = "PulsarVendorError";
  }
}

/**
 * Thrown when the user calls a method requiring a connection while the
 * client is not connected.
 */
export class PulsarNotConnectedError extends Error {
  constructor() {
    super("PulsarClient is not connected; call connect() first");
    this.name = "PulsarNotConnectedError";
  }
}
