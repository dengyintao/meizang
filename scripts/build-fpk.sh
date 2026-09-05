#!/bin/bash
set -eu
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STAGING_DIR="${PROJECT_DIR}/build/fnos"

npm --prefix "${PROJECT_DIR}/frontend" run build
rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}/app/server" "${STAGING_DIR}/app/ui/images" "${STAGING_DIR}/wizard"
cp -R "${PROJECT_DIR}/packaging/fnos/." "${STAGING_DIR}/"
cp -R "${PROJECT_DIR}/backend" "${STAGING_DIR}/app/server/backend"
cp -R "${PROJECT_DIR}/frontend/dist" "${STAGING_DIR}/app/server/frontend"

find "${STAGING_DIR}" -name '.DS_Store' -delete
chmod 755 "${STAGING_DIR}"/cmd/*

if [ "${1:-}" = "--stage-only" ]; then
    echo "FPK staging ready: ${STAGING_DIR}"
    exit 0
fi

FNPACK_BIN="${FNPACK_BIN:-$(command -v fnpack || true)}"
if [ -z "${FNPACK_BIN}" ]; then
    echo "fnpack 未安装。请安装飞牛官方 fnpack 1.2.3 后重试。" >&2
    exit 1
fi

cd "${PROJECT_DIR}"
"${FNPACK_BIN}" build --directory "${STAGING_DIR}"
shasum -a 256 meizang.fpk > meizang.fpk.sha256
echo "Built ${PROJECT_DIR}/meizang.fpk"
