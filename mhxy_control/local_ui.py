"""Local-only OCR of selected phone UI regions; never sends screenshots to an API."""
import asyncio
import io
import re
import time
from PIL import Image
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.graphics.imaging import BitmapDecoder, BitmapPixelFormat, SoftwareBitmap
from winrt.windows.storage.streams import InMemoryRandomAccessStream, DataWriter


def compact(text):
    return re.sub(r"\s+", "", text)


class LocalUI:
    def __init__(self):
        self.engine = OcrEngine.try_create_from_user_profile_languages()
        if self.engine is None:
            raise RuntimeError("Windows OCR language is not available")

    async def read(self, image_bytes, box, neutral=False, scale=1):
        started = time.perf_counter()
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        crop = image.crop(tuple(box))
        if neutral:
            # Exclude coloured text carets from verification of neutral editor text.
            crop.putdata([(0, 0, 0) if max(p) - min(p) > 70 else p for p in crop.get_flattened_data()])
        if scale != 1:
            crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.BICUBIC)
        data = io.BytesIO()
        crop.save(data, format="PNG")
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(data.getvalue())
        await writer.store_async()
        writer.detach_stream()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        if bitmap.bitmap_pixel_format != BitmapPixelFormat.BGRA8:
            bitmap = SoftwareBitmap.convert(bitmap, BitmapPixelFormat.BGRA8)
        result = await self.engine.recognize_async(bitmap)
        lines = []
        for line in result.lines:
            words = []
            for word in line.words:
                rect = word.bounding_rect
                words.append({"text": word.text, "box": [round(rect.x / scale + box[0], 1), round(rect.y / scale + box[1], 1), round(rect.width / scale, 1), round(rect.height / scale, 1)]})
            lines.append({"text": line.text, "words": words})
        stream.close()
        return {"text": result.text, "lines": lines, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}

    async def matches(self, image_bytes, box, expected):
        result = await self.read(image_bytes, box)
        return compact(expected) in compact(result["text"])
