#include <obs-module.h>
#include <obs-websocket-api.h>
#include <callback/calldata.h>
#include <callback/proc.h>
#include <callback/signal.h>

#include <cstdlib>
#include <cstring>
#include <string>

OBS_DECLARE_MODULE()
OBS_MODULE_USE_DEFAULT_LOCALE("z-group-composition-shutdown-test-helper", "en-US")

namespace {
struct RemoveRequest {
  std::string source_name;
  obs_data_t *response = nullptr;
};

obs_source_t *pending_unload_source = nullptr;

struct BrowserSourceCount {
  size_t count = 0;
};

bool count_browser_sources(void *data, obs_source_t *source)
{
  auto &count = *static_cast<BrowserSourceCount *>(data);
  const char *id = obs_source_get_id(source);
  if (id && (std::strcmp(id, "browser_source") == 0 ||
             std::strcmp(id, "browser_source_v2") == 0)) {
    ++count.count;
    return false;
  }
  return true;
}

void no_browser_pre_shutdown(void *, calldata_t *cd)
{
  BrowserSourceCount browser_sources;
  obs_enum_sources(count_browser_sources, &browser_sources);
  const bool success = browser_sources.count == 0;
  if (cd)
    calldata_set_bool(cd, "success", success);
  blog(LOG_INFO,
       "PULSAR_GROUP_COMPOSITION_TEST_HELPER event=no_browser_pre_shutdown "
       "success=%d browser_sources=%zu",
       success ? 1 : 0, browser_sources.count);
}

void remove_source_graphics(void *data)
{
  auto &request = *static_cast<RemoveRequest *>(data);
  obs_source_t *source = obs_get_source_by_name(request.source_name.c_str());
  if (!source) {
    obs_data_set_bool(request.response, "ok", false);
    obs_data_set_string(request.response, "error", "source-not-found");
    return;
  }

  const char *uuid = obs_source_get_uuid(source);
  const std::string removed_uuid = uuid ? uuid : "";
  obs_source_remove(source);
  obs_data_set_bool(request.response, "ok", true);
  obs_data_set_bool(request.response, "executedOnGraphicsTask",
                    obs_in_task_thread(OBS_TASK_GRAPHICS));
  obs_data_set_string(request.response, "removedSourceUuid", removed_uuid.c_str());
  obs_source_release(source);
}

void remove_source_request(obs_data_t *request, obs_data_t *response, void *)
{
  const char *source_name = obs_data_get_string(request, "sourceName");
  if (!source_name || !*source_name) {
    obs_data_set_bool(response, "ok", false);
    obs_data_set_string(response, "error", "sourceName-required");
    return;
  }

  RemoveRequest task{source_name, response};
  obs_queue_task(OBS_TASK_GRAPHICS, remove_source_graphics, &task, true);
}

void arm_pending_unload_removal(obs_data_t *request, obs_data_t *response, void *)
{
  if (pending_unload_source) {
    obs_data_set_bool(response, "ok", false);
    obs_data_set_string(response, "error", "already-armed");
    return;
  }
  const char *source_name = obs_data_get_string(request, "sourceName");
  if (!source_name || !*source_name) {
    obs_data_set_bool(response, "ok", false);
    obs_data_set_string(response, "error", "sourceName-required");
    return;
  }

  obs_data_t *settings = obs_data_create();
  obs_data_set_int(settings, "width", 8);
  obs_data_set_int(settings, "height", 8);
  obs_data_set_int(settings, "color", 0xff00ffff);
  pending_unload_source = obs_source_create_private(
      "color_source_v3", source_name, settings);
  obs_data_release(settings);
  if (!pending_unload_source) {
    obs_data_set_bool(response, "ok", false);
    obs_data_set_string(response, "error", "synthetic-source-create-failed");
    return;
  }

  const char *uuid = obs_source_get_uuid(pending_unload_source);
  obs_data_set_bool(response, "ok", true);
  obs_data_set_string(response, "sourceName", source_name);
  obs_data_set_string(response, "sourceUuid", uuid ? uuid : "");
  blog(LOG_INFO,
       "PULSAR_GROUP_COMPOSITION_TEST_HELPER event=pending_unload_source_armed");
}
} // namespace

bool obs_module_load(void)
{
  blog(LOG_INFO, "PULSAR_GROUP_COMPOSITION_TEST_HELPER event=module_load");
  const char *no_browser_fixture =
      std::getenv("PULSAR_TEST_NO_BROWSER_PRE_SHUTDOWN");
  if (no_browser_fixture && std::strcmp(no_browser_fixture, "1") == 0) {
    proc_handler_t *global = obs_get_proc_handler();
    if (!global) {
      blog(LOG_ERROR,
           "PULSAR_GROUP_COMPOSITION_TEST_HELPER "
           "event=no_browser_pre_shutdown_registration_failed "
           "reason=proc_handler_unavailable");
      return false;
    }
    proc_handler_add(global,
                     "void pulsar_browser_pre_shutdown(out bool success)",
                     no_browser_pre_shutdown, nullptr);
    blog(LOG_INFO,
         "PULSAR_GROUP_COMPOSITION_TEST_HELPER "
         "event=no_browser_pre_shutdown_fixture_registered "
         "scope=no_browser_sources_only cef_shutdown=not_exercised");
  }
  return true;
}

void obs_module_post_load(void)
{
  auto vendor = obs_websocket_register_vendor("group-composition-test-helper");
  if (vendor) {
    obs_websocket_vendor_register_request(vendor, "RemoveSourceOnGraphics",
                                          remove_source_request, nullptr);
    obs_websocket_vendor_register_request(vendor, "ArmPendingUnloadRemoval",
                                          arm_pending_unload_removal, nullptr);
  }
}

void obs_module_unload(void)
{
  if (!pending_unload_source)
    return;
  const char *uuid = obs_source_get_uuid(pending_unload_source);
  const std::string source_uuid = uuid ? uuid : "";
  const bool on_graphics_task = obs_in_task_thread(OBS_TASK_GRAPHICS);
  obs_source_remove(pending_unload_source);
  signal_handler_t *handler = obs_get_signal_handler();
  bool global_signal_forwarded = false;
  if (handler) {
    calldata_t data;
    uint8_t storage[128];
    calldata_init_fixed(&data, storage, sizeof(storage));
    calldata_set_ptr(&data, "source", pending_unload_source);
    signal_handler_signal(handler, "source_remove", &data);
    global_signal_forwarded = true;
  }
  blog(LOG_INFO,
       "PULSAR_GROUP_COMPOSITION_TEST_HELPER event=module_unload_removed_source "
       "uuid=%s on_graphics_task=%d removed=%d global_signal_forwarded=%d",
       source_uuid.c_str(), on_graphics_task ? 1 : 0,
       obs_source_removed(pending_unload_source) ? 1 : 0,
       global_signal_forwarded ? 1 : 0);
  obs_source_release(pending_unload_source);
  pending_unload_source = nullptr;
}
