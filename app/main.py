"""HTTP API for the BWF clipping service."""

from __future__ import annotations

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from .bwf import MAX_UPLOAD_BYTES, BWFError, clip_bwf

app = FastAPI(title="BWF Clip Service", version="1.0.0")

# Headroom for multipart framing on top of the 16 MiB file budget, used only
# for the early Content-Length rejection; the uploaded file itself is always
# measured exactly.
_REQUEST_HEADROOM = 1 * 1024 * 1024


def _error(status, code, message):
    return JSONResponse(status_code=status,
                        content={"error": {"code": code, "message": message}})


@app.exception_handler(BWFError)
async def bwf_error_handler(_request: Request, exc: BWFError):
    return JSONResponse(status_code=exc.status, content=exc.payload())


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/bwf/clip")
async def clip(
    request: Request,
    file: UploadFile = File(...),
    startFrame: int = Form(..., ge=0),
    frameCount: int = Form(..., gt=0),
):
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() \
            and int(declared) > MAX_UPLOAD_BYTES + _REQUEST_HEADROOM:
        return _error(413, "payload_too_large",
                      "request body exceeds the 16 MiB upload budget")

    source = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(source) > MAX_UPLOAD_BYTES:
        return _error(413, "file_too_large",
                      f"WAV file is larger than the {MAX_UPLOAD_BYTES}-byte "
                      "(16 MiB) limit")

    # clip_bwf validates everything before assembling output, so a failure
    # here can never emit partial audio.
    result = clip_bwf(source, startFrame, frameCount)
    return Response(
        content=result.data,
        media_type="audio/wav",
        headers={
            "X-Time-Reference": str(result.time_reference),
            "X-Frame-Count": str(result.frame_count),
            "X-Audio-SHA256": result.audio_sha256,
            "Content-Disposition": 'attachment; filename="clip.wav"',
        },
    )
