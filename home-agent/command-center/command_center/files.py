"""Attachment import and disposable-workspace operations."""

import shutil
from pathlib import PurePosixPath

from .config import ATTACHMENTS_PATH, MAX_FILE_BYTES, MAX_TEXT_BYTES, WORKSPACE_PATH


def scoped_path(root, relative, *, require_exists=True):
    if not isinstance(relative, str) or not relative or len(relative) > 512:
        raise ValueError("file path is required")
    parsed = PurePosixPath(relative)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError("file path must stay inside its designated workspace")
    candidate = (root / parsed).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("file path escapes its designated workspace")
    if require_exists and not candidate.exists():
        raise ValueError("file not found")
    return candidate


def file_summary(root):
    if not root.exists():
        return []
    files = []
    for candidate in sorted(root.rglob("*")):
        if not candidate.is_file():
            continue
        try:
            resolved = candidate.resolve()
            if root not in resolved.parents or resolved.stat().st_size > MAX_FILE_BYTES:
                continue
            stat = resolved.stat()
            files.append({"name": str(resolved.relative_to(root)), "size_bytes": stat.st_size,
                          "modified": int(stat.st_mtime)})
        except OSError:
            continue
        if len(files) >= 100:
            break
    return files


def attachment_files():
    return file_summary(ATTACHMENTS_PATH)


def workspace_files():
    return file_summary(WORKSPACE_PATH)


def save_attachment_to_workspace(attachment_name, destination=None):
    source = scoped_path(ATTACHMENTS_PATH, attachment_name)
    if not source.is_file() or source.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("attachment is not an allowed file")
    target = scoped_path(WORKSPACE_PATH, destination or source.name, require_exists=False)
    if target.exists():
        raise ValueError("workspace destination already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return {"name": str(target.relative_to(WORKSPACE_PATH)), "size_bytes": target.stat().st_size}


def read_workspace_text(name):
    path = scoped_path(WORKSPACE_PATH, name)
    if not path.is_file() or path.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError("text file is missing or too large")
    if path.suffix.lower() == ".pdf":
        raise ValueError("use the PDF reader for PDF files")
    return {"name": str(path.relative_to(WORKSPACE_PATH)),
            "content": path.read_bytes()[:MAX_TEXT_BYTES].decode("utf-8", "replace")}


def write_workspace_text(name, content):
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("text content is required and limited to 512 KiB")
    path = scoped_path(WORKSPACE_PATH, name, require_exists=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"name": str(path.relative_to(WORKSPACE_PATH)), "size_bytes": path.stat().st_size}


def delete_workspace_file(name):
    path = scoped_path(WORKSPACE_PATH, name)
    if not path.is_file():
        raise ValueError("only files in the workspace can be deleted")
    path.unlink()
    return {"deleted": str(path.relative_to(WORKSPACE_PATH))}


def read_pdf(source, name, max_pages=20):
    roots = {"attachment": ATTACHMENTS_PATH, "workspace": WORKSPACE_PATH}
    if source not in roots:
        raise ValueError("PDF source must be attachment or workspace")
    path = scoped_path(roots[source], name)
    if not path.is_file() or path.suffix.lower() != ".pdf" or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("PDF is missing or too large")
    try:
        max_pages = max(1, min(int(max_pages), 30))
        from pypdf import PdfReader
        reader = PdfReader(str(path), strict=False)
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages[:max_pages])
    except Exception as exc:
        raise ValueError(f"could not read PDF: {exc}") from exc
    return {"name": str(path.relative_to(roots[source])), "source": source,
            "pages_read": min(len(reader.pages), max_pages), "total_pages": len(reader.pages),
            "text": text[:100_000], "truncated": len(text) > 100_000 or len(reader.pages) > max_pages}


def register_routes(router):
    router.get("/v1/files/attachments", lambda _p, _q: attachment_files())
    router.get("/v1/files/workspace", lambda _p, _q: workspace_files())
    router.post("/v1/files/import-attachment",
                lambda p, _q: save_attachment_to_workspace(p["attachment_name"], p.get("destination")), 201)
    router.post("/v1/files/read-text", lambda p, _q: read_workspace_text(p["name"]))
    router.post("/v1/files/read-pdf",
                lambda p, _q: read_pdf(p["source"], p["name"], p.get("max_pages", 20)))
    router.post("/v1/files/write-text", lambda p, _q: write_workspace_text(p["name"], p["content"]), 201)
    router.post("/v1/files/delete", lambda p, _q: delete_workspace_file(p["name"]))
