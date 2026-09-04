"""Static adversarial contract for the producer-owned D3D11 helper boundary."""

from pathlib import Path


ROOT = Path(__file__).parents[2]
PATCH = ROOT / "patches" / "0043-fix-d3d11-private-helper-authenticated-return.patch"


def _text() -> str:
    return PATCH.read_text(encoding="utf-8")


def _added(path: str) -> str:
    text = _text()
    marker = f"diff --git a/{path} b/{path}"
    start = text.index(marker)
    end = text.find("\ndiff --git ", start + len(marker))
    section = text[start:] if end < 0 else text[start:end]
    return "\n".join(line[1:] for line in section.splitlines() if line.startswith("+") and not line.startswith("+++"))


def _source(path: str) -> str:
    text = _text()
    marker = f"diff --git a/{path} b/{path}"
    start = text.index(marker)
    end = text.find("\ndiff --git ", start + len(marker))
    return text[start:] if end < 0 else text[start:end]


def test_capability_is_private_hmac_and_generation_bound() -> None:
    auth = _added("plugins/win-dshow/virtualcam-module/d3d11-return-auth.c")
    header = _added("plugins/win-dshow/virtualcam-module/d3d11-return-auth.h")
    producer = _source("plugins/win-dshow/virtualcam.c")

    assert "BCRYPT_ALG_HANDLE_HMAC_FLAG" in auth
    assert "BCryptGenRandom" in auth
    assert "PULSAR_RETURN_AUTH_CAPABILITY_BYTES 32U" in header
    assert "PULSAR_RETURN_AUTH_GENERATION_BYTES 16U" in header
    assert "bootstrap.capability" in producer
    assert "pulsar_return_auth_write(parent_write, &bootstrap, sizeof(bootstrap))" in producer
    assert "--read-handle=%llu --write-handle=%llu" in producer
    assert "capability" not in producer[producer.index("command_line") : producer.index("PROCESS_INFORMATION")]


def test_event_or_public_pid_cannot_authorize_d3d11_handles() -> None:
    producer = _source("plugins/win-dshow/virtualcam.c")
    transport = _source("plugins/win-dshow/virtualcam-module/d3d11-return-transport.cpp")

    assert "pulsar_d3d11_return_producer_bind_helper" in producer
    assert "pulsar_d3d11_return_producer_set_consumer_pid" in producer
    assert "vcam->d3d11 && !vcam->helper_authenticated" in producer
    assert "HANDLE helper_process = nullptr" in transport
    assert "helper_bound ? duplicate_handle_for_process" in transport
    assert "helper_bound ? GetProcessId(producer->helper_process)" in transport
    assert "GetNamedPipeClientProcessId" not in transport
    assert "return_helper_image_matches(process.hProcess, helper_path)" in producer
    assert "PULSAR_D3D11_RETURN_HELPER_PATH" not in producer


def test_helper_process_receives_only_inherited_handles_and_proof_precedes_duplication() -> None:
    producer = _source("plugins/win-dshow/virtualcam.c")
    transport = _source("plugins/win-dshow/virtualcam-module/d3d11-return-transport.cpp")

    assert "PROC_THREAD_ATTRIBUTE_HANDLE_LIST" in producer
    assert "HANDLE inherited[2] = {child_read, child_write}" in producer
    assert "pulsar_return_auth_make_proof" in producer
    assert producer.index("pulsar_return_auth_equal(proof.mac") < producer.index(
        "pulsar_d3d11_return_producer_bind_helper"
    )
    prepare = transport[transport.index("producer_prepare_ring") :]
    assert prepare.index("helper_bound") < prepare.index("duplicate_handle_for_process(handle")
    assert "create_owner_only_mapping" in transport


def test_crash_generation_mismatch_and_two_lanes_fail_closed_to_cpu() -> None:
    producer = _added("plugins/win-dshow/virtualcam.c")
    helper = _added("plugins/win-dshow/virtualcam-module/pulsar-d3d11-return-helper.cpp")

    assert "ERROR_ACCESS_DENIED" in helper
    assert "challenge.generation" in helper
    assert "return_helper_frame_reader" in producer
    assert "helper_failed" in producer
    assert "return_helper_close(vcam)" in producer
    assert "using CPU return queue" in producer
    assert "PULSAR_D3D11_PROGRAM_RETURN" in helper
    assert "PULSAR_D3D11_PREVIEW_RETURN" in helper


def test_directshow_never_opens_the_private_d3d11_consumer() -> None:
    filter_source = _added("plugins/win-dshow/virtualcam-module/virtualcam-filter.cpp")

    assert "producer-launched" in filter_source
    assert "d3d11_requested = false" in filter_source
    assert "pulsar_d3d11_return_consumer_open" not in filter_source
    assert "pulsar_d3d11_return_consumer_read" not in filter_source
