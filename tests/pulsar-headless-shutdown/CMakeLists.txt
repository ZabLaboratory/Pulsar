cmake_minimum_required(VERSION 3.28)
project(pulsar-audio-callback-gate-test LANGUAGES CXX)
enable_testing()

file(READ "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-browser/browser-client.cpp"
     PULSAR_BROWSER_CLIENT_CPP)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-browser/browser-client.hpp"
     PULSAR_BROWSER_CLIENT_HPP)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-browser/obs-browser-plugin.cpp"
     PULSAR_BROWSER_PLUGIN_CPP)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-browser/obs-browser-source.cpp"
     PULSAR_BROWSER_SOURCE_CPP)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-browser/obs-browser-source.hpp"
     PULSAR_BROWSER_SOURCE_HPP)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-browser/browser-source-task-state.hpp"
     PULSAR_BROWSER_TASK_STATE_HPP)
if(PULSAR_BROWSER_CLIENT_CPP MATCHES "audio_callbacks_closed[.]load" OR
   PULSAR_BROWSER_CLIENT_CPP MATCHES "audio_callbacks_in_flight[.]fetch_add")
    message(FATAL_ERROR
        "browser audio admission must not split gate load from in-flight accounting")
endif()
if(NOT PULSAR_BROWSER_CLIENT_HPP MATCHES "BrowserAudioCallbackGate" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "audio_callbacks[.]try_acquire")
    message(FATAL_ERROR "browser client is missing the linearized audio callback gate")
endif()
if(PULSAR_BROWSER_CLIENT_HPP MATCHES "finalize_browser_close[(]CefRefPtr" OR
   PULSAR_BROWSER_CLIENT_CPP MATCHES "BrowserSourceFinalizeBrowserClose[(]CefRefPtr" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "self, browser_id" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "browser_finalization_intent" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "browser_detached")
    message(FATAL_ERROR "CEF close finalization must be deferred with browser_id only")
endif()
if(NOT PULSAR_BROWSER_PLUGIN_CPP MATCHES "BrowserSourceWaitForCefReady" OR
   NOT PULSAR_BROWSER_PLUGIN_CPP MATCHES "BrowserSourceCefInitializationFailed" OR
   NOT PULSAR_BROWSER_PLUGIN_CPP MATCHES "event=manager_started" OR
   NOT PULSAR_BROWSER_PLUGIN_CPP MATCHES "event=cef_ready" OR
   NOT PULSAR_BROWSER_PLUGIN_CPP MATCHES "event=readiness_timeout" OR
   NOT PULSAR_BROWSER_PLUGIN_CPP MATCHES "event=readiness_failed" OR
   NOT PULSAR_BROWSER_PLUGIN_CPP MATCHES "event=source_create_rejected" OR
   NOT PULSAR_BROWSER_PLUGIN_CPP MATCHES "event=post_rejected")
    message(FATAL_ERROR "CEF readiness barrier or diagnostics are missing")
endif()
if(NOT PULSAR_BROWSER_CLIENT_HPP MATCHES "close_keepalive" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "close_keepalive[ ]*=[ ]*self" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "close_keepalive[ ]*=[ ]*nullptr" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "client_keepalive_acquired" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "client_keepalive_released" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "CefRefPtr<BrowserClient>[ ]+callback_self[(]this[)]" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "client_callback_keepalive_acquired")
    message(FATAL_ERROR "browser client must retain itself through CEF close finalization")
endif()
if(NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "browser_on_before_close_entry" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "browser_on_before_close_exit")
    message(FATAL_ERROR "CEF close callbacks must expose entry and exit markers")
endif()
if(PULSAR_BROWSER_CLIENT_CPP MATCHES "bs->source" OR
   PULSAR_BROWSER_CLIENT_CPP MATCHES "obs_source_output_audio[(]bs[->]source")
    message(FATAL_ERROR "browser client must use a strong source reference for host callbacks")
endif()
if(NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "obs_source_get_weak_source" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "obs_weak_source_get_source" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "obs_weak_source_release" OR
   NOT PULSAR_BROWSER_SOURCE_HPP MATCHES "obs_weak_source_t" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "BrowserSourceBrowserIdForSource" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "DetachBrowser" OR
   PULSAR_BROWSER_SOURCE_CPP MATCHES "BrowserSourceFinalizeBrowserClose[(]CefRefPtr" OR
   PULSAR_BROWSER_SOURCE_HPP MATCHES "BrowserSourceFinalizeBrowserClose[(]CefRefPtr")
    message(FATAL_ERROR "browser source weak-to-strong lifetime bridge is missing")
endif()
if(NOT PULSAR_BROWSER_SOURCE_HPP MATCHES "BrowserSourceTaskState" OR
   NOT PULSAR_BROWSER_SOURCE_HPP MATCHES "std::atomic<bool> create_browser" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "BrowserSourceAcquireTask" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "active_source_tasks" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "std::deque<int> close_ids" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "BrowserSourceScheduleNextClose" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "browser_close_call" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "browser_close_return" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "browser_close_enqueued" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "browser_close_deferred" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "BrowserSourceBeginDestroyTaskState" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "BrowserSourceReleaseTask" OR
   NOT PULSAR_BROWSER_TASK_STATE_HPP MATCHES "request_delete_and_take_if_idle" OR
   NOT PULSAR_BROWSER_TASK_STATE_HPP MATCHES "request_delete_release_own_lease" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "BrowserSourceRequestDeleteIfIdle" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "BrowserSourceRequestDeleteReleaseOwnLease" OR
   PULSAR_BROWSER_SOURCE_CPP MATCHES "BrowserSourceRequestDelete[(]" OR
   PULSAR_BROWSER_SOURCE_CPP MATCHES "BrowserSourceDeleteIfIdle[(]" OR
   PULSAR_BROWSER_SOURCE_CPP MATCHES "QueueCEFTask[(][[]this" OR
   PULSAR_BROWSER_SOURCE_CPP MATCHES "QueueCEFTask[(][[]&" OR
   PULSAR_BROWSER_SOURCE_CPP MATCHES "bs->RequestBrowserCloseForShutdown" OR
   PULSAR_BROWSER_SOURCE_CPP MATCHES "std::vector<CefRefPtr<CefBrowser>>")
    message(FATAL_ERROR "browser source CEF tasks must use a lifetime gate and browser snapshot")
endif()
add_executable(pulsar-audio-callback-gate-test audio-callback-gate-test.cpp)
target_compile_features(pulsar-audio-callback-gate-test PRIVATE cxx_std_17)
target_include_directories(pulsar-audio-callback-gate-test PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-browser"
)
set_target_properties(pulsar-audio-callback-gate-test PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe"
)

add_test(NAME pulsar-audio-callback-gate-test COMMAND pulsar-audio-callback-gate-test)
set_tests_properties(pulsar-audio-callback-gate-test PROPERTIES LABELS "unit")

add_executable(pulsar-browser-source-task-state-test browser-source-task-state-test.cpp)
target_compile_features(pulsar-browser-source-task-state-test PRIVATE cxx_std_17)
target_include_directories(pulsar-browser-source-task-state-test PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-browser"
)
set_target_properties(pulsar-browser-source-task-state-test PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe"
)

add_test(NAME pulsar-browser-source-task-state-test COMMAND pulsar-browser-source-task-state-test)
set_tests_properties(pulsar-browser-source-task-state-test PROPERTIES LABELS "unit")
