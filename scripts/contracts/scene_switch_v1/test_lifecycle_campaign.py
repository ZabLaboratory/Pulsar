"""Volume and determinism gates for issue #247."""

from __future__ import annotations

from .lifecycle_campaign import (
    DEFAULT_ATTEMPTS,
    DEFAULT_CYCLES,
    build_report,
)


def test_lifecycle_alias_campaign_meets_issue_247_matrix() -> None:
    report = build_report()
    lifecycle = report["lifecycle"]
    assert lifecycle["cycles"] >= DEFAULT_CYCLES >= 100
    assert lifecycle["counts"] == {
        "prepare_accepted": lifecycle["cycles"],
        "preview_ready": lifecycle["cycles"],
        "take_accepted": lifecycle["cycles"],
        "take_committed": lifecycle["cycles"],
        "precommit_preview_frozen": lifecycle["cycles"],
        "promoted_lane_rejected": lifecycle["cycles"],
        "postcommit_prepare_accepted": lifecycle["cycles"],
        "postcommit_preview_ready": lifecycle["cycles"],
    }

    assert len(lifecycle["records"]) == lifecycle["cycles"]
    for record in lifecycle["records"]:
        assert record["results"]["premature_prepare"]["error_code"] == "PREVIEW_FROZEN"
        assert record["results"]["promoted_lane_prepare"]["error_code"] == "PREVIEW_LANE_MISMATCH"
        assert record["route_after_commit"]["on_air_lane"] != record["route_after_commit"]["preview_lane"]
        assert record["route_after_commit"]["surfaces"] == record["route_after_preview_mutation"]["surfaces"]
        assert record["frames"]["program_hashes_before"] == record["frames"]["program_hashes_after_preview_mutation"]
        commit = record["frames"]["commit"]
        committed_event = record["results"]["take_committed"]
        assert committed_event["frame_id"] == commit["frame_id"]
        assert committed_event["pts_ns"] == commit["pts_ns"]


def test_duplicate_stale_and_conflict_campaign_commits_once_per_intent() -> None:
    report = build_report(cycles=100, attempts=DEFAULT_ATTEMPTS)
    attempts = report["attempts"]
    assert attempts["attempts"] >= DEFAULT_ATTEMPTS >= 1000
    assert sum(attempts["group_counts"].values()) == attempts["attempts"]
    assert attempts["group_counts"] == {
        "duplicate_take_concurrent": DEFAULT_ATTEMPTS // 4,
        "duplicate_premature_concurrent": DEFAULT_ATTEMPTS // 4,
        "stale_revision": DEFAULT_ATTEMPTS // 4,
        "conflicting_command_reuse": DEFAULT_ATTEMPTS // 4,
    }
    assert attempts["result_counts"] == {
        "TakeAccepted": DEFAULT_ATTEMPTS // 4,
        "PREVIEW_FROZEN": DEFAULT_ATTEMPTS // 4,
        "REVISION_STALE": DEFAULT_ATTEMPTS // 4,
        "IDEMPOTENCY_CONFLICT": DEFAULT_ATTEMPTS // 4,
    }
    assert attempts["committed_event_count"] == 1
    assert attempts["commit_callback_attempts"] >= 32
    assert attempts["final_snapshot"]["role_map"] == {"on_air": "B", "preview": "A"}
    assert attempts["final_snapshot"]["revisions"] == {"program": 1, "preview": 1, "role_map": 1}

    double_take = report["concurrent_take_race"]
    assert double_take == {
        "command_attempts": 2,
        "accepted_count": 1,
        "rejected_count": 1,
        "rejection_codes": ["SERVER_SEQ_STALE"],
        "commit_count": 1,
        "final_role_map": {"on_air": "B", "preview": "A"},
        "final_revisions": {"program": 1, "preview": 1, "role_map": 1},
    }


def test_abort_cases_preserve_mapping_and_revisions() -> None:
    report = build_report(cycles=100, attempts=1000)
    aborts = report["abort_mapping_preservation"]
    assert aborts["count"] == 4
    assert {case["reason"] for case in aborts["cases"]} == {
        "operator",
        "shutdown",
        "superseded",
        "timeout",
    }
    for case in aborts["cases"]:
        assert case["result"]["event_type"] == "TakeAborted"
        assert case["before"]["role_map"] == case["after"]["role_map"]
        assert case["before"]["revisions"] == case["after"]["revisions"]


def test_campaign_report_is_reproducible_without_wall_clock_or_scheduler_data() -> None:
    first = build_report(cycles=100, attempts=1000)
    second = build_report(cycles=100, attempts=1000)
    assert first == second
