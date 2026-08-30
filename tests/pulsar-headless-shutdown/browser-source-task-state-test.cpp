/******************************************************************************
 Copyright (C) 2026 ZabLaboratory

 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 2 of the License, or
 (at your option) any later version.
 ******************************************************************************/

#include "browser-source-task-state.hpp"
#include "browser-audio-callback-gate.hpp"

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <mutex>
#include <thread>

static bool check(bool condition, const char *message)
{
	if (!condition)
		std::fprintf(stderr, "browser source task-state test: %s\n", message);
	return condition;
}

struct FakeBrowserClose {
	int refs = 0;
	bool invalidated = false;
	bool on_before_close_seen = false;
	bool browser_ref_survived_close = false;
};

struct FakeBrowserOwner {
	FakeBrowserClose *browser = nullptr;

	explicit FakeBrowserOwner(FakeBrowserClose *browser_) : browser(browser_)
	{
		++browser->refs;
	}

	~FakeBrowserOwner()
	{
		reset();
	}

	void reset()
	{
		if (browser) {
			--browser->refs;
			browser = nullptr;
		}
	}
};

struct FakeBrowserHost {
	FakeBrowserClose *browser = nullptr;

	void CloseBrowser()
	{
		browser->on_before_close_seen = true;
		browser->invalidated = true;
		browser->browser_ref_survived_close = browser->refs != 0;
	}
};

static bool test_synchronous_close_releases_browser_before_invalidation()
{
	FakeBrowserClose browser;
	{
		FakeBrowserOwner browser_owner(&browser);
		FakeBrowserHost browser_host{&browser};
		/* Model the production host-only handoff: no CefBrowser owner survives. */
		browser_owner.reset();
		browser_host.CloseBrowser();
	}

	return check(browser.on_before_close_seen, "fake close did not enter OnBeforeClose") &&
	       check(browser.invalidated, "fake close did not invalidate the browser") &&
	       check(!browser.browser_ref_survived_close,
		     "a browser owner survived synchronous OnBeforeClose") &&
	       check(browser.refs == 0, "fake browser retained a reference after close");
}

static bool test_admission_then_destroy()
{
	BrowserSourceTaskState state;
	state.source = reinterpret_cast<BrowserSource *>(static_cast<std::uintptr_t>(1));

	std::mutex rendezvous_mutex;
	std::condition_variable rendezvous;
	bool admitted = false;
	bool release = false;
	BrowserSourceTaskRelease released;
	std::thread create_task([&]() {
		if (!state.acquire(false))
			return;
		{
			std::lock_guard<std::mutex> lock(rendezvous_mutex);
			admitted = true;
		}
		rendezvous.notify_one();
		{
			std::unique_lock<std::mutex> lock(rendezvous_mutex);
			rendezvous.wait(lock, [&]() { return release; });
		}
		released = state.release_task();
	});

	{
		std::unique_lock<std::mutex> lock(rendezvous_mutex);
		if (!rendezvous.wait_for(lock, std::chrono::seconds(2), [&]() { return admitted; })) {
			release = true;
			lock.unlock();
			rendezvous.notify_one();
			create_task.join();
			return check(false, "admission rendezvous timed out");
		}
	}

	const bool destroying = state.begin_destroy();
	const bool late_admission = state.acquire(false);
	const BrowserSourceTaskRelease early_delete = state.request_delete_and_take_if_idle(true);
	{
		std::lock_guard<std::mutex> lock(rendezvous_mutex);
		release = true;
	}
	rendezvous.notify_one();
	create_task.join();

	return check(destroying, "destroy did not close admission") &&
	       check(!late_admission, "a task entered after destroy") &&
	       check(early_delete.source == nullptr, "delete raced an active lease") &&
	       check(released.source != nullptr, "last lease did not own deletion") &&
	       check(released.complete_destroy_task, "destroy completion was not preserved") &&
	       check(!released.underflow, "lease release underflowed");
}

static bool test_destroy_then_admission()
{
	BrowserSourceTaskState state;
	state.source = reinterpret_cast<BrowserSource *>(static_cast<std::uintptr_t>(1));

	const bool destroying = state.begin_destroy();
	const bool late_admission = state.acquire(false);
	const BrowserSourceTaskRelease deleted = state.request_delete_and_take_if_idle(true);
	const BrowserSourceTaskRelease duplicate = state.request_delete_and_take_if_idle(true);
	const BrowserSourceTaskRelease underflow = state.release_task();

	return check(destroying, "destroy did not win before admission") &&
	       check(!late_admission, "destroy-then-create admitted a task") &&
	       check(deleted.source != nullptr, "idle destroy did not detach source") &&
	       check(deleted.complete_destroy_task, "idle destroy lost completion") &&
	       check(duplicate.source == nullptr, "source detached twice") &&
	       check(underflow.underflow, "release underflow was not detected");
}

static bool test_destroy_task_releases_own_lease()
{
	BrowserSourceTaskState own_only;
	own_only.source = reinterpret_cast<BrowserSource *>(static_cast<std::uintptr_t>(1));
	const bool own_only_destroying = own_only.begin_destroy();
	const bool own_only_acquired = own_only.acquire(true);
	const BrowserSourceTaskRelease own_only_release = own_only.request_delete_release_own_lease(true);
	const BrowserSourceTaskRelease own_only_duplicate = own_only.request_delete_release_own_lease(true);

	BrowserSourceTaskState other_first;
	other_first.source = reinterpret_cast<BrowserSource *>(static_cast<std::uintptr_t>(1));
	const bool other_first_destroying = other_first.begin_destroy();
	const bool other_first_own = other_first.acquire(true);
	const bool other_first_other = other_first.acquire(true);
	const BrowserSourceTaskRelease other_release = other_first.release_task();
	const BrowserSourceTaskRelease own_release = other_first.request_delete_release_own_lease(true);
	const BrowserSourceTaskRelease other_underflow = other_first.release_task();

	BrowserSourceTaskState own_first;
	own_first.source = reinterpret_cast<BrowserSource *>(static_cast<std::uintptr_t>(1));
	const bool own_first_destroying = own_first.begin_destroy();
	const bool own_first_own = own_first.acquire(true);
	const bool own_first_other = own_first.acquire(true);
	const BrowserSourceTaskRelease own_waiting = own_first.request_delete_release_own_lease(true);
	const BrowserSourceTaskRelease other_last = own_first.release_task();
	const BrowserSourceTaskRelease own_underflow = own_first.release_task();

	return check(own_only_destroying && own_only_acquired, "own-only setup did not acquire its lease") &&
	       check(own_only_release.source != nullptr && own_only_release.complete_destroy_task,
		     "own-only lease did not complete deletion") &&
	       check(own_only.active_tasks == 0, "own-only release left an active lease") &&
	       check(own_only_duplicate.source == nullptr && !own_only_duplicate.complete_destroy_task,
		     "own-only duplicate completed deletion twice") &&
	       check(own_only_duplicate.underflow, "own-only duplicate was not detected") &&
	       check(other_first_destroying && other_first_own && other_first_other,
		     "other-first setup did not acquire both leases") &&
	       check(other_release.source == nullptr, "other-first release deleted too early") &&
	       check(own_release.source != nullptr && own_release.complete_destroy_task,
		     "own lease did not complete the last release") &&
	       check(other_first.active_tasks == 0, "other-first release left an active lease") &&
	       check(other_underflow.underflow, "other-first double release was accepted") &&
	       check(own_first_destroying && own_first_own && own_first_other,
		     "own-first setup did not acquire both leases") &&
	       check(own_waiting.source == nullptr, "own-first release deleted with another lease") &&
	       check(other_last.source != nullptr && other_last.complete_destroy_task,
		     "last concurrent lease did not complete deletion") &&
	       check(own_first.active_tasks == 0, "own-first release left an active lease") &&
	       check(own_underflow.underflow, "own-first double release was accepted");
}

static bool test_delete_without_pending_source_does_not_complete_counter()
{
	/* CEF-not-ready/DeleteNow paths do not arm pending_source_destructions. */
	BrowserSourceTaskState state;
	state.source = reinterpret_cast<BrowserSource *>(static_cast<std::uintptr_t>(1));
	const bool destroying = state.begin_destroy();
	const BrowserSourceTaskRelease release = state.request_delete_and_take_if_idle(false);

	return check(destroying, "unarmed delete path did not enter destroying state") &&
	       check(release.source != nullptr, "unarmed delete path did not detach its source") &&
	       check(!release.complete_destroy_task,
		     "unarmed delete path attempted to complete a pending source destruction") &&
	       check(state.deleted, "unarmed delete path was not marked deleted");
}

static bool test_inverted_two_browser_finalization_waits_for_audio()
{
	/* Exercise the production per-source close tracker with two CEF IDs. */
	BrowserSourceTaskState source;
	source.source = reinterpret_cast<BrowserSource *>(static_cast<std::uintptr_t>(1));
	const bool destroying = source.begin_destroy();
	const bool armed = source.arm_browser_ids(std::vector<int>{101, 202});
	BrowserAudioCallbackGate audio;
	audio.mark_stream_started();
	const bool old_audio_admitted = audio.try_acquire();

	/* The second browser may close first, but source deletion is not allowed. */
	const bool second_deleted = source.finalize_browser_id(202);
	/* Model an OnAfterCreated callback racing between two close callbacks. */
	const bool late_browser_added = source.add_browser_id(303);
	const bool first_deleted = source.finalize_browser_id(101);
	audio.mark_close_callback_seen();
	audio.mark_stream_stopped();
	const bool claimed_while_audio_in_flight = audio.try_claim_finalization();
	const bool audio_claimed_after_release = audio.release_and_try_claim();
	const bool late_deleted = source.finalize_browser_id(303);
	const bool duplicate_delete = source.finalize_browser_id(303);

	return check(destroying && armed, "multi-browser source close was not armed") &&
	       check(old_audio_admitted, "old audio callback was not admitted") &&
	       check(!second_deleted, "source deleted while another browser ID remained") &&
	       check(late_browser_added, "late browser ID was not admitted while source was destroying") &&
	       check(!first_deleted, "source deleted while a late browser ID remained") &&
	       check(!claimed_while_audio_in_flight, "audio finalization raced an in-flight callback") &&
	       check(audio_claimed_after_release, "audio quiescence was not observed") &&
	       check(late_deleted, "last browser ID did not complete source deletion") &&
	       check(source.browser_delete_completed, "source deletion was not exactly once") &&
	       check(!duplicate_delete, "duplicate browser finalization deleted the source twice") &&
	       check(source.pending_browser_ids.empty(), "browser IDs remained pending after finalization");
}

int main()
{
	return test_synchronous_close_releases_browser_before_invalidation() &&
	       test_admission_then_destroy() && test_destroy_then_admission() &&
	       test_destroy_task_releases_own_lease() &&
	       test_delete_without_pending_source_does_not_complete_counter() &&
	       test_inverted_two_browser_finalization_waits_for_audio()
		       ? 0
		       : 1;
}
