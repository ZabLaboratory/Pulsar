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

#ifdef PLUGIN_TESTS
#include <util/profiler.hpp>
#endif

#include <cstdint>
#include <limits>
#include <string>

#include <util/platform.h>

#include "RequestHandler.h"
#include "pulsar-dual-lane-control.h"
#include "pulsar-runtime-telemetry.h"

namespace {

std::string DeadlineDelta(uint64_t deadline, uint64_t now)
{
    if (deadline >= now)
        return std::string("+") + std::to_string(deadline - now);
    return std::string("-") + std::to_string(now - deadline);
}

bool IsReadOnlyRequest(const std::string &requestType)
{
	// The control plane deliberately has a small allowlist: explicit Get*
	// requests are observational, while every other current or future request
	// is treated as potentially mutating. This keeps a newly added command from
	// bypassing the Preview freeze by accident.
	return requestType.rfind("Get", 0) == 0;
}

bool IsSafetyStopRequest(const std::string &requestType)
{
	// A rollback freeze protects the Preview/scene graph, but operators must
	// still be able to stop active outputs to contain an incident or complete
	// process teardown. Keep this escape hatch explicit and finite: starts,
	// toggles, and all scene/input mutations remain gated.
	return requestType == "StopRecord" || requestType == "StopStream" ||
	       requestType == "StopReplayBuffer" || requestType == "StopVirtualCam" ||
	       requestType == "StopOutput";
}

// Abort is intentionally not labelled read-only: it mutates the scene-switch
// state, but must reach that adapter to cancel a frozen, pre-boundary Take.
// This is the sole controlled bypass of the generic dual-lane mutation lease.
// GetState is included because it is the adapter's explicit observation API.
bool IsControlledSceneSwitchPendingBypass(const Request &request)
{
	if (request.RequestType != "CallVendorRequest" || !request.RequestData.is_object())
		return false;
	const auto vendor = request.RequestData.find("vendorName");
	const auto nestedRequest = request.RequestData.find("requestType");
	// This classifier runs before CallVendorRequest's normal ValidateString
	// checks. Never use json::value() here: a present non-string otherwise
	// throws from the worker thread and turns malformed remote input into a
	// process-level availability failure. Such data must simply stay gated and
	// then receive the ordinary request validation error.
	if (vendor == request.RequestData.end() || nestedRequest == request.RequestData.end() ||
	    !vendor->is_string() || !nestedRequest->is_string())
		return false;
	if (vendor->get<std::string>() != "pulsar-scene-switch")
		return false;
	const std::string nested = nestedRequest->get<std::string>();
	return nested == "Abort" || nested == "GetState";
}

// The public scene/transition request shape remains unchanged.  A latency
// campaign may add this private, opt-in envelope to the request data so the
// runtime can carry command/intent identity through the legacy ingress.  It is
// consumed before the handler invokes obs_frontend_set_current_scene(); an
// invalid envelope is ignored for telemetry and never changes Cut semantics.
struct RuntimeTelemetryIngress {
    bool requested = false;
    bool envelopeValid = false;
    pulsar_runtime_telemetry::BeginTakeStatus status;
    const char *failure = nullptr;
};

RuntimeTelemetryIngress BeginRuntimeTakeTelemetry(const Request &request)
{
    RuntimeTelemetryIngress ingress;
    if (request.RequestType != "SetCurrentProgramScene" && request.RequestType != "TriggerStudioModeTransition")
        return ingress;
    if (!request.RequestData.is_object() || !request.RequestData.contains("pulsarTelemetry") ||
        !request.RequestData["pulsarTelemetry"].is_object())
        return ingress;

    ingress.requested = true;

    const json &metadata = request.RequestData["pulsarTelemetry"];
    const char *requiredStrings[] = {"command_id", "intent_id", "runtime_instance_id", "take_command_id",
                                     "target_lane_id", "target_scene_id", "payload_sha256"};
    std::string values[7];
    for (size_t i = 0; i < 7; ++i) {
        const auto it = metadata.find(requiredStrings[i]);
        if (it == metadata.end() || !it->is_string()) {
            ingress.failure = "missing_or_non_string_field";
            blog(LOG_ERROR, "[pulsar-runtime-telemetry] ingress envelope invalid request=%s reason=%s field=%s",
                 request.RequestType.c_str(), ingress.failure, requiredStrings[i]);
            return ingress;
        }
        values[i] = it->get<std::string>();
    }

    const auto freezeIt = metadata.find("freeze_until_monotonic_ns");
    if (freezeIt == metadata.end() || !freezeIt->is_number_unsigned()) {
        ingress.failure = "missing_or_non_unsigned_freeze_deadline";
        blog(LOG_ERROR, "[pulsar-runtime-telemetry] ingress envelope invalid request=%s reason=%s",
             request.RequestType.c_str(), ingress.failure);
        return ingress;
    }
    const uint64_t freeze = freezeIt->get<uint64_t>();
    if (freeze > static_cast<uint64_t>(std::numeric_limits<int64_t>::max())) {
        ingress.failure = "freeze_deadline_out_of_range";
        blog(LOG_ERROR, "[pulsar-runtime-telemetry] ingress envelope invalid request=%s reason=%s",
             request.RequestType.c_str(), ingress.failure);
        return ingress;
    }

    ingress.envelopeValid = true;
    const uint64_t ingressNowNs = os_gettime_ns();
    const std::string ingressDelta = DeadlineDelta(freeze, ingressNowNs);
    ingress.status = pulsar_runtime_telemetry::begin_take_status(
        values[0].c_str(), values[1].c_str(), values[2].c_str(), values[3].c_str(), values[4].c_str(),
        values[5].c_str(), static_cast<int64_t>(freeze), values[6].c_str());
    blog(ingress.status.accepted ? LOG_INFO : LOG_ERROR,
         "[pulsar-runtime-telemetry] ingress request=%s command_id=%s "
         "freeze_until_monotonic_ns=%llu ingress_now_monotonic_ns=%llu deadline_delta_ns=%s "
         "called=%d available=%d accepted=%d",
         request.RequestType.c_str(), values[0].c_str(), static_cast<unsigned long long>(freeze),
         static_cast<unsigned long long>(ingressNowNs), ingressDelta.c_str(), ingress.status.called,
         ingress.status.available, ingress.status.accepted);
    return ingress;
}

} // namespace

const std::unordered_map<std::string, RequestMethodHandler> RequestHandler::_handlerMap{
	// General
	{"GetVersion", &RequestHandler::GetVersion},
	{"GetStats", &RequestHandler::GetStats},
	{"BroadcastCustomEvent", &RequestHandler::BroadcastCustomEvent},
	{"CallVendorRequest", &RequestHandler::CallVendorRequest},
	{"GetHotkeyList", &RequestHandler::GetHotkeyList},
	{"TriggerHotkeyByName", &RequestHandler::TriggerHotkeyByName},
	{"TriggerHotkeyByKeySequence", &RequestHandler::TriggerHotkeyByKeySequence},
	{"Sleep", &RequestHandler::Sleep},

	// Config
	{"GetPersistentData", &RequestHandler::GetPersistentData},
	{"SetPersistentData", &RequestHandler::SetPersistentData},
	{"GetSceneCollectionList", &RequestHandler::GetSceneCollectionList},
	{"SetCurrentSceneCollection", &RequestHandler::SetCurrentSceneCollection},
	{"CreateSceneCollection", &RequestHandler::CreateSceneCollection},
	{"GetProfileList", &RequestHandler::GetProfileList},
	{"SetCurrentProfile", &RequestHandler::SetCurrentProfile},
	{"CreateProfile", &RequestHandler::CreateProfile},
	{"RemoveProfile", &RequestHandler::RemoveProfile},
	{"GetProfileParameter", &RequestHandler::GetProfileParameter},
	{"SetProfileParameter", &RequestHandler::SetProfileParameter},
	{"GetVideoSettings", &RequestHandler::GetVideoSettings},
	{"SetVideoSettings", &RequestHandler::SetVideoSettings},
	{"GetStreamServiceSettings", &RequestHandler::GetStreamServiceSettings},
	{"SetStreamServiceSettings", &RequestHandler::SetStreamServiceSettings},
	{"GetRecordDirectory", &RequestHandler::GetRecordDirectory},
	{"SetRecordDirectory", &RequestHandler::SetRecordDirectory},

	// Canvases
	{"GetCanvasList", &RequestHandler::GetCanvasList},

	// Sources
	{"GetSourceActive", &RequestHandler::GetSourceActive},
	{"GetSourceScreenshot", &RequestHandler::GetSourceScreenshot},
	{"SaveSourceScreenshot", &RequestHandler::SaveSourceScreenshot},
	{"GetSourcePrivateSettings", &RequestHandler::GetSourcePrivateSettings},
	{"SetSourcePrivateSettings", &RequestHandler::SetSourcePrivateSettings},

	// Scenes
	{"GetSceneList", &RequestHandler::GetSceneList},
	{"GetGroupList", &RequestHandler::GetGroupList},
	{"GetCurrentProgramScene", &RequestHandler::GetCurrentProgramScene},
	{"SetCurrentProgramScene", &RequestHandler::SetCurrentProgramScene},
	{"GetCurrentPreviewScene", &RequestHandler::GetCurrentPreviewScene},
	{"SetCurrentPreviewScene", &RequestHandler::SetCurrentPreviewScene},
	{"CreateScene", &RequestHandler::CreateScene},
	{"RemoveScene", &RequestHandler::RemoveScene},
	{"SetSceneName", &RequestHandler::SetSceneName},
	{"GetSceneSceneTransitionOverride", &RequestHandler::GetSceneSceneTransitionOverride},
	{"SetSceneSceneTransitionOverride", &RequestHandler::SetSceneSceneTransitionOverride},

	// Inputs
	{"GetInputList", &RequestHandler::GetInputList},
	{"GetInputKindList", &RequestHandler::GetInputKindList},
	{"GetSpecialInputs", &RequestHandler::GetSpecialInputs},
	{"CreateInput", &RequestHandler::CreateInput},
	{"RemoveInput", &RequestHandler::RemoveInput},
	{"SetInputName", &RequestHandler::SetInputName},
	{"GetInputDefaultSettings", &RequestHandler::GetInputDefaultSettings},
	{"GetInputSettings", &RequestHandler::GetInputSettings},
	{"SetInputSettings", &RequestHandler::SetInputSettings},
	{"GetInputMute", &RequestHandler::GetInputMute},
	{"SetInputMute", &RequestHandler::SetInputMute},
	{"ToggleInputMute", &RequestHandler::ToggleInputMute},
	{"GetInputVolume", &RequestHandler::GetInputVolume},
	{"SetInputVolume", &RequestHandler::SetInputVolume},
	{"GetInputAudioBalance", &RequestHandler::GetInputAudioBalance},
	{"SetInputAudioBalance", &RequestHandler::SetInputAudioBalance},
	{"GetInputAudioSyncOffset", &RequestHandler::GetInputAudioSyncOffset},
	{"SetInputAudioSyncOffset", &RequestHandler::SetInputAudioSyncOffset},
	{"GetInputAudioMonitorType", &RequestHandler::GetInputAudioMonitorType},
	{"SetInputAudioMonitorType", &RequestHandler::SetInputAudioMonitorType},
	{"GetInputAudioTracks", &RequestHandler::GetInputAudioTracks},
	{"SetInputAudioTracks", &RequestHandler::SetInputAudioTracks},
	{"GetInputDeinterlaceMode", &RequestHandler::GetInputDeinterlaceMode},
	{"SetInputDeinterlaceMode", &RequestHandler::SetInputDeinterlaceMode},
	{"GetInputDeinterlaceFieldOrder", &RequestHandler::GetInputDeinterlaceFieldOrder},
	{"SetInputDeinterlaceFieldOrder", &RequestHandler::SetInputDeinterlaceFieldOrder},
	{"GetInputPropertiesListPropertyItems", &RequestHandler::GetInputPropertiesListPropertyItems},
	{"PressInputPropertiesButton", &RequestHandler::PressInputPropertiesButton},

	// Transitions
	{"GetTransitionKindList", &RequestHandler::GetTransitionKindList},
	{"GetSceneTransitionList", &RequestHandler::GetSceneTransitionList},
	{"GetCurrentSceneTransition", &RequestHandler::GetCurrentSceneTransition},
	{"SetCurrentSceneTransition", &RequestHandler::SetCurrentSceneTransition},
	{"SetCurrentSceneTransitionDuration", &RequestHandler::SetCurrentSceneTransitionDuration},
	{"SetCurrentSceneTransitionSettings", &RequestHandler::SetCurrentSceneTransitionSettings},
	{"GetCurrentSceneTransitionCursor", &RequestHandler::GetCurrentSceneTransitionCursor},
	{"TriggerStudioModeTransition", &RequestHandler::TriggerStudioModeTransition},
	{"SetTBarPosition", &RequestHandler::SetTBarPosition},

	// Filters
	{"GetSourceFilterKindList", &RequestHandler::GetSourceFilterKindList},
	{"GetSourceFilterList", &RequestHandler::GetSourceFilterList},
	{"GetSourceFilterDefaultSettings", &RequestHandler::GetSourceFilterDefaultSettings},
	{"CreateSourceFilter", &RequestHandler::CreateSourceFilter},
	{"RemoveSourceFilter", &RequestHandler::RemoveSourceFilter},
	{"SetSourceFilterName", &RequestHandler::SetSourceFilterName},
	{"GetSourceFilter", &RequestHandler::GetSourceFilter},
	{"SetSourceFilterIndex", &RequestHandler::SetSourceFilterIndex},
	{"SetSourceFilterSettings", &RequestHandler::SetSourceFilterSettings},
	{"SetSourceFilterEnabled", &RequestHandler::SetSourceFilterEnabled},

	// Scene Items
	{"GetSceneItemList", &RequestHandler::GetSceneItemList},
	{"GetGroupSceneItemList", &RequestHandler::GetGroupSceneItemList},
	{"GetSceneItemId", &RequestHandler::GetSceneItemId},
	{"GetSceneItemSource", &RequestHandler::GetSceneItemSource},
	{"CreateSceneItem", &RequestHandler::CreateSceneItem},
	{"RemoveSceneItem", &RequestHandler::RemoveSceneItem},
	{"DuplicateSceneItem", &RequestHandler::DuplicateSceneItem},
	{"GetSceneItemTransform", &RequestHandler::GetSceneItemTransform},
	{"SetSceneItemTransform", &RequestHandler::SetSceneItemTransform},
	{"GetSceneItemEnabled", &RequestHandler::GetSceneItemEnabled},
	{"SetSceneItemEnabled", &RequestHandler::SetSceneItemEnabled},
	{"GetSceneItemLocked", &RequestHandler::GetSceneItemLocked},
	{"SetSceneItemLocked", &RequestHandler::SetSceneItemLocked},
	{"GetSceneItemIndex", &RequestHandler::GetSceneItemIndex},
	{"SetSceneItemIndex", &RequestHandler::SetSceneItemIndex},
	{"GetSceneItemBlendMode", &RequestHandler::GetSceneItemBlendMode},
	{"SetSceneItemBlendMode", &RequestHandler::SetSceneItemBlendMode},
	{"GetSceneItemPrivateSettings", &RequestHandler::GetSceneItemPrivateSettings},
	{"SetSceneItemPrivateSettings", &RequestHandler::SetSceneItemPrivateSettings},

	// Outputs
	{"GetVirtualCamStatus", &RequestHandler::GetVirtualCamStatus},
	{"ToggleVirtualCam", &RequestHandler::ToggleVirtualCam},
	{"StartVirtualCam", &RequestHandler::StartVirtualCam},
	{"StopVirtualCam", &RequestHandler::StopVirtualCam},
	{"GetReplayBufferStatus", &RequestHandler::GetReplayBufferStatus},
	{"ToggleReplayBuffer", &RequestHandler::ToggleReplayBuffer},
	{"StartReplayBuffer", &RequestHandler::StartReplayBuffer},
	{"StopReplayBuffer", &RequestHandler::StopReplayBuffer},
	{"SaveReplayBuffer", &RequestHandler::SaveReplayBuffer},
	{"GetLastReplayBufferReplay", &RequestHandler::GetLastReplayBufferReplay},
	{"GetOutputList", &RequestHandler::GetOutputList},
	{"GetOutputStatus", &RequestHandler::GetOutputStatus},
	{"ToggleOutput", &RequestHandler::ToggleOutput},
	{"StartOutput", &RequestHandler::StartOutput},
	{"StopOutput", &RequestHandler::StopOutput},
	{"GetOutputSettings", &RequestHandler::GetOutputSettings},
	{"SetOutputSettings", &RequestHandler::SetOutputSettings},

	// Stream
	{"GetStreamStatus", &RequestHandler::GetStreamStatus},
	{"ToggleStream", &RequestHandler::ToggleStream},
	{"StartStream", &RequestHandler::StartStream},
	{"StopStream", &RequestHandler::StopStream},
	{"SendStreamCaption", &RequestHandler::SendStreamCaption},

	// Record
	{"GetRecordStatus", &RequestHandler::GetRecordStatus},
	{"ToggleRecord", &RequestHandler::ToggleRecord},
	{"StartRecord", &RequestHandler::StartRecord},
	{"StopRecord", &RequestHandler::StopRecord},
	{"ToggleRecordPause", &RequestHandler::ToggleRecordPause},
	{"PauseRecord", &RequestHandler::PauseRecord},
	{"ResumeRecord", &RequestHandler::ResumeRecord},
	{"SplitRecordFile", &RequestHandler::SplitRecordFile},
	{"CreateRecordChapter", &RequestHandler::CreateRecordChapter},

	// Media Inputs
	{"GetMediaInputStatus", &RequestHandler::GetMediaInputStatus},
	{"SetMediaInputCursor", &RequestHandler::SetMediaInputCursor},
	{"OffsetMediaInputCursor", &RequestHandler::OffsetMediaInputCursor},
	{"TriggerMediaInputAction", &RequestHandler::TriggerMediaInputAction},

	// Ui
	{"GetStudioModeEnabled", &RequestHandler::GetStudioModeEnabled},
	{"SetStudioModeEnabled", &RequestHandler::SetStudioModeEnabled},
	{"OpenInputPropertiesDialog", &RequestHandler::OpenInputPropertiesDialog},
	{"OpenInputFiltersDialog", &RequestHandler::OpenInputFiltersDialog},
	{"OpenInputInteractDialog", &RequestHandler::OpenInputInteractDialog},
	{"GetMonitorList", &RequestHandler::GetMonitorList},
	{"OpenVideoMixProjector", &RequestHandler::OpenVideoMixProjector},
	{"OpenSourceProjector", &RequestHandler::OpenSourceProjector},
};

RequestHandler::RequestHandler(SessionPtr session) : _session(session) {}

RequestResult RequestHandler::ProcessRequest(const Request &request)
{
#ifdef PLUGIN_TESTS
	ScopeProfiler prof{"obs_websocket_request_processing"};
#endif

	// Acquire the process bridge before validating/looking up the handler. A
	// non-Get command which arrives after TakeAccepted must fail closed even if
	// it is unknown to this build: future mutation-capable handlers cannot
	// silently bypass AC-04. The explicit safety-stop allowlist is the only
	// exception: stopping an already-running output must remain available for
	// incident containment and graceful teardown after a rollback freeze.
	// The lease serializes every other handler invocation so a mutation already
	// in flight completes before a Cut publishes pending.
	const bool controlledSceneSwitchBypass = IsControlledSceneSwitchPendingBypass(request);
	const bool safetyStop = IsSafetyStopRequest(request.RequestType);
	pulsar_dual_lane_control::MutationLease mutationLease(
		!IsReadOnlyRequest(request.RequestType) && !controlledSceneSwitchBypass && !safetyStop);
	if (!mutationLease.allowed()) {
		const char *reason = mutationLease.frozen()
			? "PREVIEW_FROZEN: WebSocket mutation rejected after the dual-lane rollback freeze."
			: "PREVIEW_FROZEN: WebSocket mutation rejected while a dual-lane Take is pending.";
		return RequestResult::Error(RequestStatus::RequestProcessingFailed, reason);
	}

	if (!request.RequestData.is_object() && !request.RequestData.is_null())
		return RequestResult::Error(RequestStatus::InvalidRequestFieldType, "Your request data is not an object.");

	if (request.RequestType.empty())
		return RequestResult::Error(RequestStatus::MissingRequestType, "Your request's `requestType` may not be empty.");

	RequestMethodHandler handler;
	try {
		handler = _handlerMap.at(request.RequestType);
	} catch (const std::out_of_range &oor) {
		UNUSED_PARAMETER(oor);
		return RequestResult::Error(RequestStatus::UnknownRequestType, "Your request type is not valid.");
	}

    const RuntimeTelemetryIngress telemetry = BeginRuntimeTakeTelemetry(request);
    RequestResult result = std::bind(handler, this, std::placeholders::_1)(request);
    if (telemetry.requested && telemetry.envelopeValid && !telemetry.status.accepted) {
        blog(LOG_ERROR,
             "[pulsar-runtime-telemetry] ingress not accepted request=%s called=%d available=%d accepted=%d; "
             "the resulting Cut is intentionally not counted as runtime evidence",
             request.RequestType.c_str(), telemetry.status.called, telemetry.status.available,
             telemetry.status.accepted);
    }
    // queueDualLaneCut consumes a valid envelope synchronously.  Retire any
    // envelope left by a rejected/non-Take route before another request can
    // inherit its command/intent identity.
    pulsar_runtime_telemetry::cancel_take();
    return result;
}

std::vector<std::string> RequestHandler::GetRequestList()
{
	std::vector<std::string> ret;
	for (auto const &[key, val] : _handlerMap)
		ret.push_back(key);

	return ret;
}
