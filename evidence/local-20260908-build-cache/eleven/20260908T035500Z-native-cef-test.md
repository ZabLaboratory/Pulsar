# Native CEF validation — raw Vitest report

Runtime: artifact from pipeline 34184658322, native candidate 7c93dfecf334fb251c409526e555aa1c6986fd70.
Local physical GPU test, PULSAR_LIVE_CAPTURE_COMPAT=1, x264 recordings, no Twitch broadcast.
11 passed, 0 failed, 0 skipped. The original machine-readable report is preserved below.

```json
{
  "numTotalTestSuites": 6,
  "numPassedTestSuites": 6,
  "numFailedTestSuites": 0,
  "numPendingTestSuites": 0,
  "numTotalTests": 11,
  "numPassedTests": 11,
  "numFailedTests": 0,
  "numPendingTests": 0,
  "numTodoTests": 0,
  "snapshot": {
    "added": 0,
    "failure": false,
    "filesAdded": 0,
    "filesRemoved": 0,
    "filesRemovedList": [],
    "filesUnmatched": 0,
    "filesUpdated": 0,
    "matched": 0,
    "total": 0,
    "unchecked": 0,
    "uncheckedKeysByFile": [],
    "unmatched": 0,
    "updated": 0,
    "didUpdate": false
  },
  "startTime": 1788839760253,
  "success": true,
  "testResults": [
    {
      "assertionResults": [
        {
          "ancestorTitles": [
            "measureFrameHealth -- real ffmpeg fixtures for the three named scenarios"
          ],
          "fullName": "measureFrameHealth -- real ffmpeg fixtures for the three named scenarios scores the animated fixture with non-trivial spatial AND temporal variance",
          "status": "passed",
          "title": "scores the animated fixture with non-trivial spatial AND temporal variance",
          "duration": 199.3364999999999,
          "failureMessages": [],
          "meta": {}
        },
        {
          "ancestorTitles": [
            "measureFrameHealth -- real ffmpeg fixtures for the three named scenarios"
          ],
          "fullName": "measureFrameHealth -- real ffmpeg fixtures for the three named scenarios scores the black fixture as flat on every axis (spatial, luma, temporal)",
          "status": "passed",
          "title": "scores the black fixture as flat on every axis (spatial, luma, temporal)",
          "duration": 85.42800000000034,
          "failureMessages": [],
          "meta": {}
        },
        {
          "ancestorTitles": [
            "measureFrameHealth -- real ffmpeg fixtures for the three named scenarios"
          ],
          "fullName": "measureFrameHealth -- real ffmpeg fixtures for the three named scenarios scores the static-but-detailed fixture as spatially healthy but temporally dead -- the case a spatial-only oracle would miss",
          "status": "passed",
          "title": "scores the static-but-detailed fixture as spatially healthy but temporally dead -- the case a spatial-only oracle would miss",
          "duration": 187.70279999999957,
          "failureMessages": [],
          "meta": {}
        },
        {
          "ancestorTitles": [
            "measureFrameHealth -- real ffmpeg fixtures for the three named scenarios"
          ],
          "fullName": "measureFrameHealth -- real ffmpeg fixtures for the three named scenarios a combined spatial+temporal oracle accepts the animated fixture and rejects BOTH degraded fixtures, from thresholds derived from the actual observed separation",
          "status": "passed",
          "title": "a combined spatial+temporal oracle accepts the animated fixture and rejects BOTH degraded fixtures, from thresholds derived from the actual observed separation",
          "duration": 352.4586999999997,
          "failureMessages": [],
          "meta": {}
        },
        {
          "ancestorTitles": [
            "deriveSeparationThreshold"
          ],
          "fullName": "deriveSeparationThreshold reports separated=false, not a forced threshold, when populations overlap",
          "status": "passed",
          "title": "reports separated=false, not a forced threshold, when populations overlap",
          "duration": 0.24969999999984793,
          "failureMessages": [],
          "meta": {}
        },
        {
          "ancestorTitles": [
            "deriveSeparationThreshold"
          ],
          "fullName": "deriveSeparationThreshold throws rather than silently deriving from an empty population",
          "status": "passed",
          "title": "throws rather than silently deriving from an empty population",
          "duration": 0.8501999999998588,
          "failureMessages": [],
          "meta": {}
        },
        {
          "ancestorTitles": [
            "checkMaterialSeparation"
          ],
          "fullName": "checkMaterialSeparation catches the exact tautology deriveSeparationThreshold's single-sample midpoint cannot: passesThreshold(healthy) is guaranteed true no matter how degraded 'healthy' itself is",
          "status": "passed",
          "title": "catches the exact tautology deriveSeparationThreshold's single-sample midpoint cannot: passesThreshold(healthy) is guaranteed true no matter how degraded 'healthy' itself is",
          "duration": 0.29480000000012296,
          "failureMessages": [],
          "meta": {}
        },
        {
          "ancestorTitles": [
            "checkMaterialSeparation"
          ],
          "fullName": "checkMaterialSeparation passes when the healthy sample is genuinely an order of magnitude above the degraded population",
          "status": "passed",
          "title": "passes when the healthy sample is genuinely an order of magnitude above the degraded population",
          "duration": 0.13719999999966603,
          "failureMessages": [],
          "meta": {}
        },
        {
          "ancestorTitles": [
            "checkMaterialSeparation"
          ],
          "fullName": "checkMaterialSeparation floors the degraded population at epsilon so a degraded value of exactly 0 doesn't trivially pass on a near-zero healthy value",
          "status": "passed",
          "title": "floors the degraded population at epsilon so a degraded value of exactly 0 doesn't trivially pass on a near-zero healthy value",
          "duration": 0.14949999999998909,
          "failureMessages": [],
          "meta": {}
        },
        {
          "ancestorTitles": [
            "checkMaterialSeparation"
          ],
          "fullName": "checkMaterialSeparation throws rather than silently deriving from an empty degraded population",
          "status": "passed",
          "title": "throws rather than silently deriving from an empty degraded population",
          "duration": 0.19039999999995416,
          "failureMessages": [],
          "meta": {}
        }
      ],
      "startTime": 1788839762391,
      "endTime": 1788839763220.295,
      "status": "passed",
      "message": "",
      "name": "D:/Documents/Zab/Pulsar/.worktrees/eleven-local-20260908-build-cache/packages/capture-pgm-compat/tests/frame-health.test.ts"
    },
    {
      "assertionResults": [
        {
          "ancestorTitles": [
            "real Pulsar/CEF capture <-> PGM compatibility (opt-in, PULSAR_LIVE_CAPTURE_COMPAT=1)"
          ],
          "fullName": "real Pulsar/CEF capture <-> PGM compatibility (opt-in, PULSAR_LIVE_CAPTURE_COMPAT=1) records healthy/black/frozen for real, measures them, and cross-checks pgm-extractor's visual-presence verdict",
          "status": "passed",
          "title": "records healthy/black/frozen for real, measures them, and cross-checks pgm-extractor's visual-presence verdict",
          "duration": 17645.856200000002,
          "failureMessages": [],
          "meta": {}
        }
      ],
      "startTime": 1788839762349,
      "endTime": 1788839779994.8562,
      "status": "passed",
      "message": "",
      "name": "D:/Documents/Zab/Pulsar/.worktrees/eleven-local-20260908-build-cache/packages/capture-pgm-compat/tests/live-capture-compat.test.ts"
    }
  ]
}
```
