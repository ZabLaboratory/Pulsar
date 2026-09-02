# Pulsar #253 DirectShow lease fastpath decision

The first #253 increment repairs pipeline telemetry only.  The DirectShow
return path still calls `OpenEventW` and closes the observation handle for each
frame in `return_consumer_is_active`.

## Safety decision

No positive lease-handle cache is introduced here.  Keeping a producer-owned
handle open would keep the named event alive after the DirectShow consumer
detaches, so handle validity would no longer prove that a consumer is active.
That stale-positive state would violate the return path's fail-closed rule and
would hide detach/reconnect transitions.  A negative-only cache would preserve
fail-closed behavior but would not remove the per-frame syscall while a
consumer is active, and is therefore deferred rather than presented as a
fastpath.

The existing behavior remains the safe baseline: an ungated output publishes,
and a gated return publishes only after a fresh lease observation.  Any future
optimization must add an explicit lifecycle/heartbeat protocol (including
stop, invalid-handle, detach and reconnect transitions), prove that no stale
positive observation survives the lease lifetime, and compare cache-hit,
reopen and fallback counters against the current x264/NVENC baseline.
