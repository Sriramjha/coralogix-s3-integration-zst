# Coralogix S3 Integration for .zst logs

CloudFormation-deployed AWS Lambda that subscribes to an **existing SNS topic**, reads new S3 objects, decompresses **Zstandard (`.zst`)**, and ships events to Coralogix.

The native Coralogix AWS shipper handles gzip well but does not decompress `.zst`. This integration fills that gap.

```
S3 (.zst)  →  SNS  →  Lambda  →  Coralogix Logs API
                     decompress zstd
                     POST /logs/v1/singles
```

Typical source: Adaptive Shield (or any producer) writing raw events to S3 as `.zst`.

## Deploy with CloudFormation

### 1. Build and upload the Lambda zip

The function needs the `zstandard` native library, so the zip must be built for Amazon Linux x86_64.

```bash
chmod +x scripts/package.sh scripts/deploy.sh
./scripts/package.sh

aws s3 cp dist/lambda.zip \
  s3://YOUR_ARTIFACT_BUCKET/coralogix-s3-integration-zst/lambda.zip
```

`YOUR_ARTIFACT_BUCKET` is any bucket CloudFormation/Lambda can read. It is **not** the Adaptive Shield log bucket.

### 2. Create the stack

```bash
aws cloudformation deploy \
  --template-file cloudformation.yaml \
  --stack-name coralogix-s3-integration-zst \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    SnsTopicArn=arn:aws:sns:REGION:ACCOUNT:your-topic \
    S3BucketName=your-log-bucket \
    LambdaCodeS3Bucket=YOUR_ARTIFACT_BUCKET \
    LambdaCodeS3Key=coralogix-s3-integration-zst/lambda.zip \
    CoralogixDomain=eu1.coralogix.com \
    CoralogixApplicationName=s3-zst \
    CoralogixSubsystemName=logs \
    CoralogixSendYourDataKey=YOUR_SEND_YOUR_DATA_KEY \
    S3KeyIncludePatterns='*.zst,*.zstd' \
    S3KeyExcludePatterns='*/tmp/*,*/test/*,*_debug.zst'
```

Or use the helper (build + upload + deploy):

```bash
./scripts/deploy.sh \
  --artifact-bucket YOUR_ARTIFACT_BUCKET \
  --sns-topic-arn arn:aws:sns:REGION:ACCOUNT:your-topic \
  --source-bucket your-log-bucket \
  --coralogix-domain eu1.coralogix.com \
  --coralogix-key YOUR_SEND_YOUR_DATA_KEY \
  --region REGION
```

You can also launch `cloudformation.yaml` in the AWS CloudFormation console after the zip is in S3.

If the log bucket uses SSE-KMS, add `S3BucketKmsKeyArn=arn:aws:kms:...`.

To keep the API key out of stack parameters, store it in Secrets Manager and pass `CoralogixApiKeySecretArn` instead of `CoralogixSendYourDataKey`. The secret can be a raw string or JSON with `CORALOGIX_SEND_YOUR_DATA_KEY`.

If a Coralogix native Lambda is already subscribed to the same SNS topic, unsubscribe it so you do not ingest unreadable compressed bytes twice.

### Include and exclude patterns

Patterns are comma-separated shell wildcards (`*`, `?`, `[seq]`) matched against the full S3 key and the filename. **Exclude wins.**

```bash
# Only Adaptive Shield raw events, skip test/debug objects
S3KeyIncludePatterns='*.zst,*.zstd,raw/*/*.zst'
S3KeyExcludePatterns='*/tmp/*,*/test/*,*_debug.zst,*.metadata.zst'
```

| Example include | Matches |
| --- | --- |
| `*.zst,*.zstd` | Any key ending in those suffixes (default) |
| `raw/*/*.zst` | `raw/2026/events.zst`, not `other/events.zst` |
| `*` | Every object the SNS notification delivers |

| Example exclude | Skips |
| --- | --- |
| `*/tmp/*` | `adaptive/tmp/file.zst` |
| `*_debug.zst` | `events_debug.zst` |
| `test/*,*.metadata.zst` | test prefix or metadata sidecars |

## What the stack creates

| Resource | Purpose |
| --- | --- |
| Lambda `*-zst-shipper` | Decompress `.zst` and POST to Coralogix |
| IAM role | `s3:GetObject` on the log bucket, CloudWatch Logs, DLQ |
| SNS subscription | Your existing topic → this Lambda |
| SQS DLQ | Failed invocations after 2 retries |
| Log group | `/aws/lambda/<stack>-zst-shipper` |

## CloudFormation parameters

| Parameter | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SnsTopicArn` | yes | | SNS topic that receives S3 notifications |
| `S3BucketName` | yes | | Bucket that stores `.zst` objects |
| `LambdaCodeS3Bucket` | yes | | Artifact bucket for `lambda.zip` |
| `LambdaCodeS3Key` | | `coralogix-s3-integration-zst/lambda.zip` | Zip key |
| `CoralogixDomain` | | `eu1.coralogix.com` | Coralogix region domain |
| `CoralogixApplicationName` | | `s3-zst` | Application name in Coralogix |
| `CoralogixSubsystemName` | | `logs` | Subsystem name in Coralogix |
| `CoralogixSendYourDataKey` | one of key/secret | | Send-Your-Data API key |
| `CoralogixApiKeySecretArn` | one of key/secret | | Secrets Manager ARN |
| `S3BucketKmsKeyArn` | | empty | KMS key if the log bucket is SSE-KMS |
| `S3KeyIncludePatterns` | | `*.zst,*.zstd` | Comma-separated shell wildcards. Key must match at least one. Use `*` for all keys. |
| `S3KeyExcludePatterns` | | empty | Comma-separated shell wildcards. Matching keys are skipped even if included. |
| `LambdaMemoryMb` | | `1024` | Raise for large files |
| `LambdaTimeoutSeconds` | | `300` | 5 minutes |

## Local test (no deploy)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python src/handler.py --file events/sample.ndjson.zst --dry-run
```

## Finding logs in Coralogix

- Application: `s3-zst` (or `CoralogixApplicationName`)
- Subsystem: `logs` (or `CoralogixSubsystemName`)
- Each event includes `cx_source=s3-zst`, `s3_bucket`, and `s3_key`

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Lambda never runs | SNS subscription exists; S3 event notification targets that topic |
| `AccessDenied` on S3 | Role `s3:GetObject`; KMS key policy if SSE-KMS |
| No logs in Coralogix | Domain, Send-Your-Data key, Explore time range |
| `Coralogix ingest error 401` | Wrong or truncated API key |
| Timeout | Raise memory/timeout; uncompressed size is much larger than `.zst` |
