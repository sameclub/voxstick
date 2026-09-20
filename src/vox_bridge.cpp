#include "vox_bridge.h"
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <SD_MMC.h>
#include <WiFi.h>

#define FIRMWARE_NAME "voxstick-s3ai"
#define FIRMWARE_VERSION "1.2"

namespace {

String bridgeHost;
uint16_t bridgePort = 8765;
String bridgeToken;
uint32_t autoSleepMillis = 10000;
String state = "NO CONFIG";
String summary;
String lastErrorText;
int lastStatusCode = 0;

WiFiClient client;

String url(const char *path) {
    return "http://" + bridgeHost + ":" + String(bridgePort) + path;
}

void applyHeaders(HTTPClient &http) {
    http.addHeader("X-Vox-Stick-Firmware-Name", FIRMWARE_NAME);
    http.addHeader("X-Vox-Stick-Firmware-Version", FIRMWARE_VERSION);
    http.addHeader("X-Vox-Stick-Firmware-Transport", "HTTP");
    http.addHeader("X-Vox-Stick-Firmware-Build-Date", __DATE__);
    if (bridgeToken.length()) http.addHeader("X-Vox-Stick-Token", bridgeToken);
}

bool begin(HTTPClient &http, const char *path, uint16_t timeoutMs) {
    if (!VoxBridge::configured() || WiFi.status() != WL_CONNECTED) {
        lastErrorText = VoxBridge::configured() ? "WiFi offline" : state;
        return false;
    }
    if (!http.begin(client, url(path))) {
        lastErrorText = "begin failed";
        return false;
    }
    http.setConnectTimeout(2500);
    http.setTimeout(timeoutMs);
    http.setReuse(false);
    return true;
}

bool postJson(const char *path, const char *body, String *response, uint16_t timeoutMs = 5000) {
    HTTPClient http;
    if (!begin(http, path, timeoutMs)) return false;
    http.addHeader("Content-Type", "application/json");
    applyHeaders(http);
    lastStatusCode = http.POST(reinterpret_cast<uint8_t *>(const_cast<char *>(body)), strlen(body));
    bool ok = lastStatusCode == 200;
    if (response && ok) *response = http.getString();
    if (!ok) lastErrorText = "HTTP " + String(lastStatusCode);
    http.end();
    return ok;
}

void copyString(char *destination, size_t size, const char *source) {
    if (!source) source = "";
    strncpy(destination, source, size - 1);
    destination[size - 1] = '\0';
}

int percentOrInvalid(JsonVariantConst value, bool &valid) {
    valid = !value.isNull();
    if (!valid) return 0;
    return constrain(value.as<int>(), 0, 100);
}

void readProvider(JsonObjectConst source, VoxProvider &provider, const char *fallbackId, const char *fallbackName) {
    copyString(provider.id, sizeof(provider.id), source["id"] | fallbackId);
    copyString(provider.displayName, sizeof(provider.displayName), source["display_name"] | fallbackName);
    copyString(provider.status, sizeof(provider.status), source["status"] | "OFFLINE");
    copyString(provider.project, sizeof(provider.project), source["project"] | "");
    copyString(provider.updatedAt, sizeof(provider.updatedAt), source["quota_updated_at"] | "");
    provider.quota5h = percentOrInvalid(source["quota_5h_remaining"], provider.quota5hValid);
    provider.quota7d = percentOrInvalid(source["quota_7d_remaining"], provider.quota7dValid);
    provider.quotaStale = source["quota_stale"] | false;
}

// Reads the whole file rather than using readStringUntil: a UTF-16 file is
// full of NUL bytes, and an Arduino String would silently truncate at the first
// one, leaving a parse failure that looks like a missing key.
bool readConfigFile(String &text, String &failure) {
    File file = SD_MMC.open("/voxstick.ini", FILE_READ);
    if (!file) {
        failure = "NO /voxstick.ini";
        return false;
    }
    size_t size = file.size();
    if (size == 0) {
        file.close();
        failure = "INI IS EMPTY";
        return false;
    }
    if (size > 2048) {
        file.close();
        failure = "CONFIG TOO LARGE";
        return false;
    }

    uint8_t buffer[2049];
    size_t read = file.read(buffer, size < sizeof(buffer) - 1 ? size : sizeof(buffer) - 1);
    file.close();
    if (read == 0) {
        failure = "INI READ FAILED";
        return false;
    }

    size_t start = 0;
    if (read >= 2 && ((buffer[0] == 0xFF && buffer[1] == 0xFE) || (buffer[0] == 0xFE && buffer[1] == 0xFF))) {
        failure = "INI IS UTF-16: SAVE AS UTF-8";
        return false;
    }
    if (read >= 3 && buffer[0] == 0xEF && buffer[1] == 0xBB && buffer[2] == 0xBF) start = 3;

    text = "";
    text.reserve(read - start + 1);
    for (size_t i = start; i < read; ++i) text += static_cast<char>(buffer[i]);
    summary = "SIZE " + String((unsigned long)size);
    return true;
}

const char *providerId(int slot) { return slot == VOX_PROVIDER_CLAUDE ? "claude" : "codex"; }
const char *providerName(int slot) { return slot == VOX_PROVIDER_CLAUDE ? "Claude" : "Codex"; }

void applyProviderIdentity(VoxState &out) {
    for (int slot = 0; slot < VOX_PROVIDER_COUNT; ++slot) {
        copyString(out.providers[slot].id, sizeof(out.providers[slot].id), providerId(slot));
        copyString(out.providers[slot].displayName, sizeof(out.providers[slot].displayName),
                   providerName(slot));
    }
}

bool parseIniLine(const String &line, String &key, String &value) {
    int separator = line.indexOf('=');
    if (separator < 0) return false;
    key = line.substring(0, separator);
    value = line.substring(separator + 1);
    key.trim();
    value.trim();
    return key.length() > 0;
}

} // namespace

namespace VoxBridge {

bool loadConfig() {
    bridgeHost = "";
    bridgeToken = "";
    bridgePort = 8765;
    autoSleepMillis = 10000;

    summary = "";
    String text, failure;
    if (!readConfigFile(text, failure)) {
        state = failure;
        return false;
    }

    // Normalise every line ending to a newline. A lone CR would otherwise
    // make the whole file look like one line and yield a garbage host.
    text.replace('\r', '\n');

    int lines = 0, matched = 0;
    int from = 0;
    while (from <= (int)text.length()) {
        int end = text.indexOf('\n', from);
        String line = end < 0 ? text.substring(from) : text.substring(from, end);
        from = end < 0 ? text.length() + 1 : end + 1;
        line.trim();
        if (line.isEmpty() || line.startsWith("#")) continue;
        ++lines;
        String key, value;
        if (!parseIniLine(line, key, value)) continue;
        if (key == "bridge_host") { bridgeHost = value; ++matched; }
        else if (key == "bridge_port") { bridgePort = value.toInt(); ++matched; }
        else if (key == "bridge_token") { bridgeToken = value; ++matched; }
        else if (key == "auto_sleep_seconds") {
            autoSleepMillis = static_cast<uint32_t>(max(0L, value.toInt())) * 1000;
            ++matched;
        }
    }
    summary += " LINES " + String(lines) + " KEYS " + String(matched);

    if (bridgeHost.isEmpty()) {
        state = lines == 0 ? "INI HAS NO SETTINGS" : "NO bridge_host";
        return false;
    }
    if (bridgePort == 0) bridgePort = 8765;
    state = "CONFIG LOADED";
    return true;
}

bool configured() { return bridgeHost.length() > 0; }
const String &configState() { return state; }
const String &configSummary() { return summary; }
const String &host() { return bridgeHost; }
uint16_t port() { return bridgePort; }
bool hasToken() { return bridgeToken.length() > 0; }
uint32_t autoSleepMs() { return autoSleepMillis; }
int lastStatus() { return lastStatusCode; }
const String &lastError() { return lastErrorText; }

void initState(VoxState &out) { applyProviderIdentity(out); }

bool pollState(VoxState &out) {
    HTTPClient http;
    if (!begin(http, "/state", 4000)) return false;
    applyHeaders(http);
    lastStatusCode = http.GET();
    if (lastStatusCode != 200) {
        lastErrorText = "HTTP " + String(lastStatusCode);
        http.end();
        return false;
    }

    // 2 KB covers a /state document with room for long alert messages.
    JsonDocument document;
    DeserializationError error = deserializeJson(document, http.getStream());
    http.end();
    if (error) {
        lastErrorText = String("JSON ") + error.c_str();
        return false;
    }

    JsonObjectConst root = document.as<JsonObjectConst>();
    copyString(out.time, sizeof(out.time), root["time"] | "--:--");
    copyString(out.activeProvider, sizeof(out.activeProvider), root["active_provider"] | "codex");
    copyString(out.bridgeVersion, sizeof(out.bridgeVersion), root["bridge_version"] | "");

    JsonObjectConst alert = root["alert"];
    copyString(out.alertEventId, sizeof(out.alertEventId), alert["event_id"] | "");
    copyString(out.alertType, sizeof(out.alertType), alert["type"] | "NONE");
    copyString(out.alertMessage, sizeof(out.alertMessage), alert["message"] | "");

    readProvider(root["codex"], out.providers[VOX_PROVIDER_CODEX], "codex", "Codex");
    readProvider(root["claude"], out.providers[VOX_PROVIDER_CLAUDE], "claude", "Claude");
    // The legacy `codex` block carries no id or display name of its own, and a
    // bridge that predates the `claude` block leaves that slot at its defaults.
    applyProviderIdentity(out);
    lastErrorText = "";
    return true;
}

bool postEvent(const char *jsonBody) { return postJson("/event", jsonBody, nullptr); }

bool refreshQuota() { return postJson("/quota/refresh", "{}", nullptr, 8000); }

bool recordingStart(const char *sessionId, int provider) {
    char body[192];
    snprintf(body, sizeof(body),
             "{\"event\":\"button_long_start\",\"source\":\"s3ai\",\"audio_source\":\"s3ai_pcm\",\"session_id\":\"%s\",\"provider\":\"%s\"}",
             sessionId, providerId(provider));
    return postJson("/recording/start", body, nullptr);
}

bool uploadAudio(const char *sessionId, const uint8_t *data, size_t length) {
    HTTPClient http;
    String path = String("/recording/audio?session_id=") + sessionId;
    if (!begin(http, path.c_str(), 30000)) return false;
    http.addHeader("Content-Type", "application/octet-stream");
    http.addHeader("X-Vox-Stick-Sample-Rate", "16000");
    http.addHeader("X-Vox-Stick-Channels", "1");
    http.addHeader("X-Vox-Stick-Bits-Per-Sample", "16");
    applyHeaders(http);
    lastStatusCode = http.POST(const_cast<uint8_t *>(data), length);
    bool ok = lastStatusCode == 200;
    if (!ok) lastErrorText = "HTTP " + String(lastStatusCode);
    http.end();
    return ok;
}

bool recordingStop(bool paste, char *statusOut, size_t statusLength, int provider) {
    char body[128];
    snprintf(body, sizeof(body), "{\"event\":\"button_long_stop\",\"source\":\"s3ai\",\"paste\":%s,\"provider\":\"%s\"}",
             paste ? "true" : "false", providerId(provider));
    String response;
    // Transcription and paste happen inside this request.
    if (!postJson("/recording/stop", body, &response, 65000)) return false;

    JsonDocument document;
    if (deserializeJson(document, response)) {
        copyString(statusOut, statusLength, "BAD RESPONSE");
        return false;
    }
    JsonObjectConst recording = document["recording"];
    const char *status = recording["status"] | "failed";
    copyString(statusOut, statusLength, status);
    return recording["pasted"] | false;
}

} // namespace VoxBridge
