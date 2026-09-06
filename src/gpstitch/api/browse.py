"""Native file-picker endpoint for the FILES panel.

Opens the host's own dialog (Finder on macOS) and returns the single path the
user chose. Nothing is enumerated: there is no directory-listing endpoint here,
so the server never exposes the filesystem to callers.
"""

import logging
import sys
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, model_validator

from gpstitch.services.file_dialog import (
    FileDialogBusy,
    FileDialogUnavailable,
    build_command,
    choose_paths,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class BrowseRequest(BaseModel):
    """Which field is asking, and whether it accepts more than one path."""

    kind: Literal["video", "gps", "folder"]
    multiple: bool = False

    @model_validator(mode="after")
    def _folders_are_single(self) -> "BrowseRequest":
        # The platforms disagree on multi-folder selection; reject at the edge
        # so it surfaces as a 422 rather than a 500 from deeper down.
        if self.kind == "folder" and self.multiple:
            raise ValueError("Multiple selection is not supported for folders")
        return self


class BrowseResponse(BaseModel):
    """Chosen paths. Empty (and path null) when the user dismissed the dialog.

    `path` is the first result, kept so single-select callers stay simple.
    """

    path: str | None = None
    paths: list[str] = []


class BrowseAvailableResponse(BaseModel):
    """Whether this host has a native picker at all."""

    available: bool


@router.get("/browse/available", response_model=BrowseAvailableResponse)
async def browse_available() -> BrowseAvailableResponse:
    """Report whether a native picker exists, so the UI can hide the button."""
    try:
        build_command(sys.platform, "video")
    except FileDialogUnavailable:
        return BrowseAvailableResponse(available=False)
    return BrowseAvailableResponse(available=True)


@router.post("/browse", response_model=BrowseResponse)
async def browse(request: BrowseRequest) -> BrowseResponse:
    """Open the native file picker and return the chosen path(s)."""
    try:
        paths = await choose_paths(request.kind, multiple=request.multiple)
    except FileDialogUnavailable as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
    except FileDialogBusy as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 - surface any dialog failure as a 500
        logger.exception("File dialog failed")
        # The detail is shown to the user verbatim, so pass the underlying
        # reason through rather than prefixing text it may already contain.
        raise HTTPException(status_code=500, detail=str(e) or "File dialog failed") from e

    return BrowseResponse(path=paths[0] if paths else None, paths=paths)
