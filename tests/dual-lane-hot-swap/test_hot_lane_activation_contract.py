"""Static contract for the permanently-hot dual-lane view fastpath."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp"
PATCH = ROOT / "patches" / "0045-perf-libobs-preserve-hot-lane-activation.patch"


def test_preview_view_is_created_as_a_permanently_active_view() -> None:
    source = FRONTEND.read_text(encoding="utf-8")
    setup = source[source.index("bool PulsarFrontendAPI::setupDualLane") :]
    setup = setup[: setup.index("void PulsarFrontendAPI::teardown")]

    assert "programView = obs_get_main_view();" in setup
    assert "previewView = obs_view_create_active();" in setup
    assert "Preview uses a second permanently-active view" in setup


def test_libobs_preserves_activation_only_for_a_complete_main_view_pair() -> None:
    patch = PATCH.read_text(encoding="utf-8")

    assert "obs_view_create_active" in patch
    assert "obs_view_create_with_type" in patch
    assert "swap->first_view->type == MAIN_VIEW" in patch
    assert "swap->second_view->type == AUX_VIEW" in patch
    assert "new_first == old_second" in patch
    assert "new_second == old_first" in patch
    assert "old_first != old_second" in patch
    assert "obs_source_transfer_main_activation" in patch
    assert "show_refs" in patch
    assert "activate_refs" in patch

    pair = patch[patch.index("const bool preserve_active_pair") :]
    assert pair.index("if (preserve_active_pair)") < pair.index("obs_source_transfer_main_activation")
    assert pair.index("obs_source_transfer_main_activation") < pair.index("\n+\t\t\tobs_source_activate(")
    assert pair.count("if (!preserve_active_pair)") >= 2


def test_generic_view_creation_remains_auxiliary() -> None:
    patch = PATCH.read_text(encoding="utf-8")
    assert "return obs_view_create_with_type(AUX_VIEW);" in patch
