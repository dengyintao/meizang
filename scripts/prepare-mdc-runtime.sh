#!/bin/bash
set -eu

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_DIR="${1:?usage: prepare-mdc-runtime.sh TARGET_DIR}"
WHEEL_CACHE="${MDC_WHEEL_CACHE:-/tmp/meizang-mdc-wheels}"

mkdir -p "${WHEEL_CACHE}" "${TARGET_DIR}"

if ! ls "${WHEEL_CACHE}"/lxml-*-cp312-*x86_64.whl >/dev/null 2>&1; then
  python3 -m pip download --dest "${WHEEL_CACHE}" \
    --platform manylinux2014_x86_64 --python-version 312 --implementation cp --abi cp312 \
    --only-binary=:all: requests dlib-bin click numpy face-recognition-models lxml \
    beautifulsoup4 pillow==10.3.0 cloudscraper pysocks==1.7.1 urllib3==1.26.19 \
    certifi MechanicalSoup opencc-python-reimplemented coloredlogs concurrent-log-handler mutagen==1.47.0
fi
if ! ls "${WHEEL_CACHE}"/mutagen-1.47.0-*.whl >/dev/null 2>&1; then
  python3 -m pip download --dest "${WHEEL_CACHE}" --no-deps --only-binary=:all: mutagen==1.47.0
fi
if ! ls "${WHEEL_CACHE}"/face_recognition-*.whl >/dev/null 2>&1; then
  python3 -m pip download --dest "${WHEEL_CACHE}" --no-deps --only-binary=:all: face-recognition==1.3.0
fi
if ! ls "${WHEEL_CACHE}"/setuptools-80.9.0-*.whl >/dev/null 2>&1; then
  python3 -m pip download --dest "${WHEEL_CACHE}" --no-deps --only-binary=:all: setuptools==80.9.0
fi
if ! ls "${WHEEL_CACHE}"/face_recognition_models-0.3.0.tar.gz >/dev/null 2>&1; then
  python3 -m pip download --dest "${WHEEL_CACHE}" --no-deps --no-binary=:all: face-recognition-models==0.3.0
fi

find "${TARGET_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
for wheel in "${WHEEL_CACHE}"/*.whl; do
  python3 -m zipfile -e "${wheel}" "${TARGET_DIR}"
done

# PyPI's only wheel is the obsolete 0.1.3 release and lacks the 5-point and
# CNN models that face_recognition imports eagerly. Overlay the complete 0.3.0
# source package so the bundled runtime remains fully offline-capable.
MODEL_TMP="$(mktemp -d /tmp/meizang-face-models.XXXXXX)"
tar -xzf "${WHEEL_CACHE}/face_recognition_models-0.3.0.tar.gz" -C "${MODEL_TMP}"
rm -rf "${TARGET_DIR}/face_recognition_models"
cp -R "${MODEL_TMP}/face_recognition_models-0.3.0/face_recognition_models" "${TARGET_DIR}/"
rm -rf "${MODEL_TMP}"

find "${TARGET_DIR}" -name '*.pyc' -delete
find "${TARGET_DIR}" -type d -name '__pycache__' -prune -exec rm -rf {} +

echo "MDC runtime ready: ${TARGET_DIR}"
