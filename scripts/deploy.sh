#!/usr/bin/env bash
set -euo pipefail

# Build lambda.zip and deploy cloudformation.yaml
#
# Usage:
#   ./scripts/deploy.sh \
#     --artifact-bucket my-cf-artifacts \
#     --sns-topic-arn arn:aws:sns:REGION:ACCOUNT:topic \
#     --source-bucket adaptive-shield-raw-events \
#     --coralogix-domain eu1.coralogix.com \
#     --coralogix-key YOUR_SEND_YOUR_DATA_KEY
#
# Optional:
#   --stack-name coralogix-s3-integration-zst
#   --region us-east-1
#   --application-name s3-zst
#   --subsystem-name logs
#   --secret-arn arn:aws:secretsmanager:...
#   --include-patterns '*.zst,*.zstd'
#   --exclude-patterns '*/tmp/*,*_debug.zst'

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STACK_NAME="coralogix-s3-integration-zst"
REGION="${AWS_DEFAULT_REGION:-}"
CODE_KEY="coralogix-s3-integration-zst/lambda.zip"
APPLICATION_NAME="s3-zst"
SUBSYSTEM_NAME="logs"
CORALOGIX_KEY=""
SECRET_ARN=""
KMS_ARN=""
INCLUDE_PATTERNS=""
EXCLUDE_PATTERNS=""

usage() {
  sed -n '3,20p' "$0"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --artifact-bucket) ARTIFACT_BUCKET="$2"; shift 2 ;;
    --sns-topic-arn) SNS_TOPIC_ARN="$2"; shift 2 ;;
    --source-bucket) SOURCE_BUCKET="$2"; shift 2 ;;
    --coralogix-domain) CORALOGIX_DOMAIN="$2"; shift 2 ;;
    --coralogix-key) CORALOGIX_KEY="$2"; shift 2 ;;
    --secret-arn) SECRET_ARN="$2"; shift 2 ;;
    --kms-key-arn) KMS_ARN="$2"; shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --application-name) APPLICATION_NAME="$2"; shift 2 ;;
    --subsystem-name) SUBSYSTEM_NAME="$2"; shift 2 ;;
    --code-key) CODE_KEY="$2"; shift 2 ;;
    --include-patterns) INCLUDE_PATTERNS="$2"; shift 2 ;;
    --exclude-patterns) EXCLUDE_PATTERNS="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

: "${ARTIFACT_BUCKET:?--artifact-bucket is required}"
: "${SNS_TOPIC_ARN:?--sns-topic-arn is required}"
: "${SOURCE_BUCKET:?--source-bucket is required}"
: "${CORALOGIX_DOMAIN:?--coralogix-domain is required}"

if [[ -z "${CORALOGIX_KEY}" && -z "${SECRET_ARN}" ]]; then
  echo "Provide --coralogix-key or --secret-arn" >&2
  exit 1
fi

AWS=(aws)
if [[ -n "${REGION}" ]]; then
  AWS+=(--region "${REGION}")
fi

echo "Building Lambda package..."
"${ROOT}/scripts/package.sh"

echo "Uploading dist/lambda.zip to s3://${ARTIFACT_BUCKET}/${CODE_KEY}"
"${AWS[@]}" s3 cp "${ROOT}/dist/lambda.zip" "s3://${ARTIFACT_BUCKET}/${CODE_KEY}"

PARAMS=(
  "SnsTopicArn=${SNS_TOPIC_ARN}"
  "S3BucketName=${SOURCE_BUCKET}"
  "LambdaCodeS3Bucket=${ARTIFACT_BUCKET}"
  "LambdaCodeS3Key=${CODE_KEY}"
  "CoralogixDomain=${CORALOGIX_DOMAIN}"
  "CoralogixApplicationName=${APPLICATION_NAME}"
  "CoralogixSubsystemName=${SUBSYSTEM_NAME}"
)

if [[ -n "${CORALOGIX_KEY}" ]]; then
  PARAMS+=("CoralogixSendYourDataKey=${CORALOGIX_KEY}")
fi
if [[ -n "${SECRET_ARN}" ]]; then
  PARAMS+=("CoralogixApiKeySecretArn=${SECRET_ARN}")
fi
if [[ -n "${KMS_ARN}" ]]; then
  PARAMS+=("S3BucketKmsKeyArn=${KMS_ARN}")
fi
if [[ -n "${INCLUDE_PATTERNS}" ]]; then
  PARAMS+=("S3KeyIncludePatterns=${INCLUDE_PATTERNS}")
fi
if [[ -n "${EXCLUDE_PATTERNS}" ]]; then
  PARAMS+=("S3KeyExcludePatterns=${EXCLUDE_PATTERNS}")
fi

echo "Deploying CloudFormation stack ${STACK_NAME}..."
"${AWS[@]}" cloudformation deploy \
  --template-file "${ROOT}/cloudformation.yaml" \
  --stack-name "${STACK_NAME}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "${PARAMS[@]}" \
  --tags Application="Coralogix S3 Integration for .zst logs"

echo "Done."
"${AWS[@]}" cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --query "Stacks[0].Outputs" \
  --output table
