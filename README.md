# Coralogix S3 Integration for .zst logs

Ship Zstandard-compressed (`.zst`) log files from Amazon S3 to Coralogix.

The native Coralogix AWS Lambda shipper can subscribe to your SNS topic, but it does not decompress `.zst`. This stack is the replacement: **SNS → Lambda → decompress → Coralogix**.

```
Producer (e.g. Adaptive Shield)
        │
        ▼
   S3 bucket  ──ObjectCreated──►  SNS topic  ──►  this Lambda
                                                     │
                                          decompress .zst
                                                     │
                                                     ▼
                                              Coralogix Logs
                                         application / subsystem
```

Follow the steps below in order. You only need the AWS CLI path **or** the Console path, not both.

---

## 1. What you need

Work on a machine that has:

| Tool | Why | Check |
| --- | --- | --- |
| [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) | Deploy the stack | `aws --version` |
| An AWS identity with rights to create Lambda, IAM, SQS, logs, and an SNS subscription | CloudFormation will create those | `aws sts get-caller-identity` |
| [Docker Desktop](https://docs.docker.com/get-docker/) | Build the Lambda zip for Amazon Linux | `docker info` |
| Git | Clone this repo | `git --version` |

You do **not** need SAM, Python, or a local Coralogix agent for production deploy.

---

## 2. Collect these values

Copy this block into a notes file and replace every `REPLACE_…` value. You will paste them into the commands in step 4.

```bash
# AWS
export AWS_REGION=REPLACE_REGION                    # e.g. us-east-1
export AWS_ACCOUNT_ID=REPLACE_ACCOUNT_ID            # 12 digits
export LOG_BUCKET=REPLACE_LOG_BUCKET                # bucket that already has .zst files
export SNS_TOPIC_ARN=arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:REPLACE_TOPIC
export ARTIFACT_BUCKET=REPLACE_ARTIFACT_BUCKET      # any bucket you can write to (not the log bucket)
export STACK_NAME=coralogix-s3-integration-zst

# Coralogix
export CX_DOMAIN=REPLACE_DOMAIN                     # see table below
export CX_APP=s3-zst
export CX_SUBSYSTEM=logs
export CX_KEY=REPLACE_SEND_YOUR_DATA_KEY

# Optional filters (comma-separated, no spaces needed)
export INCLUDE_PATTERNS='*.zst,*.zstd'
export EXCLUDE_PATTERNS='*/tmp/*,*/test/*,*_debug.zst'
```

### Where to find each value

**S3 log bucket (`LOG_BUCKET`)**  
AWS Console → **S3** → the bucket Adaptive Shield (or another producer) writes to.  
Confirm objects look like `something.zst`.

**SNS topic (`SNS_TOPIC_ARN`)**  
1. Open that same bucket → **Properties** → **Event notifications**.  
2. Open the notification that fires on object create.  
3. Copy the destination SNS topic ARN.  
If there is no notification yet, create one: destination = SNS, events = `s3:ObjectCreated:*`.

**Artifact bucket (`ARTIFACT_BUCKET`)**  
A normal S3 bucket used only to store `lambda.zip`. Create one if you do not have it:

```bash
aws s3 mb s3://REPLACE_ARTIFACT_BUCKET --region "$AWS_REGION"
```

**Coralogix domain (`CX_DOMAIN`)**  
Use the domain of the Coralogix account that should receive the logs (no `https://`, no `ingress.` prefix):

| Your Coralogix UI | Value to use |
| --- | --- |
| `https://xxx.coralogix.com` | `coralogix.com` |
| `https://xxx.eu1.coralogix.com` | `eu1.coralogix.com` |
| `https://xxx.eu2.coralogix.com` | `eu2.coralogix.com` |
| `https://xxx.coralogix.in` | `coralogix.in` |
| Team URL like `https://cx498.app.coralogix.com` | `cx498.coralogix.com` |

**Send-Your-Data key (`CX_KEY`)**  
Coralogix UI → **Data Flow** → **API Keys** → **Send-Your-Data**. Create one if needed. Treat it like a password.

**Optional: KMS**  
If the log bucket shows **SSE-KMS** under Properties → Default encryption, also set:

```bash
export KMS_KEY_ARN=arn:aws:kms:${AWS_REGION}:${AWS_ACCOUNT_ID}:key/REPLACE_KEY_ID
```

---

## 3. Clone the repo

```bash
git clone https://github.com/Sriramjha/coralogix-s3-integration-zst.git
cd coralogix-s3-integration-zst
chmod +x scripts/package.sh scripts/deploy.sh
```

Load the variables from step 2 in this same terminal (`export …`).

---

## 4. Deploy (pick one)

### Option A — one script (recommended)

This builds the Linux zip, uploads it, and creates/updates the CloudFormation stack.

```bash
./scripts/deploy.sh \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --artifact-bucket "$ARTIFACT_BUCKET" \
  --sns-topic-arn "$SNS_TOPIC_ARN" \
  --source-bucket "$LOG_BUCKET" \
  --coralogix-domain "$CX_DOMAIN" \
  --coralogix-key "$CX_KEY" \
  --application-name "$CX_APP" \
  --subsystem-name "$CX_SUBSYSTEM" \
  --include-patterns "$INCLUDE_PATTERNS" \
  --exclude-patterns "$EXCLUDE_PATTERNS"
```

If the log bucket is KMS-encrypted, add `--kms-key-arn "$KMS_KEY_ARN"`.

The first run can take 2–3 minutes (Docker pull + zip + stack create). On success you will see a table of stack outputs (`FunctionName`, `DeadLetterQueueUrl`, …).

### Option B — AWS CLI, step by step

Use this if you want to see each action.

**4B.1 Build the Lambda package** (needs Docker running):

```bash
./scripts/package.sh
# creates dist/lambda.zip
```

**4B.2 Upload the zip**

```bash
aws s3 cp dist/lambda.zip \
  "s3://${ARTIFACT_BUCKET}/coralogix-s3-integration-zst/lambda.zip" \
  --region "$AWS_REGION"
```

**4B.3 Create the stack**

```bash
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --template-file cloudformation.yaml \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    SnsTopicArn="$SNS_TOPIC_ARN" \
    S3BucketName="$LOG_BUCKET" \
    LambdaCodeS3Bucket="$ARTIFACT_BUCKET" \
    LambdaCodeS3Key=coralogix-s3-integration-zst/lambda.zip \
    CoralogixDomain="$CX_DOMAIN" \
    CoralogixApplicationName="$CX_APP" \
    CoralogixSubsystemName="$CX_SUBSYSTEM" \
    CoralogixSendYourDataKey="$CX_KEY" \
    S3KeyIncludePatterns="$INCLUDE_PATTERNS" \
    S3KeyExcludePatterns="$EXCLUDE_PATTERNS"
```

Add `S3BucketKmsKeyArn="$KMS_KEY_ARN"` on the last line if needed.

### Option C — AWS Console

1. On a machine with Docker, run `./scripts/package.sh`.  
2. In **S3**, upload `dist/lambda.zip` to `s3://ARTIFACT_BUCKET/coralogix-s3-integration-zst/lambda.zip`.  
3. Open **CloudFormation** → **Create stack** → **With new resources** → **Upload a template file** → choose `cloudformation.yaml`.  
4. Stack name: `coralogix-s3-integration-zst`.  
5. Fill the form (groups are labeled **S3 and SNS**, **Lambda package**, **Coralogix**).  
   - **SNS topic ARN** and **Source S3 bucket** are the log pipeline.  
   - **Artifact bucket** / key point at the zip from step 2.  
   - Enter the Send-Your-Data key **or** a Secrets Manager ARN, not both empty.  
6. Capabilities → check **I acknowledge that AWS CloudFormation might create IAM resources with custom names**.  
7. **Submit**. Wait until status is `CREATE_COMPLETE`.

---

## 5. Verify it works

**5.1 Stack is healthy**

```bash
aws cloudformation describe-stacks \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].{Status:StackStatus,Outputs:Outputs}" \
  --output table
```

Expect `CREATE_COMPLETE` or `UPDATE_COMPLETE`.

**5.2 SNS is subscribed to the Lambda**

```bash
aws sns list-subscriptions-by-topic \
  --region "$AWS_REGION" \
  --topic-arn "$SNS_TOPIC_ARN" \
  --output table
```

You should see a `lambda` subscription whose endpoint is `…:function:coralogix-s3-integration-zst-zst-shipper`.

**5.3 Trigger a real file**

Upload a `.zst` object into the log bucket (or wait for Adaptive Shield to write one):

```bash
aws s3 cp events/sample.ndjson.zst \
  "s3://${LOG_BUCKET}/verify/sample.ndjson.zst" \
  --region "$AWS_REGION"
```

**5.4 Check Lambda logs** (wait ~30 seconds):

```bash
aws logs tail "/aws/lambda/${STACK_NAME}-zst-shipper" \
  --region "$AWS_REGION" \
  --since 10m \
  --follow
```

Look for `Reading s3://…` and `Shipped N events`.  
If you see `Skipping … (include=… exclude=…)`, the object did not match your patterns — adjust include/exclude and update the stack.

**5.5 Confirm in Coralogix**

1. Open Coralogix → **Explore**.  
2. Time range: **Last 15 minutes**.  
3. Filter:

```text
applicationName:s3-zst AND subsystemName:logs
```

Each event includes `cx_source=s3-zst`, `s3_bucket`, and `s3_key`.

---

## 6. Include and exclude patterns

The Lambda only downloads keys that match **include** and do not match **exclude**. Exclude always wins.

Set them at deploy time (`S3KeyIncludePatterns` / `S3KeyExcludePatterns`) or later with a stack update.

| Goal | Include | Exclude |
| --- | --- | --- |
| All `.zst` / `.zstd` files (default) | `*.zst,*.zstd` | *(empty)* |
| Only one prefix | `raw/*/*.zst` | *(empty)* |
| All zst except temp/test/debug | `*.zst,*.zstd` | `*/tmp/*,*/test/*,*_debug.zst` |
| Everything SNS sends | `*` | `*.metadata.zst` |

Patterns are comma-separated shell wildcards (`*`, `?`). They are matched against the full S3 key and the filename, case-insensitive.

```bash
# Example: Adaptive Shield raw events, skip junk
INCLUDE_PATTERNS='*.zst,*.zstd'
EXCLUDE_PATTERNS='*/tmp/*,*/test/*,*_debug.zst,*.metadata.zst'
```

---

## 7. After deploy — important checks

1. **Unsubscribe the native Coralogix shipper** from the same SNS topic if it is still attached. Otherwise Coralogix may also ingest raw compressed bytes.  
   SNS console → topic → **Subscriptions** → delete the old Lambda subscription only.  
2. Leave this stack’s `lambda` subscription in place.  
3. Failed invocations retry twice, then land on the SQS DLQ named `${STACK_NAME}-dlq`.

```bash
aws sqs get-queue-attributes \
  --region "$AWS_REGION" \
  --queue-url "$(aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK_NAME" --query "Stacks[0].Outputs[?OutputKey=='DeadLetterQueueUrl'].OutputValue" --output text)" \
  --attribute-names ApproximateNumberOfMessages
```

---

## 8. Update the stack later

Change a parameter (domain, patterns, memory) and run the **same** `./scripts/deploy.sh` or `aws cloudformation deploy` command again. CloudFormation updates in place.

To ship a code change:

```bash
git pull
./scripts/package.sh
aws s3 cp dist/lambda.zip \
  "s3://${ARTIFACT_BUCKET}/coralogix-s3-integration-zst/lambda.zip" \
  --region "$AWS_REGION"
aws lambda update-function-code \
  --region "$AWS_REGION" \
  --function-name "${STACK_NAME}-zst-shipper" \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --s3-key coralogix-s3-integration-zst/lambda.zip
```

---

## 9. Remove the integration

```bash
aws cloudformation delete-stack --region "$AWS_REGION" --stack-name "$STACK_NAME"
aws cloudformation wait stack-delete-complete --region "$AWS_REGION" --stack-name "$STACK_NAME"
```

This removes the Lambda, IAM role, SNS subscription, DLQ, and log group. It does **not** delete your log bucket, SNS topic, or artifact zip.

---

## 10. Optional: test on your laptop (no AWS)

Useful to confirm a real `.zst` file parses before you deploy.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Sample file shipped with the repo
python src/handler.py --file events/sample.ndjson.zst --dry-run

# Your own file
python src/handler.py --file /path/to/events.json.zst --dry-run
```

`--dry-run` decompresses and parses only. It does not call Coralogix.

To send a local file for real, create a `.env` from `.env.example` and omit `--dry-run`.

---

## What the stack creates

| Resource | Name pattern | Purpose |
| --- | --- | --- |
| Lambda | `${STACK_NAME}-zst-shipper` | Read S3, decompress `.zst`, POST to Coralogix |
| IAM role | `${STACK_NAME}-zst-shipper-role` | `s3:GetObject` on the log bucket, logs, DLQ |
| SNS subscription | on your existing topic | Invoke the Lambda on new objects |
| SQS queue | `${STACK_NAME}-dlq` | Failed events after 2 retries |
| Log group | `/aws/lambda/${STACK_NAME}-zst-shipper` | Execution logs (14-day retention by default) |

The stack does **not** create the log bucket or the SNS topic. Those must already exist.

---

## CloudFormation parameters (reference)

| Parameter | Required | Default | What to enter |
| --- | --- | --- | --- |
| `SnsTopicArn` | yes | | Full ARN of the topic used by the S3 notification |
| `S3BucketName` | yes | | Bucket that stores `.zst` objects |
| `LambdaCodeS3Bucket` | yes | | Bucket that holds `lambda.zip` |
| `LambdaCodeS3Key` | | `coralogix-s3-integration-zst/lambda.zip` | Key of that zip |
| `CoralogixDomain` | | `eu1.coralogix.com` | Domain from the table in step 2 |
| `CoralogixApplicationName` | | `s3-zst` | Application name in Explore |
| `CoralogixSubsystemName` | | `logs` | Subsystem name in Explore |
| `CoralogixSendYourDataKey` | one of key / secret | | Send-Your-Data API key |
| `CoralogixApiKeySecretArn` | one of key / secret | | Secrets Manager ARN (raw key or JSON with `CORALOGIX_SEND_YOUR_DATA_KEY`) |
| `S3BucketKmsKeyArn` | | empty | KMS key ARN if the log bucket uses SSE-KMS |
| `S3KeyIncludePatterns` | | `*.zst,*.zstd` | Comma-separated include wildcards. Use `*` for all keys |
| `S3KeyExcludePatterns` | | empty | Comma-separated exclude wildcards. Exclude wins |
| `CoralogixBatchSize` | | `200` | Max events per HTTP POST |
| `LambdaMemoryMb` | | `1024` | Increase if files decompress to tens of MB |
| `LambdaTimeoutSeconds` | | `300` | 5 minutes |
| `LogRetentionDays` | | `14` | CloudWatch retention |

To keep the API key out of the stack, create a secret and pass only `CoralogixApiKeySecretArn`.

---

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `docker: command not found` or package.sh fails | Install/start Docker Desktop, then rerun `./scripts/package.sh` |
| `Unable to locate credentials` | `aws configure` or export `AWS_PROFILE` / SSO: `aws sso login` |
| Stack `ROLLBACK_COMPLETE` | CloudFormation → stack → **Events** (red rows). Common cause: empty API key, bad topic ARN, or zip key not found |
| `S3 error: The specified key does not exist` during deploy | Upload `lambda.zip` first (step 4B.2). Bucket/key must match the parameters |
| Lambda never invoked | S3 event notification must target **this** SNS topic; SNS must show a `lambda` subscription |
| `AccessDenied` on `GetObject` | Confirm `S3BucketName` is the log bucket. If SSE-KMS, set `S3BucketKmsKeyArn` and allow the Lambda role on the key policy |
| Log line `Skipping s3://… (include=…)` | Key failed the pattern filter. Widen include or narrow exclude, then update the stack |
| `Coralogix ingest error 401` | Wrong Send-Your-Data key, or `CoralogixDomain` has a typo / extra `ingress.` prefix |
| No logs in Explore | Use **Last 15 minutes**, filter `applicationName:s3-zst`, confirm `CX_DOMAIN` is the same account you are viewing |
| Lambda timeout | Raise `LambdaMemoryMb` and `LambdaTimeoutSeconds`. Uncompressed size is much larger than the `.zst` |
| Messages on the DLQ | `aws logs tail /aws/lambda/${STACK_NAME}-zst-shipper --since 1h` and fix the error before replaying |

Still stuck: download one `.zst` from the log bucket and run step 10 locally. If dry-run prints `Shipped N events`, the file format is fine and the problem is AWS/Coralogix config.
