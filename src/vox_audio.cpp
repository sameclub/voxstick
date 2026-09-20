#include "vox_audio.h"
#include <driver/i2s_std.h>
#include <driver/gpio.h>
#include <atomic>
#include <math.h>

// MSM261S microphone and MAX98357A amplifier, S3AI pin assignment.
constexpr gpio_num_t MIC_SCK = GPIO_NUM_4, MIC_WS = GPIO_NUM_5, MIC_SD = GPIO_NUM_6;
constexpr gpio_num_t AMP_DOUT = GPIO_NUM_15, AMP_BCLK = GPIO_NUM_16, AMP_LRCK = GPIO_NUM_17;

namespace {

constexpr int FRAMES_PER_READ = 240; // 15 ms at 16 kHz
constexpr float DC_ALPHA = 0.002f;
constexpr int MIC_GAIN = 8;
// Speech has to stand this far above room tone before the meter lifts at all.
constexpr float METER_GATE = 3.0f;

i2s_chan_handle_t rxChannel = nullptr;
uint8_t *takeBuffer = nullptr;
std::atomic<uint32_t> takeBytes{0};
std::atomic<bool> recording{false}, stopCapture{false}, captureStopped{false}, micReady{false};
std::atomic<bool> takeFull{false};
std::atomic<int> levelPercent{0}, rawRms{0}, rmsLeft{0}, rmsRight{0}, activeSlot{0};
float noiseFloor = 0.0f, peakLevel = 0.0f;
bool meterSeeded = false;
std::atomic<uint32_t> errorCount{0};
portMUX_TYPE bufferMux = portMUX_INITIALIZER_UNLOCKED;

void captureTask(void *) {
    // Static: a 240-frame stereo 32-bit read is 1.9 KiB, too much for the stack.
    static int32_t raw[FRAMES_PER_READ * 2];
    static int16_t pcm[FRAMES_PER_READ];
    float dc[2] = {0.0f, 0.0f};
    double energy[2] = {0.0, 0.0};
    int energyFrames = 0;

    while (!stopCapture.load()) {
        size_t bytes = 0;
        if (i2s_channel_read(rxChannel, raw, sizeof(raw), &bytes, 40) != ESP_OK || bytes != sizeof(raw)) {
            ++errorCount;
            continue;
        }
        const int slot = activeSlot.load();
        double takeEnergy = 0.0;
        for (int i = 0; i < FRAMES_PER_READ; ++i) {
            float filtered[2];
            for (int channel = 0; channel < 2; ++channel) {
                // MSM261S signed data sits in the high bits of each 32-bit slot.
                float sample = static_cast<float>(raw[i * 2 + channel]) / 65536.0f;
                dc[channel] += DC_ALPHA * (sample - dc[channel]);
                filtered[channel] = sample - dc[channel];
                energy[channel] += filtered[channel] * filtered[channel];
            }
            int value = static_cast<int>(filtered[slot] * MIC_GAIN);
            pcm[i] = static_cast<int16_t>(constrain(value, -32768, 32767));
            takeEnergy += static_cast<double>(pcm[i]) * pcm[i];
        }

        if (recording.load() && takeBuffer) {
            taskENTER_CRITICAL(&bufferMux);
            uint32_t used = takeBytes.load();
            uint32_t room = VOX_MAX_RECORD_BYTES - used;
            uint32_t copy = room < sizeof(pcm) ? room : sizeof(pcm);
            if (copy) {
                memcpy(takeBuffer + used, pcm, copy);
                takeBytes.store(used + copy);
            }
            taskEXIT_CRITICAL(&bufferMux);
            if (copy < sizeof(pcm)) takeFull.store(true);
        }

        float rms = sqrtf(takeEnergy / FRAMES_PER_READ);
        if (!meterSeeded) {
            // Seed from the first block. Creeping up from zero would take tens
            // of seconds to find the room tone, and until then every bit of
            // background hiss would be drawn as if it were speech.
            noiseFloor = peakLevel = rms;
            meterSeeded = true;
        } else {
            // The floor drops instantly and recovers slowly; the peak does the
            // opposite. The meter then spans whatever this microphone puts out.
            noiseFloor = rms < noiseFloor ? rms : noiseFloor * 1.002f;
            peakLevel = rms > peakLevel ? rms : peakLevel * 0.996f;
        }
        float span = peakLevel - noiseFloor;
        bool speaking = peakLevel > noiseFloor * METER_GATE && span > 0.0f;
        int percent = speaking ? static_cast<int>((rms - noiseFloor) * 100.0f / span) : 0;
        levelPercent.store(constrain(percent, 0, 100));
        rawRms.store(static_cast<int>(rms));

        if (++energyFrames >= 32) {
            int left = static_cast<int>(sqrt(energy[0] / (energyFrames * FRAMES_PER_READ)) * 32768.0);
            int right = static_cast<int>(sqrt(energy[1] / (energyFrames * FRAMES_PER_READ)) * 32768.0);
            rmsLeft.store(left);
            rmsRight.store(right);
            // Only switch on clear evidence, not on near-equal background noise.
            if (left > 4 && left > right * 4) activeSlot.store(0);
            if (right > 4 && right > left * 4) activeSlot.store(1);
            energy[0] = energy[1] = 0.0;
            energyFrames = 0;
        }
    }

    i2s_channel_disable(rxChannel);
    i2s_del_channel(rxChannel);
    rxChannel = nullptr;
    micReady.store(false);
    captureStopped.store(true);
    vTaskDelete(nullptr);
}

bool openMicrophone() {
    i2s_chan_config_t cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);
    cfg.dma_desc_num = 6;
    cfg.dma_frame_num = FRAMES_PER_READ;
    if (i2s_new_channel(&cfg, nullptr, &rxChannel) != ESP_OK) return false;

    i2s_std_config_t std = {};
    std.clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(VOX_SAMPLE_RATE);
    std.slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO);
    std.gpio_cfg.mclk = I2S_GPIO_UNUSED;
    std.gpio_cfg.bclk = MIC_SCK;
    std.gpio_cfg.ws = MIC_WS;
    std.gpio_cfg.dout = I2S_GPIO_UNUSED;
    std.gpio_cfg.din = MIC_SD;

    bool ok = i2s_channel_init_std_mode(rxChannel, &std) == ESP_OK && i2s_channel_enable(rxChannel) == ESP_OK;
    // The unselected microphone slot is tri-stated; avoid a floating input.
    gpio_pulldown_en(MIC_SD);
    if (!ok) {
        i2s_del_channel(rxChannel);
        rxChannel = nullptr;
    }
    return ok;
}

// Writes one tone through a temporary TX channel. MAX98357A shuts down when
// the clock stops, so the channel is torn down after every alert.
struct Tone {
    int frequency;
    int durationMs;
    int gapMs;
};

void playTones(const Tone *tones, int count) {
    i2s_chan_handle_t tx = nullptr;
    i2s_chan_config_t cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);
    cfg.dma_desc_num = 4;
    cfg.dma_frame_num = 256;
    if (i2s_new_channel(&cfg, &tx, nullptr) != ESP_OK) return;

    i2s_std_config_t std = {};
    std.clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(VOX_SAMPLE_RATE);
    std.slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO);
    std.gpio_cfg.mclk = I2S_GPIO_UNUSED;
    std.gpio_cfg.bclk = AMP_BCLK;
    std.gpio_cfg.ws = AMP_LRCK;
    std.gpio_cfg.dout = AMP_DOUT;
    std.gpio_cfg.din = I2S_GPIO_UNUSED;

    if (i2s_channel_init_std_mode(tx, &std) != ESP_OK || i2s_channel_enable(tx) != ESP_OK) {
        i2s_del_channel(tx);
        return;
    }

    static int16_t block[256];
    for (int index = 0; index < count; ++index) {
        const Tone &tone = tones[index];
        int total = VOX_SAMPLE_RATE * tone.durationMs / 1000;
        // 4 ms of attack and release keep the amplifier from clicking.
        int ramp = VOX_SAMPLE_RATE * 4 / 1000;
        for (int produced = 0; produced < total;) {
            int chunk = total - produced;
            if (chunk > static_cast<int>(sizeof(block) / sizeof(block[0]))) chunk = sizeof(block) / sizeof(block[0]);
            for (int i = 0; i < chunk; ++i) {
                int position = produced + i;
                float envelope = 1.0f;
                if (position < ramp) envelope = static_cast<float>(position) / ramp;
                else if (position > total - ramp) envelope = static_cast<float>(total - position) / ramp;
                float phase = 2.0f * PI * tone.frequency * position / VOX_SAMPLE_RATE;
                block[i] = static_cast<int16_t>(sinf(phase) * 7000.0f * envelope);
            }
            size_t written = 0;
            i2s_channel_write(tx, block, chunk * sizeof(int16_t), &written, 200);
            produced += chunk;
        }
        if (tone.gapMs > 0) {
            int silence = VOX_SAMPLE_RATE * tone.gapMs / 1000;
            memset(block, 0, sizeof(block));
            for (int produced = 0; produced < silence;) {
                int chunk = silence - produced;
                if (chunk > static_cast<int>(sizeof(block) / sizeof(block[0]))) chunk = sizeof(block) / sizeof(block[0]);
                size_t written = 0;
                i2s_channel_write(tx, block, chunk * sizeof(int16_t), &written, 200);
                produced += chunk;
            }
        }
    }

    i2s_channel_disable(tx);
    i2s_del_channel(tx);
}

} // namespace

namespace VoxAudio {

bool beginMicrophone() {
    if (!takeBuffer) takeBuffer = static_cast<uint8_t *>(ps_malloc(VOX_MAX_RECORD_BYTES));
    if (!takeBuffer) return false;
    if (micReady.load()) return true;
    stopCapture.store(false);
    captureStopped.store(false);
    meterSeeded = false; // the room may have changed across a sleep
    if (!openMicrophone()) return false;
    if (xTaskCreatePinnedToCore(captureTask, "vox-mic", 4096, nullptr, 4, nullptr, 0) != pdPASS) {
        i2s_channel_disable(rxChannel);
        i2s_del_channel(rxChannel);
        rxChannel = nullptr;
        return false;
    }
    micReady.store(true);
    return true;
}

void endMicrophone() {
    if (!micReady.load()) return;
    recording.store(false);
    stopCapture.store(true);
    uint32_t start = millis();
    while (!captureStopped.load() && millis() - start < 300) delay(2);
}

bool microphoneOK() { return micReady.load(); }

void startRecording() {
    taskENTER_CRITICAL(&bufferMux);
    takeBytes.store(0);
    taskEXIT_CRITICAL(&bufferMux);
    takeFull.store(false);
    recording.store(true);
}

void stopRecording() { recording.store(false); }
bool isRecording() { return recording.load(); }
const uint8_t *recordedData() { return takeBuffer; }
uint32_t recordedBytes() { return takeBytes.load(); }
bool recordingFull() { return takeFull.load(); }
int level() { return levelPercent.load(); }
int rawLevel() { return rawRms.load(); }
int micRMS(int slot) { return slot == 0 ? rmsLeft.load() : rmsRight.load(); }
int micSlot() { return activeSlot.load(); }
uint32_t readErrors() { return errorCount.load(); }

void playAlert(VoxAlertSound sound) {
    // Matches docs/STATES_AND_SOUNDS.md from the upstream project.
    static const Tone done[] = {{880, 80, 40}, {1320, 120, 0}};
    static const Tone error[] = {{240, 100, 60}, {240, 100, 60}, {240, 100, 0}};
    static const Tone approval[] = {{600, 100, 60}, {800, 100, 0}};
    switch (sound) {
        case VOX_SOUND_DONE: playTones(done, 2); break;
        case VOX_SOUND_ERROR: playTones(error, 3); break;
        case VOX_SOUND_APPROVAL: playTones(approval, 2); break;
    }
}

} // namespace VoxAudio
