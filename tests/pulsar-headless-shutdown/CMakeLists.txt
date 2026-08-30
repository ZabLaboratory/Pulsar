cmake_minimum_required(VERSION 3.28)
project(pulsar-audio-callback-gate-test LANGUAGES CXX)
enable_testing()

file(READ "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-browser/browser-client.cpp"
     PULSAR_BROWSER_CLIENT_CPP)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-browser/browser-client.hpp"
     PULSAR_BROWSER_CLIENT_HPP)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-browser/obs-browser-source.cpp"
     PULSAR_BROWSER_SOURCE_CPP)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-browser/obs-browser-source.hpp"
     PULSAR_BROWSER_SOURCE_HPP)
if(PULSAR_BROWSER_CLIENT_CPP MATCHES "audio_callbacks_closed[.]load" OR
   PULSAR_BROWSER_CLIENT_CPP MATCHES "audio_callbacks_in_flight[.]fetch_add")
    message(FATAL_ERROR
        "browser audio admission must not split gate load from in-flight accounting")
endif()
if(NOT PULSAR_BROWSER_CLIENT_HPP MATCHES "BrowserAudioCallbackGate" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "audio_callbacks[.]try_acquire")
    message(FATAL_ERROR "browser client is missing the linearized audio callback gate")
endif()
if(NOT PULSAR_BROWSER_CLIENT_HPP MATCHES "close_keepalive" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "close_keepalive[ ]*=[ ]*self" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "close_keepalive[ ]*=[ ]*nullptr" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "client_keepalive_acquired" OR
   NOT PULSAR_BROWSER_CLIENT_CPP MATCHES "client_keepalive_released")
    message(FATAL_ERROR "browser client must retain itself through CEF close finalization")
endif()
if(PULSAR_BROWSER_CLIENT_CPP MATCHES "bs->source" OR
   PULSAR_BROWSER_CLIENT_CPP MATCHES "obs_source_output_audio[(]bs[->]source")
    message(FATAL_ERROR "browser client must use a strong source reference for host callbacks")
endif()
if(NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "obs_source_get_weak_source" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "obs_weak_source_get_source" OR
   NOT PULSAR_BROWSER_SOURCE_CPP MATCHES "obs_weak_source_release" OR
   NOT PULSAR_BROWSER_SOURCE_HPP MATCHES "obs_weak_source_t")
    message(FATAL_ERROR "browser source weak-to-strong lifetime bridge is missing")
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
