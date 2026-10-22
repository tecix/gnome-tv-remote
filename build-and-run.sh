#!/usr/bin/env bash
set -euo pipefail

APP_ID="io.github.tecix.TvRemote"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${ROOT_DIR}/flatpak/${APP_ID}.yml"
BUILD_DIR="${ROOT_DIR}/build-dir"

if ! command -v flatpak >/dev/null 2>&1; then
  echo "Error: flatpak is not installed." >&2
  exit 1
fi

if ! command -v flatpak-builder >/dev/null 2>&1; then
  echo "Error: flatpak-builder is not installed." >&2
  exit 1
fi

if [[ ! -f "${MANIFEST}" ]]; then
  echo "Error: Flatpak manifest not found: ${MANIFEST}" >&2
  exit 1
fi

echo "==> Building Flatpak (${APP_ID})"
flatpak-builder --user --install --force-clean "${BUILD_DIR}" "${MANIFEST}"

echo "==> Running (${APP_ID})"
exec flatpak run "${APP_ID}" "$@"


