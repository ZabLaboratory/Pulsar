# Browser fail-closed lifetime extension

This scoped extension records the follow-up authorized by the root owner after
the signed native-composition checkpoint. It targets only the two
`browser_pre_shutdown_ready()` failure branches in
`plugins/pulsar-headless/main.cpp` and their focused shutdown tests/probe.

The observed defect is that both branches release `RuntimeState`'s kernel-backed
instance/runtime-directory/legacy-alias leases, close the shutdown event, and
return from `main()` after the browser CEF barrier has failed. Returning runs
C++/Qt/libobs/plugin teardown despite the code's fail-closed intent; a private
missing-browser-proc run demonstrated an access violation during that path.

The planned correction keeps the process fail-closed: emit and flush a
browser-specific failure marker, retain leases and shutdown handles until OS
process termination, skip frontend and libobs teardown, and use the existing
Windows `TerminateProcess` boundary with `_Exit(1)` fallback. Only those browser
failure branches are in scope; the frontend cleanup failure branch and browser
fence semantics are unchanged. The private no-browser fixture remains explicitly
test-only and does not demonstrate real CEF teardown.

The root's prior explicit authorization for this bounded lifecycle correction is
recorded in the parent task thread. The proposal was sent before editing
`main.cpp`; this file is the pre-edit scope record. Validation will distinguish
the synthetic no-browser fail-closed path from real browser-module shutdown.
