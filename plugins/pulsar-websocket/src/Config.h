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

#include <atomic>
#include <QString>
#include <util/config-file.h>

#include "utils/Json.h"
#include "plugin-macros.generated.h"

struct Config {
	void Load(json config = nullptr);
	void Save();

	std::atomic<bool> PortOverridden = false;
	std::atomic<bool> PasswordOverridden = false;

	std::atomic<bool> FirstLoad = true;
	// Pulsar fork: ServerEnabled defaults to true (vs false upstream).
	// Upstream relied on the SettingsDialog UI to opt the user into
	// starting the server. With the dialog removed, the server is
	// the entire reason the plugin exists, so enable by default.
	std::atomic<bool> ServerEnabled = true;
	std::atomic<uint16_t> ServerPort = 4455;
	// Pulsar fork (#134): the listen address, LOOPBACK by default.
	// Upstream obs-websocket listens on every interface because it is a
	// desktop app the user opts into exposing. Pulsar is spawned as a child
	// process and every consumer -- Prism (packages/pulsar-bundle*/src/spawn.ts
	// -> ws://127.0.0.1:<port>), the CI probes, the PULSAR_READY sentinel --
	// connects over the loopback, so a wider bind buys nothing and exposes the
	// whole v5 surface (including the egress path #131 made live) to the LAN
	// behind a single password. Widened only by an explicit PULSAR_WS_BIND.
	std::string BindAddress = "127.0.0.1";
	std::atomic<bool> Ipv4Only = false;
	std::atomic<bool> DebugEnabled = false;
	std::atomic<bool> AlertsEnabled = false;
	std::atomic<bool> AuthRequired = true;
	std::string ServerPassword;
};

// ADR-005 §3.6: the loopback predicate WebSocketServer::Start() already
// computes to decide the bind address (WebSocketServer.cpp) -- hoisted here
// so it has a single definition shared by the server itself and by the
// diagnostic surface's bind-condition check (both need to agree on the
// same three literal forms).
bool ComputeLoopbackOnly(const std::string &bindAddress);

json MigrateGlobalConfigData();
bool MigratePersistentData();
