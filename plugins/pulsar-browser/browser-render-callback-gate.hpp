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

#pragma once

#include <condition_variable>
#include <cstddef>
#include <mutex>

/*
 * CEF render callbacks can run while BrowserSource::Destroy tears down the
 * graphics textures.  Admission and the final destruction fence share one
 * lock so a callback cannot pass valid() and then race texture destruction.
 */
class BrowserRenderCallbackGate final {
public:
	class Lease final {
	public:
		Lease() = default;
		Lease(const Lease &) = delete;
		Lease &operator=(const Lease &) = delete;

		Lease(Lease &&other) noexcept : gate_(other.gate_) { other.gate_ = nullptr; }
		Lease &operator=(Lease &&other) noexcept
		{
			if (this != &other) {
				release();
				gate_ = other.gate_;
				other.gate_ = nullptr;
			}
			return *this;
		}

		~Lease() { release(); }

		explicit operator bool() const { return gate_ != nullptr; }

	private:
		explicit Lease(BrowserRenderCallbackGate *gate) : gate_(gate) {}

		void release()
		{
			if (gate_) {
				gate_->release();
				gate_ = nullptr;
			}
		}

		BrowserRenderCallbackGate *gate_ = nullptr;
		friend class BrowserRenderCallbackGate;
	};

	class CallbackPause final {
	public:
		CallbackPause() = default;
		CallbackPause(const CallbackPause &) = delete;
		CallbackPause &operator=(const CallbackPause &) = delete;

		CallbackPause(CallbackPause &&other) noexcept : gate_(other.gate_) { other.gate_ = nullptr; }
		CallbackPause &operator=(CallbackPause &&other) noexcept
		{
			if (this != &other) {
				resume();
				gate_ = other.gate_;
				other.gate_ = nullptr;
			}
			return *this;
		}

		~CallbackPause() { resume(); }

		explicit operator bool() const { return gate_ != nullptr; }

	private:
		explicit CallbackPause(BrowserRenderCallbackGate *gate) : gate_(gate) {}

		void resume()
		{
			if (gate_) {
				gate_->resume_admission();
				gate_ = nullptr;
			}
		}

		BrowserRenderCallbackGate *gate_ = nullptr;
		friend class BrowserRenderCallbackGate;
	};

	Lease try_acquire()
	{
		std::lock_guard<std::mutex> lock(mutex_);
		if (admission_closed_ || admission_paused_)
			return {};
		++in_flight_;
		return Lease(this);
	}

	/* Permanently stop render callback admission and wait for all admitted
	 * callbacks before the owning BrowserSource destroys its textures. */
	void close_and_wait()
	{
		std::unique_lock<std::mutex> lock(mutex_);
		admission_closed_ = true;
		idle_.wait(lock, [this]() { return in_flight_ == 0; });
	}

	/* Temporarily block new callbacks while the current callback replaces its
	 * textures. The caller owns one lease, so waiting for <= 1 drains every
	 * other callback without self-deadlocking. If another callback already owns
	 * the pause, fail immediately so two resizers cannot wait on each other. */
	CallbackPause try_pause_for_current_callback()
	{
		std::unique_lock<std::mutex> lock(mutex_);
		if (admission_paused_)
			return {};
		admission_paused_ = true;
		idle_.wait(lock, [this]() { return in_flight_ <= 1; });
		return CallbackPause(this);
	}

	/* Temporarily block new callbacks for ordinary texture teardown. Wait for a
	 * callback-owned pause to finish instead of joining its in-flight lease. */
	CallbackPause pause_for_texture_destroy()
	{
		std::unique_lock<std::mutex> lock(mutex_);
		idle_.wait(lock, [this]() { return !admission_paused_; });
		admission_paused_ = true;
		idle_.wait(lock, [this]() { return in_flight_ == 0; });
		return CallbackPause(this);
	}

private:
	void resume_admission()
	{
		std::lock_guard<std::mutex> lock(mutex_);
		admission_paused_ = false;
		idle_.notify_all();
	}

	void release()
	{
		std::lock_guard<std::mutex> lock(mutex_);
		if (in_flight_ == 0)
			return;
		--in_flight_;
		if (in_flight_ <= 1 || (admission_closed_ && in_flight_ == 0))
			idle_.notify_all();
	}

	std::mutex mutex_;
	std::condition_variable idle_;
	std::size_t in_flight_ = 0;
	bool admission_closed_ = false;
	bool admission_paused_ = false;
};
