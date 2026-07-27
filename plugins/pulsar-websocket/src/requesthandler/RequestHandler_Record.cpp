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

#include "RequestHandler.h"
#include "OutputEffect.h"

using ActionWatch = Utils::Obs::OutputHelper::ActionWatch;
using ActionVerdict = Utils::Obs::OutputHelper::ActionVerdict;

// Issue #130 -- DEFENSIVE REFUSAL, the cause is upstream.
//
// obs_output_pause() has no guard on "has the encoder produced a frame yet".
// It computes the pause start from pause->last_video_ts (obs-output.c,
// get_closest_v_ts), which video_pause_check_internal only ever fills when the
// FIRST encoded frame goes through (libobs/obs-encoder.c). Before that it is 0,
// so ts_start is quantised against the wall clock instead of the encoder
// timeline; video_pause_check_internal then drops every frame with
// ts >= pause->ts_start and only lifts the pause on an EXACT ts == pause->ts_end,
// which that faulty base never reaches. The muxer is wedged for good: the replay
// buffer sharing the encoders stops producing files, and Stop* answer Success()
// while outputActive stays true.
//
// Fixing libobs is out of Pulsar's mandate (LICENSE-INVARIANTS.md / fork
// doctrine: no divergence from libobs for an exotic trigger). The websocket
// layer is the only layer that can NAME the cause -- obs_frontend_recording_pause()
// returns void -- so it refuses the precondition instead of entering the wedge.
// outputBytes > 0 is a conservative proxy for "the muxer took at least one
// encoded packet": it can refuse a legitimate pause for the few tens of ms after
// StartRecord, never the reverse. The client lifts the condition itself --
// outputBytes is already a GetRecordStatus response field.
static constexpr const char *kPauseBeforeFirstByte =
	"Cannot pause the recording before the muxer has written its first byte "
	"(outputBytes is still 0). libobs's pause timeline is not initialised until "
	"the first encoded frame is muxed; pausing now wedges the output permanently. "
	"Poll GetRecordStatus until outputBytes > 0, then retry.";

// True when the record output has not muxed a single byte yet.
static bool RecordOutputHasNoBytesYet()
{
	OBSOutputAutoRelease output = obs_frontend_get_recording_output();
	return !output || obs_output_get_total_bytes(output) == 0;
}

/**
 * Gets the status of the record output.
 *
 * @responseField outputActive        | Boolean | Whether the output is active
 * @responseField outputPaused        | Boolean | Whether the output is paused
 * @responseField outputTimecode      | String  | Current formatted timecode string for the output
 * @responseField outputDuration      | Number  | Current duration in milliseconds for the output
 * @responseField outputBytes         | Number  | Number of bytes sent by the output
 *
 * @requestType GetRecordStatus
 * @complexity 2
 * @rpcVersion -1
 * @initialVersion 5.0.0
 * @api requests
 * @category record
 */
RequestResult RequestHandler::GetRecordStatus(const Request &)
{
	OBSOutputAutoRelease recordOutput = obs_frontend_get_recording_output();

	uint64_t outputDuration = Utils::Obs::NumberHelper::GetOutputDuration(recordOutput);

	json responseData;
	responseData["outputActive"] = obs_output_active(recordOutput);
	responseData["outputPaused"] = obs_output_paused(recordOutput);
	responseData["outputTimecode"] = Utils::Obs::StringHelper::DurationToTimecode(outputDuration);
	responseData["outputDuration"] = outputDuration;
	responseData["outputBytes"] = (uint64_t)obs_output_get_total_bytes(recordOutput);

	return RequestResult::Success(responseData);
}

/**
 * Toggles the status of the record output.
 *
 * @responseField outputActive | Boolean | The new active state of the output
 *
 * @requestType ToggleRecord
 * @complexity 1
 * @rpcVersion -1
 * @initialVersion 5.0.0
 * @api requests
 * @category record
 */
RequestResult RequestHandler::ToggleRecord(const Request &)
{
	bool wasActive = obs_frontend_recording_active();

	OBSOutputAutoRelease output = obs_frontend_get_recording_output();
	ActionWatch watch(output, wasActive ? "stopping" : "starting");

	if (wasActive)
		obs_frontend_recording_stop();
	else
		obs_frontend_recording_start();

	if (wasActive) {
		if (Utils::Obs::OutputHelper::SettleStop(output, watch) == ActionVerdict::Refused)
			return OutputStopFailure(output, "The record output");
	} else {
		if (Utils::Obs::OutputHelper::SettleStart(output, watch) == ActionVerdict::Refused)
			return OutputStartFailure(output, "The record output");
	}

	json responseData;
	responseData["outputActive"] = !wasActive;
	return RequestResult::Success(responseData);
}

/**
 * Starts the record output.
 *
 * @requestType StartRecord
 * @complexity 1
 * @rpcVersion -1
 * @initialVersion 5.0.0
 * @api requests
 * @category record
 */
RequestResult RequestHandler::StartRecord(const Request &)
{
	if (obs_frontend_recording_active())
		return RequestResult::Error(RequestStatus::OutputRunning);

	OBSOutputAutoRelease output = obs_frontend_get_recording_output();
	ActionWatch watch(output, "starting");

	obs_frontend_recording_start();

	if (Utils::Obs::OutputHelper::SettleStart(output, watch) == ActionVerdict::Refused)
		return OutputStartFailure(output, "The record output");

	return RequestResult::Success();
}

/**
 * Stops the record output.
 *
 * @responseField outputPath | String | File name for the saved recording
 *
 * @requestType StopRecord
 * @complexity 1
 * @rpcVersion -1
 * @initialVersion 5.0.0
 * @api requests
 * @category record
 */
RequestResult RequestHandler::StopRecord(const Request &)
{
	if (!obs_frontend_recording_active())
		return RequestResult::Error(RequestStatus::OutputNotRunning);

	OBSOutputAutoRelease output = obs_frontend_get_recording_output();
	ActionWatch watch(output, "stopping");

	obs_frontend_recording_stop();

	if (Utils::Obs::OutputHelper::SettleStop(output, watch) == ActionVerdict::Refused)
		return OutputStopFailure(output, "The record output");

	json responseData;
	responseData["outputPath"] = Utils::Obs::StringHelper::GetLastRecordFileName();

	return RequestResult::Success(responseData);
}

/**
 * Toggles pause on the record output.
 *
 * @requestType ToggleRecordPause
 * @complexity 1
 * @rpcVersion -1
 * @initialVersion 5.0.0
 * @api requests
 * @category record
 */
RequestResult RequestHandler::ToggleRecordPause(const Request &)
{
	// Issue #130 / #120 family: neither branch checked that a recording was
	// running at all. obs_output_pause() returns false on an inactive output
	// and obs_frontend_recording_pause() swallows it, so this answered
	// Success() with outputPaused flipped in the response and nothing paused
	// on the server.
	if (!obs_frontend_recording_active())
		return RequestResult::Error(RequestStatus::OutputNotRunning);

	json responseData;
	if (obs_frontend_recording_paused()) {
		obs_frontend_recording_pause(false);
		responseData["outputPaused"] = false;
	} else {
		if (RecordOutputHasNoBytesYet())
			return RequestResult::Error(RequestStatus::InvalidResourceState, kPauseBeforeFirstByte);
		obs_frontend_recording_pause(true);
		responseData["outputPaused"] = true;
	}

	return RequestResult::Success(responseData);
}

/**
 * Pauses the record output.
 *
 * @requestType PauseRecord
 * @complexity 1
 * @rpcVersion -1
 * @initialVersion 5.0.0
 * @api requests
 * @category record
 */
RequestResult RequestHandler::PauseRecord(const Request &)
{
	// Issue #130 / #120 family: an inactive output cannot be paused --
	// obs_output_pause() returns false and obs_frontend_recording_pause()
	// discards that, so this used to answer Success() having done nothing.
	if (!obs_frontend_recording_active())
		return RequestResult::Error(RequestStatus::OutputNotRunning);

	if (obs_frontend_recording_paused())
		return RequestResult::Error(RequestStatus::OutputPaused);

	// Issue #130: refuse the muxer-wedging precondition, with the cause named.
	if (RecordOutputHasNoBytesYet())
		return RequestResult::Error(RequestStatus::InvalidResourceState, kPauseBeforeFirstByte);

	// TODO: Call signal directly to perform blocking wait
	obs_frontend_recording_pause(true);

	return RequestResult::Success();
}

/**
 * Resumes the record output.
 *
 * @requestType ResumeRecord
 * @complexity 1
 * @rpcVersion -1
 * @initialVersion 5.0.0
 * @api requests
 * @category record
 */
RequestResult RequestHandler::ResumeRecord(const Request &)
{
	if (!obs_frontend_recording_paused())
		return RequestResult::Error(RequestStatus::OutputNotPaused);

	// TODO: Call signal directly to perform blocking wait
	obs_frontend_recording_pause(false);

	return RequestResult::Success();
}

/**
 * Splits the current file being recorded into a new file.
 *
 * @requestType SplitRecordFile
 * @complexity 2
 * @rpcVersion -1
 * @initialVersion 5.5.0
 * @api requests
 * @category record
 */
RequestResult RequestHandler::SplitRecordFile(const Request &)
{
	if (!obs_frontend_recording_active())
		return RequestResult::Error(RequestStatus::OutputNotRunning);

	if (!obs_frontend_recording_split_file())
		return RequestResult::Error(RequestStatus::RequestProcessingFailed,
					    "Verify that file splitting is enabled in the output settings.");

	return RequestResult::Success();
}

/**
 * Adds a new chapter marker to the file currently being recorded.
 *
 * Note: As of OBS 30.2.0, the only file format supporting this feature is Hybrid MP4.
 *
 * @requestField ?chapterName | String | Name of the new chapter
 *
 * @requestType CreateRecordChapter
 * @complexity 2
 * @rpcVersion -1
 * @initialVersion 5.5.0
 * @api requests
 * @category record
 */
RequestResult RequestHandler::CreateRecordChapter(const Request &request)
{
	std::string chapterName;
	if (request.Contains("chapterName")) {
		RequestStatus::RequestStatus statusCode;
		std::string comment;
		if (!request.ValidateOptionalString("chapterName", statusCode, comment))
			return RequestResult::Error(statusCode, comment);
		chapterName = request.RequestData["chapterName"];
	}

	if (!obs_frontend_recording_active())
		return RequestResult::Error(RequestStatus::OutputNotRunning);

	if (!obs_frontend_recording_add_chapter(chapterName.empty() ? nullptr : chapterName.c_str()))
		return RequestResult::Error(RequestStatus::RequestProcessingFailed,
					    "Verify that the output being used supports chapter markers.");

	return RequestResult::Success();
}
