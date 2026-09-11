# Upload validation and serving

## Problem

The panel accepts three kinds of upload: editor images (announcements, FAQ),
the superadmin app-file manager, and receipt slips. Only the receipt path had
content validation (`allowed_receipt_file` checks the extension and, when
python-magic is usable, the MIME type). The other two were weaker:

* `POST /api/upload` (editor media) checked only the size and ran
  `secure_filename`. Any file type was accepted and stored under
  `/static/uploads/`, served from the panel origin. An uploaded `.html` or
  `.svg` opened directly in a browser is a stored-XSS primitive: the global CSP
  for HTML responses allowed same-origin scripts, and `image/svg+xml` documents
  execute embedded script when navigated to.
* `POST /api/app-files/upload` had an extension whitelist but no content check,
  so `icon.png` could contain anything. `.svg` is an allowed app-icon format,
  which is another document that can carry script.
* Nothing distinguished an image (safe to render inline) from anything else at
  serve time.

## Change

New module `panel/security/uploads.py`:

* `sniff_kind(head)` identifies PNG, JPEG, GIF, WebP, BMP, TIFF and HEIC from
  the first bytes.
* `validate_extension_content(file_storage, ext)` requires image bytes for
  extensions whose container is known and leaves executables, archives, videos
  and text formats to the serving policy.
* `validate_image_upload(file_storage)` adds the editor whitelist
  (`png jpg jpeg gif webp`), rejects a content/extension mismatch, and caps the
  declared dimensions at `MAX_IMAGE_PIXELS` (50 megapixels, read from the header
  so a decompression bomb is refused without decoding). The stream is rewound
  afterwards, and the file is still readable when Pillow is unavailable.
* `serve_policy(path)` returns the headers for a stored upload: `nosniff`,
  `Content-Security-Policy: default-src 'none'; sandbox`, 
  `Cross-Origin-Resource-Policy: same-origin`, and `Content-Disposition: inline`
  only for the raster image extensions, `attachment` for everything else.

Wiring:

* `POST /api/upload` validates before writing a byte and answers 415 with a
  reason. `POST /api/app-files/upload` validates image extensions the same way.
* `add_security_headers` (app.py) applies `serve_policy` to any response under
  `/static/uploads/` or `/static/app-files/`, overriding the document CSP. A file
  that somehow reaches the directory still cannot run as a same-origin document.

SVG is deliberately **not** an inline image: it is a document, so it downloads.

## Verification

`tests/test_upload_security.py` (24 tests): magic-byte identification and
extension parsing; path recognition; a real PNG accepted and HTML disguised as
PNG rejected; documents and binaries (`.html .svg .php .exe .js`) refused; a
missing extension refused; a content/extension mismatch refused; the stream
rewound after validation; a 60,000x60,000 image refused as a bomb; SVG served as
an attachment with a sandbox CSP while a PNG stays inline; the endpoint storing a
valid image and returning a URL inside `/static/uploads/`; a rejected upload
leaving the directory untouched; the size cap; a traversal filename staying
inside the upload directory; and both serving policies end to end for
`/static/uploads/` and `/static/app-files/`.

## Residual risk

* Magic bytes prove the container, not the content: a malformed PNG that passes
  the signature check is still served, but as an image, and the sandbox CSP keeps
  a disguised document inert.
* The dimension guard needs Pillow. Without it the signature check still runs and
  a bomb is only bounded by the upload size (10 MB for the editor, 500 MB for
  app files, which are superadmin-only and meant to carry installers/videos).
* `python-magic`/libmagic is not required: the built-in signatures cover the
  image formats the panel accepts, so receipt validation no longer depends on a
  library that is often installed without its native data files.
* App files intentionally include executables and archives. They download rather
  than render, so the panel origin cannot execute them.
