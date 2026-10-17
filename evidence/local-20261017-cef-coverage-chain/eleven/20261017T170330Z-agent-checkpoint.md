# CEF full-resolution qualification checkpoint

Owner: Eleven, root thread 01a11a8b-8f4f-7fc2-abc4-7983042e4f63.
Continuation worktree: D:/Documents/Zab/Pulsar/.worktrees/eleven-local-20261017-cef-coverage-chain.
Branch: eleven/local-20261017-cef-coverage-chain. Base: e6b5a4762c3094eef900dabdd56bd2c4308148ac.
Root resumed the retained harness after the prior agent became unavailable; no other unit files changed.

The profile authors LSML at 1920x1080 before loading Vision. The 20261017T165509Z run passed using a real 1922x3246 CEF atlas, one unchanged scene/version, opacity state patch 0.5 to 1, malformed-mask non-mutation, exact historical-presenter rejection and graceful CEF shutdown (host exit 0). Independent pixels are recorded in 20261017T165800Z-independent-1080-pixels.json. The 20261017T170300Z provenance companion identifies the actual dirty native build checkout separately from the clean repository ABI reference, and retains a redacted host log.

Validation: node --check page.mjs; Python AST parsing of the harness; offline 1080 profile preflight 20261017T170309Z; git diff --check. After the runtime run only documentation and preflight qualification wording were clarified; runtime code was unchanged.

Limits: not sustained 60-fps pacing, not real devices, not full Orion/LSDP, not installed runtime. The separately developed native deadlock correction was not in this CEF run. Existing signed 320x180 evidence is unchanged. No Live/REC or transition behavior was modified.
