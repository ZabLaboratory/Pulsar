# Translation-only group composition probe

The retained-tree geometry path passed an isolated synthetic four-owner,
eight-capture run at 1920x1080. It exercised five translation-only commits,
one same-object capture color setting change followed by another drag, a
structural internal group filter change, and reset. The probe did not start
Live or REC and did not touch the shared Prism development runtime.

The native plugin used by this run was built from the modified source in this
worktree with `PULSAR_GROUP_COMPOSITION_RESET_TRACE`. Its private host runtime
was staged from the previously qualified OBS runtime recorded in the JSON
provenance. That runtime used build-source commit
`70a58302435308e609e0b1114efe3ef402f810b7` with local changes in
`plugins/obs-browser` and an untracked `plugins/pulsar-nv-secure-load/`; the
separate ABI/preflight checkout was clean at
`6328ff8b3c29f10f29b7e1fad75f170efc41e198`. This is the focused geometry
result, not the later full distribution build. The complete machine-readable
receipt, including the redacted host log lines, is
[`20261018T152955Z-reset-exact.json`](20261018T152955Z-reset-exact.json).

For the five geometry-only commits (generations 2–6), `geometryUpdateMs` was
0.138–0.242 ms, the graphics callback was 11.919–14.981 ms, `buildGroupMs`
was 0, and no capture filters or shaders were created. WebSocket round trips
were 14.457–32.487 ms. The four repeated translation commits observed 1, 1,
2 and 1 skipped render frames in their short statistics windows; the adjacent
roughly half-second idle controls observed 5–12 skips. The probe applies its
idle-relative bound. The counters remain noisy on this synthetic host, so this
result does not claim zero skipped frames.

The final translated output matched the full-rebuild pixel baseline exactly:
1920x1080 RGBA pixel SHA-256
`6fb634df26056911d2e4eb88375046ba79b1ff50bfdcb20ac8efcc774d42844d`. The
retained OBS scene model hash remained
`1e70f5533f93587e03af03333a7c907f711d3dcd6066d3e48d31d78d0e6340d7` before
and after translations. The full-resolution final image is
[`20261018T152955Z-translation-final-1920x1080.png`](20261018T152955Z-translation-final-1920x1080.png).

Changing a setting on the same retained capture object changed its rendered
pixels, and the following translation still used the fast path and preserved
the setting. Changing an internal group opacity filter instead declined the
retained path with prevalidation code 4 and completed the full rebuild: the
response was successful at generation 8 with its requested `planHash`,
`buildGroupMs` was 245.915 ms, and 12 filter/shader creates were observed. Reset
then restored the nominal pixels. Changed geometry produces a changed
`planHash`; each response preserves the request's generation and hash fields.

The initial candidate build in this run took 224.548 ms in `buildGroupMs` and
created 12 filters/shaders. These readings are synchronous native timings;
they exclude WebSocket travel unless the field is explicitly the round trip.
The workload and GPU scene are synthetic, so this evidence does not qualify
physical capture devices or either active broadcast lane.
