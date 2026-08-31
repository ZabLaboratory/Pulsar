/******************************************************************************
 Copyright (C) 2026 ZabLaboratory

 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 2 of the License, or
 (at your option) any later version.
 ******************************************************************************/

#pragma once

#include <cstddef>
#include <mutex>
#include <set>
#include <vector>

struct BrowserSource;

struct BrowserSourceTaskRelease {
	BrowserSource *source = nullptr;
	bool complete_destroy_task = false;
	bool underflow = false;
};

/*
 * CEF tasks outlive the OBS callback that posted them.  This small control
 * block linearizes task admission with source destruction and keeps the raw
 * BrowserSource pointer valid until the final task lease is released.
 */
struct BrowserSourceTaskState {
	std::mutex mutex;
	BrowserSource *source = nullptr;
	std::size_t active_tasks = 0;
	bool destroying = false;
	bool delete_requested = false;
	bool delete_completion_required = false;
	bool deleted = false;
	std::set<int> pending_browser_ids;
	bool browser_delete_requested = false;
	bool browser_delete_completed = false;

	bool acquire(bool allow_destroying)
	{
		std::lock_guard<std::mutex> lock(mutex);
		if (!source || deleted || (destroying && !allow_destroying))
			return false;
		++active_tasks;
		return true;
	}

	bool begin_destroy()
	{
		std::lock_guard<std::mutex> lock(mutex);
		if (!source || deleted || destroying)
			return false;
		destroying = true;
		return true;
	}

	BrowserSource *current_source()
	{
		std::lock_guard<std::mutex> lock(mutex);
		return source;
	}

	bool is_destroying()
	{
		std::lock_guard<std::mutex> lock(mutex);
		return destroying;
	}

	bool arm_browser_ids(const std::vector<int> &browser_ids)
	{
		std::lock_guard<std::mutex> lock(mutex);
		if (!source || deleted || browser_delete_requested || browser_delete_completed)
			return false;
		browser_delete_requested = true;
		pending_browser_ids.insert(browser_ids.begin(), browser_ids.end());
		return true;
	}

	bool add_browser_id(int browser_id)
	{
		std::lock_guard<std::mutex> lock(mutex);
		if (!source || deleted || !browser_delete_requested || browser_delete_completed || browser_id < 0)
			return false;
		pending_browser_ids.insert(browser_id);
		return true;
	}

	bool finalize_browser_id(int browser_id)
	{
		std::lock_guard<std::mutex> lock(mutex);
		if (!browser_delete_requested || browser_delete_completed ||
		    pending_browser_ids.erase(browser_id) == 0)
			return false;
		if (pending_browser_ids.empty()) {
			browser_delete_completed = true;
			return true;
		}
		return false;
	}

	bool browser_deletion_complete_locked()
	{
		if (!browser_delete_requested)
			return true;
		if (!pending_browser_ids.empty())
			return false;
		browser_delete_completed = true;
		return true;
	}

	BrowserSourceTaskRelease request_delete_and_take_if_idle(bool completion_required)
	{
		std::lock_guard<std::mutex> lock(mutex);
		delete_requested = true;
		delete_completion_required |= completion_required;
		if (active_tasks != 0 || deleted || !browser_deletion_complete_locked())
			return {};
		deleted = true;
		BrowserSourceTaskRelease release{source, delete_completion_required, false};
		source = nullptr;
		return release;
	}

	BrowserSourceTaskRelease request_delete_release_own_lease(bool completion_required)
	{
		std::lock_guard<std::mutex> lock(mutex);
		delete_requested = true;
		delete_completion_required |= completion_required;
		if (active_tasks == 0)
			return BrowserSourceTaskRelease{nullptr, false, true};
		--active_tasks;
		if (active_tasks != 0 || deleted || !browser_deletion_complete_locked())
			return {};
		deleted = true;
		BrowserSourceTaskRelease release{source, delete_completion_required, false};
		source = nullptr;
		return release;
	}

	BrowserSourceTaskRelease release_task()
	{
		std::lock_guard<std::mutex> lock(mutex);
		if (active_tasks == 0)
			return BrowserSourceTaskRelease{nullptr, false, true};
		--active_tasks;
		if (active_tasks != 0 || !delete_requested || deleted || !browser_deletion_complete_locked())
			return {};
		deleted = true;
		BrowserSourceTaskRelease release{source, delete_completion_required, false};
		source = nullptr;
		return release;
	}

};
