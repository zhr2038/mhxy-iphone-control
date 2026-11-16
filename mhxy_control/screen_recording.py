"""Record received phone frames at real wall-clock speed, including idle time."""
import asyncio
import contextlib
import datetime
import json
import math
from pathlib import Path
import shutil
import subprocess
import time


class ScreenRecording:
    fps = 5

    async def start(self, decoder, path):
        if not decoder.latest:
            raise RuntimeError("Wait for a live phone frame before recording")
        async def read_frame():
            return decoder.latest[1]

        return await self.start_frames(read_frame, path)

    async def start_frames(self, read_frame, path):
        self.read_frame = read_frame
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("FFmpeg is required for screen recording")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "image2pipe", "-c:v", "bmp", "-framerate", str(self.fps), "-i", "pipe:0",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(self.path),
            stdin=asyncio.subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.started = time.monotonic()
        self.started_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.frames = 0
        self.error = None
        self.task = asyncio.create_task(self.capture())
        return {"recording": True, "file": str(self.path), "started_utc": self.started_utc, "fps": self.fps, "speed": "real-time"}

    async def write_frame(self):
        self.proc.stdin.write(await self.read_frame())
        self.frames += 1
        await self.proc.stdin.drain()

    async def capture(self):
        try:
            while True:
                await self.write_frame()
                due = self.started + self.frames / self.fps
                await asyncio.sleep(max(0, due - time.monotonic()))
        except (OSError, ValueError) as exc:
            self.error = exc

    async def stop(self):
        stopped = time.monotonic()
        wall_seconds = stopped - self.started
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        # Preserve elapsed time even if frame delivery paused or the UI was static.
        try:
            while self.frames < math.ceil(wall_seconds * self.fps) and not self.error:
                await self.write_frame()
        finally:
            self.proc.stdin.close()
            error = await self.proc.stderr.read()
            await self.proc.wait()
        if self.proc.returncode or self.error:
            raise RuntimeError("Recording failed: " + (error.decode(errors="replace")[-200:] or str(self.error)))
        result = {"recording": False, "file": str(self.path), "started_utc": self.started_utc,
                  "stopped_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "wall_seconds": round(wall_seconds, 3), "video_seconds": self.frames / self.fps,
                  "frames": self.frames, "fps": self.fps, "speed": "real-time; no cuts or acceleration"}
        self.path.with_suffix(".timing.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
