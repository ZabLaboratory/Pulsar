/*
obs-websocket
Copyright (C) 2016-2021 Stephane Lepin <stephane.lepin@gmail.com>
Copyright (C) 2020-2021 Kyle Manning <tt2468@gmail.com>

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License along
with this program. If not, see <https://www.gnu.org/licenses/>
*/

#include <cstdlib>
#include <util/platform.h>

#include "Obs.h"
#include "plugin-macros.generated.h"

// Granularity of the bounded post-action poll. Small enough that the
// nominal case (an output that flips active inside obs_output_start)
// costs at most one tick, coarse enough not to spin.
static const uint32_t POLL_STEP_MS = 5;

static const uint32_t DEFAULT_VERIFY_TIMEOUT_MS = 250;
static const uint32_t MAX_VERIFY_TIMEOUT_MS = 2000;
static const uint32_t RECORD_STOP_VERIFY_TIMEOUT_MS = 2500;

uint32_t Utils::Obs::OutputHelper::VerifyTimeoutMs()
{
	static const uint32_t cached = []() -> uint32_t {
		const char *raw = std::getenv("PULSAR_OUTPUT_VERIFY_MS");
		if (!raw || !*raw)
			return DEFAULT_VERIFY_TIMEOUT_MS;

		char *end = nullptr;
		long value = std::strtol(raw, &end, 10);
		if (end == raw || *end != '\0' || value < 0 || value > (long)MAX_VERIFY_TIMEOUT_MS) {
			blog(LOG_WARNING,
			     "[Utils::Obs::OutputHelper] PULSAR_OUTPUT_VERIFY_MS=%s rejected (expected 0..%u); using %u",
			     raw, MAX_VERIFY_TIMEOUT_MS, DEFAULT_VERIFY_TIMEOUT_MS);
			return DEFAULT_VERIFY_TIMEOUT_MS;
		}
		return (uint32_t)value;
	}();

	return cached;
}

uint32_t Utils::Obs::OutputHelper::RecordStopVerifyTimeoutMs()
{
	return RECORD_STOP_VERIFY_TIMEOUT_MS;
}

std::string Utils::Obs::OutputHelper::GetLastError(obs_output_t *output)
{
	if (!output)
		return "";

	const char *error = obs_output_get_last_error(output);
	return error ? error : "";
}

Utils::Obs::OutputHelper::ActionWatch::ActionWatch(obs_output_t *output, const char *signalName)
	: _output(obs_output_get_ref(output)), _signalName(signalName)
{
	if (!_output)
		return;

	signal_handler_t *sh = obs_output_get_signal_handler(_output);
	if (sh)
		signal_handler_connect(sh, _signalName, OnSignal, this);
}

Utils::Obs::OutputHelper::ActionWatch::~ActionWatch()
{
	if (!_output)
		return;

	signal_handler_t *sh = obs_output_get_signal_handler(_output);
	if (sh)
		signal_handler_disconnect(sh, _signalName, OnSignal, this);
}

void Utils::Obs::OutputHelper::ActionWatch::OnSignal(void *param, calldata_t *)
{
	static_cast<ActionWatch *>(param)->_accepted.store(true);
}

// Shared body of SettleStart/SettleStop.
//
// wantActive           -- the state the action promised.
// signalProvesAccepted -- whether "no signal" is, on its own, proof of a
//                         refusal. True for a start: obs_output_start emits
//                         "starting" on every path where it took the action,
//                         so silence means it bailed and nothing is in flight
//                         -- the verdict is already decided, don't wait.
//                         False for a stop: obs_output_stop skips "stopping"
//                         when a stop is already in flight, so silence there
//                         is ambiguous and only the state read can conclude.
static Utils::Obs::OutputHelper::ActionVerdict settle(obs_output_t *output,
							      const Utils::Obs::OutputHelper::ActionWatch &watch, bool wantActive,
							      bool signalProvesAccepted, uint32_t timeoutMs)
{
	using Verdict = Utils::Obs::OutputHelper::ActionVerdict;

	auto reached = [&]() { return obs_output_active(output) == wantActive; };

	// Nominal path: every non-networked output (replay_buffer,
	// ffmpeg_muxer, virtualcam_output) flips its active flag inside
	// obs_output_start, so this returns without ever sleeping.
	if (reached())
		return Verdict::Landed;

	if (signalProvesAccepted && !watch.Accepted())
		return Verdict::Refused;

	// Poll a short, bounded window -- never an open-ended wait for the
	// output to activate.
	for (uint32_t waited = 0; waited < timeoutMs;) {
		const uint32_t sleep_ms = std::min(POLL_STEP_MS, timeoutMs - waited);
		os_sleep_ms(sleep_ms);
		waited += sleep_ms;
		if (reached())
			return Verdict::Landed;
	}

	if (!watch.Accepted())
		return Verdict::Refused;

	// Accepted, still running its course (an rtmp connect thread, an
	// ffmpeg_muxer flush). Reporting failure here would be as wrong as
	// reporting success on a refusal.
	return Verdict::Pending;
}

Utils::Obs::OutputHelper::ActionVerdict Utils::Obs::OutputHelper::SettleStart(obs_output_t *output,
									     const ActionWatch &watch)
{
	return settle(output, watch, true, true, Utils::Obs::OutputHelper::VerifyTimeoutMs());
}

Utils::Obs::OutputHelper::ActionVerdict Utils::Obs::OutputHelper::SettleStop(obs_output_t *output,
									    const ActionWatch &watch, uint32_t timeoutMs)
{
	return settle(output, watch, false, false, timeoutMs);
}

Utils::Obs::OutputHelper::ActionVerdict Utils::Obs::OutputHelper::SettleRecordStop(
	obs_output_t *output, const ActionWatch &watch)
{
	return settle(output, watch, false, false, Utils::Obs::OutputHelper::RecordStopVerifyTimeoutMs());
}
