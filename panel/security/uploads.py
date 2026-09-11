"""Upload content validation (phase 25).

An upload must match what it claims to be. The extension decides the serving
policy (inline image or download), so the bytes have to agree with it: a file
named logo.png that actually contains HTML would otherwise be stored under the
panel origin and served back as a document.

Two layers:

* validate_image_upload() checks the extension whitelist, the magic bytes and
  the declared dimensions before a byte is written.
* serve_policy() is applied when anything under the upload directories is served
  back, so an unexpected file still reaches the browser as a sandboxed download
  instead of a same-origin document.
"""
import os

# Extensions the panel editor accepts. Raster formats only: SVG is XML and can
# carry script, so it is never treated as an inline image.
EDITOR_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})

# extension -> the magic kinds that may claim it.
EXTENSION_KINDS = {
    "png": {"png"},
    "jpg": {"jpeg"},
    "jpeg": {"jpeg"},
    "gif": {"gif"},
    "webp": {"webp"},
    "bmp": {"bmp"},
    "tif": {"tiff"},
    "tiff": {"tiff"},
    "heic": {"heic"},
    "heif": {"heic"},
}

# Extensions that are deliberately not inline images (served as attachments).
DOWNLOAD_ONLY_EXTENSIONS = frozenset({
    "svg", "html", "htm", "xhtml", "xml", "js", "mjs", "css", "json",
    "exe", "msi", "apk", "aab", "dmg", "pkg", "deb", "rpm", "appimage",
    "zip", "tar", "gz", "mp4", "webm", "mkv", "mov", "pdf",
})

SNIFF_BYTES = 32
# A 1x1 pixel PNG can declare huge dimensions; Image.open reads only the header,
# so the ceiling costs nothing and stops decompression bombs.
MAX_IMAGE_PIXELS = 50_000_000
UPLOAD_URL_PREFIXES = ("/static/uploads/", "/static/app-files/")


def extension_of(filename) -> str:
    return os.path.splitext(str(filename or ""))[1].lower().lstrip(".")


def sniff_kind(head) -> str:
    """Identify an image container from its first bytes ("" when unknown)."""
    head = bytes(head or b"")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in (
            b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1"):
        return "heic"
    return ""


def _stream_of(file_storage):
    return getattr(file_storage, "stream", file_storage)


def read_head(file_storage, size=SNIFF_BYTES) -> bytes:
    stream = _stream_of(file_storage)
    try:
        head = stream.read(size)
    except Exception:
        head = b""
    try:
        stream.seek(0)
    except Exception:
        pass
    return head or b""


def image_dimensions(file_storage):
    """(width, height) or None when Pillow is unavailable or the header is bad."""
    try:
        from PIL import Image
    except Exception:
        return None
    stream = _stream_of(file_storage)
    try:
        stream.seek(0)
        with Image.open(stream) as image:
            return tuple(image.size)
    except Exception:
        return None
    finally:
        try:
            stream.seek(0)
        except Exception:
            pass


def validate_extension_content(file_storage, ext):
    """Check the magic bytes for extensions whose container we know.

    Executables, archives and text formats have no single reliable signature, so
    they are accepted here and handled by the serving policy (attachment);
    image extensions must not carry something else.
    """
    expected = EXTENSION_KINDS.get(str(ext or "").lower().lstrip("."))
    if not expected:
        return True, None
    kind = sniff_kind(read_head(file_storage))
    if not kind:
        return False, "The file content is not a recognised image"
    if kind not in expected:
        return False, ("The file content (%s) does not match its extension (.%s)"
                       % (kind, expected and sorted(expected)[0]))
    return True, None


def validate_image_upload(file_storage, allowed_extensions=EDITOR_IMAGE_EXTENSIONS):
    """Return (ok, reason). Never raises for client-controlled input."""
    filename = str(getattr(file_storage, "filename", "") or "")
    if not filename:
        return False, "No filename"
    ext = extension_of(filename)
    if not ext:
        return False, "The file has no extension"
    if ext not in allowed_extensions:
        return False, "File type not allowed: .%s" % ext

    ok, reason = validate_extension_content(file_storage, ext)
    if not ok:
        return False, reason

    size = image_dimensions(file_storage)
    if size is None:
        # Pillow is unavailable or the header is unreadable: the magic bytes
        # already proved the container, so accept rather than block uploads in a
        # deployment without Pillow.
        return True, None
    width, height = size
    if width <= 0 or height <= 0:
        return False, "The image has invalid dimensions"
    if width * height > MAX_IMAGE_PIXELS:
        return False, "The image is too large (%dx%d)" % (width, height)
    return True, None


def is_upload_path(path) -> bool:
    text = str(path or "")
    return any(text.startswith(prefix) for prefix in UPLOAD_URL_PREFIXES)


def serve_policy(path) -> dict:
    """Response headers for a stored upload (defence in depth)."""
    ext = extension_of(path)
    inline = ext in EDITOR_IMAGE_EXTENSIONS
    return {
        "X-Content-Type-Options": "nosniff",
        # sandbox: no scripts, no same-origin access even if the file is a
        # document that slipped through validation.
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Content-Disposition": "inline" if inline else "attachment",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
