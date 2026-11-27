"""Validate Web query images before durable Run admission."""

from __future__ import annotations

import base64
import binascii
from hashlib import sha256
from io import BytesIO
from typing import Literal

from fastapi import HTTPException
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from open_deep_research.configuration import Configuration

ImageMediaType = Literal["image/jpeg", "image/png", "image/webp"]
IMAGE_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
MAX_IMAGE_PIXELS = 25_000_000
MAX_REQUEST_BYTES = 64 * 1024


class QueryImage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=180)
    media_type: ImageMediaType
    data: str = Field(
        min_length=4, description="Base64 image bytes without a data URL prefix."
    )


class ImageInfo(BaseModel):
    position: int
    name: str
    media_type: ImageMediaType
    size_bytes: int


class ImageLimits(BaseModel):
    enabled: bool
    max_images: int
    max_bytes: int
    max_pixels: int
    media_types: list[ImageMediaType]


def image_limits(config: Configuration) -> ImageLimits:
    return ImageLimits(
        enabled=config.rag_query_image_enabled
        and config.rag_query_image_max_images > 0,
        max_images=config.rag_query_image_max_images,
        max_bytes=config.rag_query_image_max_bytes,
        max_pixels=MAX_IMAGE_PIXELS,
        media_types=list(IMAGE_FORMATS),
    )


def create_body_limit(config: Configuration) -> int:
    """Account for base64 expansion, filenames and JSON framing, only for Run creation."""
    limits = image_limits(config)
    if not limits.enabled:
        return MAX_REQUEST_BYTES
    return MAX_REQUEST_BYTES + limits.max_images * (
        encoded_size(limits.max_bytes) + 2048
    )


def encoded_size(size: int) -> int:
    return 4 * ((size + 2) // 3)


def validate_images(
    images: list[QueryImage], config: Configuration
) -> tuple[list[dict], list[tuple[int, str, bytes]]]:
    if not images:
        return [], []
    limits = image_limits(config)
    if not limits.enabled:
        raise HTTPException(400, "Query images are disabled.")
    if len(images) > limits.max_images:
        raise HTTPException(413, "Too many query images.")

    metadata, uploads = [], []
    for position, image in enumerate(images):
        if len(image.data) > encoded_size(limits.max_bytes):
            raise HTTPException(413, "Query image exceeds the byte limit.")
        try:
            content = base64.b64decode(image.data, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(400, "Invalid query image base64.") from None
        if len(content) > limits.max_bytes:
            raise HTTPException(413, "Query image exceeds the byte limit.")
        try:
            with Image.open(
                BytesIO(content), formats=list(IMAGE_FORMATS.values())
            ) as decoded:
                if decoded.format != IMAGE_FORMATS[image.media_type] or getattr(
                    decoded, "is_animated", False
                ):
                    raise ValueError("Image format or animation is unsupported")
                if decoded.width * decoded.height > MAX_IMAGE_PIXELS:
                    raise HTTPException(413, "Query image exceeds the pixel limit.")
                decoded.verify()
            # Verify headers/checksums as well as compressed pixel data.
            with Image.open(
                BytesIO(content), formats=[IMAGE_FORMATS[image.media_type]]
            ) as decoded:
                decoded.load()
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise HTTPException(413, "Query image exceeds the pixel limit.") from None
        except (OSError, ValueError, SyntaxError, EOFError):
            raise HTTPException(415, "Unsupported or damaged query image.") from None

        name = image.name.replace(chr(92), "/").rsplit("/", 1)[-1]
        name = "".join(
            character for character in name if character.isprintable()
        ).strip()
        info = ImageInfo(
            position=position,
            name=name or f"image-{position + 1}",
            media_type=image.media_type,
            size_bytes=len(content),
        )
        metadata.append(info.model_dump() | {"sha256": sha256(content).hexdigest()})
        uploads.append((position, image.media_type, content))
    return metadata, uploads
