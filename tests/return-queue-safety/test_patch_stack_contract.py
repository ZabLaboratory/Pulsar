"""Static guards for the canonical patch-stack order and replay inputs."""

from pathlib import Path


ROOT = Path(__file__).parents[2]
PATCHES = ROOT / "patches"


def test_libobs_continuations_have_unique_deterministic_sequence() -> None:
    expected = (
        "0026-fix-libobs-borrowed-video-mailbox.patch",
        "0027-fix-win-dshow-lease-watcher.patch",
        "0028-fix-libobs-pipeline-stats-atomic-after-mailbox.patch",
    )
    actual = tuple(
        path.name
        for path in sorted(PATCHES.glob("00??-*.patch"))
        if path.name.startswith(("0026-", "0027-", "0028-"))
    )
    assert actual == expected
    assert len({name[:4] for name in actual}) == len(actual)
    assert not (PATCHES / "0027-fix-libobs-borrowed-video-mailbox.patch").exists()


def test_libobs_continuation_patches_are_replayable_mailboxes_without_conflicts() -> None:
    for name in (
        "0026-fix-libobs-borrowed-video-mailbox.patch",
        "0027-fix-win-dshow-lease-watcher.patch",
        "0028-fix-libobs-pipeline-stats-atomic-after-mailbox.patch",
    ):
        text = (PATCHES / name).read_text(encoding="utf-8")
        assert text.startswith("From ")
        assert "Subject: [PATCH]" in text
        assert "<<<<<<<" not in text
        assert ">>>>>>>" not in text
        assert "Work-Unit: ZabLaboratory/Pulsar#253" in text
