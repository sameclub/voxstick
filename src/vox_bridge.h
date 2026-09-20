#pragma once
#include <Arduino.h>

// Display slots. The bridge always reports Codex in the legacy `codex` block and
// the active agent in `provider`, so both can be shown without extra requests.
enum VoxProviderId { VOX_PROVIDER_CODEX = 0, VOX_PROVIDER_CLAUDE = 1, VOX_PROVIDER_COUNT };

struct VoxProvider {
    char id[16] = "";
    char displayName[20] = "";
    char status[20] = "OFFLINE";
    char project[40] = "";
    char updatedAt[8] = "";
    int quota5h = 0;
    int quota7d = 0;
    bool quota5hValid = false;
    bool quota7dValid = false;
    bool quotaStale = false;
};

struct VoxState {
    char time[8] = "--:--";
    char activeProvider[16] = "codex";
    char bridgeVersion[16] = "";
    char alertEventId[56] = "";
    char alertType[16] = "NONE";
    char alertMessage[80] = "";
    VoxProvider providers[VOX_PROVIDER_COUNT];
};

namespace VoxBridge {

// Reads /voxstick.ini from the TF card. Returns false with a reason in
// configState() when the file is missing or malformed.
bool loadConfig();
bool configured();
const String &configState();
// What the parser actually saw: size, line count, keys it recognised.
const String &configSummary();
const String &host();
uint16_t port();
bool hasToken();
uint32_t autoSleepMs(); // 0 disables the idle timer

// Names the two slots before the first poll, so the screen never shows a
// nameless agent while the bridge is unreachable.
void initState(VoxState &out);
bool pollState(VoxState &out);
bool postEvent(const char *jsonBody);
bool refreshQuota();
bool recordingStart(const char *sessionId, int provider);
bool uploadAudio(const char *sessionId, const uint8_t *data, size_t length);
bool recordingStop(bool paste, char *statusOut, size_t statusLength, int provider);

int lastStatus();
const String &lastError();

} // namespace VoxBridge
