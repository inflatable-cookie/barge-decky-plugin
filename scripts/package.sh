#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_NAME="barge-decky-plugin"
VERSION="$(node -e "console.log(JSON.parse(require('fs').readFileSync('$ROOT_DIR/package.json', 'utf8')).version)")"
OUT_DIR="$ROOT_DIR/out"
STAGE_DIR="$OUT_DIR/stage/$PLUGIN_NAME"
ZIP_PATH="$OUT_DIR/${PLUGIN_NAME}-v${VERSION}.zip"

if [ ! -f "$ROOT_DIR/dist/index.js" ]; then
  echo "error: dist/index.js is missing; run the frontend build first" >&2
  exit 1
fi

rm -rf "$OUT_DIR/stage" "$ZIP_PATH"
mkdir -p "$STAGE_DIR"

cp "$ROOT_DIR/package.json" "$STAGE_DIR/"
cp "$ROOT_DIR/plugin.json" "$STAGE_DIR/"
cp "$ROOT_DIR/main.py" "$STAGE_DIR/"
cp "$ROOT_DIR/LICENSE" "$ROOT_DIR/NOTICE" "$STAGE_DIR/"
cp "$ROOT_DIR/README.md" "$STAGE_DIR/"
cp -R "$ROOT_DIR/dist" "$STAGE_DIR/dist"

if [ -d "$ROOT_DIR/py_modules" ]; then
  mkdir -p "$STAGE_DIR/py_modules"
  cp -R "$ROOT_DIR/py_modules/barge_plugin" "$STAGE_DIR/py_modules/"
  find "$STAGE_DIR/py_modules" -name '__pycache__' -type d -exec rm -rf {} +
fi

(
  cd "$OUT_DIR/stage"
  zip -rq "$ZIP_PATH" "$PLUGIN_NAME"
)
rm -rf "$OUT_DIR/stage"

echo "$ZIP_PATH"
