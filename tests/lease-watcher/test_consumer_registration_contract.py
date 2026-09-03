"""Static guards for owner-bound DirectShow return consumer registration."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATCHES = sorted(ROOT.glob("patches/00[3-4][0-9]-*.patch"))


def _patch_text() -> str:
    if not PATCHES:
        raise AssertionError("the upstream registration patch chain is missing")
    return "\n".join(patch.read_text(encoding="utf-8") for patch in PATCHES)


def test_precreated_event_cannot_attach_without_registration() -> None:
    text = _patch_text()
    assert "const bool lease_present = lease != NULL;" in text
    assert "const bool registration_live = return_consumer_registration_live(vcam);" in text
    assert "const bool active = lease_present && registration_live;" in text
    assert "GetNamedPipeClientProcessId(vcam->consumer_registration_pipe, &pid)" in text
    assert "OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, FALSE, pid)" in text
    assert "ProcessIdToSessionId(pid, &observed_session)" in text
    assert "WaitForSingleObject(process, 0) == WAIT_TIMEOUT" in text


def test_stale_registration_is_released_for_reconnect() -> None:
    text = _patch_text()
    assert "DisconnectNamedPipe(vcam->consumer_registration_pipe);" in text
    assert "return_consumer_registration_pipe_start(vcam)" in text


def test_registration_claim_is_owner_and_session_bound() -> None:
    text = _patch_text()
    assert "video_queue_get_challenge(video_queue_t *vq" in text
    assert "ProcessIdToSessionId(pid, &observed_session)" in text
    assert "consumer_challenge_low" in text
    assert "consumer_challenge_high" in text
    assert "CreateNamedPipeW" in text
    assert "CreateFileW(pipe_name, GENERIC_WRITE" in text


def test_release_cannot_clear_a_new_owner_registration() -> None:
    text = _patch_text()
    assert "PIPE_NOWAIT" in text
    assert "GetNamedPipeClientProcessId" in text
    assert "return_consumer_process_image_allowed(process)" in text


def test_consumer_mapping_is_read_only_and_challenge_is_non_authoritative() -> None:
    text = _patch_text()
    assert "consumer_challenge_low" in text and "consumer_challenge_high" in text
    assert "FILE_MAP_READ" in text
    assert "CreateNamedPipeW" in text
    assert "GetNamedPipeClientProcessId" in text
    assert "if (!(uint32_t)challenge)" in text
    assert "the 80-byte header ABI is unchanged" in text


def test_same_user_helper_cannot_pass_process_owner_gate() -> None:
    text = _patch_text()
    assert "QueryFullProcessImageNameW(process" in text
    assert "return_consumer_process_image_allowed(process)" in text
    for image in ("obs64.exe", "obs32.exe", "ffmpeg.exe", "pulsar.exe"):
        assert f'_wcsicmp(image_name, L"{image}") == 0' in text


def test_consumer_releases_registration_on_stop_and_destruction() -> None:
    text = _patch_text()
    assert "ReleaseConsumerRegistrationPipe();" in text
    assert "+\tReleaseConsumerRegistrationPipe();\n \treturn OutputFilter::Stop();" in text
    assert "+\tReleaseConsumerRegistrationPipe();\n \tif (d3d11_requested" in text
    assert "+\t\t\tReleaseConsumerRegistrationPipe();\n \t\t\tvideo_queue_close(vq);" in text
