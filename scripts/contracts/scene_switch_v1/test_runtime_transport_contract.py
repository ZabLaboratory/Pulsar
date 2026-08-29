"""Static integration gate for the production scene-switch vendor adapter."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp"
FRONTEND_CMAKE = ROOT / "plugins" / "pulsar-frontend-stub" / "CMakeLists.txt"
PROTOCOL = ROOT / "docs" / "PROTOCOL.md"


def test_runtime_vendor_is_dedicated_and_only_uses_call_vendor_surface() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    protocol = PROTOCOL.read_text(encoding="utf-8")

    assert 'kVendorName = "pulsar-scene-switch"' in source
    for request_type in ("Prepare", "Take", "Abort", "Dispatch", "GetState"):
        assert f'register_request(vendor_, "{request_type}"' in source
    assert 'register_request(vendor_, "GetState"' in source
    assert "obs_websocket_vendor_emit_event" in source
    assert "pulsar-scene-switch" in protocol
    assert "not** top-level obs-websocket requests" in protocol


def test_frontend_declares_its_own_json_dependency() -> None:
    cmake = FRONTEND_CMAKE.read_text(encoding="utf-8")
    assert "obs-deps-*-x64" in cmake
    assert "list(LENGTH _pulsar_obs_deps _pulsar_obs_deps_count)" in cmake
    assert "No non-Qt x64 obs-deps directory found" in cmake
    assert 'set(nlohmann_json_DIR "${PULSAR_OBS_DEPS}/share/cmake/nlohmann_json")' in cmake
    assert '"${nlohmann_json_DIR}/nlohmann_jsonConfig.cmake"' in cmake
    assert 'find_package(nlohmann_json 3.11 REQUIRED)' in cmake
    assert "nlohmann_json::nlohmann_json" in cmake


def test_runtime_adapter_keeps_protocol_and_physical_boundaries_explicit() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    for required in (
        "IDEMPOTENCY_CONFLICT",
        "REVISION_STALE",
        "SERVER_SEQ_STALE",
        "PREVIEW_FROZEN",
        "PREVIEW_NOT_READY",
        "sceneSwitchPrepare",
        "sceneSwitchTake",
        "sceneSwitchAbort",
        "OnSceneSwitchPreviewVideoFrame",
        "video_output_connect(previewVideo",
        "takeCommitted(sceneSwitchTakeId, frameId, ptsNs)",
        "obs_view_cancel_atomic_swap()",
        "BCryptFinishHash",
    ):
        assert required in source

    # #245's Program route remains the only audio binding and the new adapter
    # must not introduce any encoder-video rebinding site.
    adapter = source[source.index("class PulsarSceneSwitchVendor") : source.index("namespace {", source.index("class PulsarSceneSwitchVendor") + 1)]
    assert "obs_encoder_set_video" not in adapter
    assert "obs_output_set_media" not in adapter


def test_runtime_adapter_uses_supported_obs_data_response_api_and_typed_revisions() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    assert "++revisions_[" not in source
    assert "obs_data_set_json" not in source
    assert "obs_data_create_from_json(serialized.c_str())" in source
    assert "obs_data_apply(response, temporary)" in source
    assert "bool revisionCanAdvance(const char *key) const" in source
    assert "bool advanceRevision(const char *key)" in source
    assert "!revisionCanAdvance(\"program\") || !revisionCanAdvance(\"role_map\")" in source


def test_prepare_scene_id_control_check_uses_one_materialized_string() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    prepare = source[source.index('if (type == "Prepare")') : source.index('} else if (type == "Take")')]

    assert "Materialize once before taking iterators" in prepare
    assert 'sceneId = in["target"]["scene_id"].get<std::string>();' in prepare
    assert "std::any_of(sceneId.begin(), sceneId.end()" in prepare
    assert 'get<std::string>().begin(), in["target"]["scene_id"].get<std::string>().end()' not in prepare


def test_runtime_adapter_guards_the_exact_v1_lifecycle_edges() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    assert '"preview_ready")' in source
    assert "Prepare command contains unknown or cross-type fields" in source
    assert "Take command contains unknown or cross-type fields" in source
    assert "Abort command contains unknown or cross-type fields" in source
    assert 'allowed.insert("last_committed_frame_id")' in source
    assert 'allowed.insert("last_committed_pts_ns")' in source
    assert 'in["reason"] == "queue_rejected"' in source
    assert "size() > 256" in source
    assert "required command_type disagree" in source
    assert 'if (pendingTake_ || state_ == "take_accepted")' in source
    assert "Abort intent_id differs from Take" in source
    assert 'const json previous = revisions_;' in source
    assert "sceneSwitchClearPrepared" in source
    assert "kMaxOutcomes = 4096" in source
    assert "RUNTIME_MISMATCH into an" in source
    assert '"idempotency_cache_entries",outcomes_.size()' in source
    assert '"idempotency_cache_capacity",kMaxOutcomes' in source
    committed = source[source.index("void takeCommitted") : source.index("private:", source.index("void takeCommitted"))]
    assert "outcomes_[take.key]" not in committed
    assert "original Dispatch(Take) response (TakeAccepted)" in committed


def test_runtime_adapter_publishes_callbacks_only_after_successful_registration() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    assert "std::atomic<PulsarSceneSwitchVendor *> g_sceneSwitchVendor{nullptr}" in source
    assert "bool start()" in source
    assert "callbacks remain safe, but it is never published" in source
    assert "g_sceneSwitchVendorStorage.start())\n        g_sceneSwitchVendor.store" in source
    assert "g_sceneSwitchVendor.store(nullptr, std::memory_order_release);\n    g_sceneSwitchVendorStorage.stop();" in source
    assert source.count("g_sceneSwitchVendor.load(std::memory_order_acquire)") >= 2


def test_prepare_rolls_back_physical_lane_and_marker_after_postcondition_failure() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    prepare = source[source.index("bool PulsarFrontendAPI::sceneSwitchPrepare") : source.index("bool PulsarFrontendAPI::sceneSwitchTake")]

    # The retained old selection precedes physical replacement, then both the
    # child composition and protocol marker are restored on an invariant miss.
    assert prepare.index("oldSelection = obs_source_get_ref(previewSelection)") < prepare.index(
        "replaceLaneCompositionLocked(previewLane, scene)"
    )
    assert "replaceLaneCompositionLocked(previewLane, oldSelection)" in prepare
    assert 'sceneSwitchPreparedCommandId.clear();' in prepare
    assert 'dualLaneInvariantLocked("scene-switch-prepare-rollback")' in prepare
