from dataclasses import dataclass
import asyncio
import time

from PIL import Image

@dataclass(frozen=True)
class Sample:
    image: Image.Image
    captured_at: float
    source_id: int | None = None


@dataclass(frozen=True)
class Frame:
    sequence: int
    sample: Sample


class LatestFrame:
    def __init__(self):
        self.frame = None
        self.sequence = 0
        self.changed = asyncio.Event()
        self.error = None
        self.last_source_id = None

    def publish(self, sample):
        if sample.source_id is not None and sample.source_id == self.last_source_id:
            return
        self.sequence += 1
        self.frame = Frame(self.sequence, sample)
        self.last_source_id = sample.source_id
        self.error = None
        self.changed.set()

    def invalidate(self, error):
        self.frame = None
        self.error = error
        self.changed.set()

    async def after(self, sequence, captured_after, max_age, deadline):
        while time.monotonic() < deadline:
            self.changed.clear()
            frame = self.frame
            now = time.monotonic()
            if (frame is not None and frame.sequence > sequence
                    and frame.sample.captured_at > captured_after
                    and 0 <= now - frame.sample.captured_at <= max_age):
                return frame
            try:
                await asyncio.wait_for(self.changed.wait(), max(0, deadline - now))
            except TimeoutError:
                break
        return None
