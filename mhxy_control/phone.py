"""Shared local ESP32 iPhone driver entry point. No OpenAI API or native Codex UI dependency."""
import argparse
import asyncio
import contextlib
import datetime
import hmac
import importlib
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import uuid

from PIL import Image, ImageOps
from .local_auth import SERVICE_DIR, service_token as token
from .screen_recording import ScreenRecording
from .feed import FrameFeed
from .serial_hid import SerialHID
from .settings import FRAME_FILE, WORK

PYTHON = sys.executable
HOST, PORT = "127.0.0.1", int(os.environ.get("MHXY_CONTROL_PORT", "18766"))


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Service:
    def __init__(self):
        self.auth = token()
        self.board = None
        self.lock = asyncio.Lock()
        self.current = None
        self.feed = FrameFeed(FRAME_FILE)
        self.recording = None
        self.stop = asyncio.Event()
        self.history = []

    async def connect(self):
        if self.board is None:
            self.board = SerialHID()
        await self.board.refresh()
    def health(self):
        b = self.board
        sample = None
        with contextlib.suppress(OSError, ValueError):
            sample = self.feed.capture()
        return {"service": "esp32-iphone-local", "pid": os.getpid(),
                "connected": b is not None and bool(b.status) and b.error is None,
                "via": b.via if b else None,
                "phone": {key: b.status[key] for key in ("fw", "build", "input", "usb", "paired", "pairing", "hid", "keyboard", "mouse", "motion_ms") if key in b.status} if b else {},
                "mirror": "mhxy", "live_cache": sample is not None,
                "video_debug": {"frame_age_ms": round(1000 * (time.monotonic() - sample.captured_at))} if sample else None,
                "status_age_ms": round((time.monotonic() - b.status_at) * 1000) if b and b.status_at else None,
                "lease": self.current["session"] if self.current else None,
                "recording": self.recording is not None}

    def check_owner(self, session):
        if not session or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", session):
            raise RuntimeError("Use begin and pass its session id")
        if not self.current or self.current["session"] != session:
            raise RuntimeError("Another chat owns control, or this session has finished. Inspect status; do not take over silently.")
        if time.monotonic() > self.current["deadline"]:
            raise TimeoutError("Task deadline exceeded")

    async def activate_hid(self):
        await self.connect()
        if self.board.status.get("hid") == "paused":
            await self.board.command("hid resume", timeout=1.5)
        if self.board.status.get("paired"):
            deadline = time.monotonic() + 8
            while self.board.status.get("hid") != "ready" and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                await self.board.refresh()
        self.check_input()

    async def pause_idle_hid(self):
        if self.board is None or "build" not in self.board.status:
            return
        await self.board.command("hid pause", timeout=1.5)
        await self.board.refresh()

    async def ensure(self):
        await self.activate_hid()
        return await self.mhxy_frame()

    def check_input(self):
        if not self.board.status_at or time.monotonic() - self.board.status_at > 3:
            raise RuntimeError("Refresh the ESP32 USB serial status")
        if self.board.status.get("hid") != "ready":
            raise RuntimeError("Pair or reconnect MHXY iPhone HID on the iPhone with AssistiveTouch enabled")

    async def mhxy_frame(self, after=0):
        sample = await asyncio.to_thread(self.feed.capture)
        if sample is not None and sample.captured_at > after:
            return sample
        # AirPlay can stop emitting frames while the screen is static. Moving
        # the pointer without clicking prompts a new frame on an active mirror.
        await self.board.refresh()
        self.check_input()
        await self.board.command("move 0 1", timeout=1.5)
        deadline = time.monotonic() + 1.6
        while time.monotonic() < deadline:
            sample = await asyncio.to_thread(self.feed.capture)
            if sample is not None and sample.captured_at > after:
                return sample
            await asyncio.sleep(0.06)
        raise RuntimeError("No fresh MHXY-PC frame. Unlock the iPhone and check its Screen Mirroring selection.")

    def save_mhxy(self, sample, path):
        sample.image.save(path)
        return {"file": str(path), "source": "mhxy-pc", "size": list(sample.image.size),
                "captured_at": sample.captured_at,
                "frame_age_ms": round(1000 * (time.monotonic() - sample.captured_at))}

    async def start_recording(self, sample, path):
        size = sample.image.size
        latest = None
        encoded = None

        async def read_frame():
            nonlocal sample, latest, encoded
            current = await asyncio.to_thread(self.feed.capture)
            if current is not None:
                sample = current
            if sample.captured_at != latest:
                image = sample.image
                if image.size != size:
                    image = ImageOps.pad(image, size, method=Image.Resampling.LANCZOS)
                pixels = io.BytesIO()
                image.save(pixels, format="BMP")
                encoded = pixels.getvalue()
                latest = sample.captured_at
            return encoded

        recording = ScreenRecording()
        result = await recording.start_frames(read_frame, path)
        self.recording = recording
        return dict(result, source="mhxy-pc", size=list(size))

    async def recipe(self, req):
        from . import app_recipes

        recipes_module = importlib.reload(app_recipes)

        async def read_frame(after):
            sample = await self.mhxy_frame(after=after)
            if sample.image.size not in {(332, 720), recipes_module.SIZE}:
                raise RuntimeError("App recipes require the calibrated MHXY-PC portrait layout (332×720)")
            pixels = io.BytesIO()
            sample.image.resize(recipes_module.SIZE, Image.Resampling.LANCZOS).save(pixels, format="PNG")
            return sample.captured_at, pixels.getvalue()

        async def send_command(command):
            await self.board.refresh()
            self.check_input()
            await self.board.command(command, timeout=4)
            self.current["last_input_at"] = time.monotonic()

        await read_frame(0)
        recipes = recipes_module.Recipes(read_frame, send_command)
        payload = req.get("payload", {})
        if req["name"] == "wechat-filehelper":
            result = await recipes.wechat_filehelper(payload["segments"], send=bool(payload.get("send", False)))
        elif req["name"] == "notes-create":
            result = await recipes.notes_create(payload["title"], payload.get("paragraphs", []), payload.get("rows", []))
        else:
            raise ValueError("Unknown recipe")
        sample = await self.mhxy_frame(after=self.current["last_input_at"])
        result["screenshot"] = self.save_mhxy(sample, req.get("out") or WORK / "recipe-result.png")
        return result

    async def mhxy_request(self, op, req):
        if op == "screenshot":
            sample = await self.mhxy_frame(after=self.current["last_input_at"])
            return self.save_mhxy(sample, req.get("out") or WORK / "latest.png")
        if op == "ocr":
            from .local_ui import LocalUI

            pixels = io.BytesIO()
            (await self.mhxy_frame(after=self.current["last_input_at"])).image.save(pixels, format="PNG")
            return await LocalUI().read(pixels.getvalue(), req["box"], neutral=req.get("neutral", False))
        if op == "recipe":
            return await self.recipe(req)
        if op != "run":
            raise ValueError("Unknown operation")
        request = req["request"]
        commands = request.get("commands")
        if set(request) - {"commands", "screenshot"} or not isinstance(commands, list) or not 1 <= len(commands) <= 32:
            raise ValueError("Provide a batch of 1 to 32 commands and an optional screenshot path")
        verbs = {"key", "type", "home", "move", "press", "release", "release-all", "click", "doubleclick",
                 "homescreen", "media", "scroll", "ping"}
        for step in commands:
            command = step.get("cmd") if isinstance(step, dict) else step
            if not isinstance(command, str) or len(command.splitlines()) != 1 or command.partition(" ")[0] not in verbs:
                raise ValueError("MHXY-PC batches support only keyboard, pointer, and ping commands")
        await self.mhxy_frame(after=self.current["last_input_at"])
        self.check_input()
        started = time.monotonic()
        self.current["last_input_at"] = started
        result = await self.board.request({"commands": commands})
        self.current["last_input_at"] = time.monotonic()
        if request.get("screenshot"):
            await asyncio.sleep(0.25)
            try:
                sample = await self.mhxy_frame(after=self.current["last_input_at"])
                result["screenshot"] = self.save_mhxy(sample, request["screenshot"])
            except RuntimeError as exc:
                result["screenshot_error"] = str(exc)
        return result

    async def external_tap(self, request):
        self.check_input()
        dx, dy = request["pointer"]
        if any(type(value) is not int or not 0 <= value <= 420 for value in (dx, dy)):
            raise ValueError("Invalid calibrated landscape pointer position")
        frame_at = float(request["frame_at"])
        if frame_at <= self.current["last_external_frame"] or not 0 <= time.monotonic() - frame_at <= 0.75:
            return {"sent": False}
        await self.board.command("home", timeout=1.5)
        await asyncio.sleep(0.15)
        await self.board.command(f"move {dx} {dy}", timeout=3)
        await asyncio.sleep(0.15)
        self.check_input()
        if not 0 <= time.monotonic() - frame_at <= 1.10:
            return {"sent": False}
        self.current["last_external_frame"] = frame_at
        await self.board.command("click", timeout=1.5)
        return {"sent": True}

    async def external_pointer(self, op, request):
        self.check_input()
        frame_at = float(request["frame_at"])
        if not 0 <= time.monotonic() - frame_at <= 0.75:
            return {"sent": False}
        positioned_at = self.current.get("pointer_at", 0.0)
        if op == "click_positioned":
            if not positioned_at or frame_at <= max(positioned_at, self.current["last_external_frame"]):
                return {"sent": False}
            self.current["last_external_frame"] = frame_at
            self.current["pointer_at"] = 0.0
            await self.board.command("click", timeout=1.5)
            return {"sent": True}
        dx, dy = request["pointer"]
        low, high = (0, 420) if op == "position_external" else (-80, 80)
        if any(type(value) is not int or not low <= value <= high for value in (dx, dy)):
            raise ValueError("Invalid calibrated pointer movement")
        if op == "position_external":
            await self.board.command("home", timeout=1.5)
            await asyncio.sleep(0.12)
        elif not positioned_at or frame_at <= positioned_at:
            return {"sent": False}
        await self.board.command(f"move {dx} {dy}", timeout=3)
        self.current["pointer_at"] = time.monotonic()
        return {"sent": True}

    async def external_drag(self, request):
        self.check_input()
        dx, dy = request["pointer"]
        moves = request["moves"]
        if (any(type(v) is not int or not 0 <= v <= 420 for v in (dx, dy))
                or not 1 <= len(moves) <= 40
                or any(len(move) != 2 or any(type(v) is not int or abs(v) > 64 for v in move)
                       for move in moves)):
            raise ValueError("Invalid calibrated drag")
        x, y = dx, dy
        for mx, my in moves:
            x += mx
            y += my
            if not (0 <= x <= 420 and 0 <= y <= 420):
                raise ValueError("Drag leaves calibrated pointer range")
        frame_at = float(request["frame_at"])
        if frame_at <= self.current["last_external_frame"] or not 0 <= time.monotonic() - frame_at <= 0.75:
            return {"sent": False}
        await self.board.command("home", timeout=1.5)
        await asyncio.sleep(0.12)
        await self.board.command(f"move {dx} {dy}", timeout=3)
        await asyncio.sleep(0.12)
        self.check_input()
        if not 0 <= time.monotonic() - frame_at <= 1.10:
            return {"sent": False}
        self.current["last_external_frame"] = frame_at
        try:
            await self.board.command("press", timeout=1.5)
            await asyncio.sleep(0.12)
            for mx, my in moves:
                await self.board.command(f"move {mx} {my}", timeout=1.5)
                await asyncio.sleep(0.05)
        finally:
            await self.board.command("release", timeout=1.5)
        return {"sent": True}

    async def finish(self, outcome="finished"):
        task = self.current
        if not task:
            return {"released": True}
        if self.board and self.board.status.get("hid") == "ready":
            with contextlib.suppress(Exception):
                await self.board.command("release-all", timeout=1)
        wall = time.monotonic() - task["started"]
        result = {"session": task["session"], "outcome": outcome, "started_utc": task["started_utc"],
                  "finished_utc": timestamp(), "task_wall_seconds": round(wall, 3),
                  "backend_seconds": round(task["backend_ms"] / 1000, 3),
                  "coordination_gap_seconds": round(max(0, wall - task["backend_ms"] / 1000), 3),
                  "gap_definition": "Model reading/reasoning, client/tool round trips and waiting combined; not pure model inference.",
                  "events": task["events"]}
        if self.recording:
            try:
                result["recording"] = dict(await self.recording.stop(), source="mhxy-pc")
            except Exception as exc:
                result["recording_error"] = str(exc)
            finally:
                self.recording = None
        try:
            await self.pause_idle_hid()
        except (OSError, RuntimeError, ValueError) as exc:
            result["hid_pause_error"] = str(exc)
        path = WORK / f"{task['session']}.timing.json"
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        result["timing_file"] = str(path)
        self.history.append(result)
        self.current = None
        return result

    async def dispatch(self, req):
        op, session = req.get("op", "status"), req.get("session")
        if op == "status":
            return self.health()
        if self.lock.locked():
            raise RuntimeError("Phone is executing another request; do not interleave inputs")
        async with self.lock:
            if op == "probe":
                await self.connect()
                return self.health()
            if op == "pair-hid":
                if self.current:
                    raise RuntimeError("Finish the active phone session before opening pairing")
                await self.connect()
                await self.board.command("pair", timeout=1)
                await self.board.refresh()
                return {"device": "MHXY iPhone HID", "pairing_seconds": 180, "state": self.health()}
            if op in {"pause-hid", "resume-hid"}:
                if self.current:
                    raise RuntimeError("Finish the active phone session before changing HID mode")
                await self.connect()
                if "build" not in self.board.status:
                    raise RuntimeError("Install firmware build 2.1 for manual keyboard mode")
                if op == "pause-hid":
                    await self.pause_idle_hid()
                else:
                    await self.activate_hid()
                return self.health()
            if op == "ensure":
                if self.current and self.current["session"] != session:
                    raise RuntimeError("Another chat owns the phone")
                try:
                    frame = self.save_mhxy(await self.ensure(), WORK / "service-ready.png")
                    return {"state": self.health(), "screenshot": frame}
                finally:
                    if self.current is None:
                        await self.pause_idle_hid()
            if op == "begin":
                if self.current:
                    raise RuntimeError("A phone session is already active; finish it before starting another")
                session = session or uuid.uuid4().hex[:16]
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", session):
                    raise ValueError("Invalid session id")
                limit = min(max(float(req.get("limit_seconds", 300)), 1), 3600)
                external = req.get("external_video") is True
                if req.get("mirror", "mhxy") != "mhxy":
                    raise ValueError("Select MHXY-PC for iPhone screen mirroring")
                if external and req.get("record"):
                    raise ValueError("Use a regular MHXY-PC session for recording")
                started, started_utc = time.monotonic(), timestamp()
                try:
                    async with asyncio.timeout(limit):
                        if external:
                            await self.activate_hid()
                            frame = None
                        else:
                            sample = await self.ensure()
                            frame = self.save_mhxy(sample, req.get("out") or WORK / "mhxy-ready.png")
                except Exception as exc:
                    with contextlib.suppress(OSError, RuntimeError, ValueError):
                        await self.pause_idle_hid()
                    failure = {"session": session, "started_utc": started_utc, "setup_seconds": round(time.monotonic() - started, 3), "error": str(exc) or type(exc).__name__, "state": self.health()}
                    (WORK / f"{session}.setup-failed.json").write_text(json.dumps(failure, indent=2) + "\n")
                    raise
                setup_ms = (time.monotonic() - started) * 1000
                self.current = {"session": session, "started": started, "started_utc": started_utc,
                                "deadline": started + limit, "backend_ms": setup_ms,
                                "external_video": external, "last_external_frame": 0.0, "last_input_at": 0.0,
                                "events": [{"op": "setup", "started_utc": started_utc, "backend_ms": round(setup_ms, 1)}]}
                result = {"session": session, "limit_seconds": limit, "setup_ms": round(setup_ms, 1),
                          "screenshot": frame, "external_video": external, "mirror": "mhxy"}
                if req.get("record"):
                    try:
                        result["recording"] = await self.start_recording(sample, req["record"])
                    except Exception:
                        await self.finish("record-start-failed")
                        raise
                return result
            if op in {"finish", "release"}:
                if self.current and self.current["session"] != session:
                    raise RuntimeError("Only the owning chat can finish this phone session")
                if not self.current:
                    for past in reversed(self.history):
                        if past["session"] == session:
                            return dict(past, already_finished=True)
                return await self.finish()
            if op == "stop":
                if self.current:
                    raise RuntimeError("Finish the active phone session first")
                with contextlib.suppress(OSError, RuntimeError, ValueError):
                    await self.pause_idle_hid()
                self.stop.set()
                return {"stopping": True}
            self.check_owner(session)
            await self.connect()
            started, started_utc = time.perf_counter(), timestamp()
            remaining = max(0.01, self.current["deadline"] - time.monotonic())
            try:
                async with asyncio.timeout(remaining):
                    if not self.current["external_video"]:
                        result = await self.mhxy_request(op, req)
                    else:
                        if op == "tap_external":
                            result = await self.external_tap(req)
                        elif op == "drag_external":
                            result = await self.external_drag(req)
                        elif op in {"position_external", "nudge_external", "click_positioned"}:
                            result = await self.external_pointer(op, req)
                        else:
                            raise RuntimeError("This session is only for game pointer control with external capture")
                return result
            finally:
                elapsed = (time.perf_counter() - started) * 1000
                if self.current:
                    self.current["backend_ms"] += elapsed
                    self.current["events"].append({"op": op, "started_utc": started_utc, "backend_ms": round(elapsed, 1)})

    async def handle(self, reader, writer):
        authenticated = False
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            if len(line) > 65536:
                raise ValueError("Request too large")
            req = json.loads(line)
            if not hmac.compare_digest(str(req.pop("auth", "")), self.auth):
                raise RuntimeError("Local service authentication failed")
            authenticated = True
            result = {"ok": True, "result": await self.dispatch(req)}
        except Exception as exc:
            result = {"ok": False, "error": str(exc) or type(exc).__name__}
            if authenticated and self.current and req.get("session") == self.current["session"]:
                with contextlib.suppress(Exception):
                    sample = self.feed.capture()
                    if sample is not None:
                        path = WORK / "last-failure.png"
                        sample.image.save(path)
                        result["screenshot"] = str(path)
        writer.write((json.dumps(result, ensure_ascii=False) + "\n").encode())
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()

    async def watchdog(self):
        while True:
            await asyncio.sleep(1)
            if self.current and time.monotonic() > self.current["deadline"] and not self.lock.locked():
                async with self.lock:
                    await self.finish("deadline-exceeded")

    async def serve(self):
        WORK.mkdir(parents=True, exist_ok=True)
        server = await asyncio.start_server(self.handle, HOST, PORT, limit=65537)
        (SERVICE_DIR / "local-service.json").write_text(json.dumps({"pid": os.getpid(), "port": PORT, "started_utc": timestamp()}) + "\n")
        watch = asyncio.create_task(self.watchdog())
        try:
            async with server:
                await self.stop.wait()
        finally:
            watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch
            if self.board:
                await self.board.close()


class LocalError(RuntimeError):
    def __init__(self, answer):
        self.answer = answer
        super().__init__(answer.get("error", "Local request failed"))


def rpc(payload, timeout=90):
    data = dict(payload, auth=token())
    with socket.create_connection((HOST, PORT), timeout=1) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(data, ensure_ascii=False) + "\n").encode())
        answer = json.loads(sock.makefile("rb").readline())
    if not answer.get("ok"):
        raise LocalError(answer)
    return answer["result"]


def start_service():
    try:
        state = rpc({"op": "status"}, timeout=2)
        if state.get("service") != "esp32-iphone-local":
            raise RuntimeError("Unexpected process on the control port")
        return
    except (OSError, ConnectionError):
        pass
    WORK.mkdir(parents=True, exist_ok=True)
    log = (WORK / "service.log").open("ab")
    subprocess.Popen([str(PYTHON), "-X", "utf8", "-m", "mhxy_control.phone", "_serve"],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=str(Path(__file__).resolve().parents[1]),
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    log.close()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            rpc({"op": "status"}, timeout=1)
            return
        except (OSError, ConnectionError):
            time.sleep(.1)
    raise RuntimeError("Local service did not start; inspect the sanitized service.log")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["_serve", "start", "status", "probe", "pair-hid", "pause-hid", "resume-hid", "begin", "finish", "release", "screenshot", "run", "ocr", "recipe", "stop"])
    parser.add_argument("--session")
    parser.add_argument("--out")
    parser.add_argument("--record")
    parser.add_argument("--mirror", choices=["mhxy"], default="mhxy")
    parser.add_argument("--limit", type=float, default=300)
    parser.add_argument("--json")
    parser.add_argument("--file")
    parser.add_argument("--stdin", action="store_true")
    parser.add_argument("--name")
    parser.add_argument("--box", nargs=4, type=int)
    parser.add_argument("--neutral", action="store_true")
    args = parser.parse_args()
    if args.action == "_serve":
        return asyncio.run(Service().serve())
    start_service()
    payload = {"op": "ensure" if args.action == "start" else args.action, "session": args.session}
    if args.out:
        payload["out"] = str(Path(args.out).resolve())
    if args.record:
        payload["record"] = str(Path(args.record).resolve())
    if args.action == "begin":
        payload["limit_seconds"] = args.limit
        payload["mirror"] = args.mirror
    if args.action == "ocr":
        if not args.box:
            raise ValueError("ocr requires --box left top right bottom")
        payload.update(box=args.box, neutral=args.neutral)
    if args.action in {"run", "recipe"}:
        text = sys.stdin.read() if args.stdin else Path(args.file).read_text(encoding="utf-8-sig") if args.file else args.json
        if not text:
            raise ValueError("Provide --stdin, --file, or --json")
        payload["request" if args.action == "run" else "payload"] = json.loads(text)
        if args.action == "recipe":
            payload["name"] = args.name
    result = rpc(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps(exc.answer if isinstance(exc, LocalError) else {"ok": False, "error": str(exc) or type(exc).__name__}, ensure_ascii=False))
        sys.exit(1)
