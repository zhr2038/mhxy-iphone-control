"""Share one timestamped image between a long-running decoder and task processes."""

import asyncio
import io
import struct
import time

from PIL import Image

from .runtime import Frame, Sample


HEADER = struct.Struct("!4sQd")
MAGIC = b"MHXY"


class FrameFeed:
    def __init__(self, path):
        self.path = path

    def publish(self, frame):
        sample = frame.sample
        pending = self.path.with_suffix(self.path.suffix + ".pending")
        with pending.open("wb") as output:
            output.write(HEADER.pack(MAGIC, frame.sequence, sample.captured_at))
            sample.image.save(output, format="JPEG", quality=92, subsampling=0)
        pending.replace(self.path)

    async def follow(self, latest, end, trace):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        sequence = 0
        previous_error = None
        while time.monotonic() < end:
            frame = await latest.after(sequence, 0, 0.5, min(end, time.monotonic() + 0.5))
            if frame is None:
                continue
            sequence = frame.sequence
            # The preview and publisher may encode the same frame concurrently.
            # Each encoder gets its own Pillow image object.
            sample = frame.sample
            copy = Frame(sequence, Sample(sample.image.copy(), sample.captured_at))
            try:
                await asyncio.to_thread(self.publish, copy)
            except OSError as exc:
                error = str(exc)
                if error != previous_error:
                    trace.event("frame_publish_error", error=error)
                previous_error = error
            else:
                if previous_error is not None:
                    trace.event("frame_publish_recovered", frame=sequence)
                previous_error = None
            await asyncio.sleep(0.08)

    def capture(self):
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return None
        if len(data) < HEADER.size:
            raise ValueError("Incomplete frame feed")
        magic, sequence, captured_at = HEADER.unpack_from(data)
        if magic != MAGIC:
            raise ValueError("Invalid frame feed header")
        if not 0 <= time.monotonic() - captured_at <= 0.75:
            return None
        with Image.open(io.BytesIO(data[HEADER.size:])) as image:
            return Sample(image.convert("RGB"), captured_at, sequence)
