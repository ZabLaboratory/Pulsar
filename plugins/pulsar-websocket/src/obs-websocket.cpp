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

#include <QAction>
#include <QMainWindow>
#include <obs-module.h>
#include <obs-frontend-api.h>

#include "obs-websocket.h"
#include "Config.h"
#include "WebSocketApi.h"
#include "websocketserver/WebSocketServer.h"
#include "eventhandler/EventHandler.h"
// Pulsar fork: forms/SettingsDialog.h removed -- the upstream Qt UI
// is gone, the websocket service runs without a settings dialog.

OBS_DECLARE_MODULE()
OBS_MODULE_USE_DEFAULT_LOCALE("obs-websocket", "en-US")
OBS_MODULE_AUTHOR("OBSProject")
const char *obs_module_name(void)
{
	return "obs-websocket";
}
const char *obs_module_description(void)
{
	return obs_module_text("OBSWebSocket.Plugin.Description");
}

os_cpu_usage_info_t *_cpuUsageInfo;
ConfigPtr _config;
EventHandlerPtr _eventHandler;
WebSocketApiPtr _webSocketApi;
WebSocketServerPtr _webSocketServer;
// Pulsar fork: _settingsDialog removed (no Qt UI in headless mode).

void OnWebSocketApiVendorEvent(std::string vendorName, std::string eventType, obs_data_t *obsEventData);
void OnEvent(uint64_t requiredIntent, std::string eventType, json eventData, uint8_t rpcVersion);
void OnObsReady(bool ready);

// ADR-005 §3.6: the diagnostic surface (registered by pulsar-multi-stream,
// a separate module) needs to know whether THIS server is bound to the
// loopback before it may serve log content. obs-websocket's own request
// dispatch (obs_websocket_call_request) has no "am I loopback-bound"
// request, and Config/_config are private to this module -- so this is
// exposed the same way obs-websocket's own API handle is (WebSocketApi's
// "obs_websocket_api_get_ph" on the GLOBAL proc handler, obs-websocket.cpp
// vs. WebSocketApi.cpp): any module in the process, DLL or the host exe
// alike, can query it without a compile-time dependency on this plugin.
static void pulsar_is_loopback_only_cb(void * /*priv_data*/, calldata_t *cd)
{
	// Local copy: obs_module_unload() can null out the global `_config`
	// from another thread between the truthiness check and the dereference
	// below. Holding our own shared_ptr keeps the Config object alive for
	// the rest of this call regardless of what happens to the global.
	ConfigPtr cfg = _config;
	if (!cfg) {
		calldata_set_bool(cd, "success", false);
		return;
	}
	calldata_set_bool(cd, "loopback", ComputeLoopbackOnly(cfg->BindAddress));
	calldata_set_bool(cd, "success", true);
}

bool obs_module_load(void)
{
	blog(LOG_INFO, "[obs_module_load] you can haz websockets (Version: %s | RPC Version: %d)", OBS_WEBSOCKET_VERSION,
	     OBS_WEBSOCKET_RPC_VERSION);
	blog(LOG_INFO, "[obs_module_load] Qt version (compile-time): %s | Qt version (run-time): %s", QT_VERSION_STR, qVersion());
	blog(LOG_INFO, "[obs_module_load] Linked ASIO Version: %d", ASIO_VERSION);

	// Initialize the cpu stats
	_cpuUsageInfo = os_cpu_usage_info_start();

	// Handle migrations
	if (!MigratePersistentData()) {
		os_cpu_usage_info_destroy(_cpuUsageInfo);
		return false;
	}
	json migratedConfig = MigrateGlobalConfigData();

	// Create the config manager then load the parameters from storage
	_config = std::make_shared<Config>();
	_config->Load(migratedConfig);

	// ADR-005 §3.6: publish the loopback predicate on the global proc
	// handler so a module outside this DLL (pulsar-multi-stream's
	// diagnostic surface) can gate log content on it without linking
	// against this plugin.
	proc_handler_add(obs_get_proc_handler(),
			 "bool pulsar_websocket_is_loopback_only(out bool loopback)", &pulsar_is_loopback_only_cb,
			 nullptr);

	// Initialize the event handler
	_eventHandler = std::make_shared<EventHandler>();
	_eventHandler->SetEventCallback(OnEvent);
	_eventHandler->SetObsReadyCallback(OnObsReady);

	// Initialize the plugin/script API
	_webSocketApi = std::make_shared<WebSocketApi>();
	_webSocketApi->SetVendorEventCallback(OnWebSocketApiVendorEvent);

	// Initialize the WebSocket server
	_webSocketServer = std::make_shared<WebSocketServer>();
	_webSocketServer->SetClientSubscriptionCallback(std::bind(&EventHandler::ProcessSubscriptionChange, _eventHandler.get(),
								  std::placeholders::_1, std::placeholders::_2));

	// Pulsar fork: SettingsDialog construction + Tools menu
	// registration removed. The upstream code calls
	// obs_frontend_get_main_window / obs_frontend_add_tools_menu_qaction
	// which require a Qt-based frontend that does not exist in the
	// headless service. The websocket server itself does not depend
	// on these -- it accepts connections, dispatches v5 requests,
	// emits events. Configuration is loaded from the persisted
	// config file (or migrated defaults).

	blog(LOG_INFO, "[obs_module_load] Module loaded.");
	return true;
}

#ifdef PLUGIN_TESTS
void test_call_request();
void test_register_event_callback();
void test_register_vendor();
#endif

void obs_module_post_load(void)
{
#ifdef PLUGIN_TESTS
	test_call_request();
	test_register_event_callback();
	test_register_vendor();
#endif

	// Server will accept clients, but requests and events will not be served until FINISHED_LOADING occurs
	if (_config->ServerEnabled) {
		blog(LOG_INFO, "[obs_module_post_load] WebSocket server is enabled, starting...");
		_webSocketServer->Start();
	}
}

void obs_module_unload(void)
{
	blog(LOG_INFO, "[obs_module_unload] Shutting down...");

	// Shutdown the WebSocket server if it is running
	if (_webSocketServer->IsListening()) {
		blog_debug("[obs_module_unload] WebSocket server is running. Stopping...");
		_webSocketServer->Stop();
	}

	// Release the WebSocket server
	_webSocketServer->SetClientSubscriptionCallback(nullptr);
	_webSocketServer = nullptr;

	// Release the plugin/script api
	_webSocketApi = nullptr;

	// Release the event handler
	_eventHandler->SetObsReadyCallback(nullptr);
	_eventHandler->SetEventCallback(nullptr);
	_eventHandler = nullptr;

	// Release the config manager
	_config = nullptr;

	// Destroy the cpu stats
	os_cpu_usage_info_destroy(_cpuUsageInfo);

	blog(LOG_INFO, "[obs_module_unload] Finished shutting down.");
}

os_cpu_usage_info_t *GetCpuUsageInfo()
{
	return _cpuUsageInfo;
}

ConfigPtr GetConfig()
{
	return _config;
}

EventHandlerPtr GetEventHandler()
{
	return _eventHandler;
}

WebSocketApiPtr GetWebSocketApi()
{
	return _webSocketApi;
}

WebSocketServerPtr GetWebSocketServer()
{
	return _webSocketServer;
}

bool IsDebugEnabled()
{
	return _config && _config->DebugEnabled;
}

/**
 * An event has been emitted from a vendor. 
 *
 * A vendor is a unique name registered by a third-party plugin or script, which allows for custom requests and events to be added to obs-websocket.
 * If a plugin or script implements vendor requests or events, documentation is expected to be provided with them.
 *
 * @dataField vendorName | String | Name of the vendor emitting the event
 * @dataField eventType  | String | Vendor-provided event typedef
 * @dataField eventData  | Object | Vendor-provided event data. {} if event does not provide any data
 *
 * @eventSubscription Vendors
 * @eventType VendorEvent
 * @complexity 3
 * @rpcVersion -1
 * @initialVersion 5.0.0
 * @api events
 * @category general
 */
void OnWebSocketApiVendorEvent(std::string vendorName, std::string eventType, obs_data_t *obsEventData)
{
	json eventData = Utils::Json::ObsDataToJson(obsEventData);

	json broadcastEventData;
	broadcastEventData["vendorName"] = vendorName;
	broadcastEventData["eventType"] = eventType;
	broadcastEventData["eventData"] = eventData;

	_webSocketServer->BroadcastEvent(EventSubscription::Vendors, "VendorEvent", broadcastEventData);
}

// Sent from: EventHandler
void OnEvent(uint64_t requiredIntent, std::string eventType, json eventData, uint8_t rpcVersion)
{
	if (_webSocketServer)
		_webSocketServer->BroadcastEvent(requiredIntent, eventType, eventData, rpcVersion);
	if (_webSocketApi)
		_webSocketApi->BroadcastEvent(requiredIntent, eventType, eventData, rpcVersion);
}

// Sent from: EventHandler
void OnObsReady(bool ready)
{
	if (_webSocketServer)
		_webSocketServer->SetObsReady(ready);
	if (_webSocketApi)
		_webSocketApi->SetObsReady(ready);
}

#ifdef PLUGIN_TESTS
void test_call_request()
{
	blog(LOG_INFO, "[test_call_request] Testing obs-websocket plugin API request calling...");

	struct obs_websocket_request_response *response = obs_websocket_call_request("GetVersion");
	if (response) {
		blog(LOG_INFO, "[test_call_request] Called GetVersion. Status Code: %u | Comment: %s | Response Data: %s",
		     response->status_code, response->comment, response->response_data);
		obs_websocket_request_response_free(response);
	} else {
		blog(LOG_ERROR, "[test_call_request] Failed to call GetVersion request via obs-websocket plugin API!");
	}

	blog(LOG_INFO, "[test_call_request] Test done.");
}

static void test_event_cb(uint64_t eventIntent, const char *eventType, const char *eventData, void *priv_data)
{
	blog(LOG_DEBUG, "[test_event_cb] New event! Type: %s | Data: %s", eventType, eventData);

	UNUSED_PARAMETER(eventIntent);
	UNUSED_PARAMETER(priv_data);
}

void test_register_event_callback()
{
	blog(LOG_INFO, "[test_register_event_callback] Registering test event callback...");

	if (!obs_websocket_register_event_callback(test_event_cb, nullptr))
		blog(LOG_ERROR, "[test_register_event_callback] Failed to register event callback!");

	blog(LOG_INFO, "[test_register_event_callback] Test done.");
}

static void test_vendor_request_cb(obs_data_t *requestData, obs_data_t *responseData, void *priv_data)
{
	blog(LOG_INFO, "[test_vendor_request_cb] Request called! Request data: %s", obs_data_get_json(requestData));

	// Set an item to the response data
	obs_data_set_string(responseData, "test", "pp");

	// Emit an event with the request data as the event data
	obs_websocket_vendor_emit_event(priv_data, "TestEvent", requestData);
}

void test_register_vendor()
{
	blog(LOG_INFO, "[test_register_vendor] Testing vendor registration...");

	// Test plugin API version fetch
	uint apiVersion = obs_websocket_get_api_version();
	blog(LOG_INFO, "[test_register_vendor] obs-websocket plugin API version: %u", apiVersion);

	// Test vendor creation
	auto vendor = obs_websocket_register_vendor("obs-websocket-test");
	if (!vendor) {
		blog(LOG_ERROR, "[test_register_vendor] Failed to create vendor!");
		return;
	}

	// Test vendor request registration
	if (!obs_websocket_vendor_register_request(vendor, "TestRequest", test_vendor_request_cb, vendor)) {
		blog(LOG_ERROR, "[test_register_vendor] Failed to register vendor request!");
		return;
	}

	blog(LOG_INFO, "[test_register_vendor] Test done.");
}
#endif
