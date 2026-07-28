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

#include <cstring>

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

// SECURITY (#158 / ADR Prism 028 §3.2). A page loaded in a browser source runs
// inside the broadcast process. Above ControlLevel::None it reads this
// process's streaming / recording state through `window.obsstudio`, and higher
// still it reads the scene list and drives the program scene. Nothing in Zab
// reads `window.obsstudio`, so the level is pinned to None wherever a settings
// object can reach obs-browser -- and pinned HARD: an explicit value on the
// wire is overridden, not honoured.
//
// Overriding rather than filling a gap is deliberate, and the threat it answers
// is not a hostile WS client (one already owns the socket that starts streams).
// It is a MISTAKE arriving over that socket: a scene collection imported into
// Prism, a settings blob copied from an OBS profile, an overlay template
// carrying `webpage_control_level: 5` from wherever it was authored. That blob
// reaches SetInputSettings exactly as easily as CreateInput, which is why both
// call this one function (Bastion, PR #161: one policy, one implementation).
// Raising the level for a NAMED need is a reviewed code change here, never a
// field in a request.
void Utils::Obs::ActionHelper::PinBrowserControlLevel(obs_data_t *settings, const char *inputKind, const char *context)
{
	if (!settings || !inputKind || std::strcmp(inputKind, kBrowserSourceKind) != 0)
		return;

	if (obs_data_has_user_value(settings, "webpage_control_level")) {
		long long asked = obs_data_get_int(settings, "webpage_control_level");
		if (asked != kWebpageControlLevelNone)
			blog(LOG_WARNING,
			     "[pulsar-websocket] %s asked for webpage_control_level=%lld ; pinned to %d (None) "
			     "-- #158, no page in Zab reads window.obsstudio",
			     context ? context : "(unknown request)", asked, kWebpageControlLevelNone);
	}
	obs_data_set_int(settings, "webpage_control_level", kWebpageControlLevelNone);
}

obs_sceneitem_t *Utils::Obs::ActionHelper::CreateInput(std::string inputName, std::string inputKind, obs_data_t *inputSettings,
						       obs_scene_t *scene, bool sceneItemEnabled)
{
	// Pin the page's reach into OBS before obs-browser ever sees the settings.
	// `inputSettings` belongs to the caller (RequestHandler::CreateInput builds
	// it fresh from the request and drops it right after), so mutating it in
	// place is safe; the local fallback covers the request that sent no
	// settings object at all, which would otherwise inherit the fork default.
	OBSDataAutoRelease ownedSettings = nullptr;
	if (inputKind == kBrowserSourceKind && !inputSettings) {
		ownedSettings = obs_data_create();
		inputSettings = ownedSettings;
	}
	PinBrowserControlLevel(inputSettings, inputKind.c_str(), "CreateInput");

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
