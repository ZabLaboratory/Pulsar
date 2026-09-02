# Pulsar #253 patch-stack tuning decision

The continuation stack is intentionally split into three uniquely numbered patches:

1. `0026-fix-libobs-borrowed-video-mailbox.patch` owns the latest-frame mailbox and
   its stop/join lifetime boundary.
2. `0027-fix-win-dshow-lease-watcher.patch` owns DirectShow lease observation and
   its lifecycle fallback.
3. `0028-fix-libobs-pipeline-stats-atomic-after-mailbox.patch` owns atomic producer
   updates and lock-free snapshots, including the mailbox counters.

The atomic patch is rebased after the mailbox because both changes touch `obs.c` and
`obs-video.c`; applying the former standalone `0026` atomic candidate would leave
duplicate sequence numbers and a conflict. The pinned upstream remains
`bd73b922891e56839b0bc86bdc519802802f9d68`; the nested browser patch is replayed
separately on its recorded submodule pin.

The lease watcher remains fail-closed: a missing watcher or invalid observation does
not publish a gated return. Any future lease-cache optimization must prove detach,
reconnect, stop and invalid-handle transitions before changing this ordering.
