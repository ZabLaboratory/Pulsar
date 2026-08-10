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
#include <filesystem>

#include <obs-frontend-api.h>

#include "Config.h"
#include "utils/Crypto.h"
#include "utils/Platform.h"
#include "utils/Obs.h"

#define CONFIG_SECTION_NAME "OBSWebSocket"

// ADR-005 §3.6: single definition of the predicate WebSocketServer::Start()
// already computed inline (the three literal loopback forms Pulsar's own
// Config::Load() can produce -- PULSAR_WS_BIND is a raw string, not
// validated against a whitelist, so this only recognises the forms this
// codebase itself writes into BindAddress; it is not a general loopback
// classifier).
bool ComputeLoopbackOnly(const std::string &bindAddress)
{
	return bindAddress == "127.0.0.1" || bindAddress == "::1" || bindAddress == "localhost";
}

#define CONFIG_PARAM_FIRSTLOAD "FirstLoad"
#define CONFIG_PARAM_ENABLED "ServerEnabled"
#define CONFIG_PARAM_PORT "ServerPort"
#define CONFIG_PARAM_ALERTS "AlertsEnabled"
#define CONFIG_PARAM_AUTHREQUIRED "AuthRequired"
#define CONFIG_PARAM_PASSWORD "ServerPassword"

#define CONFIG_FILE_NAME "config.json"
#define PARAM_FIRSTLOAD "first_load"
#define PARAM_ENABLED "server_enabled"
#define PARAM_PORT "server_port"
#define PARAM_ALERTS "alerts_enabled"
#define PARAM_AUTHREQUIRED "auth_required"
#define PARAM_PASSWORD "server_password"

#define CMDLINE_WEBSOCKET_PORT "websocket_port"
#define CMDLINE_WEBSOCKET_IPV4_ONLY "websocket_ipv4_only"
#define CMDLINE_WEBSOCKET_PASSWORD "websocket_password"
#define CMDLINE_WEBSOCKET_DEBUG "websocket_debug"

void Config::Load(json config)
{
	// Only load from plugin config directory if there hasn't been a migration
	if (config.is_null()) {
		std::string configFilePath = Utils::Obs::StringHelper::GetModuleConfigPath(CONFIG_FILE_NAME);
		Utils::Json::GetJsonFileContent(configFilePath, config); // Fetch the existing config, which may not exist
	}

	if (!config.is_object()) {
		blog(LOG_INFO, "[Config::Load] Existing configuration not found, using defaults.");
		config = json::object();
	}

	if (config.contains(PARAM_FIRSTLOAD) && config[PARAM_FIRSTLOAD].is_boolean())
		FirstLoad = config[PARAM_FIRSTLOAD];
	if (config.contains(PARAM_ENABLED) && config[PARAM_ENABLED].is_boolean())
		ServerEnabled = config[PARAM_ENABLED];
	if (config.contains(PARAM_ALERTS) && config[PARAM_ALERTS].is_boolean())
		AlertsEnabled = config[PARAM_ALERTS];
	if (config.contains(PARAM_PORT) && config[PARAM_PORT].is_number_unsigned())
		ServerPort = config[PARAM_PORT];
	if (config.contains(PARAM_AUTHREQUIRED) && config[PARAM_AUTHREQUIRED].is_boolean())
		AuthRequired = config[PARAM_AUTHREQUIRED];
	if (config.contains(PARAM_PASSWORD) && config[PARAM_PASSWORD].is_string())
		ServerPassword = config[PARAM_PASSWORD];

	// Set server password and save it to the config before processing overrides,
	// so that there is always a true configured password regardless of if
	// future loads use the override flag.
	if (FirstLoad) {
		FirstLoad = false;
		if (ServerPassword.empty()) {
			blog(LOG_INFO, "[Config::Load] (FirstLoad) Generating new server password.");
			ServerPassword = Utils::Crypto::GeneratePassword();
		} else {
			blog(LOG_INFO, "[Config::Load] (FirstLoad) Not generating new password since one is already configured.");
		}
		Save();
	}

	// If there are migrated settings, write them to disk before processing arguments.
	if (!config.empty())
		Save();

	// Process `--websocket_port` override
	QString portArgument = Utils::Platform::GetCommandLineArgument(CMDLINE_WEBSOCKET_PORT);
	if (portArgument != "") {
		bool ok;
		uint16_t serverPort = portArgument.toUShort(&ok);
		if (ok) {
			blog(LOG_INFO, "[Config::Load] --websocket_port passed. Overriding WebSocket port with: %d", serverPort);
			PortOverridden = true;
			ServerPort = serverPort;
		} else {
			blog(LOG_WARNING, "[Config::Load] Not overriding WebSocket port since integer conversion failed.");
		}
	}

	// Pulsar fork (#134): PULSAR_WS_BIND overrides the loopback default.
	//
	// Env, not config.json and not a command-line flag: every other Pulsar knob
	// is a PULSAR_* env var read at spawn (docs/PROTOCOL.md), the parent process
	// is the only party that legitimately decides how far this server reaches,
	// and keeping it out of the persisted config means a stale/tampered
	// config.json cannot silently widen the bind on a later boot.
	if (const char *e = std::getenv("PULSAR_WS_BIND"); e && *e)
		BindAddress = e;

	// Process `--websocket_ipv4_only` override
	if (Utils::Platform::GetCommandLineFlagSet(CMDLINE_WEBSOCKET_IPV4_ONLY)) {
		blog(LOG_INFO, "[Config::Load] --websocket_ipv4_only passed. Binding only to IPv4 interfaces.");
		Ipv4Only = true;
	}

	// Process `--websocket_password` override
	QString passwordArgument = Utils::Platform::GetCommandLineArgument(CMDLINE_WEBSOCKET_PASSWORD);
	if (passwordArgument != "") {
		blog(LOG_INFO, "[Config::Load] --websocket_password passed. Overriding WebSocket password.");
		PasswordOverridden = true;
		AuthRequired = true;
		ServerPassword = passwordArgument.toStdString();
	}

	// Process `--websocket_debug` override
	if (Utils::Platform::GetCommandLineFlagSet(CMDLINE_WEBSOCKET_DEBUG)) {
		// Debug does not persist on reload, so we let people override it with a flag.
		blog(LOG_INFO, "[Config::Load] --websocket_debug passed. Enabling debug logging.");
		DebugEnabled = true;
	}
}

void Config::Save()
{
	json config;

	std::string configFilePath = Utils::Obs::StringHelper::GetModuleConfigPath(CONFIG_FILE_NAME);
	Utils::Json::GetJsonFileContent(configFilePath, config); // Fetch the existing config, which may not exist

	config[PARAM_FIRSTLOAD] = FirstLoad.load();
	config[PARAM_ENABLED] = ServerEnabled.load();
	if (!PortOverridden)
		config[PARAM_PORT] = ServerPort.load();
	config[PARAM_ALERTS] = AlertsEnabled.load();
	if (!PasswordOverridden) {
		config[PARAM_AUTHREQUIRED] = AuthRequired.load();
		config[PARAM_PASSWORD] = ServerPassword;
	}

	if (Utils::Json::SetJsonFileContent(configFilePath, config))
		blog(LOG_DEBUG, "[Config::Save] Saved config.");
	else
		blog(LOG_ERROR, "[Config::Save] Failed to write config file!");
}

// Pulsar fork: the original MigrateGlobalConfigData / MigratePersistentData
// both depend on the OBS frontend (`obs_frontend_get_app_config`,
// `obs_frontend_get_current_profile_path`) which returns null in our
// headless service and would crash the migrate path. Pulsar starts
// from a clean state -- there are no legacy obs-websocket configs
// to migrate. Both functions become no-ops.

json MigrateGlobalConfigData()
{
	return json();
}

bool MigratePersistentData()
{
	std::error_code ec;
	auto moduleConfigDirectory = std::filesystem::u8path(Utils::Obs::StringHelper::GetModuleConfigPath(""));
	if (!std::filesystem::exists(moduleConfigDirectory, ec))
		std::filesystem::create_directories(moduleConfigDirectory, ec);
	if (ec) {
		blog(LOG_ERROR, "[MigratePersistentData] Failed to create directory `%s`: %s", moduleConfigDirectory.c_str(),
		     ec.message().c_str());
		return false;
	}
	return true;
}
