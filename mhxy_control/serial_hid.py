"""USB command channel for the MHXY BLE HID bridge."""

import asyncio
import json
import os
import threading
import time

import serial
from serial.tools import list_ports


def port_name():
    if override := os.environ.get("MHXY_SERIAL_PORT"):
        return override
    matches = [p.device for p in list_ports.comports() if (p.vid, p.pid) == (0x303A, 0x1001)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise RuntimeError("Multiple Espressif serial devices found; set MHXY_SERIAL_PORT to the board's COM port")
    raise RuntimeError("Connect the XIAO ESP32-S3 Plus to this PC by USB")


class SerialHID:
    via = "usb-serial"

    def __init__(self):
        self.port = None
        self.status = {}
        self.status_at = None
        self.error = None
        self.counter = 0
        self.lock = threading.Lock()

    def _exchange(self, command, timeout):
        try:
            raw = command.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("Use printable ASCII or a Pinyin recipe for Chinese input") from exc
        if not raw or len(raw) > 280 or any(b < 32 or b > 126 for b in raw):
            raise ValueError("Use one printable ASCII HID command")
        with self.lock:
            try:
                if self.port is None:
                    try:
                        self.port = serial.Serial(port_name(), 115200, timeout=0.06, write_timeout=1)
                    except serial.SerialException as exc:
                        raise RuntimeError("Could not open the ESP32 USB serial port; close any serial monitor") from exc
                self.counter = self.counter % 999999 + 1
                prefix = f"MH2:{self.counter}\t".encode("ascii")
                self.port.write(prefix + raw + b"\n")
                end = time.monotonic() + timeout
                received = b""
                while time.monotonic() < end:
                    received += self.port.readline(512)
                    if received.endswith(b"\n"):
                        if received.startswith(prefix):
                            reply = received[len(prefix):].decode("ascii").rstrip("\r\n")
                            break
                        received = b""
                    elif len(received) > 512:
                        received = b""
                else:
                    raise TimeoutError("ESP32 USB serial did not answer")
            except (OSError, RuntimeError, ValueError) as exc:
                self.status_at = None
                self.error = exc
                if self.port is not None:
                    self.port.close()
                    self.port = None
                raise
        return reply

    async def refresh(self):
        reply = await asyncio.to_thread(self._exchange, "status", 1.5)
        try:
            status = json.loads(reply.removeprefix("ok "))
        except ValueError:
            status = {}
        if not isinstance(status, dict) or any(status.get(k) != v for k, v in {
            "fw": "mhxy-hid-2", "input": "ble-hid", "usb": "serial-only",
        }.items()):
            detail = reply.removeprefix("err ") if reply.startswith("err ") else "flash the MHXY Bluetooth HID firmware"
            await self.close()
            self.status_at = None
            self.error = RuntimeError("ESP32 input is disabled: " + detail)
            raise self.error
        self.status, self.status_at, self.error = status, time.monotonic(), None

    async def command(self, command, timeout=12):
        if self.status_at is None:
            raise RuntimeError("Verify the MHXY HID firmware before sending commands")
        reply = await asyncio.to_thread(self._exchange, command, timeout)
        if reply != "ok" and not reply.startswith("ok "):
            raise RuntimeError(reply.removeprefix("err "))
        return reply

    async def request(self, req):
        started = time.monotonic()
        replies = []
        for step in req["commands"]:
            command = step if isinstance(step, str) else step["cmd"]
            if time.monotonic() - (self.status_at or 0) > 2:
                await self.refresh()
            replies.append(await self.command(command))
            wait = 150 if isinstance(step, str) else min(max(step.get("after_ms", 150), 0), 2000)
            await asyncio.sleep(wait / 1000)
        elapsed = round((time.monotonic() - started) * 1000, 1)
        return {"replies": replies, "control_ms": elapsed, "elapsed_ms": elapsed}

    async def close(self):
        def close_port():
            with self.lock:
                if self.port is not None:
                    self.port.close()
                    self.port = None
        await asyncio.to_thread(close_port)
