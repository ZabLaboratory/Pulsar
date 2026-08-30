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

#include <cstddef>
#include <mutex>

/*
 * CEF audio packets may arrive on a dedicated audio thread while the UI
 * thread is delivering OnBeforeClose.  Admission, in-flight accounting, and
 * the finalization claim must therefore share one linearization point.
 */
class BrowserAudioCallbackGate final {
public:
	bool try_acquire()
	{
		std::lock_guard<std::mutex> lock(mutex_);
		if (admission_closed_)
			return false;
		++in_flight_;
		return true;
	}

	void mark_stream_started()
	{
		std::lock_guard<std::mutex> lock(mutex_);
		stream_started_ = true;
		stream_stopped_ = false;
	}

	void mark_stream_stopped()
	{
		std::lock_guard<std::mutex> lock(mutex_);
		stream_stopped_ = true;
	}

	void mark_close_callback_seen()
	{
		std::lock_guard<std::mutex> lock(mutex_);
		admission_closed_ = true;
		close_callback_seen_ = true;
	}

	bool should_deliver() const
	{
		std::lock_guard<std::mutex> lock(mutex_);
		return !close_callback_seen_;
	}

	/* Called on the UI thread when OnBeforeClose/OnAudioStreamStopped runs. */
	bool try_claim_finalization()
	{
		std::lock_guard<std::mutex> lock(mutex_);
		return try_claim_finalization_locked();
	}

	/* Called by the audio thread after releasing its callback lease. */
	bool release_and_try_claim()
	{
		std::lock_guard<std::mutex> lock(mutex_);
		if (in_flight_ == 0)
			return false;
		--in_flight_;
		return try_claim_finalization_locked();
	}

private:
	bool try_claim_finalization_locked()
	{
		if (!close_callback_seen_ || (stream_started_ && !stream_stopped_) || in_flight_ != 0 ||
		    finalization_claimed_)
			return false;
		finalization_claimed_ = true;
		return true;
	}

	mutable std::mutex mutex_;
	bool admission_closed_ = false;
	bool close_callback_seen_ = false;
	bool stream_started_ = false;
	bool stream_stopped_ = true;
	bool finalization_claimed_ = false;
	std::size_t in_flight_ = 0;
};
