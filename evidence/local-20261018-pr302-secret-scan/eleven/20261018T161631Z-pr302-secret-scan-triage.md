# PR #302 detect-secrets triage

## Scope

This audit covers the exact Pulsar PR checkout that failed the `detect-secrets audit against baseline` compliance step. The CI run's TruffleHog verified-secret scan passed. The local check used detect-secrets 1.5.0 with the same tracked-file scan mode as the workflow.

## Findings

The unmodified baseline contained 22 records. The exact scan added 6,773 findings across 276 files: 6,771 `Hex High Entropy String` findings and two `Base64 High Entropy String` findings. No existing finding disappeared, and no other detector type was added.

Each of the 6,771 hex findings was matched back to its exact source-line token by comparing the detector's stored SHA-1 fingerprint, without printing or persisting the token. All matched values consist only of hexadecimal characters: 6,150 are 64 characters, 601 are 40 characters, and 20 are 32 characters. The 64-character values are SHA-256 content/provenance digests for source, runtime, binary, fixture, and composition-plan data. The 40-character values are Git commit/blob revisions. All 20 32-character values are `resetRequestId` correlation IDs generated as `uuid.uuid4().hex` by the reset probe.

The two Base64-plugin findings are 73-character `checkout` labels containing a 40-character Git source revision between fixed `upstream-source-` and `-windows-checkout` text. They are provenance labels, not credentials.

The 22 findings outside JSON fields were individually classified: one Markdown base-commit reference; two checkout labels; two CEF presenter/commit references; twelve CEF preflight digest constants; and five group-composition probe digest or revision constants. The remaining structured fields use digest/revision names such as `sha256`, `planHash`, `revision`, `head`, `base`, or file-path keys. No candidate was in a field named like a token, password, credential, authorization value, or API key.

## Baseline decision and validation

All 6,773 new findings were reviewed as non-secret provenance or correlation values and labeled `is_secret: false` in `.secrets.baseline`. The original 22 baseline records were preserved. No detector, threshold, workflow, or filter was changed.

Validation used the exact workflow command, `detect-secrets scan --baseline .secrets.baseline`, followed by the workflow's comparison of the `results` maps. The scan produced no new or missing result after the audited baseline was saved. The labels on all 6,773 added records remain `is_secret: false`.
