#!/bin/bash
set -eu

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
REPOSITORY="dengyintao/meizang"
APP_NAME="meizang"
APP_DIR="/vol2/@appcenter/${APP_NAME}"
DATA_DIR="/vol2/@appdata/${APP_NAME}"
LOG_FILE="${DATA_DIR}/release-updater.log"
LOCK_FILE="/run/${APP_NAME}-release-updater.lock"
FORCE="${1:-}"

mkdir -p "${DATA_DIR}"
touch "${LOG_FILE}"
exec >>"${LOG_FILE}" 2>&1

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(timestamp)] $*"; }
fail() { log "更新失败：$*"; exit 1; }

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    log "已有更新检查正在运行，本次跳过"
    exit 0
fi

WORK_DIR="$(mktemp -d /tmp/meizang-release.XXXXXX)"
trap 'rm -rf "${WORK_DIR}"' EXIT

API_URL="https://api.github.com/repos/${REPOSITORY}/releases/latest"
RELEASE_JSON="${WORK_DIR}/release.json"
curl --fail --location --silent --show-error --retry 3 --connect-timeout 15 --max-time 60 \
    -H 'Accept: application/vnd.github+json' \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    "${API_URL}" -o "${RELEASE_JSON}" || fail "无法获取 GitHub 最新 Release"

LATEST_TAG="$(sed -n 's/^[[:space:]]*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' "${RELEASE_JSON}" | head -1)"
printf '%s\n' "${LATEST_TAG}" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' \
    || fail "Release 标签格式不受信任：${LATEST_TAG:-空}"
LATEST_VERSION="${LATEST_TAG#v}"
CURRENT_VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)"/\1/p' "${APP_DIR}/server/backend/meizang/__init__.py" 2>/dev/null | head -1 || true)"

if [ "${FORCE}" != "--force" ] && [ "${CURRENT_VERSION}" = "${LATEST_VERSION}" ]; then
    log "当前已是最新版 ${CURRENT_VERSION}"
    exit 0
fi
if [ "${FORCE}" != "--force" ] && [ -n "${CURRENT_VERSION}" ]; then
    HIGHEST="$(printf '%s\n%s\n' "${CURRENT_VERSION}" "${LATEST_VERSION}" | sort -V | tail -1)"
    if [ "${HIGHEST}" != "${LATEST_VERSION}" ]; then
        log "当前版本 ${CURRENT_VERSION} 高于 GitHub 稳定版 ${LATEST_VERSION}，跳过降级"
        exit 0
    fi
fi

BASE_URL="https://github.com/${REPOSITORY}/releases/download/${LATEST_TAG}"
PACKAGE="${WORK_DIR}/${APP_NAME}.fpk"
CHECKSUM="${WORK_DIR}/${APP_NAME}.fpk.sha256"
log "发现版本 ${LATEST_VERSION}（当前 ${CURRENT_VERSION:-未知}），开始下载"
curl --fail --location --silent --show-error --retry 3 --connect-timeout 15 --max-time 900 \
    "${BASE_URL}/${APP_NAME}.fpk" -o "${PACKAGE}" || fail "FPK 下载失败"
curl --fail --location --silent --show-error --retry 3 --connect-timeout 15 --max-time 60 \
    "${BASE_URL}/${APP_NAME}.fpk.sha256" -o "${CHECKSUM}" || fail "校验文件下载失败"

(cd "${WORK_DIR}" && sha256sum --check --status "${APP_NAME}.fpk.sha256") \
    || fail "Release SHA-256 校验失败"

MANIFEST="$(tar -xOzf "${PACKAGE}" manifest 2>/dev/null)" || fail "无法读取 FPK 清单"
PACKAGE_NAME="$(printf '%s\n' "${MANIFEST}" | sed -n 's/^appname[[:space:]]*=[[:space:]]*//p' | tr -d '[:space:]')"
PACKAGE_VERSION="$(printf '%s\n' "${MANIFEST}" | sed -n 's/^version[[:space:]]*=[[:space:]]*//p' | tr -d '[:space:]')"
PACKAGE_PLATFORM="$(printf '%s\n' "${MANIFEST}" | sed -n 's/^platform[[:space:]]*=[[:space:]]*//p' | tr -d '[:space:]')"
[ "${PACKAGE_NAME}" = "${APP_NAME}" ] || fail "FPK 应用 ID 不匹配：${PACKAGE_NAME}"
[ "${PACKAGE_VERSION}" = "${LATEST_VERSION}" ] || fail "FPK 版本与 Release 不匹配：${PACKAGE_VERSION}"
[ "${PACKAGE_PLATFORM}" = "x86" ] || fail "FPK 平台不匹配：${PACKAGE_PLATFORM}"

log "校验通过，调用 fnOS 应用中心升级 ${APP_NAME}"
appcenter-cli install-fpk "${PACKAGE}" || fail "fnOS 应用中心安装失败"

HEALTH=''
for _attempt in $(seq 1 30); do
    HEALTH="$(curl --silent --show-error --unix-socket "${APP_DIR}/${APP_NAME}.sock" \
        http://localhost/api/health 2>/dev/null || true)"
    echo "${HEALTH}" | grep -q '"status": "ok"' && break
    sleep 2
done
echo "${HEALTH}" | grep -q '"version": "'"${LATEST_VERSION}"'"' \
    || fail "安装完成但健康检查未返回版本 ${LATEST_VERSION}"
log "自动升级完成：${CURRENT_VERSION:-未知} -> ${LATEST_VERSION}"
