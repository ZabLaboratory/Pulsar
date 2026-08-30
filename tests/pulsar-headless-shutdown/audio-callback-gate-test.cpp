/******************************************************************************
 Copyright (C) 2026 ZabLaboratory

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

#include "browser-audio-callback-gate.hpp"

#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <mutex>
#include <thread>

static bool check(bool condition, const char *message)
{
	if (!condition)
		std::fprintf(stderr, "audio callback gate test: %s\n", message);
	return condition;
}

static bool test_close_wins_before_admission()
{
	BrowserAudioCallbackGate gate;
	gate.mark_stream_started();
	gate.mark_stream_stopped();
	gate.mark_close_callback_seen();
	return check(!gate.try_acquire(), "late admission after close") &&
	       check(gate.try_claim_finalization(), "close should claim after zero callbacks") &&
	       check(!gate.try_claim_finalization(), "finalization claimed twice");
}

static bool test_admitted_callback_blocks_close_until_release()
{
	BrowserAudioCallbackGate gate;
	gate.mark_stream_started();

	std::mutex rendezvous_mutex;
	std::condition_variable rendezvous;
	bool admitted = false;
	bool release = false;
	bool callback_claimed = false;
	std::thread callback([&]() {
		if (!gate.try_acquire())
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
		callback_claimed = gate.release_and_try_claim();
	});

	{
		std::unique_lock<std::mutex> lock(rendezvous_mutex);
		if (!rendezvous.wait_for(lock, std::chrono::seconds(2), [&]() { return admitted; })) {
			release = true;
			lock.unlock();
			rendezvous.notify_one();
			callback.join();
			return check(false, "callback admission rendezvous timed out");
		}
	}

	gate.mark_close_callback_seen();
	gate.mark_stream_stopped();
	if (!check(!gate.try_claim_finalization(), "close claimed while callback was in flight")) {
		{
			std::lock_guard<std::mutex> lock(rendezvous_mutex);
			release = true;
		}
		rendezvous.notify_one();
		callback.join();
		return false;
	}
	{
		std::lock_guard<std::mutex> lock(rendezvous_mutex);
		release = true;
	}
	rendezvous.notify_one();
	callback.join();
	return check(callback_claimed, "last callback did not claim finalization") &&
	       check(!gate.try_acquire(), "admission reopened after close");
}

int main()
{
	return test_close_wins_before_admission() && test_admitted_callback_blocks_close_until_release() ? 0 : 1;
}
