#!/usr/bin/env bash
set -euo pipefail

# Build a Lambda zip for Amazon Linux (x86_64 / python3.12).
# Requires Docker. Output: dist/lambda.zip

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="${ROOT}/build"
DIST="${ROOT}/dist"
IMAGE="${SAM_BUILD_IMAGE:-public.ecr.aws/sam/build-python3.12:latest}"

rm -rf "${BUILD}" "${DIST}"
mkdir -p "${BUILD}" "${DIST}"

docker run --rm \
  --platform linux/amd64 \
  -v "${ROOT}:/var/task" \
  -w /var/task \
  "${IMAGE}" \
  /bin/bash -c "pip install -r src/requirements.txt -t /var/task/build --quiet && cp src/handler.py /var/task/build/"

(
  cd "${BUILD}"
  zip -qr "${DIST}/lambda.zip" .
)

echo "Wrote ${DIST}/lambda.zip ($(du -h "${DIST}/lambda.zip" | awk '{print $1}'))"
echo "Upload with:"
echo "  aws s3 cp dist/lambda.zip s3://<artifact-bucket>/coralogix-s3-integration-zst/lambda.zip"
