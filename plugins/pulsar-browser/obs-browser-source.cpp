/******************************************************************************
 Copyright (C) 2014 by John R. Bradley <jrb@turrettech.com>
 Copyright (C) 2023 by Lain Bailey <lain@obsproject.com>

 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 2 of the License, or
 (at your option) any later version.

 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with this program.  If not, see <http://www.gnu.org/licenses/>.
 ******************************************************************************/

#include "obs-browser-source.hpp"
#include "browser-client.hpp"
#include "browser-scheme.hpp"
#include "wide-string.hpp"
#include <nlohmann/json.hpp>
#include <obs.hpp>
#include <util/threading.h>
#include <algorithm>
#ifdef ENABLE_BROWSER_QT_LOOP
#include <QApplication>
#endif
#include <util/dstr.h>
#include <functional>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <map>
#include <set>
#include <thread>
#include <mutex>
#include <utility>

#ifdef __linux__
#include "linux-keyboard-helpers.hpp"
#endif

#ifdef ENABLE_BROWSER_QT_LOOP
#include <QEventLoop>
#include <QThread>
#endif

#if !defined(_WIN32) && !defined(__APPLE__)
#include "drm-format.hpp"
#endif

using namespace std;

extern bool QueueCEFTask(std::function<void()> task);
#ifdef ENABLE_BROWSER_QT_LOOP
extern MessageObject messageObject;
#endif

static mutex browser_list_mutex;
static BrowserSource *first_browser = nullptr;
static std::atomic<uint64_t> next_source_generation{1};

/*
 * A BrowserSource can disappear before CEF has finished destroying its
 * browser.  Keep the shutdown barrier keyed by CEF's browser identifier so
 * module unload never guesses that a queued CloseBrowser() has completed.
 * All callbacks below run on the CEF UI thread, while BeginShutdown is called
 * by the module unload thread; the small mutex protects that seam.
 */
static mutex browser_lifecycle_mutex;
static set<int> live_browser_ids;
static map<int, BrowserSource *> browser_sources_by_id;
static map<int, BrowserSource *> pending_browser_sources;
static set<int> browser_close_observed_ids;
static set<int> browser_close_requested_ids;
static std::size_t pending_source_destructions = 0;
static std::atomic<std::size_t> active_source_tasks{0};
static deque<int> shutdown_close_ids;
static deque<int> source_destroy_close_ids;
static int shutdown_close_in_flight = -1;
static int source_destroy_close_in_flight = -1;
static bool shutdown_close_request_active = false;
static bool source_destroy_close_request_active = false;
static bool shutdown_close_next_task_posted = false;
static bool source_destroy_close_next_task_posted = false;
static bool shutdown_close_sequence_started = false;
static set<BrowserSource *> destroying_sources;
enum class BrowserLifecyclePhase : int {
	Running,
	Closing,
	Drained,
};

static BrowserLifecyclePhase browser_lifecycle_state = BrowserLifecyclePhase::Running;

static void BrowserSourceScheduleNextClose();
static void BrowserSourceScheduleNextDestroyClose();

[[noreturn]] static void BrowserSourceDestroyFatal(const char *reason)
{
	std::fprintf(stderr,
		     "PULSAR_CEF_SHUTDOWN event=source_destroy_failed reason=%s action=exit_nonzero\n",
		     reason);
	std::fflush(stderr);
	std::_Exit(EXIT_FAILURE);
}

static bool BrowserSourceAcquireTask(const std::shared_ptr<BrowserSourceTaskState> &state,
					     bool allow_destroying)
{
	if (!state)
		return false;

	lock_guard<mutex> lifecycle_lock(browser_lifecycle_mutex);
	if (!state->acquire(allow_destroying))
		return false;
	active_source_tasks.fetch_add(1, std::memory_order_release);
	return true;
}

static BrowserSource *BrowserSourceTaskSource(const std::shared_ptr<BrowserSourceTaskState> &state)
{
	if (!state)
		return nullptr;
	return state->current_source();
}

static bool BrowserSourceBeginDestroyTaskState(const std::shared_ptr<BrowserSourceTaskState> &state)
{
	if (!state)
		return false;

	lock_guard<mutex> lifecycle_lock(browser_lifecycle_mutex);
	return state->begin_destroy();
}

static BrowserSourceTaskRelease BrowserSourceReleaseTask(const std::shared_ptr<BrowserSourceTaskState> &state)
{
	if (!state)
		BrowserSourceDestroyFatal("missing_source_task_state");

	lock_guard<mutex> lifecycle_lock(browser_lifecycle_mutex);
	BrowserSourceTaskRelease release = state->release_task();
	if (release.underflow)
		BrowserSourceDestroyFatal("source_task_underflow");
	const auto active_tasks = active_source_tasks.load(std::memory_order_relaxed);
	if (active_tasks == 0)
		BrowserSourceDestroyFatal("source_task_global_underflow");
	active_source_tasks.fetch_sub(1, std::memory_order_release);
	return release;
}

static BrowserSourceTaskRelease BrowserSourceRequestDeleteReleaseOwnLease(
	const std::shared_ptr<BrowserSourceTaskState> &state, bool completion_required)
{
	if (!state)
		BrowserSourceDestroyFatal("missing_source_task_state");

	lock_guard<mutex> lifecycle_lock(browser_lifecycle_mutex);
	BrowserSourceTaskRelease release = state->request_delete_release_own_lease(completion_required);
	if (release.underflow)
		BrowserSourceDestroyFatal("source_task_underflow");
	const auto active_tasks = active_source_tasks.load(std::memory_order_relaxed);
	if (active_tasks == 0)
		BrowserSourceDestroyFatal("source_task_global_underflow");
	active_source_tasks.fetch_sub(1, std::memory_order_release);
	return release;
}

static BrowserSourceTaskRelease BrowserSourceRequestDeleteIfIdle(
	const std::shared_ptr<BrowserSourceTaskState> &state, bool completion_required)
{
	if (!state)
		BrowserSourceDestroyFatal("missing_source_task_state");

	return state->request_delete_and_take_if_idle(completion_required);
}

static void BrowserSourceCompleteTaskRelease(BrowserSourceTaskRelease release)
{
	if (!release.source)
		return;
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		destroying_sources.erase(release.source);
	}
	delete release.source;
	if (release.complete_destroy_task)
		BrowserSourceDestroyTaskComplete();
}

static void SendBrowserVisibility(CefRefPtr<CefBrowser> browser, bool isVisible)
{
	if (!browser)
		return;

	if (isVisible) {
		browser->GetHost()->WasResized();
		browser->GetHost()->WasHidden(false);
		browser->GetHost()->Invalidate(PET_VIEW);
	} else {
		browser->GetHost()->WasHidden(true);
	}

	CefRefPtr<CefProcessMessage> msg = CefProcessMessage::Create("Visibility");
	CefRefPtr<CefListValue> args = msg->GetArgumentList();
	args->SetBool(0, isVisible);
	SendBrowserProcessMessage(browser, PID_RENDERER, msg);
}

void DispatchJSEvent(std::string eventName, std::string jsonString, BrowserSource *browser = nullptr);

BrowserSource::BrowserSource(obs_data_t *, obs_source_t *source_)
	: source(source_),
	  weak_source(obs_source_get_weak_source(source_)),
	  source_generation(next_source_generation.fetch_add(1, std::memory_order_relaxed)),
	  task_state(std::make_shared<BrowserSourceTaskState>())
{
	{
		lock_guard<mutex> lock(task_state->mutex);
		task_state->source = this;
	}
	blog(LOG_INFO,
	     "PULSAR_CEF_SHUTDOWN event=source_created generation=%llu",
	     static_cast<unsigned long long>(source_generation));

	/* Register Refresh hotkey */
	auto refreshFunction = [](void *data, obs_hotkey_id, obs_hotkey_t *, bool pressed) {
		if (pressed) {
			BrowserSource *bs = (BrowserSource *)data;
			bs->Refresh();
		}
	};

	obs_hotkey_register_source(source, "ObsBrowser.Refresh", obs_module_text("RefreshNoCache"), refreshFunction,
				   (void *)this);

	auto jsEventFunction = [](void *p, calldata_t *calldata) {
		const auto eventName = calldata_string(calldata, "eventName");
		if (!eventName)
			return;
		auto jsonString = calldata_string(calldata, "jsonString");
		if (!jsonString)
			jsonString = "null";
		DispatchJSEvent(eventName, jsonString, (BrowserSource *)p);
	};

	proc_handler_t *ph = obs_source_get_proc_handler(source);
	proc_handler_add(ph, "void javascript_event(string eventName, string jsonString)", jsEventFunction,
			 (void *)this);

	/* defer update */
	obs_source_update(source, nullptr);

	lock_guard<mutex> lock(browser_list_mutex);
	p_prev_next = &first_browser;
	next = first_browser;
	if (first_browser)
		first_browser->p_prev_next = &next;
	first_browser = this;
}

static void ActuallyCloseBrowser(CefRefPtr<CefBrowserHost> browser_host)
{
	if (!browser_host)
		return;

	/*
         * This stops rendering
         * http://magpcss.org/ceforum/viewtopic.php?f=6&t=12079
         * https://bitbucket.org/chromiumembedded/cef/issues/1363/washidden-api-got-broken-on-branch-2062)
         */
	browser_host->WasHidden(true);
	browser_host->CloseBrowser(true);
}

void BrowserSourceBeginShutdown()
{
	std::size_t browser_count;
	std::size_t pending_source_tasks;
	std::size_t active_tasks;
	bool already_started;
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		already_started = browser_lifecycle_state != BrowserLifecyclePhase::Running;
		if (!already_started)
			browser_lifecycle_state = BrowserLifecyclePhase::Closing;
		browser_count = live_browser_ids.size();
		pending_source_tasks = pending_source_destructions;
		active_tasks = active_source_tasks.load(std::memory_order_acquire);
	}

	blog(LOG_INFO,
	     "PULSAR_CEF_SHUTDOWN event=begin phase=Closing browser_count=%llu "
	     "pending_source_tasks=%llu active_source_tasks=%llu already_started=%d",
	     static_cast<unsigned long long>(browser_count),
	     static_cast<unsigned long long>(pending_source_tasks),
	     static_cast<unsigned long long>(active_tasks), already_started ? 1 : 0);
}

bool BrowserSourceCanCreateBrowser()
{
	lock_guard<mutex> lock(browser_lifecycle_mutex);
	if (browser_lifecycle_state == BrowserLifecyclePhase::Running)
		return true;

	blog(LOG_WARNING, "PULSAR_CEF_SHUTDOWN event=create_rejected reason=shutdown_started");
	return false;
}

void BrowserSourceBrowserCreated(CefRefPtr<CefBrowser> browser, BrowserSource *source)
{
	if (!browser)
		return;

	const int browser_id = browser->GetIdentifier();
	bool close_immediately;
	bool close_for_destroying_source = false;
	std::size_t browser_count;
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		live_browser_ids.insert(browser_id);
		browser_sources_by_id[browser_id] = source;
		close_for_destroying_source = source && destroying_sources.find(source) != destroying_sources.end();
		close_immediately = browser_lifecycle_state != BrowserLifecyclePhase::Running ||
			close_for_destroying_source;
		if (close_for_destroying_source) {
			if (!source->task_state || !source->task_state->add_browser_id(browser_id))
				BrowserSourceDestroyFatal("browser_destroy_state_not_armed");
			auto [pending_it, inserted] = pending_browser_sources.emplace(browser_id, source);
			if (!inserted && pending_it->second != source)
				BrowserSourceDestroyFatal("pending_browser_source_mismatch");
		}
		browser_count = live_browser_ids.size();
	}

	blog(LOG_INFO,
	     "PULSAR_CEF_SHUTDOWN event=browser_created browser_id=%d generation=%llu browser_count=%llu",
	     browser_id, source ? static_cast<unsigned long long>(source->source_generation) : 0ULL,
	     static_cast<unsigned long long>(browser_count));

	/*
	 * A CreateBrowser task can already be executing when unload flips the
	 * shutdown bit. Count this real browser first, then enqueue it behind the
	 * active close sequence; the barrier cannot observe a false zero and no
	 * second CloseBrowser call can race the current OnBeforeClose callback.
	 */
	if (close_for_destroying_source) {
		blog(LOG_WARNING,
		     "PULSAR_CEF_SHUTDOWN event=browser_close_enqueued browser_id=%d reason=source_destroying",
		     browser_id);
		BrowserSourceEnqueueDestroyClose(browser_id);
	} else if (close_immediately) {
		bool enqueued = false;
		{
			lock_guard<mutex> lock(browser_lifecycle_mutex);
			if (shutdown_close_sequence_started && shutdown_close_in_flight != browser_id &&
			    std::find(shutdown_close_ids.begin(), shutdown_close_ids.end(), browser_id) ==
				    shutdown_close_ids.end()) {
				shutdown_close_ids.push_back(browser_id);
				enqueued = true;
			}
		}
		if (enqueued) {
			blog(LOG_WARNING,
			     "PULSAR_CEF_SHUTDOWN event=browser_close_enqueued browser_id=%d reason=shutdown_started",
			     browser_id);
			BrowserSourceScheduleNextClose();
		}
	}
}

void BrowserSourceBrowserClosed(int browser_id)
{
	if (browser_id < 0)
		return;

	BrowserSource *source = nullptr;
	std::size_t browser_count;
	bool known_browser;
	bool duplicate;
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		known_browser = live_browser_ids.find(browser_id) != live_browser_ids.end();
		duplicate = known_browser && !browser_close_observed_ids.insert(browser_id).second;
		if (known_browser) {
			auto source_it = browser_sources_by_id.find(browser_id);
			if (source_it != browser_sources_by_id.end())
				source = source_it->second;
		}
		browser_count = live_browser_ids.size();
	}

	if (!known_browser || duplicate) {
		blog(LOG_WARNING,
		     "PULSAR_CEF_SHUTDOWN event=browser_close_duplicate browser_id=%d browser_count=%llu",
		     browser_id, static_cast<unsigned long long>(browser_count));
		return;
	}

	/*
	 * Detach immediately on the CEF UI callback, but outside the lifecycle
	 * mutex.  Destroy() takes the source lock before the lifecycle lock; doing
	 * this in the opposite order would create a lock inversion.  The live/id
	 * entry pins the BrowserSource until finalization.
	 */
	if (source) {
		source->DetachBrowser(browser_id);
		blog(LOG_INFO,
		     "PULSAR_CEF_SHUTDOWN event=browser_detached browser_id=%d",
		     browser_id);
	}

	blog(LOG_INFO,
	     "PULSAR_CEF_SHUTDOWN event=browser_close_observed browser_id=%d browser_count=%llu",
	     browser_id, static_cast<unsigned long long>(browser_count));
}

bool BrowserSourceRequestBrowserClose(int browser_id, CefRefPtr<CefBrowserHost> browser_host)
{
	if (browser_id < 0 || !browser_host)
		return false;

	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		if (browser_sources_by_id.find(browser_id) == browser_sources_by_id.end() ||
		    browser_close_observed_ids.find(browser_id) != browser_close_observed_ids.end())
			return false;
		/* CloseBrowser can be requested by both restart and shutdown paths. */
		if (!browser_close_requested_ids.insert(browser_id).second)
			return false;
	}

	ActuallyCloseBrowser(browser_host);
	return true;
}

static CefRefPtr<CefBrowser> BrowserSourceFindBrowser(int browser_id)
{
	{
		lock_guard<mutex> lock(browser_list_mutex);
		for (BrowserSource *source = first_browser; source; source = source->next) {
			CefRefPtr<CefBrowser> browser = source->GetBrowser();
			if (browser && browser->GetIdentifier() == browser_id)
				return browser;
		}
	}
	/*
	 * A source can be unlinked after Destroy() while its browser remains live
	 * in the lifecycle map.  The lifecycle lock pins that map entry while the
	 * source supplies one browser ref; no raw source pointer escapes this block.
	 */
	lock_guard<mutex> lifecycle_lock(browser_lifecycle_mutex);
	const auto source_it = browser_sources_by_id.find(browser_id);
	if (source_it != browser_sources_by_id.end() && source_it->second)
		return source_it->second->GetBrowser();
	return nullptr;
}

static void BrowserSourceHandleMissingCloseBrowser(int browser_id)
{
	bool retry = false;
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		const bool live = live_browser_ids.find(browser_id) != live_browser_ids.end();
		const bool mapped = browser_sources_by_id.find(browser_id) != browser_sources_by_id.end();
		if (live && mapped) {
			/* A prior close may already be in CEF; wait for its finalization. */
			shutdown_close_in_flight = browser_id;
			blog(LOG_ERROR,
			     "PULSAR_CEF_SHUTDOWN event=browser_close_deferred browser_id=%d "
			     "reason=browser_ref_missing action=await_finalization",
			     browser_id);
			return;
		}
		retry = live || mapped;
	}
	if (retry)
		BrowserSourceDestroyFatal("close_browser_lookup_failed");
	BrowserSourceScheduleNextClose();
}

static void BrowserSourceRequestNextCloseOnUi()
{
	int browser_id = -1;
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		shutdown_close_next_task_posted = false;
		if (!shutdown_close_sequence_started || shutdown_close_in_flight >= 0 ||
		    shutdown_close_request_active || shutdown_close_ids.empty() ||
		    source_destroy_close_in_flight >= 0 || source_destroy_close_request_active ||
		    source_destroy_close_next_task_posted)
			return;
		browser_id = shutdown_close_ids.front();
		shutdown_close_ids.pop_front();
	}

	/* Resolve one browser by stable ID, retaining no batch of CefRefPtr objects. */
	CefRefPtr<CefBrowser> browser = BrowserSourceFindBrowser(browser_id);
	if (!browser) {
		BrowserSourceHandleMissingCloseBrowser(browser_id);
		return;
	}
	CefRefPtr<CefBrowserHost> browser_host = browser->GetHost();
	browser = nullptr;
	if (!browser_host) {
		BrowserSourceHandleMissingCloseBrowser(browser_id);
		return;
	}

	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		if (!shutdown_close_sequence_started || shutdown_close_in_flight >= 0 ||
		    shutdown_close_request_active || source_destroy_close_in_flight >= 0 ||
		    source_destroy_close_request_active || source_destroy_close_next_task_posted) {
			shutdown_close_ids.push_front(browser_id);
			return;
		}
		shutdown_close_in_flight = browser_id;
		shutdown_close_request_active = true;
	}

	blog(LOG_INFO,
	     "PULSAR_CEF_SHUTDOWN event=browser_close_call browser_id=%d reason=shutdown_started",
	     browser_id);
	blog(LOG_INFO,
	     "PULSAR_CEF_SHUTDOWN event=browser_ref_released_before_close browser_id=%d",
	     browser_id);
	const bool requested = BrowserSourceRequestBrowserClose(browser_id, browser_host);
	browser_host = nullptr;
	blog(LOG_INFO,
	     "PULSAR_CEF_SHUTDOWN event=browser_close_return browser_id=%d requested=%d",
	     browser_id, requested ? 1 : 0);
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		shutdown_close_request_active = false;
		if (!requested && shutdown_close_in_flight == browser_id &&
		    browser_close_observed_ids.find(browser_id) == browser_close_observed_ids.end())
			shutdown_close_in_flight = -1;
	}
	/* A synchronous OnBeforeClose/finalization may have freed the turn. */
	BrowserSourceScheduleNextClose();
}

static void BrowserSourceScheduleNextClose()
{
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		if (!shutdown_close_sequence_started || shutdown_close_in_flight >= 0 ||
		    shutdown_close_request_active || shutdown_close_ids.empty() ||
		    source_destroy_close_in_flight >= 0 || source_destroy_close_request_active ||
		    source_destroy_close_next_task_posted ||
		    shutdown_close_next_task_posted)
			return;
		shutdown_close_next_task_posted = true;
	}

	if (!QueueCEFTask([]() { BrowserSourceRequestNextCloseOnUi(); })) {
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		shutdown_close_next_task_posted = false;
		BrowserSourceDestroyFatal("close_next_task_post_failed");
	}
}

static void BrowserSourceRequestNextDestroyCloseOnUi()
{
	int browser_id = -1;
	bool resume_global_shutdown = false;
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		source_destroy_close_next_task_posted = false;
		/* The global shutdown sequence owns close serialization once started. */
		if (shutdown_close_sequence_started) {
			resume_global_shutdown = true;
		} else if (source_destroy_close_in_flight >= 0 ||
		    source_destroy_close_request_active || source_destroy_close_ids.empty())
			return;
		else {
			browser_id = source_destroy_close_ids.front();
			source_destroy_close_ids.pop_front();
			source_destroy_close_in_flight = browser_id;
			source_destroy_close_request_active = true;
		}
	}
	if (resume_global_shutdown) {
		BrowserSourceScheduleNextClose();
		return;
	}

	/* Resolve one browser by ID; never retain a batch across CloseBrowser. */
	CefRefPtr<CefBrowser> browser = BrowserSourceFindBrowser(browser_id);
	if (!browser) {
		bool live = false;
		bool mapped = false;
		{
			lock_guard<mutex> lock(browser_lifecycle_mutex);
			live = live_browser_ids.find(browser_id) != live_browser_ids.end();
			mapped = browser_sources_by_id.find(browser_id) != browser_sources_by_id.end();
			if (live && mapped) {
				/* Restart may already have posted a close for this browser. */
				source_destroy_close_request_active = false;
			}
		}
		if (!live || !mapped)
			BrowserSourceDestroyFatal("source_close_browser_lookup_failed");
		blog(LOG_ERROR,
		     "PULSAR_CEF_SHUTDOWN event=browser_close_deferred browser_id=%d "
		     "reason=browser_ref_missing action=await_finalization",
		     browser_id);
		return;
	}
	CefRefPtr<CefBrowserHost> browser_host = browser->GetHost();
	browser = nullptr;
	if (!browser_host)
		BrowserSourceDestroyFatal("source_close_host_missing");

	blog(LOG_INFO,
	     "PULSAR_CEF_SHUTDOWN event=browser_close_call browser_id=%d reason=source_destroy",
	     browser_id);
	blog(LOG_INFO,
	     "PULSAR_CEF_SHUTDOWN event=browser_ref_released_before_close browser_id=%d",
	     browser_id);
	const bool requested = BrowserSourceRequestBrowserClose(browser_id, browser_host);
	browser_host = nullptr;
	blog(LOG_INFO,
	     "PULSAR_CEF_SHUTDOWN event=browser_close_return browser_id=%d requested=%d",
	     browser_id, requested ? 1 : 0);
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		source_destroy_close_request_active = false;
		/* Keep the in-flight ID until OnBeforeClose/finalization. */
	}
}

static void BrowserSourceScheduleNextDestroyClose()
{
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		if (shutdown_close_sequence_started || source_destroy_close_in_flight >= 0 ||
		    source_destroy_close_request_active || source_destroy_close_ids.empty() ||
		    source_destroy_close_next_task_posted)
			return;
		source_destroy_close_next_task_posted = true;
	}

	if (!QueueCEFTask([]() { BrowserSourceRequestNextDestroyCloseOnUi(); })) {
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		source_destroy_close_next_task_posted = false;
		BrowserSourceDestroyFatal("source_close_next_task_post_failed");
	}
}

void BrowserSourceEnqueueDestroyClose(int browser_id)
{
	if (browser_id < 0)
		return;

	bool schedule = false;
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		if (shutdown_close_sequence_started)
			return;
		if (std::find(source_destroy_close_ids.begin(), source_destroy_close_ids.end(), browser_id) ==
		    source_destroy_close_ids.end() &&
		    source_destroy_close_in_flight != browser_id)
			source_destroy_close_ids.push_back(browser_id);
		schedule = true;
	}
	if (schedule)
		BrowserSourceScheduleNextDestroyClose();
}

void BrowserSourceFinalizeBrowserClose(int browser_id)
{
	if (browser_id < 0)
		return;

	BrowserSource *source = nullptr;
	bool pending_destroy = false;
	bool known_browser = false;
	bool source_has_remaining_browser = false;
	bool source_delete_ready = false;
	std::size_t browser_count;
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		auto source_it = browser_sources_by_id.find(browser_id);
		if (source_it != browser_sources_by_id.end()) {
			source = source_it->second;
			browser_sources_by_id.erase(source_it);
		}
		auto pending_it = pending_browser_sources.find(browser_id);
		if (pending_it != pending_browser_sources.end()) {
			pending_destroy = true;
			if (source && pending_it->second != source)
				BrowserSourceDestroyFatal("pending_browser_source_mismatch");
			if (!source)
				source = pending_it->second;
			pending_browser_sources.erase(pending_it);
		}
		known_browser = live_browser_ids.find(browser_id) != live_browser_ids.end();
		browser_close_requested_ids.erase(browser_id);
		if (!known_browser && !source) {
			browser_count = live_browser_ids.size();
		} else {
			live_browser_ids.erase(browser_id);
			browser_close_observed_ids.erase(browser_id);
			browser_count = live_browser_ids.size();
		}
		if (source) {
			for (const auto &entry : browser_sources_by_id) {
				if (entry.second == source) {
					source_has_remaining_browser = true;
					break;
				}
			}
			if (!source_has_remaining_browser) {
				for (const auto &entry : pending_browser_sources) {
					if (entry.second == source) {
						source_has_remaining_browser = true;
						break;
					}
				}
			}
		}
		source_destroy_close_ids.erase(
			std::remove(source_destroy_close_ids.begin(), source_destroy_close_ids.end(), browser_id),
			source_destroy_close_ids.end());
		if (source_destroy_close_in_flight == browser_id)
			source_destroy_close_in_flight = -1;

		/* BrowserSourceBrowserCreated uses the same lifecycle -> task-state
		 * lock order.  Decide whether this was the last ID while both locks are
		 * held, so an OnAfterCreated callback cannot add a new ID between the
		 * map scan and the per-source completion claim. */
		if (source && pending_destroy) {
			if (!source->task_state)
				BrowserSourceDestroyFatal("browser_destroy_state_missing");
			const bool browser_delete_completed = source->task_state->finalize_browser_id(browser_id);
			if (source_has_remaining_browser == browser_delete_completed)
				BrowserSourceDestroyFatal("browser_destroy_state_map_mismatch");
			source_delete_ready = browser_delete_completed;
		}
	}

	if (!known_browser && !source) {
		blog(LOG_WARNING,
		     "PULSAR_CEF_SHUTDOWN event=browser_finalize_duplicate browser_id=%d browser_count=%llu",
		     browser_id, static_cast<unsigned long long>(browser_count));
		return;
	}

	if (source && pending_destroy && source_delete_ready) {
		source->UnlinkFromBrowserList();
		const auto task_state = source->task_state;
		BrowserSourceCompleteTaskRelease(BrowserSourceRequestDeleteIfIdle(task_state, true));
	}

	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		if (shutdown_close_in_flight == browser_id)
			shutdown_close_in_flight = -1;
	}
	BrowserSourceScheduleNextClose();
	BrowserSourceScheduleNextDestroyClose();

	blog(LOG_INFO,
	     "PULSAR_CEF_SHUTDOWN event=browser_closed browser_id=%d browser_count=%llu",
	     browser_id, static_cast<unsigned long long>(browser_count));
}

std::vector<int> BrowserSourceBrowserIdsForSource(BrowserSource *source)
{
	std::vector<int> ids;
	if (!source)
		return ids;

	lock_guard<mutex> lock(browser_lifecycle_mutex);
	for (const auto &[browser_id, mapped_source] : browser_sources_by_id) {
		if (mapped_source == source)
			ids.push_back(browser_id);
	}
	for (const auto &[browser_id, mapped_source] : pending_browser_sources) {
		if (mapped_source == source && std::find(ids.begin(), ids.end(), browser_id) == ids.end())
			ids.push_back(browser_id);
	}
	std::sort(ids.begin(), ids.end());
	return ids;
}

std::size_t BrowserSourceLiveBrowserCount()
{
	lock_guard<mutex> lock(browser_lifecycle_mutex);
	return live_browser_ids.size();
}

bool BrowserSourceShutdownStarted()
{
	lock_guard<mutex> lock(browser_lifecycle_mutex);
	return browser_lifecycle_state != BrowserLifecyclePhase::Running;
}

bool BrowserSourceShutdownComplete()
{
	lock_guard<mutex> lock(browser_lifecycle_mutex);
	return browser_lifecycle_state == BrowserLifecyclePhase::Closing && live_browser_ids.empty() &&
	       browser_sources_by_id.empty() && pending_browser_sources.empty() &&
	       browser_close_observed_ids.empty() && pending_source_destructions == 0 &&
	       active_source_tasks.load(std::memory_order_acquire) == 0 &&
	       source_destroy_close_ids.empty() && source_destroy_close_in_flight < 0 &&
	       !source_destroy_close_request_active && !source_destroy_close_next_task_posted;
}

bool BrowserSourceMarkDrained()
{
	lock_guard<mutex> lock(browser_lifecycle_mutex);
	if (browser_lifecycle_state == BrowserLifecyclePhase::Drained)
		return true;
	if (browser_lifecycle_state != BrowserLifecyclePhase::Closing || !live_browser_ids.empty() ||
	    !browser_sources_by_id.empty() || !pending_browser_sources.empty() ||
	    !browser_close_observed_ids.empty() || pending_source_destructions != 0 ||
	    active_source_tasks.load(std::memory_order_acquire) != 0 ||
	    !source_destroy_close_ids.empty() || source_destroy_close_in_flight >= 0 ||
	    source_destroy_close_request_active || source_destroy_close_next_task_posted)
		return false;

	browser_lifecycle_state = BrowserLifecyclePhase::Drained;
	return true;
}

BrowserSourceDestroyDisposition BrowserSourcePrepareDestroy(BrowserSource *source,
									     std::vector<int> *browser_ids)
{
	if (!source || !browser_ids)
		return BrowserSourceDestroyDisposition::Fatal;
	browser_ids->clear();
	lock_guard<mutex> lock(browser_lifecycle_mutex);
	switch (browser_lifecycle_state) {
	case BrowserLifecyclePhase::Running:
	case BrowserLifecyclePhase::Closing:
		if (!source->task_state || !source->task_state->arm_browser_ids(std::vector<int>{}))
			return BrowserSourceDestroyDisposition::Fatal;
		destroying_sources.insert(source);
		for (const auto &[browser_id, mapped_source] : browser_sources_by_id) {
			if (mapped_source != source)
				continue;
			if (live_browser_ids.find(browser_id) == live_browser_ids.end())
				return BrowserSourceDestroyDisposition::Fatal;
			browser_ids->push_back(browser_id);
			if (!source->task_state->add_browser_id(browser_id))
				return BrowserSourceDestroyDisposition::Fatal;
			auto [pending_it, inserted] = pending_browser_sources.emplace(browser_id, source);
			if (!inserted && pending_it->second != source)
				return BrowserSourceDestroyDisposition::Fatal;
		}
		for (const auto &[browser_id, mapped_source] : pending_browser_sources) {
			if (mapped_source == source &&
			    std::find(browser_ids->begin(), browser_ids->end(), browser_id) == browser_ids->end()) {
				browser_ids->push_back(browser_id);
				if (!source->task_state->add_browser_id(browser_id))
					return BrowserSourceDestroyDisposition::Fatal;
			}
		}
		std::sort(browser_ids->begin(), browser_ids->end());
		++pending_source_destructions;
		return BrowserSourceDestroyDisposition::QueueOnCefUi;
	case BrowserLifecyclePhase::Drained:
		return BrowserSourceDestroyDisposition::DeleteNow;
	}

	return BrowserSourceDestroyDisposition::Fatal;
}

void BrowserSourceDestroyTaskComplete()
{
	bool underflow = false;
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		if (pending_source_destructions == 0)
			underflow = true;
		else
			--pending_source_destructions;
	}

	if (underflow)
		BrowserSourceDestroyFatal("source_task_underflow");
}

void BrowserSourceCloseAllBrowsers()
{
	const std::size_t browser_count = BrowserSourceLiveBrowserCount();
	std::deque<int> close_ids;
	/* Open admission before the source snapshot so a concurrent creation is
	 * enqueued by BrowserSourceBrowserCreated instead of falling through the
	 * gap between discovery and sequence activation. */
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		shutdown_close_sequence_started = true;
		/* The lifecycle map also contains sources unlinked during restart/remove. */
		std::set<int> close_id_set(live_browser_ids.begin(), live_browser_ids.end());
		for (const int browser_id : live_browser_ids)
			close_ids.push_back(browser_id);
		/* A source-local close queue must be transferred to the global owner.
		 * Otherwise a queued ID left behind when shutdown starts would keep the
		 * barrier permanently non-drained even though the global sequence closes
		 * the same live browser.  An already in-flight local ID remains owned by
		 * its current CEF turn and is released by BrowserSourceFinalizeBrowserClose.
		 */
		for (const int browser_id : source_destroy_close_ids) {
			if (close_id_set.insert(browser_id).second)
				close_ids.push_back(browser_id);
		}
		source_destroy_close_ids.clear();
	}
	/*
	 * Keep only stable browser IDs, never a batch of owning CefRefPtr objects.
	 * Each ID is resolved and closed on its own CEF turn after the preceding
	 * browser has reached BrowserSourceFinalizeBrowserClose().
	 */
	std::size_t source_count;
	{
		lock_guard<mutex> lock(browser_lifecycle_mutex);
		std::set<int> queued_ids(shutdown_close_ids.begin(), shutdown_close_ids.end());
		for (const int browser_id : close_ids) {
			if (queued_ids.insert(browser_id).second)
				shutdown_close_ids.push_back(browser_id);
		}
		source_count = shutdown_close_ids.size();
	}

	blog(LOG_INFO,
	     "PULSAR_CEF_SHUTDOWN event=close_requested browser_count=%llu source_count=%llu",
	     static_cast<unsigned long long>(browser_count),
	     static_cast<unsigned long long>(source_count));
	BrowserSourceScheduleNextClose();
}

BrowserSource::~BrowserSource()
{
	if (cefBrowser && !BrowserSourceShutdownStarted()) {
		CefRefPtr<CefBrowserHost> browser_host = cefBrowser->GetHost();
		cefBrowser = nullptr;
		ActuallyCloseBrowser(browser_host);
	} else if (cefBrowser)
		BrowserSourceDestroyFatal("destructor_with_live_browser");
	if (weak_source) {
		obs_weak_source_release(weak_source);
		weak_source = nullptr;
	}
	blog(LOG_INFO, "PULSAR_CEF_SHUTDOWN event=source_destroyed generation=%llu",
	     static_cast<unsigned long long>(source_generation));
}

void BrowserSource::UnlinkFromBrowserList()
{
	lock_guard<mutex> lock(browser_list_mutex);
	if (!p_prev_next)
		return;
	if (next)
		next->p_prev_next = p_prev_next;
	*p_prev_next = next;
	p_prev_next = nullptr;
	next = nullptr;
}

void BrowserSource::DetachBrowser(int browser_id)
{
	std::lock_guard<std::recursive_mutex> auto_lock(lockBrowser);
	if (cefBrowser && cefBrowser->GetIdentifier() == browser_id)
		cefBrowser = nullptr;
}

void BrowserSource::Destroy()
{
	if (!BrowserSourceBeginDestroyTaskState(task_state))
		return;
	destroying.store(true, std::memory_order_release);
	DestroyTextures();

	std::vector<int> browser_ids = BrowserSourceBrowserIdsForSource(this);
	if (browser_ids.empty() && !BrowserSourceCefReady()) {
		blog(LOG_WARNING,
		     "PULSAR_CEF_SHUTDOWN event=source_destroy_rejected reason=cef_not_ready");
		SetBrowser(nullptr);
		UnlinkFromBrowserList();
		BrowserSourceCompleteTaskRelease(BrowserSourceRequestDeleteIfIdle(task_state, true));
		return;
	}

	const BrowserSourceDestroyDisposition disposition =
		BrowserSourcePrepareDestroy(this, &browser_ids);
	if (disposition == BrowserSourceDestroyDisposition::DeleteNow) {
		UnlinkFromBrowserList();
		SetBrowser(nullptr);
		BrowserSourceCompleteTaskRelease(BrowserSourceRequestDeleteIfIdle(task_state, true));
		return;
	}
	if (disposition == BrowserSourceDestroyDisposition::Fatal)
		BrowserSourceDestroyFatal("invalid_lifecycle_phase");

	if (browser_ids.empty()) {
		/* No browser remains; the source task state owns the final delete. */
		SetBrowser(nullptr);
		UnlinkFromBrowserList();
		BrowserSourceCompleteTaskRelease(BrowserSourceRequestDeleteIfIdle(task_state, true));
		return;
	}

	/* Every browser ID owned by this source must close before the source can die. */
	for (const int browser_id : browser_ids)
		BrowserSourceEnqueueDestroyClose(browser_id);
}

void BrowserSource::ExecuteOnBrowser(BrowserFunc func, bool async)
{
	if (!async) {
#ifdef ENABLE_BROWSER_QT_LOOP
		if (QThread::currentThread() == qApp->thread()) {
			if (!BrowserSourceAcquireTask(task_state, false))
				return;
			CefRefPtr<CefBrowser> browser = GetBrowser();
			if (browser)
				func(browser);
			BrowserSourceCompleteTaskRelease(BrowserSourceReleaseTask(task_state));
			return;
		}
#endif
		os_event_t *finishedEvent;
		os_event_init(&finishedEvent, OS_EVENT_TYPE_AUTO);
		if (!BrowserSourceAcquireTask(task_state, false)) {
			os_event_destroy(finishedEvent);
			return;
		}
		const auto task_state_for_callback = task_state;
		auto task = [task_state_for_callback, func, finishedEvent]() {
			BrowserSource *source = BrowserSourceTaskSource(task_state_for_callback);
			if (source) {
				CefRefPtr<CefBrowser> browser = source->GetBrowser();
				if (browser)
					func(browser);
			}
			os_event_signal(finishedEvent);
			BrowserSourceCompleteTaskRelease(BrowserSourceReleaseTask(task_state_for_callback));
		};
		bool success = QueueCEFTask([task]() {
#ifdef ENABLE_BROWSER_QT_LOOP
			QMetaObject::invokeMethod(&messageObject, "ExecuteTask", Qt::QueuedConnection,
						  Q_ARG(MessageTask, task));
#else
			task();
#endif
		});
		if (success) {
			os_event_wait(finishedEvent);
		} else {
			BrowserSourceCompleteTaskRelease(BrowserSourceReleaseTask(task_state));
		}
		os_event_destroy(finishedEvent);
	} else {
		CefRefPtr<CefBrowser> browser = GetBrowser();
		if (!!browser) {
#ifdef ENABLE_BROWSER_QT_LOOP
			QueueBrowserTask(cefBrowser, func);
#else
			QueueCEFTask([browser = std::move(browser), func]() mutable { func(std::move(browser)); });
#endif
		}
	}
}

bool BrowserSource::CreateBrowser()
{
	if (!BrowserSourceCanCreateBrowser()) {
		create_browser = false;
		return true;
	}
	if (!BrowserSourceWaitForCefReady()) {
		create_browser = false;
		blog(LOG_WARNING,
		     "PULSAR_CEF_SHUTDOWN event=source_create_rejected reason=cef_not_ready");
		return true;
	}

	if (!BrowserSourceAcquireTask(task_state, false)) {
		create_browser = false;
		blog(LOG_WARNING,
		     "PULSAR_CEF_SHUTDOWN event=source_create_rejected reason=source_destroying");
		return true;
	}
	const auto create_task_state = task_state;
	const bool posted = QueueCEFTask([create_task_state]() {
		BrowserSource *source = BrowserSourceTaskSource(create_task_state);
		if (!source || !BrowserSourceCanCreateBrowser() || source->destroying.load(std::memory_order_acquire) ||
		    create_task_state->is_destroying()) {
			BrowserSourceCompleteTaskRelease(BrowserSourceReleaseTask(create_task_state));
			return;
		}

#ifdef ENABLE_BROWSER_SHARED_TEXTURE
		if (hwaccel) {
			obs_enter_graphics();
#if defined(__APPLE__) || defined(_WIN32)
			source->tex_sharing_avail = gs_shared_texture_available();
#else
			source->tex_sharing_avail = obs_cef_all_drm_formats_supported();
#endif
			obs_leave_graphics();
		}
#else
		bool hwaccel = false;
#endif

		CefRefPtr<BrowserClient> browserClient = new BrowserClient(
			source, hwaccel && source->tex_sharing_avail, source->reroute_audio, source->webpage_control_level);

		CefWindowInfo windowInfo;
		windowInfo.bounds.width = source->width;
		windowInfo.bounds.height = source->height;
		windowInfo.windowless_rendering_enabled = true;

#ifdef ENABLE_BROWSER_SHARED_TEXTURE
		windowInfo.shared_texture_enabled = hwaccel;
#endif

		CefBrowserSettings cefBrowserSettings;

#ifdef ENABLE_BROWSER_SHARED_TEXTURE
#ifdef BROWSER_EXTERNAL_BEGIN_FRAME_ENABLED
		if (!source->fps_custom) {
			windowInfo.external_begin_frame_enabled = true;
			cefBrowserSettings.windowless_frame_rate = 0;
		} else {
			cefBrowserSettings.windowless_frame_rate = source->fps;
		}
#else
		struct obs_video_info ovi;
		obs_get_video_info(&ovi);
		source->canvas_fps = (double)ovi.fps_num / (double)ovi.fps_den;
		cefBrowserSettings.windowless_frame_rate = (source->fps_custom) ? source->fps : source->canvas_fps;
#endif
#else
		cefBrowserSettings.windowless_frame_rate = source->fps;
#endif

		cefBrowserSettings.default_font_size = 16;
		cefBrowserSettings.default_fixed_font_size = 16;

		auto browser = CefBrowserHost::CreateBrowserSync(windowInfo, browserClient, source->url, cefBrowserSettings,
								 CefRefPtr<CefDictionaryValue>(), nullptr);

		source->SetBrowser(browser);

		if (source->reroute_audio && browser)
			browser->GetHost()->SetAudioMuted(true);
		OBSSourceAutoRelease source_ref = source->GetStrongSource();
		if (source_ref && obs_source_showing(source_ref))
			source->is_showing = true;

		SendBrowserVisibility(browser, source->is_showing);
		BrowserSourceCompleteTaskRelease(BrowserSourceReleaseTask(create_task_state));
	});
	if (!posted) {
		BrowserSourceCompleteTaskRelease(BrowserSourceReleaseTask(task_state));
		return false;
	}
	return true;
}

void BrowserSource::DestroyBrowser()
{
	ExecuteOnBrowser(
		[](CefRefPtr<CefBrowser> browser) {
			const int browser_id = browser ? browser->GetIdentifier() : -1;
			CefRefPtr<CefBrowserHost> browser_host = browser ? browser->GetHost() : nullptr;
			browser = nullptr;
			if (browser_id >= 0)
				BrowserSourceRequestBrowserClose(browser_id, browser_host);
		},
		true);
	SetBrowser(nullptr);
}

void BrowserSource::SendMouseClick(const struct obs_mouse_event *event, int32_t type, bool mouse_up,
				   uint32_t click_count)
{
	uint32_t modifiers = event->modifiers;
	int32_t x = event->x;
	int32_t y = event->y;

	ExecuteOnBrowser(
		[=](CefRefPtr<CefBrowser> cefBrowser) {
			CefMouseEvent e;
			e.modifiers = modifiers;
			e.x = x;
			e.y = y;
			CefBrowserHost::MouseButtonType buttonType = (CefBrowserHost::MouseButtonType)type;
			cefBrowser->GetHost()->SendMouseClickEvent(e, buttonType, mouse_up, click_count);
		},
		true);
}

void BrowserSource::SendMouseMove(const struct obs_mouse_event *event, bool mouse_leave)
{
	uint32_t modifiers = event->modifiers;
	int32_t x = event->x;
	int32_t y = event->y;

	ExecuteOnBrowser(
		[=](CefRefPtr<CefBrowser> cefBrowser) {
			CefMouseEvent e;
			e.modifiers = modifiers;
			e.x = x;
			e.y = y;
			cefBrowser->GetHost()->SendMouseMoveEvent(e, mouse_leave);
		},
		true);
}

void BrowserSource::SendMouseWheel(const struct obs_mouse_event *event, int x_delta, int y_delta)
{
	uint32_t modifiers = event->modifiers;
	int32_t x = event->x;
	int32_t y = event->y;

	ExecuteOnBrowser(
		[=](CefRefPtr<CefBrowser> cefBrowser) {
			CefMouseEvent e;
			e.modifiers = modifiers;
			e.x = x;
			e.y = y;
			cefBrowser->GetHost()->SendMouseWheelEvent(e, x_delta, y_delta);
		},
		true);
}

void BrowserSource::SendFocus(bool focus)
{
	ExecuteOnBrowser([=](CefRefPtr<CefBrowser> cefBrowser) { cefBrowser->GetHost()->SetFocus(focus); }, true);
}

void BrowserSource::SendKeyClick(const struct obs_key_event *event, bool key_up)
{
	if (destroying)
		return;

	std::string text = event->text;
#ifdef __linux__
	uint32_t native_vkey = KeyboardCodeFromXKeysym(event->native_vkey);
	uint32_t modifiers = event->native_modifiers;
#elif defined(_WIN32) || defined(__APPLE__)
	uint32_t native_vkey = event->native_vkey;
	uint32_t modifiers = event->modifiers;
#else
	uint32_t native_vkey = event->native_vkey;
	uint32_t native_scancode = event->native_scancode;
	uint32_t modifiers = event->native_modifiers;
#endif

	ExecuteOnBrowser(
		[=](CefRefPtr<CefBrowser> cefBrowser) {
			CefKeyEvent e;
			e.windows_key_code = native_vkey;
#ifdef __APPLE__
			e.native_key_code = native_vkey;
#endif

			e.type = key_up ? KEYEVENT_KEYUP : KEYEVENT_RAWKEYDOWN;

			if (!text.empty()) {
				wstring wide = to_wide(text);
				if (wide.size())
					e.character = wide[0];
			}

			//e.native_key_code = native_vkey;
			e.modifiers = modifiers;

			cefBrowser->GetHost()->SendKeyEvent(e);
			if (!text.empty() && !key_up) {
				e.type = KEYEVENT_CHAR;
#ifdef __linux__
				e.windows_key_code = KeyboardCodeFromXKeysym(e.character);
#elif defined(_WIN32)
				e.windows_key_code = e.character;
#elif !defined(__APPLE__)
				e.native_key_code = native_scancode;
#endif
				cefBrowser->GetHost()->SendKeyEvent(e);
			}
		},
		true);
}

void BrowserSource::SetShowing(bool showing)
{
	if (destroying)
		return;

	is_showing = showing;

	if (shutdown_on_invisible) {
		if (showing) {
			Update();
		} else {
			DestroyBrowser();
		}
	} else {
		ExecuteOnBrowser(
			[=](CefRefPtr<CefBrowser> cefBrowser) {
				CefRefPtr<CefProcessMessage> msg = CefProcessMessage::Create("Visibility");
				CefRefPtr<CefListValue> args = msg->GetArgumentList();
				args->SetBool(0, showing);
				SendBrowserProcessMessage(cefBrowser, PID_RENDERER, msg);
			},
			true);
		nlohmann::json json;
		json["visible"] = showing;
		DispatchJSEvent("obsSourceVisibleChanged", json.dump(), this);
#if defined(BROWSER_EXTERNAL_BEGIN_FRAME_ENABLED) && defined(ENABLE_BROWSER_SHARED_TEXTURE)
		if (showing && !fps_custom) {
			reset_frame = false;
		}
#endif

		SendBrowserVisibility(cefBrowser, showing);

		if (showing)
			return;

		obs_enter_graphics();

		if (!hwaccel && texture) {
			DestroyTextures();
		}

		obs_leave_graphics();
	}
}

void BrowserSource::SetActive(bool active)
{
	ExecuteOnBrowser(
		[=](CefRefPtr<CefBrowser> cefBrowser) {
			CefRefPtr<CefProcessMessage> msg = CefProcessMessage::Create("Active");
			CefRefPtr<CefListValue> args = msg->GetArgumentList();
			args->SetBool(0, active);
			SendBrowserProcessMessage(cefBrowser, PID_RENDERER, msg);
		},
		true);
	nlohmann::json json;
	json["active"] = active;
	DispatchJSEvent("obsSourceActiveChanged", json.dump(), this);
}

void BrowserSource::Refresh()
{
	ExecuteOnBrowser([](CefRefPtr<CefBrowser> cefBrowser) { cefBrowser->ReloadIgnoreCache(); }, true);
}

void BrowserSource::SetBrowser(CefRefPtr<CefBrowser> b)
{
	std::lock_guard<std::recursive_mutex> auto_lock(lockBrowser);
	cefBrowser = b;
}

CefRefPtr<CefBrowser> BrowserSource::GetBrowser()
{
	std::lock_guard<std::recursive_mutex> auto_lock(lockBrowser);
	return cefBrowser;
}

obs_source_t *BrowserSource::GetStrongSource()
{
	return weak_source ? obs_weak_source_get_source(weak_source) : nullptr;
}

#ifdef ENABLE_BROWSER_SHARED_TEXTURE
#ifdef BROWSER_EXTERNAL_BEGIN_FRAME_ENABLED
inline void BrowserSource::SignalBeginFrame()
{
	if (reset_frame) {
		ExecuteOnBrowser(
			[](CefRefPtr<CefBrowser> cefBrowser) { cefBrowser->GetHost()->SendExternalBeginFrame(); },
			true);

		reset_frame = false;
	}
}
#endif
#endif

void BrowserSource::Update(obs_data_t *settings)
{
	if (settings) {
		bool n_is_local;
		int n_width;
		int n_height;
		bool n_fps_custom;
		int n_fps;
		bool n_shutdown;
		bool n_restart;
		bool n_reroute;
		ControlLevel n_webpage_control_level;
		std::string n_url;
		std::string n_css;

		n_is_local = obs_data_get_bool(settings, "is_local_file");
		n_width = (int)obs_data_get_int(settings, "width");
		n_height = (int)obs_data_get_int(settings, "height");
		n_fps_custom = obs_data_get_bool(settings, "fps_custom");
		n_fps = (int)obs_data_get_int(settings, "fps");
		n_shutdown = obs_data_get_bool(settings, "shutdown");
		n_restart = obs_data_get_bool(settings, "restart_when_active");
		n_css = obs_data_get_string(settings, "css");
		n_url = obs_data_get_string(settings, n_is_local ? "local_file" : "url");
		n_reroute = obs_data_get_bool(settings, "reroute_audio");
		n_webpage_control_level =
			static_cast<ControlLevel>(obs_data_get_int(settings, "webpage_control_level"));

		if (n_is_local && !n_url.empty()) {
			n_url = CefURIEncode(n_url, false);

#ifdef _WIN32
			size_t slash = n_url.find("%2F");
			size_t colon = n_url.find("%3A");

			if (slash != std::string::npos && colon != std::string::npos && colon < slash)
				n_url.replace(colon, 3, ":");
#endif

			while (n_url.find("%5C") != std::string::npos)
				n_url.replace(n_url.find("%5C"), 3, "/");

			while (n_url.find("%2F") != std::string::npos)
				n_url.replace(n_url.find("%2F"), 3, "/");

			// Local files are routed through our custom scheme handler to give them acess to other local files
			n_url = "http://absolute/" + n_url;
		}

		if (n_is_local == is_local && n_fps_custom == fps_custom && n_fps == fps &&
		    n_shutdown == shutdown_on_invisible && n_restart == restart && n_css == css && n_url == url &&
		    n_reroute == reroute_audio && n_webpage_control_level == webpage_control_level) {

			if (n_width == width && n_height == height)
				return;

			width = n_width;
			height = n_height;
			ExecuteOnBrowser(
				[=](CefRefPtr<CefBrowser> cefBrowser) {
					const CefSize cefSize(width, height);
					cefBrowser->GetHost()->GetClient()->GetDisplayHandler()->OnAutoResize(
						cefBrowser, cefSize);
					cefBrowser->GetHost()->WasResized();
					cefBrowser->GetHost()->Invalidate(PET_VIEW);
				},
				true);
			return;
		}

		is_local = n_is_local;
		width = n_width;
		height = n_height;
		fps = n_fps;
		fps_custom = n_fps_custom;
		shutdown_on_invisible = n_shutdown;
		reroute_audio = n_reroute;
		webpage_control_level = n_webpage_control_level;
		restart = n_restart;
		css = n_css;
		url = n_url;

		obs_source_set_audio_active(source, reroute_audio);
	}

	DestroyBrowser();
	DestroyTextures();

	if (!shutdown_on_invisible || obs_source_showing(source))
		create_browser = true;

	first_update = false;
}

void BrowserSource::Tick()
{
	if (create_browser && CreateBrowser())
		create_browser = false;
#if defined(ENABLE_BROWSER_SHARED_TEXTURE)
#if defined(BROWSER_EXTERNAL_BEGIN_FRAME_ENABLED)
	if (!fps_custom)
		reset_frame = true;
#else
	struct obs_video_info ovi;
	obs_get_video_info(&ovi);
	double video_fps = (double)ovi.fps_num / (double)ovi.fps_den;

	if (!fps_custom) {
		if (!!cefBrowser && canvas_fps != video_fps) {
			cefBrowser->GetHost()->SetWindowlessFrameRate(video_fps);
			canvas_fps = video_fps;
		}
	}
#endif
#endif
}

extern void ProcessCef();

void BrowserSource::Render()
{
	bool flip = false;
#if defined(ENABLE_BROWSER_SHARED_TEXTURE) && CHROME_VERSION_BUILD < 6367
	flip = hwaccel;
#endif

	if (texture) {
#ifdef __APPLE__
		int type = gs_get_device_type();
		gs_effect_t *effect;

		if (type == GS_DEVICE_OPENGL) {
			effect = obs_get_base_effect((hwaccel) ? OBS_EFFECT_DEFAULT_RECT : OBS_EFFECT_DEFAULT);
		} else {
			effect = obs_get_base_effect(OBS_EFFECT_DEFAULT);
		}
#else
		gs_effect_t *effect = obs_get_base_effect(OBS_EFFECT_DEFAULT);
#endif

		bool linear_sample = extra_texture == NULL;
		gs_texture_t *draw_texture = texture;
		if (!linear_sample && !obs_source_get_texcoords_centered(source)) {
			gs_copy_texture(extra_texture, texture);
			draw_texture = extra_texture;

			linear_sample = true;
		}

		const bool previous = gs_framebuffer_srgb_enabled();
		gs_enable_framebuffer_srgb(true);

		gs_blend_state_push();
		gs_blend_function(GS_BLEND_ONE, GS_BLEND_INVSRCALPHA);

		gs_eparam_t *const image = gs_effect_get_param_by_name(effect, "image");

		const char *tech;
		if (linear_sample) {
			gs_effect_set_texture_srgb(image, draw_texture);
			tech = "Draw";
		} else {
			gs_effect_set_texture(image, draw_texture);
			tech = "DrawSrgbDecompress";
		}

		const uint32_t flip_flag = flip ? GS_FLIP_V : 0;
		while (gs_effect_loop(effect, tech))
			gs_draw_sprite(draw_texture, flip_flag, 0, 0);

		gs_blend_state_pop();

		gs_enable_framebuffer_srgb(previous);
	}

#if defined(BROWSER_EXTERNAL_BEGIN_FRAME_ENABLED) && defined(ENABLE_BROWSER_SHARED_TEXTURE)
	SignalBeginFrame();
#elif defined(ENABLE_BROWSER_QT_LOOP)
	ProcessCef();
#endif
}

static void ExecuteOnBrowser(BrowserFunc func, BrowserSource *bs)
{
	lock_guard<mutex> lock(browser_list_mutex);

	if (bs) {
		BrowserSource *bsw = reinterpret_cast<BrowserSource *>(bs);
		bsw->ExecuteOnBrowser(func, true);
	}
}

static void ExecuteOnAllBrowsers(BrowserFunc func)
{
	lock_guard<mutex> lock(browser_list_mutex);

	BrowserSource *bs = first_browser;
	while (bs) {
		BrowserSource *bsw = reinterpret_cast<BrowserSource *>(bs);
		bsw->ExecuteOnBrowser(func, true);
		bs = bs->next;
	}
}

void DispatchJSEvent(std::string eventName, std::string jsonString, BrowserSource *browser)
{
	const auto jsEvent = [=](CefRefPtr<CefBrowser> cefBrowser) {
		CefRefPtr<CefProcessMessage> msg = CefProcessMessage::Create("DispatchJSEvent");
		CefRefPtr<CefListValue> args = msg->GetArgumentList();

		args->SetString(0, eventName);
		args->SetString(1, jsonString);
		SendBrowserProcessMessage(cefBrowser, PID_RENDERER, msg);
	};

	if (!browser)
		ExecuteOnAllBrowsers(jsEvent);
	else
		ExecuteOnBrowser(jsEvent, browser);
}
