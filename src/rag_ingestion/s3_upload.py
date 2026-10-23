"""Presigned-upload + reprocessing-download helpers - ported from hrb_chatbot_service's
common/clients/storage_client/s3_upload_client.py, narrowed to the 3 functions this
service's own ingestion routes actually use (the leave-document/admin-doc functions in
the original belong to other features, not ported here). Built on top of
hrb_common.storage's generic (bucket, key) functions rather than hand-rolling boto3
calls again."""

from pathlib import Path

import boto3

from hrb_common.observability.secrets import read_secret
from hrb_common.storage.s3_client import delete_object, generate_presigned_upload_url as _generate_presigned_upload_url

DEFAULT_REGION = "us-east-1"
DEFAULT_EXPIRY_SECONDS = 300

# Matches documents/service.py's own UPLOAD_DIRECTORY.
UPLOAD_DIRECTORY = Path("data/uploads")


def _key(document_id: str, filename: str) -> str:
    return f"{document_id}/{filename}"


def generate_presigned_upload_url(document_id: str, filename: str, content_type: str) -> dict:
    bucket = read_secret("S3_UPLOAD_BUCKET", "")
    region = read_secret("AWS_REGION", DEFAULT_REGION)
    expiry_seconds = int(read_secret("S3_PRESIGNED_URL_EXPIRY_SECONDS", str(DEFAULT_EXPIRY_SECONDS)))

    result = _generate_presigned_upload_url(
        bucket, _key(document_id, filename), content_type, expiry_seconds=expiry_seconds, region=region
    )
    return {"upload_url": result["upload_url"], "expires_in_seconds": expiry_seconds}


def delete_uploaded_object(document_id: str, filename: str) -> None:
    """Best-effort - deleting an already-missing key is a no-op, not an error."""
    bucket = read_secret("S3_UPLOAD_BUCKET", "")
    region = read_secret("AWS_REGION", DEFAULT_REGION)
    delete_object(bucket, _key(document_id, filename), region=region)


def download_for_reprocessing(file_path: str, document_id: str, filename: str) -> str:
    """A local path is returned unchanged. An s3://bucket/key URI (every document
    uploaded through the presigned-upload path) is downloaded to the same local
    layout a synchronous upload already uses, and that local path is returned."""
    if not file_path.startswith("s3://"):
        return file_path

    without_scheme = file_path[len("s3://") :]
    bucket, _, key = without_scheme.partition("/")
    region = read_secret("AWS_REGION", DEFAULT_REGION)
    s3_client = boto3.client("s3", region_name=region)

    document_directory = UPLOAD_DIRECTORY / document_id
    document_directory.mkdir(parents=True, exist_ok=True)
    local_path = document_directory / filename

    s3_client.download_file(bucket, key, str(local_path))
    return str(local_path)
