"""Probe-side regression evidence for CEF startup/source-destroy ordering.

This test does not change or replace the browser implementation.  It records
the currently observable startup contract and models the M10 interleaving that
caused the process to exit while replacing the first browser source.  The
native fix belongs to Forge; this fixture makes the failure boundary explicit
so a later fix cannot silently remove the readiness barrier or the diagnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "pulsar-browser" / "obs-browser-plugin.cpp"
SOURCE = ROOT / "plugins" / "pulsar-browser" / "obs-browser-source.cpp"
M10 = ROOT / "scripts" / "probe-m10-canvas-live.py"


def _body(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


@dataclass
class _StartupDestroyModel:
    """Deterministic model of a post-before-ready followed by source destroy."""

    manager_initialized: bool = False
    cef_ready: bool = False
    create_posted: bool = False
    destroy_posted: bool = False
    fatal_reason: str | None = None
    events: list[str] = field(default_factory=list)

    def obs_browser_initialize(self) -> None:
        if self.manager_initialized:
            return
        self.manager_initialized = True
        self.events.append("manager_thread_started")

    def browser_init_completes(self) -> None:
        self.cef_ready = True
        self.events.append("cef_started_event_signaled")

    def queue_cef_task(self, task: str) -> bool:
        # CefPostTask has no acceptance guarantee before CefInitialize has
        # completed (or after shutdown); this is the boundary under test.
        accepted = self.cef_ready
        self.events.append(f"QueueCEFTask({task})={accepted}")
        return accepted

    def create_browser(self) -> None:
        self.create_posted = self.queue_cef_task("create")
        self.events.append("source_create_returned")

    def destroy_browser_source(self) -> None:
        self.destroy_posted = self.queue_cef_task("destroy")
        if not self.destroy_posted:
            self.fatal_reason = "cef_destroy_task_post_failed"
            self.events.append("BrowserSourceDestroyFatal")


def test_startup_can_expose_browser_source_before_cef_ready() -> None:
    plugin = PLUGIN.read_text(encoding="utf-8")
    initialize = _body(plugin, "extern \"C\" void obs_browser_initialize", "void RegisterBrowserSource")
    browser_init = _body(plugin, "static void BrowserInit(void)", "static void BrowserShutdown")

    # Non-Qt initialization returns after starting BrowserManagerThread; the
    # readiness event is signaled only later, after CefInitialize succeeds.
    assert "manager_thread = thread(BrowserManagerThread);" in initialize
    assert "os_event_wait(cef_started_event" not in initialize
    assert "CefInitialize(" in browser_init
    assert browser_init.index("CefInitialize(") < browser_init.index(
        "os_event_signal(cef_started_event);"
    )


def test_queue_cef_task_is_directly_bound_to_post_acceptance() -> None:
    plugin = PLUGIN.read_text(encoding="utf-8")
    queue = _body(plugin, "bool QueueCEFTask(std::function<void()> task)", "[[noreturn]] static void FailCefShutdown")

    assert "return CefPostTask(TID_UI" in queue
    assert "cef_started_event" not in queue


def test_source_destroy_turns_post_failure_into_process_abort() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    destroy = _body(source, "void BrowserSource::Destroy()", "void BrowserSource::ExecuteOnBrowser")

    assert "QueueCEFTask" in destroy
    assert destroy.count("cef_destroy_task_post_failed") == 2
    assert "std::_Exit(EXIT_FAILURE);" in source


def test_replacement_interleaving_reproduces_m10_failure_boundary() -> None:
    state = _StartupDestroyModel()
    state.obs_browser_initialize()
    state.create_browser()
    # The first source is replaced before the manager thread has completed
    # BrowserInit.  Its deferred libobs destruction has the same post failure
    # path as the observed M10 log.
    state.destroy_browser_source()

    assert state.events == [
        "manager_thread_started",
        "QueueCEFTask(create)=False",
        "source_create_returned",
        "QueueCEFTask(destroy)=False",
        "BrowserSourceDestroyFatal",
    ]
    assert state.fatal_reason == "cef_destroy_task_post_failed"
    assert state.cef_ready is False


def test_m10_hard_termination_is_cleanup_mask_not_source_failure_cause() -> None:
    m10 = M10.read_text(encoding="utf-8")
    spawn = _body(m10, "    def spawn(self)", "    def _pump_stdout")
    shutdown = _body(m10, "    def shutdown(self", "# --------------------------------------------------------------------------")

    assert "CREATE_NO_WINDOW" in spawn
    assert "self.proc.terminate()" in shutdown
    assert "self.proc.kill()" in shutdown
    # The source-destroy fatal is emitted by Pulsar before this runner cleanup
    # can act, so a clean reap cannot be treated as graceful CEF shutdown.
    assert "cef_destroy_task_post_failed" not in shutdown
