/*
obs-websocket
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

#include "Obs.h"
#include "plugin-macros.generated.h"

struct CreateSceneItemData {
	obs_source_t *source;                             // In
	bool sceneItemEnabled;                            // In
	obs_transform_info *sceneItemTransform = nullptr; // In
	obs_sceneitem_crop *sceneItemCrop = nullptr;      // In
	OBSSceneItem sceneItem;                           // Out
};

static void CreateSceneItemHelper(void *_data, obs_scene_t *scene)
{
	auto *data = static_cast<CreateSceneItemData *>(_data);
	data->sceneItem = obs_scene_add(scene, data->source);

	if (data->sceneItemTransform)
		obs_sceneitem_set_info2(data->sceneItem, data->sceneItemTransform);

	if (data->sceneItemCrop)
		obs_sceneitem_set_crop(data->sceneItem, data->sceneItemCrop);

	obs_sceneitem_set_visible(data->sceneItem, data->sceneItemEnabled);
}

obs_sceneitem_t *Utils::Obs::ActionHelper::CreateSceneItem(obs_source_t *source, obs_scene_t *scene, bool sceneItemEnabled,
							   obs_transform_info *sceneItemTransform,
							   obs_sceneitem_crop *sceneItemCrop)
{
	// Sanity check for valid scene
	if (!(source && scene))
		return nullptr;

	// Create data struct and populate for scene item creation
	CreateSceneItemData data;
	data.source = source;
	data.sceneItemEnabled = sceneItemEnabled;
	data.sceneItemTransform = sceneItemTransform;
	data.sceneItemCrop = sceneItemCrop;

	// Enter graphics context and create the scene item
	obs_enter_graphics();
	obs_scene_atomic_update(scene, CreateSceneItemHelper, &data);
	obs_leave_graphics();

	obs_sceneitem_addref(data.sceneItem);

	return data.sceneItem;
}

// obs-browser's `webpage_control_level`, ControlLevel::None == 0
// (plugins/pulsar-browser/obs-browser-source.hpp). Duplicated as a literal so
// this translation unit does not have to link CEF ;
// scripts/check-webpage-control-level.py cross-checks the two and fails the
// lint job on drift.
static constexpr const char *kBrowserSourceKind = "browser_source";
static constexpr int kWebpageControlLevelNone = 0;

obs_sceneitem_t *Utils::Obs::ActionHelper::CreateInput(std::string inputName, std::string inputKind, obs_data_t *inputSettings,
						       obs_scene_t *scene, bool sceneItemEnabled)
{
	// SECURITY (#158 / ADR Prism 028 §3.2) -- the v5 CreateInput surface is the
	// OTHER way a browser source enters this process, and it takes its settings
	// verbatim from the caller. Left alone, a `browser_source` created without a
	// `webpage_control_level` key inherits obs-browser's default, i.e. a level
	// chosen by the fork rather than by us : the page then reads this process's
	// streaming / recording state through `window.obsstudio`.
	//
	// Pin it, on this single choke point (every v5 input creation goes through
	// here), and pin it HARD : an explicit value in the request is overridden,
	// not honoured. Zab has no overlay that reads `window.obsstudio`, so a
	// request asking for more is either a mistake or an escalation attempt, and
	// granting one on demand would be a new capability -- which #158 forbids.
	// Raising the level for a NAMED need is a code change here, reviewed, not a
	// field in a wire request.
	// `inputSettings` belongs to the caller (RequestHandler::CreateInput builds
	// it fresh from the request and drops it right after), so pinning in place
	// is safe -- and it is the only form that also covers the request that sent
	// no settings object at all.
	OBSDataAutoRelease ownedSettings = nullptr;
	if (inputKind == kBrowserSourceKind) {
		if (!inputSettings) {
			ownedSettings = obs_data_create();
			inputSettings = ownedSettings;
		}
		if (obs_data_has_user_value(inputSettings, "webpage_control_level")) {
			long long asked = obs_data_get_int(inputSettings, "webpage_control_level");
			if (asked != kWebpageControlLevelNone)
				blog(LOG_WARNING,
				     "[pulsar-websocket] CreateInput('%s') asked for webpage_control_level=%lld ; "
				     "pinned to %d (None) -- #158, no page in Zab reads window.obsstudio",
				     inputName.c_str(), asked, kWebpageControlLevelNone);
		}
		obs_data_set_int(inputSettings, "webpage_control_level", kWebpageControlLevelNone);
	}

	// Create the input
	OBSSourceAutoRelease input = obs_source_create(inputKind.c_str(), inputName.c_str(), inputSettings, nullptr);

	// Check that everything was created properly
	if (!input)
		return nullptr;

	// Apparently not all default input properties actually get applied on creation (smh)
	uint32_t flags = obs_source_get_output_flags(input);
	if ((flags & OBS_SOURCE_MONITOR_BY_DEFAULT) != 0)
		obs_source_set_monitoring_type(input, OBS_MONITORING_TYPE_MONITOR_ONLY);

	// Create a scene item for the input
	obs_sceneitem_t *ret = CreateSceneItem(input, scene, sceneItemEnabled);

	// If creation failed, remove the input
	if (!ret)
		obs_source_remove(input);

	return ret;
}

obs_source_t *Utils::Obs::ActionHelper::CreateSourceFilter(obs_source_t *source, std::string filterName, std::string filterKind,
							   obs_data_t *filterSettings)
{
	obs_source_t *filter = obs_source_create_private(filterKind.c_str(), filterName.c_str(), filterSettings);

	if (!filter)
		return nullptr;

	obs_source_filter_add(source, filter);

	return filter;
}

void Utils::Obs::ActionHelper::SetSourceFilterIndex(obs_source_t *source, obs_source_t *filter, size_t index)
{
	size_t currentIndex = Utils::Obs::NumberHelper::GetSourceFilterIndex(source, filter);
	obs_order_movement direction = index > currentIndex ? OBS_ORDER_MOVE_DOWN : OBS_ORDER_MOVE_UP;

	while (currentIndex != index) {
		obs_source_filter_set_order(source, filter, direction);

		if (direction == OBS_ORDER_MOVE_DOWN)
			currentIndex++;
		else
			currentIndex--;
	}
}
