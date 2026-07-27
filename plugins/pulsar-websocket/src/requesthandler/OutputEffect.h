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

#pragma once

#include <string>

#include "rpc/RequestResult.h"
#include "../utils/Obs.h"

// ---------------------------------------------------------------------------
// Post-action verification shared by the four output families -- replay
// buffer, record, virtualcam, stream (Pulsar issue #120, ADR Prism 026 §3.2).
//
// The obs-frontend-api start/stop entry points are `void`, and libobs declines
// silently when an output is not configured. A handler that calls one and then
// returns Success() is asserting an effect it never observed: the client is
// told "started" while GetXStatus reports outputActive:false the next
// millisecond.
//
// The v5 request signatures are unchanged -- no new request, no new status
// enum, no blocking wait. The handler simply re-reads the state the server
// already has (Utils::Obs::OutputHelper) and maps the verdict:
//
//   Landed  -> Success. The effect is there.
//   Pending -> Success. libobs accepted the action and is completing it
//              asynchronously (rtmp connect thread, ffmpeg_muxer flush).
//              Claiming failure here would be as wrong as claiming success
//              on a refusal.
//   Refused -> Error, carrying the cause READ off the server:
//              obs_output_get_last_error() when libobs recorded one, else
//              the structural state that made libobs refuse (see
//              DescribeOutputRefusal). Never a generic message when a real
//              cause is observable.
//
// Status mapping reuses the existing v5 codes so the wire contract does not
// move: a start that produced nothing is OutputNotRunning ("an output is not
// running and should be"), a stop that produced nothing is OutputRunning.
// ---------------------------------------------------------------------------

// obs_output_start() bails on an unconfigured output BEFORE reaching
// obs_output_actual_start(), so those refusals leave last_error_message
// untouched -- there is nothing to quote. Rather than fall back to "it
// failed", read the structural reason off the output itself: a service
// output with no service bound and an encoded output with no encoder bound
// are exactly the two states libobs refuses on, and both are observable.
inline std::string DescribeOutputRefusal(obs_output_t *output)
{
	std::string cause = Utils::Obs::OutputHelper::GetLastError(output);
	if (!cause.empty())
		return cause;

	if (!output)
		return "the output does not exist.";

	uint32_t flags = obs_output_get_flags(output);

	if ((flags & OBS_OUTPUT_SERVICE) != 0) {
		// obs_output_get_service borrows -- no ref to release.
		obs_service_t *service = obs_output_get_service(output);
		if (!service)
			return "no streaming service is configured -- call SetStreamServiceSettings first, "
			       "or use the pulsar:StartDestination multi-stream API.";
		// Issue #131: since the frontend binds `streamService` to the output
		// before starting it, "no service at all" is no longer the only
		// service-shaped refusal libobs can produce. obs_output_start's first
		// gate is obs_service_can_try_to_connect(), which an rtmp_common
		// service still missing its server/key answers false to -- observable,
		// so name it instead of falling through to the generic sentence.
		if (!obs_service_can_try_to_connect(service))
			return "the configured streaming service is not ready to connect -- its "
			       "server/key are incomplete. Push a complete service with "
			       "SetStreamServiceSettings, or use the pulsar:StartDestination "
			       "multi-stream API.";
	}

	if ((flags & OBS_OUTPUT_ENCODED) != 0 && !obs_output_get_video_encoder(output))
		return "no video encoder is attached to this output.";

	return "libobs declined the action and recorded no cause -- the output is not configured.";
}

inline RequestResult OutputStartFailure(obs_output_t *output, const char *label)
{
	return RequestResult::Error(RequestStatus::OutputNotRunning,
				    std::string(label) + " did not start: " + DescribeOutputRefusal(output));
}

inline RequestResult OutputStopFailure(obs_output_t *output, const char *label)
{
	return RequestResult::Error(RequestStatus::OutputRunning,
				    std::string(label) + " did not stop: " + DescribeOutputRefusal(output));
}
