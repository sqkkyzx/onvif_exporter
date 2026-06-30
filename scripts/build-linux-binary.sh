#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-$(grep -m 1 'version = ' pyproject.toml | cut -d '"' -f 2)}"
ARCHIVE_NAME="onvif-exporter-linux-x86_64-v${VERSION}"

if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends binutils ffmpeg
  rm -rf /var/lib/apt/lists/*
fi

python -m pip install --upgrade pip
python -m pip install --no-cache-dir pyinstaller
python -m pip install --no-cache-dir .

rm -rf build dist release onvif-exporter.spec "${ARCHIVE_NAME}.spec"

pyinstaller \
  --clean \
  --noconfirm \
  --onefile \
  --name onvif-exporter \
  --add-data "pyproject.toml:." \
  --collect-all anyio \
  --collect-all fastapi \
  --collect-all onvif \
  --collect-all starlette \
  --collect-all uvicorn \
  --collect-all zeep \
  main.py

./dist/onvif-exporter --version

mkdir -p "release/${ARCHIVE_NAME}"
cp dist/onvif-exporter "release/${ARCHIVE_NAME}/onvif-exporter"
cat > "release/${ARCHIVE_NAME}/README.md" <<EOF
# ONVIF Exporter Linux Binary

This archive contains the Linux x86_64 PyInstaller build of ONVIF Exporter v${VERSION}.

Runtime requirement on Debian LXC:

\`\`\`bash
sudo apt-get update
sudo apt-get install -y ffmpeg
chmod +x ./onvif-exporter
CV_MAX_CONCURRENCY=1 ONVIF_MAX_CONCURRENCY=4 ./onvif-exporter
\`\`\`

The service listens on 0.0.0.0:9121 by default.
EOF

tar -C release -czf "release/${ARCHIVE_NAME}.tar.gz" "${ARCHIVE_NAME}"
ls -lh "release/${ARCHIVE_NAME}.tar.gz"
