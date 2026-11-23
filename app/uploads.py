"""Uploaded chat attachments and document text extraction.

Files are stored under ``data/uploads`` with random names. The original name is
only display metadata; the browser sends the stored id back when submitting a
message. Images are converted to data URLs for vision-capable chat models, while
documents are converted to text locally before the model call.
"""
from __future__ import annotations

import base64
import html
import io
import mimetypes
import re
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_FILES = 6
MAX_DOCUMENT_CHARS = 60_000
MAX_DOCUMENT_TOTAL_CHARS = 90_000

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".json",
    ".html",
    ".htm",
    ".xml",
    ".log",
}
STORED_NAME = re.compile(r"^[a-f0-9]{32}\.[a-z0-9]{1,8}$")


class UploadError(ValueError):
    pass


def safe_name(name: str) -> str:
    value = Path(str(name or "upload")).name
    value = "".join(ch for ch in value if ch.isprintable() and ch not in "\r\n\t")
    value = value.strip()[:200]
    return value or "upload"


def _extension(name: str) -> str:
    return Path(name).suffix.lower()


def _kind(extension: str) -> str:
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in DOCUMENT_EXTENSIONS:
        return "document"
    raise UploadError(
        "Unsupported file type. Use images (PNG, JPEG, WEBP, GIF) or documents "
        "(PDF, DOCX, TXT, MD, CSV, JSON, HTML, XML)."
    )


def _mime(name: str, kind: str) -> str:
    guessed = mimetypes.guess_type(name)[0] or ""
    if kind == "image":
        return guessed or "image/png"
    return guessed or "application/octet-stream"


def _check_content(extension: str, data: bytes) -> None:
    if not data:
        raise UploadError("The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(
            "The uploaded file is too large (max %d MB)."
            % (MAX_UPLOAD_BYTES // (1024 * 1024))
        )
    if extension == ".png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise UploadError("This file does not look like a PNG image.")
    if extension in (".jpg", ".jpeg") and not data.startswith(b"\xff\xd8"):
        raise UploadError("This file does not look like a JPEG image.")
    if extension == ".gif" and not data.startswith((b"GIF87a", b"GIF89a")):
        raise UploadError("This file does not look like a GIF image.")
    if extension == ".webp" and not (
        data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    ):
        raise UploadError("This file does not look like a WEBP image.")
    if extension == ".pdf" and not data.startswith(b"%PDF"):
        raise UploadError("This file does not look like a PDF document.")
    if extension == ".docx" and not data.startswith(b"PK"):
        raise UploadError("This file does not look like a DOCX document.")


def save_upload(
    uploads_dir: Path, original_name: str, data: bytes
) -> Dict[str, Any]:
    name = safe_name(original_name)
    extension = _extension(name)
    if extension == ".jpeg":
        extension = ".jpg"
    kind = _kind(extension)
    _check_content(extension, data)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    stored = uuid.uuid4().hex + extension
    (uploads_dir / stored).write_bytes(data)
    return {
        "id": stored,
        "name": name,
        "mime": _mime(name, kind),
        "kind": kind,
        "size": len(data),
        "url": "/api/uploads/" + stored,
    }


def path_for(uploads_dir: Path, stored_id: str) -> Path:
    if not STORED_NAME.match(stored_id or ""):
        raise UploadError("Invalid upload id.")
    root = uploads_dir.resolve()
    path = (root / stored_id).resolve()
    if root != path.parent:
        raise UploadError("Invalid upload path.")
    if not path.is_file():
        raise UploadError("The uploaded file is missing.")
    return path


def attachment_for(
    uploads_dir: Path, stored_id: str, original_name: Optional[str] = None
) -> Dict[str, Any]:
    path = path_for(uploads_dir, stored_id)
    extension = path.suffix.lower()
    kind = _kind(extension)
    name = safe_name(original_name or stored_id)
    return {
        "id": stored_id,
        "name": name,
        "mime": _mime(name, kind),
        "kind": kind,
        "size": path.stat().st_size,
        "url": "/api/uploads/" + stored_id,
    }


def attachment_from_input(
    uploads_dir: Path, item: Dict[str, Any]
) -> Dict[str, Any]:
    return attachment_for(
        uploads_dir,
        str(item.get("id") or ""),
        str(item.get("name") or ""),
    )


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag.lower() in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        elif tag.lower() in ("p", "div", "br", "li", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise UploadError("PDF extraction is unavailable on this server.") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages)
    except Exception as exc:
        raise UploadError("Could not read this PDF: %s" % exc) from exc


def _docx_text(data: bytes) -> str:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise UploadError("DOCX extraction is unavailable on this server.") from exc
    try:
        document = Document(io.BytesIO(data))
        parts = [paragraph.text for paragraph in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts)
    except Exception as exc:
        raise UploadError("Could not read this DOCX: %s" % exc) from exc


def extract_text(path: Path) -> str:
    extension = path.suffix.lower()
    try:
        data = path.read_bytes()
        if extension == ".pdf":
            text = _pdf_text(data)
        elif extension == ".docx":
            text = _docx_text(data)
        elif extension in (".html", ".htm"):
            parser = _HTMLText()
            parser.feed(_decode_text(data))
            text = html.unescape("".join(parser.parts))
        else:
            text = _decode_text(data)
    except UploadError:
        raise
    except Exception as exc:
        raise UploadError("Could not read this document: %s" % exc) from exc
    text = text.replace("\x00", "").strip()
    if len(text) > MAX_DOCUMENT_CHARS:
        text = text[:MAX_DOCUMENT_CHARS] + "\n\n[Document truncated]"
    return text


def image_data_url(path: Path, mime: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return "data:%s;base64,%s" % (mime or "image/png", encoded)


def user_content(
    text: str,
    attachments: Iterable[Dict[str, Any]],
    uploads_dir: Path,
) -> Any:
    """Build a user chat message, converting files to text or vision blocks."""
    prompt = (text or "").strip()
    documents: List[str] = []
    images: List[Dict[str, Any]] = []
    total_doc_chars = 0
    for attachment in attachments:
        kind = attachment.get("kind")
        try:
            path = path_for(uploads_dir, str(attachment.get("id") or ""))
        except UploadError:
            continue
        name = safe_name(str(attachment.get("name") or path.name))
        if kind == "image":
            images.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_data_url(
                            path, str(attachment.get("mime") or "image/png")
                        )
                    },
                }
            )
        else:
            try:
                extracted = extract_text(path)
            except UploadError as exc:
                extracted = "[Could not extract text: %s]" % exc
            if total_doc_chars >= MAX_DOCUMENT_TOTAL_CHARS:
                documents.append("[Additional document omitted: %s]" % name)
                continue
            remaining = MAX_DOCUMENT_TOTAL_CHARS - total_doc_chars
            extracted = extracted[:remaining]
            total_doc_chars += len(extracted)
            documents.append("[Attached document: %s]\n%s" % (name, extracted))
    if documents:
        block = "\n\n".join(documents)
        prompt = (prompt + "\n\n" + block).strip() if prompt else block
    if not images:
        return prompt or "Please analyze the attached file(s)."
    blocks: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": prompt or "Please analyze the attached image(s).",
        }
    ]
    blocks.extend(images)
    return blocks

