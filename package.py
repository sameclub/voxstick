"""Package an app for Launcher; optionally export the standalone factory image."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil

parser = argparse.ArgumentParser()
parser.add_argument('--factory', action='store_true', help='also export the standalone merged firmware')
args = parser.parse_args()
VERSION = '1.2'
root = Path(__file__).resolve().parent
build = root / '.pio/build/s3ai-voxstick'
out = root / 'dist'
# The version lives in the firmware too; refuse to ship a mismatched name.
for source, marker in ((root / 'src/main.cpp', f'#define VOXSTICK_VERSION "{VERSION}"'),
                       (root / 'src/vox_bridge.cpp', f'#define FIRMWARE_VERSION "{VERSION}"')):
    if marker not in source.read_text(encoding='utf-8'):
        raise SystemExit(f'{source.name} does not report version {VERSION}; bump it or fix VERSION')
data = (build / 'firmware.bin').read_bytes()
if len(data) < 288 or data[0] != 0xE9 or int.from_bytes(data[12:14], 'little') != 9 or len(data) > 0x300000:
    raise SystemExit('Invalid or oversized ESP32-S3 application')
out.mkdir(exist_ok=True)
app = out / f'VoxStick-v{VERSION}.bin'
app.write_bytes(data)
files = [app]
if args.factory:
    factory = build / 'firmware.factory.bin'
    if not factory.is_file():
        raise SystemExit('No merged firmware.factory.bin produced by this toolchain; app export completed')
    target = out / f'VoxStick-v{VERSION}-factory.bin'
    shutil.copyfile(factory, target)
    files.append(target)
manifest = [{'name': 'VoxStick', 'version': VERSION, 'file': p.name, 'bytes': p.stat().st_size,
             'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
             'format': 'application-only' if p == app else 'merged-standalone'} for p in files]
(out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
(out / 'SHA256SUMS.txt').write_text(''.join(f"{item['sha256']}  {item['file']}\n" for item in manifest), encoding='ascii')
shutil.copyfile(root / 'lib/wifi-portal/LICENSE', out / 'wifi-portal-MIT.txt')
print(json.dumps(manifest, indent=2))
