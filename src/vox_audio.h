#pragma once
#include <Arduino.h>

// 16 kHz / mono / 16-bit: what the bridge uploads to Tencent SentenceRecognition.
constexpr uint32_t VOX_SAMPLE_RATE = 16000;
// 56 s of audio. SentenceRecognition accepts 60 s and 3 MB of base64.
constexpr uint32_t VOX_MAX_RECORD_BYTES = 1792000;

enum VoxAlertSound { VOX_SOUND_DONE, VOX_SOUND_ERROR, VOX_SOUND_APPROVAL };

namespace VoxAudio {

// Microphone: allocates the PSRAM take buffer and starts the capture task.
bool beginMicrophone();
void endMicrophone();
bool microphoneOK();

void startRecording();
void stopRecording();
bool isRecording();
const uint8_t *recordedData();
uint32_t recordedBytes();
bool recordingFull();
int level();          // 0..100, auto-ranged between noise floor and peak
int rawLevel();       // unscaled RMS, for diagnostics
int micRMS(int slot); // diagnostics
int micSlot();
uint32_t readErrors();

// Speaker: alert tones are generated as PCM, never fetched or stored as files.
void playAlert(VoxAlertSound sound);

} // namespace VoxAudio
