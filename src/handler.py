#!/usr/bin/env python3
"""
Coralogix S3 Integration for .zst logs.

SNS-triggered Lambda: download S3 objects, decompress Zstandard, POST events
to the Coralogix Logs API.

  POST https://ingress.<domain>/logs/v1/singles
  Authorization: Bearer <Send-Your-Data API key>
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import boto3
import zstandard

LOGGER = logging.getLogger()
LOGGER.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

TIMESTAMP_KEYS = (
    "timestamp",
    "@timestamp",
    "time",
    "eventTime",
    "created_at",
    "createdAt",
    "ingested_at",
    "event_timestamp",
    "eventTimestamp",
)

ENVELOPE_KEYS = ("data", "events", "logs", "Records", "items", "results", "alerts")

SEVERITY_MAP = {
    "debug": 1,
    "verbose": 1,
    "trace": 1,
    "info": 3,
    "information": 3,
    "notice": 3,
    "warning": 4,
    "warn": 4,
    "error": 5,
    "err": 5,
    "critical": 6,
    "crit": 6,
    "alert": 6,
    "emergency": 6,
    "fatal": 6,
    "low": 3,
    "medium": 4,
    "high": 5,
    "very high": 6,
}

RETRY_STATUS = {429, 500, 502, 503, 504}
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b"


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def parse_iso_to_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value * 1000) if value < 10_000_000_000 else int(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        return None


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


def env_csv(name: str, default: Optional[List[str]] = None) -> List[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return list(default or [])
    return [part.strip() for part in raw.split(",") if part.strip()]


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _lookup_timestamp(obj: Dict[str, Any]) -> Optional[int]:
    for key in TIMESTAMP_KEYS:
        if key in obj:
            ms = parse_iso_to_ms(obj[key])
            if ms:
                return ms
    return None


def extract_timestamp_ms(event: Any) -> int:
    if isinstance(event, dict):
        ms = _lookup_timestamp(event)
        if ms:
            return ms
        nested = event.get("data")
        if isinstance(nested, dict):
            ms = _lookup_timestamp(nested)
            if ms:
                return ms
    return utc_now_ms()


def map_severity(event: Any) -> int:
    if not isinstance(event, dict):
        return 3
    candidates: List[Any] = [
        event.get("severity"),
        event.get("severityLevel"),
        event.get("level"),
        event.get("alert_type"),
        event.get("alertType"),
    ]
    data = event.get("data")
    if isinstance(data, dict):
        candidates.extend(
            [data.get("severity"), data.get("alert_type"), data.get("alertType")]
        )
    for raw in candidates:
        if raw is None:
            continue
        if isinstance(raw, (int, float)):
            n = int(raw)
            if 1 <= n <= 6:
                return n
            continue
        mapped = SEVERITY_MAP.get(str(raw).strip().lower())
        if mapped:
            return mapped
    return 3


def resolve_api_key() -> str:
    secret_arn = os.getenv("CORALOGIX_API_KEY_SECRET_ARN", "").strip()
    if secret_arn:
        sm = boto3.client("secretsmanager")
        payload = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return payload.strip()
        for key in (
            "CORALOGIX_SEND_YOUR_DATA_KEY",
            "send_your_data_key",
            "api_key",
            "privateKey",
            "value",
        ):
            value = parsed.get(key)
            if value:
                return str(value).strip()
        raise RuntimeError(
            "Secret JSON did not contain CORALOGIX_SEND_YOUR_DATA_KEY (or api_key)"
        )
    return require_env("CORALOGIX_SEND_YOUR_DATA_KEY")


class CoralogixShipper:
    def __init__(
        self,
        api_key: str,
        domain: str,
        application_name: str,
        subsystem_name: str,
        batch_size: int = 200,
        max_batch_bytes: int = 1_500_000,
        timeout: float = 60.0,
        dry_run: bool = False,
    ) -> None:
        domain = domain.lstrip(".").strip()
        self.url = f"https://ingress.{domain}/logs/v1/singles"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.application_name = application_name
        self.subsystem_name = subsystem_name
        self.batch_size = max(1, batch_size)
        self.max_batch_bytes = max(32_768, max_batch_bytes)
        self.timeout = timeout
        self.dry_run = dry_run
        self.max_retries = env_int("CORALOGIX_MAX_RETRIES", 4)

    def _to_cx_record(
        self, event: Any, bucket: str, key: str
    ) -> Dict[str, Any]:
        if isinstance(event, dict):
            payload = dict(event)
        else:
            payload = {"message": event}
        payload.setdefault("cx_source", "s3-zst")
        payload.setdefault("s3_bucket", bucket)
        payload.setdefault("s3_key", key)
        return {
            "applicationName": self.application_name,
            "subsystemName": self.subsystem_name,
            "severity": map_severity(event),
            "category": "s3-zst",
            "timestamp": extract_timestamp_ms(event),
            "text": json.dumps(payload, separators=(",", ":"), default=str),
        }

    def _post(self, body: bytes) -> None:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            req = urllib.request.Request(
                self.url, data=body, headers=self.headers, method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(
                            f"Coralogix ingest error {resp.status}: {resp.read()[:500]!r}"
                        )
                    return
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:500]
                last_error = RuntimeError(
                    f"Coralogix ingest error {exc.code}: {detail!r}"
                )
                if exc.code not in RETRY_STATUS or attempt == self.max_retries:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt == self.max_retries:
                    raise
            sleep_s = min(2 ** attempt, 20)
            LOGGER.warning(
                "Coralogix POST failed (attempt %s/%s), retrying in %ss: %s",
                attempt,
                self.max_retries,
                sleep_s,
                last_error,
            )
            time.sleep(sleep_s)
        if last_error:
            raise last_error

    def ship(self, events: Iterable[Any], bucket: str, key: str) -> int:
        batch: List[Dict[str, Any]] = []
        batch_bytes = 2
        sent = 0

        def flush() -> None:
            nonlocal sent, batch, batch_bytes
            if not batch:
                return
            body = json.dumps(batch, separators=(",", ":")).encode("utf-8")
            LOGGER.info(
                "Shipping %s events (%s bytes) from s3://%s/%s",
                len(batch),
                len(body),
                bucket,
                key,
            )
            if not self.dry_run:
                self._post(body)
            sent += len(batch)
            batch = []
            batch_bytes = 2

        for event in events:
            record = self._to_cx_record(event, bucket, key)
            encoded = json.dumps(record, separators=(",", ":")).encode("utf-8")
            extra = len(encoded) + (1 if batch else 0)
            if batch and (
                len(batch) >= self.batch_size
                or batch_bytes + extra > self.max_batch_bytes
            ):
                flush()
                extra = len(encoded)
            if extra > self.max_batch_bytes:
                LOGGER.warning(
                    "Single event from s3://%s/%s is %s bytes; shipping alone",
                    bucket,
                    key,
                    extra,
                )
            batch.append(record)
            batch_bytes += extra
        flush()
        return sent


def decompress_stream(raw: io.BufferedIOBase, key: str) -> io.BufferedIOBase:
    """Return a readable binary stream of decompressed bytes."""
    lower = key.lower()
    if lower.endswith(".zst") or lower.endswith(".zstd"):
        return zstandard.ZstdDecompressor().stream_reader(raw)
    if lower.endswith(".gz") or lower.endswith(".gzip"):
        return gzip.GzipFile(fileobj=raw)  # type: ignore[return-value]

    header = raw.read(4)
    remainder = io.BytesIO(header + raw.read())
    if header.startswith(ZSTD_MAGIC):
        return zstandard.ZstdDecompressor().stream_reader(remainder)
    if header.startswith(GZIP_MAGIC):
        return gzip.GzipFile(fileobj=remainder)  # type: ignore[return-value]
    return remainder


def unwrap_records(parsed: Any) -> List[Any]:
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ENVELOPE_KEYS:
            value = parsed.get(key)
            if isinstance(value, list):
                return value
        return [parsed]
    return [parsed]


def iter_records_from_text(text: str) -> Iterator[Any]:
    stripped = text.lstrip().lstrip("\ufeff").lstrip()
    if not stripped:
        return
    if stripped[0] in "[{":
        try:
            parsed = json.loads(stripped)
            yield from unwrap_records(parsed)
            return
        except json.JSONDecodeError:
            pass
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            yield {"message": line}
            continue
        yield from unwrap_records(parsed)


def _read_chunk(binary: Any, size: int) -> bytes:
    data = binary.read(size)
    if not data:
        return b""
    if isinstance(data, str):
        return data.encode("utf-8")
    return data


def _emit_line(line: bytes) -> Iterator[Any]:
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return
    try:
        yield from unwrap_records(json.loads(text))
    except json.JSONDecodeError:
        yield {"message": text}


def iter_records_from_stream(binary: Any) -> Iterator[Any]:
    """Stream NDJSON from any .read()-able body; load JSON arrays in full."""
    buf = b""
    while len(buf) < 64 * 1024:
        chunk = _read_chunk(binary, 64 * 1024)
        if not chunk:
            break
        buf += chunk

    stripped = buf.lstrip().lstrip(b"\xef\xbb\xbf").lstrip()
    if not stripped:
        return

    if stripped.startswith(b"["):
        parts = [stripped]
        while True:
            chunk = _read_chunk(binary, 1024 * 1024)
            if not chunk:
                break
            parts.append(chunk)
        yield from iter_records_from_text(b"".join(parts).decode("utf-8", errors="replace"))
        return

    decoder_buf = stripped
    while True:
        while b"\n" in decoder_buf:
            line, decoder_buf = decoder_buf.split(b"\n", 1)
            yield from _emit_line(line)
        chunk = _read_chunk(binary, 64 * 1024)
        if not chunk:
            break
        decoder_buf += chunk
    if decoder_buf.strip():
        # Pretty-printed single object, or a final NDJSON line without newline.
        text = decoder_buf.decode("utf-8", errors="replace")
        yield from iter_records_from_text(text)


def decode_s3_key(key: str) -> str:
    return urllib.parse.unquote_plus(key)


def suffix_allowed(key: str) -> bool:
    suffixes = env_csv("S3_KEY_SUFFIXES", [".zst", ".zstd"])
    if not suffixes:
        return True
    lower = key.lower()
    return any(lower.endswith(suffix.lower()) for suffix in suffixes)


def extract_s3_objects(event: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return (bucket, key) pairs from SNS, S3, or EventBridge payloads."""
    objects: List[Tuple[str, str]] = []

    records = event.get("Records") or []
    if records:
        for record in records:
            if "s3" in record:
                objects.extend(_from_s3_record(record))
                continue
            sns = record.get("Sns") or record.get("sns")
            if sns:
                message = sns.get("Message", "")
                objects.extend(_from_sns_message(message))
                continue
            body = record.get("body")
            if body:
                objects.extend(_from_sns_message(body))
        return objects

    if event.get("source") == "aws.s3" or event.get("detail-type") == "Object Created":
        detail = event.get("detail") or {}
        bucket = (detail.get("bucket") or {}).get("name")
        key = (detail.get("object") or {}).get("key")
        if bucket and key:
            objects.append((bucket, decode_s3_key(key)))
        return objects

    if "Message" in event:
        objects.extend(_from_sns_message(event["Message"]))

    return objects


def _from_s3_record(record: Dict[str, Any]) -> List[Tuple[str, str]]:
    s3 = record.get("s3") or {}
    bucket = (s3.get("bucket") or {}).get("name")
    key = (s3.get("object") or {}).get("key")
    if bucket and key:
        return [(bucket, decode_s3_key(str(key)))]
    return []


def _from_sns_message(message: Any) -> List[Tuple[str, str]]:
    if not message:
        return []
    if not isinstance(message, str):
        message = json.dumps(message)
    text = message.strip()
    if not text or text == "s3:TestEvent":
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        LOGGER.warning("SNS message is not JSON; skipping")
        return []

    if isinstance(payload, dict) and payload.get("Event") == "s3:TestEvent":
        LOGGER.info("Ignoring S3 test event")
        return []
    if isinstance(payload, dict) and payload.get("Records"):
        found: List[Tuple[str, str]] = []
        for record in payload["Records"]:
            found.extend(_from_s3_record(record))
        if found:
            return found
    return extract_s3_objects(payload) if isinstance(payload, dict) else []


def process_object(
    s3_client: Any, shipper: CoralogixShipper, bucket: str, key: str
) -> int:
    if not suffix_allowed(key):
        LOGGER.info("Skipping s3://%s/%s (suffix filter)", bucket, key)
        return 0

    LOGGER.info("Reading s3://%s/%s", bucket, key)
    response = s3_client.get_object(Bucket=bucket, Key=key)
    size = int(response.get("ContentLength") or 0)
    if size == 0:
        LOGGER.info("Skipping empty object s3://%s/%s", bucket, key)
        return 0

    body = response["Body"]
    try:
        decompressed = decompress_stream(body, key)
        events = iter_records_from_stream(decompressed)
        sent = shipper.ship(events, bucket, key)
    finally:
        try:
            body.close()
        except Exception:
            pass

    LOGGER.info("Shipped %s events from s3://%s/%s", sent, bucket, key)
    return sent


def build_shipper() -> CoralogixShipper:
    dry_run = os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
    return CoralogixShipper(
        api_key="dry-run" if dry_run else resolve_api_key(),
        domain=os.getenv("CORALOGIX_DOMAIN", "coralogix.com").strip(),
        application_name=os.getenv("CORALOGIX_APPLICATION_NAME", "s3-zst"),
        subsystem_name=os.getenv("CORALOGIX_SUBSYSTEM_NAME", "logs"),
        batch_size=env_int("CORALOGIX_BATCH_SIZE", 200),
        max_batch_bytes=env_int("CORALOGIX_MAX_BATCH_BYTES", 1_500_000),
        timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "60")),
        dry_run=dry_run,
    )


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    LOGGER.info("Invocation started")
    objects = extract_s3_objects(event)
    if not objects:
        LOGGER.info("No S3 objects in event")
        return {"ok": True, "files": 0, "events": 0}

    s3_client = boto3.client("s3")
    shipper = build_shipper()
    total_events = 0
    errors: List[str] = []

    for bucket, key in objects:
        try:
            total_events += process_object(s3_client, shipper, bucket, key)
        except Exception as exc:
            msg = f"s3://{bucket}/{key}: {exc}"
            LOGGER.exception("Failed processing %s", msg)
            errors.append(msg)

    if errors:
        raise RuntimeError(
            f"Failed {len(errors)}/{len(objects)} file(s): " + "; ".join(errors)
        )

    result = {"ok": True, "files": len(objects), "events": total_events}
    LOGGER.info("Invocation complete: %s", result)
    return result


def _local_main() -> None:
    """Local helper: python handler.py --file ./sample.zst [--dry-run]"""
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ship an Adaptive Shield .zst file")
    parser.add_argument("--file", help="Local .zst / .gz / json file")
    parser.add_argument("--s3-uri", help="s3://bucket/key")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--event-file", help="Lambda SNS/S3 event JSON")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"

    if args.event_file:
        with open(args.event_file, encoding="utf-8") as fh:
            event = json.load(fh)
        print(json.dumps(lambda_handler(event, None), indent=2))
        return

    shipper = build_shipper()

    if args.s3_uri:
        parsed = urllib.parse.urlparse(args.s3_uri)
        bucket = parsed.netloc
        key = decode_s3_key(parsed.path.lstrip("/"))
        sent = process_object(boto3.client("s3"), shipper, bucket, key)
        print(f"Shipped {sent} events from {args.s3_uri}")
        return

    if not args.file:
        parser.error("Provide --file, --s3-uri, or --event-file")

    path = args.file
    with open(path, "rb") as fh:
        decompressed = decompress_stream(fh, path)
        sent = shipper.ship(iter_records_from_stream(decompressed), "local", path)
    print(f"Shipped {sent} events from {path}")


if __name__ == "__main__":
    _local_main()
