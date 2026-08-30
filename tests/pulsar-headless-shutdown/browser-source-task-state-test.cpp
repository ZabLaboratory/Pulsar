/******************************************************************************
 Copyright (C) 2026 ZabLaboratory

 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 2 of the License, or
 (at your option) any later version.
 ******************************************************************************/

#include "browser-source-task-state.hpp"

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

int main()
{
	return test_admission_then_destroy() && test_destroy_then_admission() ? 0 : 1;
}
