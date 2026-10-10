# Continuous Preview playback correction

Authority: user reports the overlay freezing during scene switches. Continue the existing transition branch, no ADR, Computer Use, default asset or live/recording. Preserve the scene-sync checkout and active host, which currently includes our existing Begin/End IPC. Native Begin/End wire shape and covered acknowledgment remain compatible.

Cause observed in source and prior native/renderer traces: the media is intentionally paused at 875 ms while Orion and native capture reconciliation run; this adds a visible stop in an authored animation. Native pause is not a stalled DirectShow queue.

Use one bounded GPU capture of the outgoing composite as the preparation cover. Keep the real composite hot behind it. Once the host's existing End(false) signals incoming readiness, start the selected media once at a frame boundary, reveal the live composite at the configured cut, and complete without pausing the media. End(true), decode error and timeout still restore the real Preview.

| Criterion | Risk | Validation | Evidence |
|---|---|---|---|
| Overlay video plays continuously | Accidental decoded-frame hold | Native clock/pause state and live sampled video over the cut | Isolated synthetic probe and real Preview trace |
| Scene preparation finishes before animation starts | Early incoming decor exposure | Delay preparation and change base while captured | Outgoing pixels retained until cut, incoming pixels after |
| Real source stays hot | Freeze source deactivates camera/CEF | Active source/tree and replay/cleanup | Native repeated preparation/playback |
| Program/Take unaffected | View or audio contamination | Existing contracts and Program source identity | Native checks plus actual idle host |
| No leaked texture/lease | Abort or timeout retains owned frame | Abort before playback, abort in motion, repeat | Terminal frame IDs and busy=false |

Reuse the existing OBS texrender/effect render path for a private frozen-frame source; do not add files/assets to Prism or replace the renderer's real native return. Snapshot is not media stored on disk. Only the frontend owns this source, and it is released with the transition.
