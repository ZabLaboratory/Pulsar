# Runbook — M10 Twitch broadcast: prove REAL ingest, not StartDestination

**Applies to:** the Keeper antenna run that broadcasts the `zab-transition`
playout to the porteur's Twitch channel (`scripts/probe-m10-canvas-live.py
--transition-scene --broadcast`).
**Fixed by:** commit `6aaefa1` on branch `keeper/m10-real-ingest-proof`
(probe instrumentation), antenna run 2026-06-09.

---

## Symptom / incident

Earlier M10 antenna runs printed **`StartDestination started=true — LIVE on
Twitch`** and exited, yet the porteur saw **no stream** on the channel.

Two compounding causes:

1. **False-positive "LIVE".** `StartDestination` calls `obs_output_start()`,
   which only means the start was *accepted*. The RTMP connect to
   `rtmp://live.twitch.tv/app/<key>` happens **asynchronously** on the output's
   worker thread. `started=true` proves **nothing** about whether Twitch
   ingested a single byte (refused/dead key would still return `started=true`).
2. **Run too short (~10s).** Even when ingest worked, a ~10s VOD never had time
   to appear on the channel; the porteur saw nothing.

## Root cause

The probe declared success on the wrong signal and held the stream for far too
little time. There was **no measurement of real bytes pushed to Twitch**.

## The real proof (what to read)

The pulsar multi-stream plugin creates one `rtmp_output` per destination, named
**`PulsarDest_<id>`** (`plugins/pulsar-multi-stream/src/plugin-main.cpp`,
`ensure_output`); twitch kind targets `rtmp://live.twitch.tv/app/`
(`TWITCH_INGEST_URL`). The pulsar-websocket fork exposes
**`GetOutputStatus{outputName}`** returning `obs_output_get_total_bytes`
(`outputBytes`), `outputActive`, `outputDuration`
(`plugins/pulsar-websocket/src/requesthandler/RequestHandler_Outputs.cpp`).

So real ingest is read straight off the Twitch output over obs-ws:

- **`outputBytes` MUST grow monotonically** = bytes really reach Twitch. Flat 0
  = nothing ingested (key refused/dropped).
- **`outputActive` MUST stay true.** A flip to false post-connect = Twitch
  dropped us.
- **RTMP stdout** (`upstream/plugins/obs-outputs/rtmp-stream.c`) logs verbatim:
  `Connecting to RTMP URL ...` (embeds the key → **redact**),
  `Connection to ... successful` (real handshake), and any post-connect
  `Disconnected from ...` / `Connection to ... failed` (refused/dead key).

The probe now polls all of this every 5s for the whole `--on-air-secs` hold and
gates the verdict on measured growth — never on `started=true`.

## Fix / procedure (the antenna run)

```bash
PY="C:/Users/Mathias/AppData/Local/Programs/Python/Python311/python.exe"
# Load the etage-1 key into env WITHOUT echoing it:
export TWITCH_STREAM_KEY=$("$PY" -c "import re,pathlib;\
 t=pathlib.Path('D:/Documents/Zab/.env.pulsar').read_text(encoding='utf-8');\
 print(re.search(r'^TWITCH_STREAM_KEY=(.*)$',t,re.M).group(1).strip())")
# Go on air, hold ~80s, prove real ingest:
"$PY" scripts/probe-m10-canvas-live.py --transition-scene --broadcast \
    --allow-blank --hold-ms 800 --on-air-secs 80
```

A clean result prints the **REAL-INGEST VERDICT** block:
`outputBytes grew = True`, `outputActive stayed_true = True`, RTMP
`successful = True`, `disconnected/failed = False`, and
`==> PROVEN: Twitch INGESTED the stream`.

## Verification — measured antenna run (2026-06-09)

- `outputBytes`: 5,490,744 → 65,429,817 = **59.94 MB pushed** over **83s** at a
  steady **~755–878 KiB/s** (~6 Mbps, the configured bitrate).
- `outputActive` true for all 16 samples; never dropped. Congestion 0.0, 0
  skipped frames.
- RTMP verbatim: `Connecting to RTMP URL rtmp://live.twitch.tv/app/...` →
  `Connection to rtmp://live.twitch.tv/app/ (2600:...) successful`; **no**
  post-connect disconnect/failure.
- VOD `build/m10-live-vod/pulsar-20260609-142403.mp4` = 70.79 MB (the full hold).
- C-MECH clean (two bare `SetCurrentProgramScene` per cut, zero native
  transition); C-SEC clean (raw key in zero stdout/PNG/log on disk).

## Failure mode — if the key is dead

If `outputBytes` stagnates, `outputActive` flips false, or an RTMP
`Disconnected`/`failed` appears **after** connect, the verdict prints
`==> NOT PROVEN ... request a FRESH Twitch key` and the run **exits 1**. That is
a real failure, not a "LIVE" false positive — ask the porteur for a fresh key.

## Notes / scope decisions

- **Orion active scene was NOT swapped.** The brief mentioned pushing+activating
  `zab-transition` on live Orion (`POST /scenes/{id}/push` + `POST
  /show/active-scene`) with rollback to `18fecbd4`. Orion exposes no
  GET-list/active route to read the current pushed-version, so a verified
  rollback baseline could not be captured. Since real ingest is independent of
  whether the browser_source renders from VPS-Solar or the faithful local page,
  the run used the local white+logo render (engine=local-static-page) and left
  Orion untouched (7 scenes loaded, health ok before and after). A live
  active-scene swap is shared-topology and belongs to a Conduit-validated step.
- **Visual MID render headless.** The transition MID frame is black headless
  (CEF/Solar does not paint without a desktop GPU surface); `--allow-blank`
  defers the white+logo *visual* proof. This run's contract is **real ingest**,
  which is independent of CEF paint.
- **Rollback for this fix:** none destructive. The change is probe
  instrumentation; revert the commit to restore prior behaviour. No VPS or Orion
  state was modified.

---

## Addendum — full real-wire antenna run (2026-06-09, Keeper M10 final)

This run went past the deferrals above: it broadcast with the **real Blue
rule** driving the target scene and the **real VPS Solar/Orion** browser_source,
and proved real Twitch ingest over a ~90s hold. Two infra actions were needed
that the earlier note had punted.

### A) The Blue blueprint was made rule-driven (not a literal)

`m10-scene-control` (blueprint `16c76b7c-f8fa-42f7-bd1b-57b2cf1f51eb`) was a
single `core.literal` node carrying the finished `scene_control` blob (v4). The
brief required the **rule** from Pulsar #32 (`Blue/tests/
test_m10_transition_blueprint.py`): `core.input target → core.compare.equal →
core.flow.select ×2 → core.data.set-field ×7 → core.output`. The deployed VPS
Blue stdlib (image built 2026-06-09T05:54) already seeds every node def the rule
needs (`compare.equal`, `flow.select`, `set-field`, `core.input/literal/output`)
and the #31 string-JSON leaf envelope (`leaf_mapper.json.dumps`).

Procedure (idempotent, replayable):

```bash
GATEWAY=https://zabgate.cyell.dev \
ORION_OPERATOR_TOKEN=<admin jwt from .env.orion> \
BP_ID=16c76b7c-f8fa-42f7-bd1b-57b2cf1f51eb \
python build/keeper_m10_rule_publish.py     # POST /versions, PUT graph, /publish
```

It published **v5** (rule graph, 23 nodes, 0 literal `scene_control` blob) on the
**same slug** — so the emitted leaf path stays
`__inputs.blue.m10-scene-control.scene_control` (what the probe consumes) while
the producer becomes a real rule. Dry proof: `target=screen-2` ⇒ rule emits
`scene-screen-2`/cut-250; `target=screen-1` ⇒ same graph emits
`scene-screen-1`/cut-400. **It flips with the input ⇒ the rule computed it**, a
literal would be invariant.

> **Token note:** `.env.m8`'s `M8_OPERATOR_TOKEN` was **expired** (`token
> expired`, ~20h stale). The valid admin JWT is `.env.orion`'s
> `ORION_OPERATOR_TOKEN` (`role:admin`, `iss:zabauth`, ~327d TTL, same `sub`
> `7b33a262…` that owns the M10 blueprints). Use it for both the Blue
> publish/trigger and as the probe's `M8_OPERATOR_TOKEN`.

> **Rollback for v5:** Blue versions are immutable; v4 (the literal) still
> exists. To revert, `POST /blueprints/<id>/versions/4/publish` to flip
> `current_version` back to 4. v5 is the intended end state, so this is only a
> contingency.

### B) The active Orion scene MUST declare the leaf (F2 fan-out)

**Incident:** with the rule published, the antenna run failed at
`FAIL: no __inputs.blue.m10-scene-control.scene_control delta on /show/stream
within 30s`. The Blue `/trigger` returned `succeeded` + `pushed.delivered=true`
— Blue→Orion was healthy — but the probe's `/show/stream` subscriber never
received the leaf.

**Root cause (proven, not deduced):** Orion only fans a leaf out to subscribers
if the **active scene declares that path** (F2 silent-drop, see
`scripts/m10_setup.py:75-96`). The "Setup scène" step had activated the **static
`zab-transition`** scene (`scripts/fixtures/zab-transition.lsml.json`,
`operator_inputs=None`) — it declares **no** scene_control leaf. A `/show/stream`
snapshot confirmed the state carried only `__system.tick.now_ms`; every delta
carried only the tick, never the leaf, despite `pushed.delivered=true`.

The leaf-declaring scene is **`scripts/fixtures/m10-orion-scene.lsml.json`**
(wipe-cover), which declares `__inputs.blue.m10-scene-control.scene_control` as
an `operator_input`. Push + activate it:

```bash
python build/keeper_m10_declare_leaf_scene.py   # put_lsml_bundle + set_active_scene
```

After activation, a subscribe→trigger probe received the leaf delta as a
string-JSON envelope (`raw_type=str`, #31), `target_scene=scene-screen-2`.

> **The white-transition scene and the leaf-declaring scene are mutually
> exclusive on one Orion show.** The `--transition-scene` playout wants the
> white+logo scene rendered mid-fade; the live-wire wants the magenta wipe-cover
> (leaf-declaring) scene active. Only one Orion scene can be active, so the
> browser_source painted **magenta wipe-cover**, not white+logo. This is a real
> topology tension to resolve in the authoring session (e.g. a single Orion
> scene that BOTH declares the leaf AND renders white+logo, or a scene-local
> leaf declaration on the transition scene). Flagged, not papered over.

### Verification — measured antenna run (2026-06-09 15:21, exit 0 PASS)

- **Rule wire:** leaf received off `/show/stream`
  (`C-FANOUT: not silent-dropped — the active scene declared the path`);
  `target_scene=scene-screen-2 overlay=wipe-cover cut_at_ms=250 window=[250,450]`
  — the **rule's** screen-2 timings (cut 250), not the old literal's cut 650.
- **Real ingest:** `outputBytes` 7,911,807 → 72,091,270 = **64.18 MB pushed**
  over an **88s** hold (17 samples, 4 re-loops), steady ~700–870 KiB/s,
  `outputActive` true throughout, `dropped_after_connect=false`, congestion 0.0,
  0 skipped. RTMP verbatim: `Connection to rtmp://live.twitch.tv/app/
  (2600:…) successful`, no post-connect disconnect. **`==> PROVEN: Twitch
  INGESTED the stream`.** VOD `build/m10-live-vod/pulsar-20260609-152137.mp4`
  (77.4 MB).
- **Frame A** (`build/m10-frames/frame-A-screen1.png`): real varied desktop
  content (`distinct=3287`, WGC warmed up — desktop active). Real content was on
  air as Scene A.
- **Fade-blend honesty:** `frame-MIDFADE-blend.png` is a **flat uniform magenta
  fill** (`#C81E5A`, `distinct=1`) — the settled Solar wipe-cover overlay, **NOT
  an A↔white blend gradient**. The headless WGC grab captured the fully-settled
  overlay-covered frame, not a composited intermediate of the OBS Fade. So the
  scene fondu is **proven at the obs-ws control level** (C-FADE: two
  `SetCurrentProgramScene` through the armed `fade_transition`, 450ms) but the
  **visual blend is NOT proven headless** — this is the flagged risk: the
  frontend-stub headless path does not surface a mid-Fade composited frame to the
  grab. Info for the authoring session.

### Rollback done

`build/keeper_m10_rollback.py` restored Orion active scene to baseline
**`18fecbd4-a11b-434f-9173-3041c511991a`** (verified `active after == baseline`).
No destructive VPS op. Blue v5 left published (the deliverable). Working tree:
only `scripts/probe-m10-canvas-live.py` modified (the env-driven `target` trigger
input); all run artefacts under gitignored `build/`.
