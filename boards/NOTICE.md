# Board definition

`_jsonfiles/` holds the PlatformIO board definition and the Arduino variant for
the generic ESP32-S3 with 16 MB flash and 8 MB OPI PSRAM that this device uses:

| File | Purpose |
|---|---|
| `esp32s3.json` | PlatformIO board manifest |
| `esp32s3.h` | Pin definitions for the S3 target |
| `pins_arduino.h` | Dispatches to the right `espxx.h` by target |
| `variant.cpp` | Arduino variant stub |

These four files are copied verbatim from
[bmorcelli/Launcher](https://github.com/bmorcelli/Launcher) (`boards/_jsonfiles/`),
MIT licensed, Copyright (c) 2023 shikarunochi. They are vendored here so the
project builds from a clean clone without a Launcher checkout.
