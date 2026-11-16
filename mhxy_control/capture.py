"""Decode local H.264 RTP into the timestamped frame file consumed by phone.py."""

import argparse
import asyncio
import contextlib

from .airplay import AirPlay
from .feed import FrameFeed
from .runtime import LatestFrame
from .settings import FRAME_FILE


class ConsoleTrace:
    def event(self, name, **fields):
        print(name, " ".join(f"{key}={value}" for key, value in fields.items()), flush=True)


async def capture(port):
    latest, trace, stopped = LatestFrame(), ConsoleTrace(), asyncio.Event()
    receiver = AirPlay(port)
    publisher = asyncio.create_task(FrameFeed(FRAME_FILE).follow(latest, float("inf"), trace))
    try:
        await receiver.read(latest, trace, stopped)
    finally:
        stopped.set()
        publisher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await publisher


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rtp-port", type=int, default=5508)
    args = parser.parse_args()
    if not 1024 <= args.rtp_port <= 65535:
        parser.error("--rtp-port must be between 1024 and 65535")
    try:
        asyncio.run(capture(args.rtp_port))
    except KeyboardInterrupt:
        pass
