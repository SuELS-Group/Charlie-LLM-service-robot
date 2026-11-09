from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from web import state

logger = logging.getLogger(__name__)
router = APIRouter()
PROJECT_ROOT = state.BASE_DIR.parent
VOICE_DB_PATH = state.CONFIG_DIR / "voice_db.json"
VOICE_SAMPLE_DIR = state.STATIC_DIR / "voice-samples"
VOICE_IDENTITY_SRC = PROJECT_ROOT / "workstation" / "ament_ws" / "src" / "voice_identity_node"
WORKSTATION_DIR = PROJECT_ROOT / "workstation"
AUDIO_EXTENSION_LIST = (".wav",
                        ".flac",
                        ".mp3",
                        ".m4a",
                        ".mp4",
                        ".aac",
                        ".ogg",
                        ".oga",
                        ".opus",
                        ".webm",
                        ".weba",
                        ".aif",
                        ".aiff",
                        ".au",
                        ".caf",
                        ".wma",)
AUDIO_EXTENSIONS = set(AUDIO_EXTENSION_LIST)
VOICE_DB_BUILD_TIMEOUT_SECONDS = 900
_build_lock = asyncio.Lock()


class AddVoiceSampleRequest(BaseModel):
    name: str
    audio_data: str
    filename: str | None = None


def _empty_db() -> dict[str, Any]:
    return {"version": 1, "speakers": {}}


def _load_db() -> dict[str, Any]:
    if not VOICE_DB_PATH.exists():
        return _empty_db()
    try:
        with VOICE_DB_PATH.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read voice DB: {exc}") from exc
    if not isinstance(data, dict):
        return _empty_db()
    data.setdefault("version", 1)
    data.setdefault("speakers", {})
    return data


def _safe_name(value: str) -> str:
    name = " ".join((value or "").strip().split())
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    return name


def _safe_file_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("._-")
    return stem or "speaker"


def _speaker_key(value: str) -> str:
    return _safe_file_stem(value).lower()


def _speaker_name_from_path(path: Path) -> str:
    stem = path.stem.strip()
    if "__" in stem:
        stem = stem.split("__", 1)[0]
    else:
        stem = re.sub(r"([_-])(sample|voice)?\d+$", "", stem, flags=re.IGNORECASE)
    name = " ".join(stem.replace("_", " ").replace("-", " ").split())
    return name or "unknown"


def _list_audio_files() -> list[Path]:
    if not VOICE_SAMPLE_DIR.exists():
        return []
    return sorted(
        path for path in VOICE_SAMPLE_DIR.iterdir() if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS)


def _sample_url(path: Path) -> str:
    return f"/static/voice-samples/{path.name}"


def _next_sample_path(name: str, suffix: str) -> Path:
    VOICE_SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    clean_suffix = suffix.lower() if suffix.lower() in AUDIO_EXTENSIONS else ".wav"
    stem = _safe_file_stem(name)
    first_path = VOICE_SAMPLE_DIR / f"{stem}{clean_suffix}"
    existing_numbers: set[int] = set()
    legacy_pattern = re.compile(rf"^{re.escape(stem)}__(\d+)$", flags=re.IGNORECASE)
    numbered_pattern = re.compile(rf"^{re.escape(stem)}[_-](\d+)$", flags=re.IGNORECASE)
    for path in _list_audio_files():
        if _speaker_key(_speaker_name_from_path(path)) != _speaker_key(name):
            continue
        if path.stem.lower() == stem.lower():
            existing_numbers.add(1)
            continue
        for pattern in (legacy_pattern, numbered_pattern):
            match = pattern.match(path.stem)
            if match:
                existing_numbers.add(int(match.group(1)))
                break
    if 1 not in existing_numbers and not first_path.exists():
        return first_path
    next_number = 2
    while next_number in existing_numbers or (VOICE_SAMPLE_DIR / f"{stem}_{next_number:02d}{clean_suffix}").exists():
        next_number += 1
    return VOICE_SAMPLE_DIR / f"{stem}_{next_number:02d}{clean_suffix}"


def _decode_audio_data(audio_data: str) -> bytes:
    payload = audio_data.split(",", 1)[1] if "," in audio_data else audio_data
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid audio data") from exc


def _voice_response(data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data if data is not None else _load_db()
    db_speakers = data.get("speakers", {}) if isinstance(data, dict) else {}
    users: dict[str, dict[str, Any]] = {}
    if isinstance(db_speakers, dict):
        for name, entry in sorted(db_speakers.items(), key=lambda item: item[0].lower()):
            embeddings = (entry or {}).get("embeddings", []) if isinstance(entry, dict) else []
            users[name] = {
                "name": name,
                "in_db": True,
                "db_sample_count": len(embeddings) if isinstance(embeddings, list) else 0,
                "updated_at": (entry or {}).get("updated_at", "") if isinstance(entry, dict) else "",
                "notes": (entry or {}).get("notes", "") if isinstance(entry, dict) else "",
                "files": [], }
    for sample_path in _list_audio_files():
        sample_name = _speaker_name_from_path(sample_path)
        key = _speaker_key(sample_name)
        user = None
        for candidate in users.values():
            if _speaker_key(candidate["name"]) == key:
                user = candidate
                break
        if user is None:
            user = {
                "name": sample_name,
                "in_db": False,
                "db_sample_count": 0,
                "updated_at": "",
                "notes": "",
                "files": [], }
            users[sample_name] = user
        try:
            stat = sample_path.stat()
            size_bytes = stat.st_size
            modified_at = datetime.fromtimestamp(stat.st_mtime).isoformat()
        except OSError:
            size_bytes = 0
            modified_at = ""
        user["files"].append({
            "filename": sample_path.name,
            "url": _sample_url(sample_path),
            "size_bytes": size_bytes,
            "modified_at": modified_at, })
    ordered_users = sorted(users.values(), key=lambda item: item["name"].lower())
    return {
        "users": ordered_users,
        "user_count": len(ordered_users),
        "db_count": len(db_speakers) if isinstance(db_speakers, dict) else 0,
        "file_count": sum(len(user["files"]) for user in ordered_users),
        "db_path": str(VOICE_DB_PATH),
        "sample_dir": str(VOICE_SAMPLE_DIR),
        "supported_extensions": list(AUDIO_EXTENSION_LIST), }


def _build_commands() -> list[tuple[list[str], Path]]:
    sample_dir = str(VOICE_SAMPLE_DIR)
    db_path = str(VOICE_DB_PATH)
    return [(["uv",
              "run",
              "python",
              "-m",
              "voice_identity_node.voice_db_builder",
              "--sample-dir",
              sample_dir,
              "--db-path",
              db_path,
              "--extensions",
              ",".join(AUDIO_EXTENSION_LIST), ], WORKSTATION_DIR),
            (["ros2",
              "run",
              "voice_identity_node",
              "voice_db_builder",
              "--sample-dir",
              sample_dir,
              "--db-path",
              db_path,
              "--extensions",
              ",".join(AUDIO_EXTENSION_LIST), ], PROJECT_ROOT), ]


async def _run_build_command() -> dict[str, Any]:
    VOICE_SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    VOICE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("PROJECT_PATH", str(PROJECT_ROOT))
    pythonpath_parts = [str(VOICE_IDENTITY_SRC)]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    errors = []
    for command, cwd in _build_commands():
        try:
            process = await asyncio.create_subprocess_exec(*command, cwd=str(cwd), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, )
        except FileNotFoundError as exc:
            errors.append(f"{command[0]} not found: {exc}")
            continue
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=VOICE_DB_BUILD_TIMEOUT_SECONDS, )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise HTTPException(status_code=504, detail="Voice DB build timed out") from exc
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode == 0:
            parsed = None
            with contextlib.suppress(Exception):
                parsed = json.loads(stdout)
            return {
                "status": "success",
                "command": " ".join(command),
                "stdout": stdout,
                "stderr": stderr,
                "builder_result": parsed, }
        errors.append(f"{' '.join(command)} failed with exit code {process.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
    raise HTTPException(status_code=500, detail="Voice DB build failed:\n" + "\n\n".join(errors))


@router.get("/voices", response_class=HTMLResponse)
async def serve_voices_page():
    page = state.STATIC_DIR / "voices.html"
    if page.exists():
        return FileResponse(page)
    return HTMLResponse("")


@router.get("/api/voices")
async def list_voices():
    return _voice_response(_load_db())


@router.post("/api/voices")
async def add_voice_sample(request: AddVoiceSampleRequest):
    name = _safe_name(request.name)
    suffix = Path(request.filename or "").suffix.lower()
    if suffix not in AUDIO_EXTENSIONS:
        suffix = ".wav"
    audio_path = _next_sample_path(name, suffix)
    audio_path.write_bytes(_decode_audio_data(request.audio_data))
    return {"status": "success", "speaker": name, "filename": audio_path.name, **_voice_response(_load_db())}


@router.delete("/api/voices/samples/{filename}")
async def remove_voice_sample(filename: str):
    sample_path = VOICE_SAMPLE_DIR / Path(filename).name
    if sample_path.suffix.lower() not in AUDIO_EXTENSIONS or not sample_path.exists():
        raise HTTPException(status_code=404, detail=f"No voice sample found for {filename}")
    sample_path.unlink()
    return {"status": "success", "removed": sample_path.name, **_voice_response(_load_db())}


@router.delete("/api/voices/users/{name}/samples")
async def remove_voice_samples_for_user(name: str):
    clean_name = _safe_name(name)
    target = _speaker_key(clean_name)
    removed = []
    for sample_path in _list_audio_files():
        if _speaker_key(_speaker_name_from_path(sample_path)) == target:
            sample_path.unlink()
            removed.append(sample_path.name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"No voice samples found for {clean_name}")
    return {"status": "success", "removed": removed, **_voice_response(_load_db())}


@router.delete("/api/voices/samples")
async def remove_all_voice_samples():
    removed = 0
    for sample_path in _list_audio_files():
        sample_path.unlink()
        removed += 1
    return {"status": "success", "removed": removed, **_voice_response(_load_db())}


@router.post("/api/voices/regenerate")
async def regenerate_voices():
    if _build_lock.locked():
        raise HTTPException(status_code=409, detail="Voice DB build is already running")
    async with _build_lock:
        result = await _run_build_command()
    return {**result, **_voice_response(_load_db())}
