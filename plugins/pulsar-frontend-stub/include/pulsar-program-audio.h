#pragma once

// Process-local contract shared by the headless frontend and the websocket
// vendor adapter.  The audio graph is owned by libobs; these constants give
// every consumer one stable name for the common r2 Program bus instead of
// inferring it from the currently selected video lane or output slot.

namespace pulsar_program_audio {

inline constexpr int kSchemaVersion = 1;
inline constexpr char kRouteId[] = "program-common";
inline constexpr char kRouteName[] = "ProgramAudio";
inline constexpr char kScope[] = "program";
inline constexpr char kCutPolicy[] = "common-program-route-unchanged";

} // namespace pulsar_program_audio
