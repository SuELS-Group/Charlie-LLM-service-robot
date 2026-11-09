from __future__ import annotations

import base64
import binascii
import contextlib
import io
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from web import state

logger = logging.getLogger(__name__)
router = APIRouter()
FACE_DB_PATH = state.CONFIG_DIR / "face_db.json"
FACE_IMAGE_DIR = state.STATIC_DIR / "face-images"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
REGENERATE_DETECT_MODEL = "hog"
MAX_REGENERATE_IMAGE_DIM = 1200
_FACE_MODULES = None
_FACE_MODULES_ERROR = None


class AddFaceRequest(BaseModel):
    name: str
    image_data: str
    filename: str | None = None


class UpdateFaceNotesRequest(BaseModel):
    name: str
    notes: str | None = None


def _empty_db() -> dict:
    return {"version": 1, "faces": {}}


def _load_db() -> dict:
    if not FACE_DB_PATH.exists():
        return _empty_db()
    try:
        with FACE_DB_PATH.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read face DB: {exc}") from exc
    if not isinstance(data, dict):
        return _empty_db()
    data.setdefault("version", 1)
    data.setdefault("faces", {})
    return data


def _save_db(data: dict) -> None:
    FACE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = FACE_DB_PATH.with_suffix(FACE_DB_PATH.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
    tmp_path.replace(FACE_DB_PATH)


def _safe_name(value: str) -> str:
    name = " ".join((value or "").strip().split())
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    return name


def _safe_file_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip(".-")
    return stem or "face"


def _list_image_files() -> list[Path]:
    if not FACE_IMAGE_DIR.exists():
        return []
    return sorted(
        path for path in FACE_IMAGE_DIR.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)


def _person_from_image(path: Path) -> str:
    return path.stem.split("__", 1)[0]


def _image_url(path: Path) -> str:
    return f"/static/face-images/{path.name}"


def _next_image_path(name: str, suffix: str) -> Path:
    FACE_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    return FACE_IMAGE_DIR / f"{_safe_file_stem(name)}{suffix}"


def _remove_existing_images(name: str) -> None:
    stem = _safe_file_stem(name).lower()
    for image_path in _list_image_files():
        if image_path.stem.lower() == stem or _person_from_image(image_path).lower() == stem:
            image_path.unlink(missing_ok=True)


def _decode_image_data(image_data: str) -> bytes:
    payload = image_data.split(",", 1)[1] if "," in image_data else image_data
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid image data") from exc


def _get_face_modules():
    global _FACE_MODULES, _FACE_MODULES_ERROR
    if _FACE_MODULES is not None:
        return _FACE_MODULES
    if _FACE_MODULES_ERROR is not None:
        raise HTTPException(status_code=500, detail=_FACE_MODULES_ERROR)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            import cv2
            import face_recognition
    except BaseException as exc:
        _FACE_MODULES_ERROR = (f"Face recognition is not available: {exc}. Install/sync setuptools in controller/.venv "
                               "so face_recognition_models can import pkg_resources.")
        raise HTTPException(status_code=500, detail=_FACE_MODULES_ERROR) from exc
    _FACE_MODULES = cv2, face_recognition
    return _FACE_MODULES


def _encode_face(image_bytes: bytes, detect_model: str) -> np.ndarray:
    cv2, face_recognition = _get_face_modules()
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    image_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image_bgr is None:
        raise HTTPException(status_code=400, detail="Could not decode image")
    height, width = image_bgr.shape[:2]
    scale = MAX_REGENERATE_IMAGE_DIM / max(height, width)
    if scale < 1.0:
        image_bgr = cv2.resize(image_bgr, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    face_locations = face_recognition.face_locations(image_rgb, model=detect_model)
    if not face_locations:
        raise HTTPException(status_code=400, detail="No face detected in image")
    largest_face = max(face_locations, key=lambda face: max(0, face[2] - face[0]) * max(0, face[1] - face[3]))
    encodings = face_recognition.face_encodings(image_rgb, [largest_face])
    if not encodings:
        raise HTTPException(status_code=400, detail="Could not compute face embedding")
    return encodings[0]


def _face_response(data: dict) -> dict:
    people = {}
    db_faces = data.get("faces", {}) if isinstance(data, dict) else {}
    notes_lookup = {}
    if isinstance(db_faces, dict):
        notes_lookup = {name: (entry or {}).get("notes", "") for name, entry in db_faces.items()}
    for image_path in _list_image_files():
        name = _person_from_image(image_path)
        notes = notes_lookup.get(_safe_name(name), "")
        people.setdefault(name, {"name": name, "images": [], "notes": notes})
        people[name]["images"].append({"filename": image_path.name, "url": _image_url(image_path)})
    return {
        "people": [people[name] for name in sorted(people, key=str.lower)],
        "people_count": len(people),
        "image_count": sum(len(person["images"]) for person in people.values()),
        "db_count": len(db_faces) if isinstance(db_faces, dict) else 0,
        "db_path": str(FACE_DB_PATH),
        "image_dir": str(FACE_IMAGE_DIR), }


@router.get("/faces", response_class=HTMLResponse)
async def serve_faces_page():
    page = state.STATIC_DIR / "faces.html"
    if page.exists():
        return FileResponse(page)
    return HTMLResponse("")


@router.get("/api/faces")
async def list_faces():
    return _face_response(_load_db())


@router.post("/api/faces")
async def add_face(request: AddFaceRequest):
    name = _safe_name(request.name)
    image_bytes = _decode_image_data(request.image_data)
    suffix = Path(request.filename or "").suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        suffix = ".jpeg"
    image_path = _next_image_path(name, suffix)
    _remove_existing_images(name)
    image_path.write_bytes(image_bytes)
    return {"status": "success", "face": name, "filename": image_path.name, **_face_response(_load_db())}


@router.post("/api/faces/notes")
async def update_face_notes(request: UpdateFaceNotesRequest):
    name = _safe_name(request.name)
    notes = (request.notes or "").strip()
    data = _load_db()
    faces = data.get("faces")
    if not isinstance(faces, dict):
        faces = {}
    entry = faces.get(name)
    if not isinstance(entry, dict):
        entry = {"embeddings": [], "updated_at": datetime.now().isoformat()}
        faces[name] = entry
    entry["notes"] = notes
    entry["updated_at"] = datetime.now().isoformat()
    data["faces"] = faces
    _save_db(data)
    return {"status": "success", "face": name, "notes": notes, **_face_response(data)}


@router.delete("/api/faces/images/{filename}")
async def remove_face_image(filename: str):
    image_path = FACE_IMAGE_DIR / Path(filename).name
    if image_path.suffix.lower() not in IMAGE_EXTENSIONS or not image_path.exists():
        raise HTTPException(status_code=404, detail=f"No face image found for {filename}")
    image_path.unlink()
    person_name = _person_from_image(image_path)
    remaining = any(_person_from_image(path).lower() == person_name.lower() for path in _list_image_files())
    data = _load_db()
    if not remaining:
        faces = data.get("faces")
        if isinstance(faces, dict):
            faces.pop(_safe_name(person_name), None)
            data["faces"] = faces
            _save_db(data)
    return {"status": "success", "removed": image_path.name, **_face_response(data)}


@router.delete("/api/faces/images")
async def remove_all_face_images():
    removed = 0
    for image_path in _list_image_files():
        try:
            image_path.unlink()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to remove {image_path.name}: {exc}") from exc
        removed += 1
    data = _load_db()
    data["faces"] = {}
    _save_db(data)
    return {"status": "success", "removed": removed, **_face_response(data)}


@router.post("/api/faces/regenerate")
async def regenerate_faces():
    existing_faces = _load_db().get("faces", {})
    faces = {}
    failed = []
    _get_face_modules()
    for image_path in _list_image_files():
        name = _safe_name(_person_from_image(image_path))
        try:
            encoding = _encode_face(image_path.read_bytes(), REGENERATE_DETECT_MODEL)
        except HTTPException as exc:
            failed.append({"file": image_path.name, "error": str(exc.detail)})
            continue
        existing = existing_faces.get(name, {}) if isinstance(existing_faces, dict) else {}
        entry = faces.setdefault(name, {
            "embeddings": [],
            "updated_at": datetime.now().isoformat(),
            "notes": (existing or {}).get("notes", f"Loaded from {FACE_IMAGE_DIR.name}"), })
        entry["embeddings"].append(encoding.tolist())
    data = {"version": 1, "faces": faces}
    _save_db(data)
    return {"status": "success", "loaded": len(faces), "failed": failed, **_face_response(data)}
