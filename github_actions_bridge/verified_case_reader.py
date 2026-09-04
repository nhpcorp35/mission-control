"""Bounded, generic access to promoted verified case sources."""
from __future__ import annotations

import re
import io
import zipfile
from typing import Any


CASE_ID_RE = re.compile(r"^NY-[A-Za-z]+-[0-9]{6}-[0-9]{4}-[A-Za-z0-9-]{2,80}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RANGE_BLOCK_BYTES = 1_048_576
_MAX_PDF_BYTES = 32 * 1_048_576


class RangeObjectReader(io.RawIOBase):
    """Seekable, cached B2 reader that never downloads an entire ZIP object."""

    def __init__(self, client: Any, bucket: str, key: str, size: int) -> None:
        self._client, self._bucket, self._key, self._size = client, bucket, key, size
        self._position = 0
        self._blocks: dict[int, bytes] = {}

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        target = offset if whence == io.SEEK_SET else self._position + offset if whence == io.SEEK_CUR else self._size + offset
        if not 0 <= target <= self._size:
            raise ValueError("archive seek is outside the verified source")
        self._position = target
        return target

    def readinto(self, buffer: bytearray) -> int:
        if self._position >= self._size:
            return 0
        wanted = min(len(buffer), self._size - self._position)
        copied = 0
        while copied < wanted:
            block_index = self._position // _RANGE_BLOCK_BYTES
            block = self._blocks.get(block_index)
            if block is None:
                start = block_index * _RANGE_BLOCK_BYTES
                end = min(self._size - 1, start + _RANGE_BLOCK_BYTES - 1)
                response = self._client.get_object(Bucket=self._bucket, Key=self._key, Range=f"bytes={start}-{end}")
                stream = response["Body"]
                try:
                    block = stream.read()
                finally:
                    stream.close()
                self._blocks[block_index] = block
            offset = self._position % _RANGE_BLOCK_BYTES
            take = min(wanted - copied, len(block) - offset)
            if take <= 0:
                raise ValueError("verified source returned an invalid range")
            buffer[copied:copied + take] = block[offset:offset + take]
            copied += take
            self._position += take
        return copied


def canonical_source_prefix(case_id: str, source_sha256: str) -> str:
    if not CASE_ID_RE.fullmatch(case_id):
        raise ValueError("case_id has an unsupported format")
    if not SHA256_RE.fullmatch(source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
    return f"cases/{case_id}/intake/source/{source_sha256}/"



def validate_source_set(case_id: str, payload: dict[str, Any]) -> list[str]:
    """Validate an additive, immutable list of verified source bundle digests."""
    if not CASE_ID_RE.fullmatch(case_id):
        raise ValueError("case_id has an unsupported format")
    if not isinstance(payload, dict) or payload.get("schema_version") != "verified-case-source-set.v1":
        raise ValueError("verified source set has an unsupported schema")
    if payload.get("case_id") != case_id:
        raise ValueError("verified source set case_id does not match")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("verified source set must contain sources")
    digests: list[str] = []
    for item in sources:
        digest = item.get("source_sha256") if isinstance(item, dict) else None
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest) or digest in digests:
            raise ValueError("verified source set contains an invalid or duplicate source")
        digests.append(digest)
    return digests


def source_set_key(case_id: str) -> str:
    """Canonical mutable pointer to immutable verified source-set versions."""
    if not CASE_ID_RE.fullmatch(case_id):
        raise ValueError("case_id has an unsupported format")
    return f"cases/{case_id}/intake/source_set.json"


def read_verified_source_set(client: Any, bucket: str, case_id: str) -> list[str]:
    """Return every verified bundle for a case, oldest/original first.

    Older promoted cases have only an immutable ``case_identity``.  Once a
    supplement is verified, the additive source-set pointer becomes the
    canonical search set.  The original identity must remain in that set.
    """
    if not CASE_ID_RE.fullmatch(case_id):
        raise ValueError("case_id has an unsupported format")
    import json

    identity = json.loads(
        client.get_object(
            Bucket=bucket, Key=f"cases/{case_id}/intake/case_identity.json"
        )["Body"].read()
    )
    original = identity.get("source_sha256") if isinstance(identity, dict) else None
    if not isinstance(original, str) or not SHA256_RE.fullmatch(original):
        raise ValueError("case has an invalid original source identity")
    try:
        payload = json.loads(
            client.get_object(Bucket=bucket, Key=source_set_key(case_id))["Body"].read()
        )
    except Exception as exc:
        response = getattr(exc, "response", None)
        if str(((response or {}).get("Error") or {}).get("Code", "")) in {"404", "NoSuchKey", "NotFound"}:
            return [original]
        raise
    digests = validate_source_set(case_id, payload)
    if original not in digests:
        raise ValueError("verified source set omits the immutable original source")
    return digests

def validate_page_request(document_name: str, pages: list[int]) -> tuple[str, list[int]]:
    if not isinstance(document_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,180}\.pdf", document_name):
        raise ValueError("document_name must be a safe PDF basename")
    if not isinstance(pages, list) or not 1 <= len(pages) <= 10:
        raise ValueError("pages must contain 1 to 10 page numbers")
    cleaned = sorted(set(pages))
    if any(not isinstance(page, int) or not 1 <= page <= 5000 for page in cleaned):
        raise ValueError("pages must be positive page numbers")
    return document_name, cleaned


def read_verified_manifest(client: Any, bucket: str, case_id: str, source_sha256: str) -> tuple[str, dict[str, Any]]:
    prefix = canonical_source_prefix(case_id, source_sha256)
    identity = client.get_object(Bucket=bucket, Key=f"cases/{case_id}/intake/case_identity.json")["Body"].read()
    if not identity:
        raise ValueError("case is not promoted and verified")
    manifest = client.get_object(Bucket=bucket, Key=prefix + "contents_manifest.json")["Body"].read()
    import json
    payload = json.loads(manifest)
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise ValueError("verified contents manifest is invalid")
    return prefix, payload


def extract_pdf_pages(archive_bytes: bytes, document_name: str, pages: list[int]) -> list[dict[str, Any]]:
    """Extract requested text pages from one verified ZIP archive in memory."""
    document_name, pages = validate_page_request(document_name, pages)
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = {name.rsplit("/", 1)[-1]: name for name in archive.namelist()}
            member = names.get(document_name)
            if not member:
                raise ValueError("document is not in the verified archive")
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(archive.read(member)))
            if max(pages) > len(reader.pages):
                raise ValueError("requested page is outside the document")
            return [{"page_number": page, "text": (reader.pages[page - 1].extract_text() or "").strip()} for page in pages]
    except zipfile.BadZipFile as exc:
        raise ValueError("verified source is not a readable ZIP archive") from exc


def read_pdf_from_object(client: Any, bucket: str, key: str, document_name: str) -> bytes:
    """Read one bounded verified PDF from its ZIP source using B2 ranges."""
    document_name, _ = validate_page_request(document_name, [1])
    size = int(client.head_object(Bucket=bucket, Key=key)["ContentLength"])
    try:
        with zipfile.ZipFile(io.BufferedReader(RangeObjectReader(client, bucket, key, size))) as archive:
            members = {name.rsplit("/", 1)[-1]: info for name, info in ((item.filename, item) for item in archive.infolist())}
            member = members.get(document_name)
            if member is None:
                raise ValueError("document is not in the verified archive")
            if member.file_size > _MAX_PDF_BYTES:
                raise ValueError("selected PDF exceeds the bounded reader limit")
            return archive.read(member)
    except zipfile.BadZipFile as exc:
        raise ValueError("verified source is not a readable ZIP archive") from exc


def extract_pdf_pages_from_object(client: Any, bucket: str, key: str, document_name: str, pages: list[int]) -> list[dict[str, Any]]:
    """Read one small PDF from a verified ZIP using B2 byte ranges only."""
    document_name, pages = validate_page_request(document_name, pages)
    size = int(client.head_object(Bucket=bucket, Key=key)["ContentLength"])
    try:
        with zipfile.ZipFile(io.BufferedReader(RangeObjectReader(client, bucket, key, size))) as archive:
            members = {name.rsplit("/", 1)[-1]: info for name, info in ((item.filename, item) for item in archive.infolist())}
            member = members.get(document_name)
            if member is None:
                raise ValueError("document is not in the verified archive")
            if member.file_size > _MAX_PDF_BYTES:
                raise ValueError("selected PDF exceeds the bounded reader limit")
            pdf = archive.read(member)
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(pdf))
            if max(pages) > len(reader.pages):
                raise ValueError("requested page is outside the document")
            return [{"page_number": page, "text": (reader.pages[page - 1].extract_text() or "").strip()} for page in pages]
    except zipfile.BadZipFile as exc:
        raise ValueError("verified source is not a readable ZIP archive") from exc
