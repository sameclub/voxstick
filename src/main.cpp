// VoxStick for S3AI Game: a desk terminal for local coding agents.
#include <Arduino.h>
#include <Arduino_GFX_Library.h>
#include <SD_MMC.h>
#include <WiFi.h>
#include <WifiPortal.h>
#include <driver/rtc_io.h>
#include <esp_ota_ops.h>
#include <esp_random.h>
#include <esp_sleep.h>
#include <esp_timer.h>

#include "vox_audio.h"
#include "vox_bridge.h"

#define VOXSTICK_VERSION "1.2"

constexpr uint16_t BG = 0x0000, WHITE = 0xFFFF, MID = 0x8410, DIM = 0x4208;
constexpr uint16_t GREEN = 0x07E6, RED = 0xF986, YELLOW = 0xFEA0, CYAN = 0x07FF;
constexpr uint16_t ACCENT_CLAUDE = 0xDBAA, ACCENT_CODEX = 0xFFFF;
constexpr uint32_t POLL_INTERVAL_MS = 2000;
constexpr int BATTERY_PIN = 8;
constexpr float BATTERY_DIVIDER = 4.0f; // (300 + 100) / 100

Arduino_HWSPI bus(12, 3, 11, 46, -1);
Arduino_ST7789 panel(&bus, 7, 4, false, 240, 240, 0, 0, 0, 0);
Arduino_Canvas canvas(240, 240, nullptr);
WifiPortal wifiPortal({"VoxStick", "#d97757", "voxstick-net"});

struct Key {
    const char *name;
    int pin;
    bool raw = false, down = false;
    uint32_t changed = 0;
};
enum KeyIndex { KEY_AI, KEY_APP, KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT,
                KEY_A, KEY_B, KEY_X, KEY_Y, KEY_SELECT, KEY_START, KEY_COUNT };
Key keys[] = {{"AI", 0}, {"APP", 10}, {"UP", 42}, {"DOWN", 45}, {"LEFT", 21}, {"RIGHT", 14},
              {"A", 1}, {"B", 43}, {"X", 44}, {"Y", 2}, {"SELECT", 48}, {"START", 47}};
static_assert(sizeof(keys) / sizeof(keys[0]) == KEY_COUNT, "keys[] out of sync with KeyIndex");

VoxState state;
int visibleProvider = VOX_PROVIDER_CLAUDE;
int recordingProvider = VOX_PROVIDER_CLAUDE;
bool displayOK = false, sdOK = false, configOK = false, micOK = false;
bool bridgeOnline = false, fullRefresh = true;
bool diagnostics = false;
int diagPage = 0;
constexpr int DIAG_PAGES = 2;
bool sleeping = false, sleepPending = false, ntpStarted = false;
uint32_t lastPoll = 0, lastActivity = 0, lastDraw = 0, recordStarted = 0;
uint16_t *previousFrame = nullptr;
char lastSoundEventId[sizeof(state.alertEventId)] = "";
char lastSoundStatus[sizeof(VoxProvider::status)] = "";
char sessionId[16] = "";
char transcriptNote[96] = "";
uint32_t transcriptNoteUntil = 0;
String busyMessage;
float batteryVolts = 0;
bool batteryValid = false;

// ---------------------------------------------------------------- drawing --

void text(int x, int y, const String &value, uint16_t color = WHITE, int scale = 1) {
    canvas.setCursor(x, y);
    canvas.setTextSize(scale);
    canvas.setTextColor(color);
    canvas.print(value);
}

void centered(int y, const String &value, uint16_t color, int scale) {
    text(120 - static_cast<int>(value.length()) * 3 * scale, y, value, color, scale);
}

uint16_t statusColor(const char *status) {
    if (!strcmp(status, "RUNNING")) return GREEN;
    if (!strcmp(status, "DONE")) return CYAN;
    if (!strcmp(status, "ERROR")) return RED;
    if (!strcmp(status, "APPROVAL")) return YELLOW;
    if (!strcmp(status, "IDLE")) return MID;
    return DIM;
}

uint16_t accentColor(int provider) {
    return provider == VOX_PROVIDER_CLAUDE ? ACCENT_CLAUDE : ACCENT_CODEX;
}

void sampleBattery() {
    uint32_t sum = 0;
    for (int i = 0; i < 16; ++i) sum += analogReadMilliVolts(BATTERY_PIN);
    float measured = (sum / 16.0f) * BATTERY_DIVIDER / 1000.0f;
    bool plausible = measured >= 2.5f && measured <= 4.5f;
    batteryVolts = plausible && batteryValid ? batteryVolts * 0.75f + measured * 0.25f : measured;
    batteryValid = plausible;
}

int batteryPercent() {
    const float volts[] = {3.4f, 3.6f, 3.7f, 3.8f, 3.95f, 4.2f};
    const int percent[] = {0, 10, 25, 50, 75, 100};
    if (!batteryValid) return -1;
    if (batteryVolts <= volts[0]) return 0;
    for (int i = 1; i < 6; ++i) {
        if (batteryVolts <= volts[i])
            return lroundf(percent[i - 1] + (batteryVolts - volts[i - 1]) * (percent[i] - percent[i - 1]) /
                                                (volts[i] - volts[i - 1]));
    }
    return 100;
}

void drawQuotaRow(int y, const char *label, const VoxProvider &provider, bool fiveHour,
                  uint16_t accent) {
    const int value = fiveHour ? provider.quota5h : provider.quota7d;
    const bool valid = fiveHour ? provider.quota5hValid : provider.quota7dValid;

    text(10, y + 4, label, MID);
    String percent = valid ? String(value) + "%" : "--%";
    // The number itself is the low-quota warning; the bar keeps the provider
    // colour so the two agents stay distinguishable at a glance.
    uint16_t percentColor = !valid ? DIM : value <= 20 ? RED : WHITE;
    text(230 - static_cast<int>(percent.length()) * 12, y, percent, percentColor, 2);

    canvas.drawRect(10, y + 22, 220, 12, DIM);
    if (valid) {
        int width = 218 * constrain(value, 0, 100) / 100;
        canvas.fillRect(11, y + 23, width, 10, accent);
    }
}

String clockText() {
    time_t now = time(nullptr);
    // 2024-01-01: anything earlier means NTP has not landed yet.
    if (now >= 1704067200) {
        tm local = {};
        localtime_r(&now, &local);
        char buffer[8];
        snprintf(buffer, sizeof(buffer), "%02d:%02d", local.tm_hour, local.tm_min);
        return buffer;
    }
    return state.time;
}

void drawStatusBar() {
    text(10, 8, clockText(), WHITE, 2);
    String right;
    if (wifiPortal.isVerifying()) right = "WIFI TEST";
    else if (wifiPortal.isProvisioning()) right = "AP";
    else if (!wifiPortal.isConnected()) right = "NO WIFI";
    else if (!bridgeOnline) right = "NO BRIDGE";
    else right = "LINK";
    int battery = batteryPercent();
    if (battery >= 0) right += " " + String(battery) + "%";
    text(232 - static_cast<int>(right.length()) * 6, 12, right,
         bridgeOnline && wifiPortal.isConnected() ? MID : YELLOW);
}

void drawHome() {
    const VoxProvider &provider = state.providers[visibleProvider];
    const uint16_t accent = accentColor(visibleProvider);
    drawStatusBar();
    canvas.drawFastHLine(10, 30, 220, DIM);

    // The agent name is what the device is read from across a desk, so it gets
    // the most weight; the status line below it is a detail.
    String name = provider.displayName[0] ? provider.displayName : "AGENT";
    name.toUpperCase();
    text(10, 40, name, accent, 4);
    if (!strcmp(state.activeProvider, provider.id)) canvas.fillCircle(226, 56, 4, accent);

    canvas.fillCircle(14, 87, 4, statusColor(provider.status));
    text(26, 84, provider.status, statusColor(provider.status));
    String project = provider.project[0] ? provider.project : "-";
    if (project.length() > 22) project = project.substring(0, 22);
    text(230 - static_cast<int>(project.length()) * 6, 84, project, DIM);

    drawQuotaRow(104, "5H", provider, true, accent);
    drawQuotaRow(150, "7D", provider, false, accent);

    if (wifiPortal.isProvisioning()) {
        if (wifiPortal.isVerifying()) {
            // The hotspot is down while the new network is tried, so the screen
            // is the only place the result shows up.
            text(10, 202, "TRYING NEW WIFI", accent);
            text(10, 224, "HOTSPOT BACK IF IT FAILS", DIM);
        } else {
            text(10, 202, "AP: " + wifiPortal.apName(), accent);
            text(10, 224, "PW: " + wifiPortal.apPassword(), accent);
        }
        return;
    }

    // Only agent-facing messages live here; plumbing errors are on the
    // diagnostics pages so the home screen stays readable.
    if (millis() < transcriptNoteUntil && transcriptNote[0]) {
        text(10, 202, String(transcriptNote).substring(0, 38), CYAN);
    } else if (strcmp(state.alertType, "NONE") && state.alertMessage[0]) {
        text(10, 202, String(state.alertMessage).substring(0, 38), statusColor(state.alertType));
    } else if (!bridgeOnline) {
        text(10, 202, "SELECT FOR DIAGNOSTICS", DIM);
    }

    text(10, 224, "AI TALK  <>SWAP  Y SYNC  SEL DIAG", DIM);
}

void drawRecording() {
    const uint16_t accent = accentColor(visibleProvider);
    centered(40, busyMessage.length() ? busyMessage : String("LISTENING"), accent, 3);

    // The centre bar shows the newest level and the outer ones lag behind it,
    // so speech travels outwards instead of all five moving in lockstep.
    static int history[5] = {0, 0, 0, 0, 0};
    if (!busyMessage.length()) {
        for (int i = 4; i > 0; --i) history[i] = history[i - 1];
        history[0] = VoxAudio::level();
    }
    const int order[5] = {2, 1, 0, 1, 2}; // distance from the centre bar
    for (int i = 0; i < 5; ++i) {
        int level = busyMessage.length() ? 18 : history[order[i]];
        int height = constrain(8 + level * 76 / 100, 8, 84);
        int x = 48 + i * 30;
        canvas.fillRect(x, 140 - height / 2, 16, height, accent);
    }

    if (!busyMessage.length()) {
        float elapsed = (millis() - recordStarted) / 1000.0f;
        float limit = VOX_MAX_RECORD_BYTES / (VOX_SAMPLE_RATE * 2.0f);
        centered(196, String(elapsed, 1) + "s / " + String(limit, 0) + "s",
                 VoxAudio::recordingFull() ? RED : MID, 1);
        centered(214, VoxAudio::recordingFull() ? "LIMIT REACHED" : "RELEASE AI TO SEND", DIM, 1);
    } else {
        centered(196, String(VoxAudio::recordedBytes() / 1024) + " KB", MID, 1);
    }
}

// Listing the card costs a directory walk, so it is cached until the page is
// re-entered rather than repeated every frame.
void drawDiagRow(int y, const char *label, const String &value, uint16_t color = MID) {
    text(10, y, label, DIM);
    text(64, y, value.substring(0, 29), color);
}

void drawLinkPage() {
    text(10, 8, "VOXSTICK / LINK", ACCENT_CLAUDE);
    String wifi = wifiPortal.isVerifying()      ? "TESTING NEW NETWORK"
                  : wifiPortal.isProvisioning() ? "AP " + wifiPortal.apName()
                  : wifiPortal.isConnected()    ? wifiPortal.ssid()
                  : wifiPortal.hasCredentials() ? "CONNECTING"
                                                : "NOT CONFIGURED";
    drawDiagRow(30, "WIFI", wifi, wifiPortal.isConnected() ? MID : YELLOW);
    drawDiagRow(46, "IP", wifiPortal.isConnected() ? WiFi.localIP().toString() : String("-"));
    drawDiagRow(62, "BRIDGE",
                configOK ? VoxBridge::host() + ":" + String(VoxBridge::port()) : String("-"),
                bridgeOnline ? GREEN : YELLOW);
    drawDiagRow(78, "TOKEN", VoxBridge::hasToken() ? "SET" : "MISSING",
                VoxBridge::hasToken() ? MID : YELLOW);
    drawDiagRow(94, "CFG", VoxBridge::configState(), configOK ? MID : YELLOW);
    drawDiagRow(110, "INI", VoxBridge::configSummary().length() ? VoxBridge::configSummary()
                                                                 : String("-"));
    drawDiagRow(126, "HTTP", String(VoxBridge::lastStatus()),
                VoxBridge::lastStatus() == 200 ? MID : YELLOW);
    drawDiagRow(142, "ERROR", VoxBridge::lastError().length() ? VoxBridge::lastError()
                                                               : String("NONE"),
                VoxBridge::lastError().length() ? RED : MID);
    drawDiagRow(158, "ALERT", String(state.alertType) + " " + state.alertMessage,
                strcmp(state.alertType, "NONE") ? statusColor(state.alertType) : MID);
    const VoxProvider &provider = state.providers[visibleProvider];
    drawDiagRow(174, "QUOTA", provider.updatedAt[0]
                                  ? String(provider.updatedAt) + (provider.quotaStale ? " STALE" : "")
                                  : String("NEVER"),
                provider.quotaStale ? YELLOW : MID);
}

void drawDevicePage() {
    text(10, 8, "VOXSTICK / DEVICE", ACCENT_CLAUDE);
    drawDiagRow(30, "FW", "v" VOXSTICK_VERSION);
    drawDiagRow(46, "BRIDGE", state.bridgeVersion[0] ? String(state.bridgeVersion) : String("-"));
    drawDiagRow(62, "TF", sdOK ? "MOUNTED" : "NOT MOUNTED", sdOK ? MID : YELLOW);
    drawDiagRow(78, "MIC", String(micOK ? "OK" : "FAIL") + "  SLOT " + String(VoxAudio::micSlot()) +
                               "  ERR " + String(VoxAudio::readErrors()),
                micOK ? MID : RED);
    drawDiagRow(94, "RMS", "L " + String(VoxAudio::micRMS(0)) + "  R " + String(VoxAudio::micRMS(1)) +
                            "  LVL " + String(VoxAudio::rawLevel()));
    drawDiagRow(110, "TAKE", String(VoxAudio::recordedBytes() / 1024) + " KB");
    drawDiagRow(126, "HEAP", String(ESP.getFreeHeap() / 1024) + "K  PSRAM " +
                                 String(ESP.getFreePsram() / 1024) + "K");
    drawDiagRow(142, "BATT", batteryPercent() >= 0
                                 ? String(batteryVolts, 2) + "V  " + String(batteryPercent()) + "%"
                                 : String("NO READING"));
    drawDiagRow(158, "UPTIME", String(millis() / 1000) + "s");
    drawDiagRow(174, "SLEEP", VoxBridge::autoSleepMs()
                                  ? String(VoxBridge::autoSleepMs() / 1000) + "s IDLE"
                                  : String("DISABLED"));
}

void drawDiagnostics() {
    if (diagPage == 1) drawDevicePage();
    else drawLinkPage();
    String pager = "PAGE " + String(diagPage + 1) + "/" + String(DIAG_PAGES);
    text(10, 224, "<> " + pager + "   SELECT EXIT", DIM);
}

void draw() {
    if (!displayOK || sleeping) return;
    canvas.fillScreen(BG);
    if (diagnostics) drawDiagnostics();
    else if (VoxAudio::isRecording() || busyMessage.length()) drawRecording();
    else drawHome();

    uint16_t *frame = canvas.getFramebuffer();
    for (int y = 0; y < 240; ++y) {
        if (fullRefresh || !previousFrame || memcmp(frame + y * 240, previousFrame + y * 240, 480)) {
            panel.draw16bitRGBBitmap(0, y, frame + y * 240, 240, 1);
            if (previousFrame) memcpy(previousFrame + y * 240, frame + y * 240, 480);
        }
    }
    fullRefresh = false;
}

void drawNow(const String &message) {
    busyMessage = message;
    draw();
}

// ----------------------------------------------------------------- alerts --

void playAlertIfNew() {
    // One sound per new alert id; fall back to status edges when the bridge
    // reports no id. Recording always wins: alerts are skipped, never queued.
    const char *status = state.providers[visibleProvider].status;
    bool isAlert = strcmp(state.alertType, "NONE") != 0;
    bool fresh = isAlert && (state.alertEventId[0] ? strcmp(state.alertEventId, lastSoundEventId) != 0
                                                   : strcmp(status, lastSoundStatus) != 0);
    strncpy(lastSoundEventId, state.alertEventId, sizeof(lastSoundEventId) - 1);
    strncpy(lastSoundStatus, status, sizeof(lastSoundStatus) - 1);
    if (!fresh || VoxAudio::isRecording()) return;

    if (!strcmp(state.alertType, "DONE")) VoxAudio::playAlert(VOX_SOUND_DONE);
    else if (!strcmp(state.alertType, "ERROR")) VoxAudio::playAlert(VOX_SOUND_ERROR);
    else if (!strcmp(state.alertType, "APPROVAL")) VoxAudio::playAlert(VOX_SOUND_APPROVAL);
}

// -------------------------------------------------------------- recording --

void beginRecording() {
    if (!micOK || !configOK || VoxAudio::isRecording()) return;
    snprintf(sessionId, sizeof(sessionId), "s3ai%08lx", static_cast<unsigned long>(esp_random()));
    VoxAudio::startRecording();
    recordStarted = millis();
    transcriptNote[0] = '\0';
    drawNow("");
    recordingProvider = visibleProvider;
    VoxBridge::recordingStart(sessionId, recordingProvider);
}

void finishRecording() {
    if (!VoxAudio::isRecording()) return;
    VoxAudio::stopRecording();
    uint32_t bytes = VoxAudio::recordedBytes();
    if (bytes < VOX_SAMPLE_RATE) { // under 0.5 s: a slip of the thumb
        drawNow("");
        busyMessage = "";
        return;
    }

    drawNow("SENDING");
    bool uploaded = VoxBridge::uploadAudio(sessionId, VoxAudio::recordedData(), bytes);
    if (!uploaded) {
        snprintf(transcriptNote, sizeof(transcriptNote), "UPLOAD FAILED");
    } else {
        drawNow("TYPING");
        char result[sizeof(transcriptNote)] = "";
        if (VoxBridge::recordingStop(true, result, sizeof(result), recordingProvider))
            snprintf(transcriptNote, sizeof(transcriptNote), "SENT");
        else
            snprintf(transcriptNote, sizeof(transcriptNote), "STOP FAILED");
    }
    transcriptNoteUntil = millis() + 4000;
    busyMessage = "";
    fullRefresh = true;
}

// ------------------------------------------------------------------ sleep --

void resumeFromSleep() {
    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_GPIO);
    for (auto &key : keys) gpio_wakeup_disable(static_cast<gpio_num_t>(key.pin));
    rtc_gpio_hold_dis(GPIO_NUM_9);
    rtc_gpio_deinit(GPIO_NUM_9);
    rtc_gpio_deinit(GPIO_NUM_10);
    pinMode(9, OUTPUT);
    digitalWrite(9, HIGH);
    pinMode(10, INPUT_PULLUP);
    // Adopt whatever the keys read right now: the key that woke us must not be
    // dispatched as a fresh press.
    for (auto &key : keys) {
        key.raw = key.down = digitalRead(key.pin) == LOW;
        key.changed = millis();
    }
    sleeping = false;
    sleepPending = false;
    if (displayOK) panel.displayOn();
    fullRefresh = true;
    micOK = VoxAudio::beginMicrophone();
    wifiPortal.begin();
    lastPoll = 0;
    lastActivity = millis();
    draw();
    Serial.println("VOXSTICK WAKE: APP GPIO10");
}

void enterSleep() {
    sleeping = true;
    wifiPortal.stopProvisioning();
    VoxAudio::endMicrophone();
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    if (displayOK) panel.displayOff();
    digitalWrite(9, LOW);
    rtc_gpio_init(GPIO_NUM_9);
    rtc_gpio_set_direction(GPIO_NUM_9, RTC_GPIO_MODE_OUTPUT_ONLY);
    rtc_gpio_set_level(GPIO_NUM_9, 0);
    rtc_gpio_hold_en(GPIO_NUM_9);

    // Every button wakes the device; APP remains the one that puts it to sleep.
    esp_err_t err = ESP_OK;
    for (auto &key : keys) {
        err = gpio_wakeup_enable(static_cast<gpio_num_t>(key.pin), GPIO_INTR_LOW_LEVEL);
        if (err != ESP_OK) break;
    }
    if (err == ESP_OK) err = esp_sleep_enable_gpio_wakeup();
    if (err != ESP_OK) {
        Serial.printf("WAKE CONFIG FAILED %d\n", err);
        resumeFromSleep();
        return;
    }
    rtc_gpio_pullup_en(GPIO_NUM_10);
    rtc_gpio_pulldown_dis(GPIO_NUM_10);
    delay(25);
    Serial.flush();
    esp_light_sleep_start();
    resumeFromSleep();
}

// Sleep is only entered once every key is up, so a held key cannot wake the
// device and immediately put it back to sleep.
bool keysIdle() {
    for (auto &key : keys) {
        if (key.raw || key.down) return false;
    }
    return true;
}

bool sleepBlocked() {
    return diagnostics || VoxAudio::isRecording() || busyMessage.length() || wifiPortal.isProvisioning();
}

// ------------------------------------------------------------------- input --

void handleKey(int index, bool down) {
    if (index == KEY_AI) {
        if (down) beginRecording();
        else finishRecording();
        return;
    }
    if (!down) return;
    if (index == KEY_APP) {
        sleepPending = true;
        return;
    }
    if (VoxAudio::isRecording()) return;
    switch (index) {
        case KEY_SELECT:
            diagnostics = !diagnostics;
            sleepPending = false;
            lastActivity = millis();
            break;
        case KEY_LEFT:
        case KEY_RIGHT:
            if (diagnostics) {
                diagPage = (diagPage + (index == KEY_RIGHT ? 1 : DIAG_PAGES - 1)) % DIAG_PAGES;
            } else {
                // Two providers, so either direction toggles.
                visibleProvider = (visibleProvider + 1) % VOX_PROVIDER_COUNT;
            }
            break;
        case KEY_Y:
            drawNow("SYNCING");
            VoxBridge::refreshQuota();
            busyMessage = "";
            lastPoll = 0;
            break;
        case KEY_A:
            VoxBridge::postEvent("{\"event\":\"button_short\",\"source\":\"s3ai\"}");
            lastPoll = 0;
            break;
        case KEY_START:
            if (wifiPortal.isProvisioning()) wifiPortal.stopProvisioning();
            else wifiPortal.startProvisioning();
            break;
        default:
            break;
    }
}

// ------------------------------------------------------------------- setup --

void setup() {
    rtc_gpio_hold_dis(GPIO_NUM_9);
    rtc_gpio_deinit(GPIO_NUM_9);
    rtc_gpio_deinit(GPIO_NUM_10);
    for (auto &key : keys) pinMode(key.pin, INPUT_PULLUP);
    while (digitalRead(10) == LOW) delay(10);

    pinMode(9, OUTPUT);
    digitalWrite(9, LOW);
    Serial.begin(115200);
    setenv("TZ", "CST-8", 1);
    tzset();
    analogReadResolution(12);

    displayOK = bus.begin(40000000, SPI_MODE0) && panel.begin(GFX_SKIP_DATABUS_BEGIN) && canvas.begin();
    if (displayOK) {
        // This panel is BGR; without the bit the accent renders blue.
        bus.beginWrite();
        bus.writeC8D8(ST7789_MADCTL, ST7789_MADCTL_MX | 0x08);
        bus.endWrite();
    }
    canvas.setTextWrap(false);
    previousFrame = static_cast<uint16_t *>(ps_malloc(240 * 240 * 2));

    VoxBridge::initState(state);
    SD_MMC.setPins(40, 39, 41);
    sdOK = SD_MMC.begin("/sdcard", true, false);
    configOK = sdOK && VoxBridge::loadConfig();

    micOK = VoxAudio::beginMicrophone();
    wifiPortal.begin();
    sampleBattery();
    lastActivity = millis();
    draw();
    digitalWrite(9, HIGH);
    Serial.printf("VOXSTICK v" VOXSTICK_VERSION " sd=%d config=%d(%s) mic=%d display=%d\n", sdOK, configOK,
                  VoxBridge::configState().c_str(), micOK, displayOK);
}

void loop() {
    uint32_t now = millis();

    for (int i = 0; i < KEY_COUNT; ++i) {
        Key &key = keys[i];
        bool raw = digitalRead(key.pin) == LOW;
        if (raw != key.raw) {
            key.raw = raw;
            key.changed = now;
        }
        if (key.down != key.raw && now - key.changed >= 30) {
            key.down = key.raw;
            lastActivity = now;
            handleKey(i, key.down);
        }
        if (key.raw || key.down) lastActivity = now;
    }

    // The take buffer is full: stop before the bridge would reject the upload.
    if (VoxAudio::isRecording() && VoxAudio::recordingFull() && !keys[KEY_AI].down) finishRecording();

    wifiPortal.update();
    if (!ntpStarted && wifiPortal.isConnected()) {
        configTzTime("CST-8", "ntp.aliyun.com", "time.cloudflare.com", "pool.ntp.org");
        ntpStarted = true;
    }

    if (!VoxAudio::isRecording() && !busyMessage.length() && now - lastPoll >= POLL_INTERVAL_MS) {
        lastPoll = now;
        bridgeOnline = VoxBridge::pollState(state);
        if (bridgeOnline) playAlertIfNew();
        sampleBattery();
    }

    uint32_t autoSleep = VoxBridge::autoSleepMs();
    if (!sleeping && !sleepPending && autoSleep && !sleepBlocked() && now - lastActivity >= autoSleep) {
        sleepPending = true;
        Serial.println("VOXSTICK AUTO SLEEP: idle");
    }
    if (sleepPending && !sleepBlocked() && keysIdle()) enterSleep();

    if (now - lastDraw >= 40) {
        lastDraw = now;
        draw();
    }
    delay(2);
}
