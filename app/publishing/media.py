"""Decode, bound and re-encode uploaded images; never accept URLs or SVG."""
from __future__ import annotations
import hashlib
import io
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError
from .domain import PublishingError

MAX_UPLOAD = 10 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 25_000_000


def prepare_image(content: bytes) -> dict:
    if not content or len(content) > MAX_UPLOAD:
        raise PublishingError('Upload an image smaller than 10 MB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as original:
                if original.format not in {'JPEG', 'PNG', 'WEBP'} or getattr(original, 'n_frames', 1) != 1:
                    raise PublishingError('Use a still JPEG, PNG or WebP image.')
                original.load()
                image = ImageOps.exif_transpose(original)
                if image.width < 20 or image.height < 20 or max(image.size) / min(image.size) > 20:
                    raise PublishingError('Use an image at least 20 pixels wide and tall, with an aspect ratio below 20:1.')
                image.thumbnail((2560, 2560))
                # Telegram photos are JPEG. Preserve appearance of transparent PNGs on white.
                if image.mode in {'RGBA', 'LA'} or 'transparency' in image.info:
                    rgba = image.convert('RGBA')
                    rgb = Image.new('RGB', rgba.size, 'white')
                    rgb.paste(rgba, mask=rgba.getchannel('A'))
                    image = rgb
                else:
                    image = image.convert('RGB')
                output = io.BytesIO()
                image.save(output, format='JPEG', quality=92, optimize=True)
                data = output.getvalue()
                return {'content': data, 'content_type': 'image/jpeg', 'width': image.width, 'height': image.height, 'sha256': hashlib.sha256(data).hexdigest()}
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise PublishingError('This image could not be decoded safely. Use a JPEG, PNG or WebP image.') from None
