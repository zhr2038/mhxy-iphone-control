"""State-checked recipes for this paired iPhone profile, not universal coordinates."""
import asyncio
import importlib
import io
import time
import json
import os
from pathlib import Path
from PIL import Image
from . import local_ui

# The shared service reloads recipes and their OCR helpers between requests.
LocalUI = importlib.reload(local_ui).LocalUI
compact = local_ui.compact

SIZE = (718, 1560)
HEADER = [20, 100, 698, 176]
ASSISTANT = "文件传输助手"
CHAT_TITLE = [225, 105, 495, 170]
WECHAT_TABS = [20, 1455, 698, 1510]
KEYBOARD_SPACE = [220, 1330, 530, 1430]
# Pencil-in-square glyph sampled from the verified Notes compose button.
NOTES_COMPOSE = bytes.fromhex(
    "1f1f1d1a1919191c1f262c1f1f1c234a515053481b65b5201c31c5fffeffed609eb7441e"
    "1686fc765558429bf86a191f16a1e11e16169bfa697e3a1c16a0e1241a9dfb6163f6381b"
    "17a2e1213dd4670e7ff6371b17a2df1f222515147af4371c1690f45032353636a9ed2d1e"
    "1a41e4fcf4f4f0f5fe911c211f1b387781807e7e5b201f211f211d18181818181a201f20"
)


def locate(result, expected):
    wanted = compact(expected)
    for line in result["lines"]:
        words = line["words"]
        for i in range(len(words)):
            text = ""
            for j in range(i, min(i + 12, len(words))):
                text += compact(words[j]["text"])
                if text == wanted:
                    boxes = [w["box"] for w in words[i:j + 1]]
                    left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
                    right, bottom = max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)
                    return (left + right) / 2, (top + bottom) / 2
                if len(text) > len(wanted):
                    break
    return None


def notes_table_visible(raw):
    pixels = Image.open(io.BytesIO(raw)).convert("L").load()

    def edge(center, side_a, side_b):
        return abs(center - side_a) >= 12 and abs(center - side_b) >= 12

    lines = []
    for y in range(275, 760):
        if all(edge(pixels[x, y], pixels[x, y - 4], pixels[x, y + 4])
               for x in (100, 210, 310, 425, 535, 620)):
            if not lines or y - lines[-1] > 4:
                lines.append(y)
    for y0, y1, y2 in zip(lines, lines[1:], lines[2:]):
        if not (40 <= y1 - y0 <= 100 and 40 <= y2 - y1 <= 100):
            continue
        y = (y0 + y1) // 2
        if any(edge(pixels[x, y], pixels[x - 5, y], pixels[x + 5, y]) for x in range(355, 365)):
            return True
    return False


def notes_compose_visible(raw):
    patch = Image.open(io.BytesIO(raw)).convert("L").crop((601, 1443, 651, 1493)).resize((12, 12)).tobytes()
    if sorted(patch)[len(patch) // 2] > 128:
        patch = bytes(255 - pixel for pixel in patch)
    return sum(abs(a - b) for a, b in zip(patch, NOTES_COMPOSE)) / len(NOTES_COMPOSE) < 28


class Recipes:
    def __init__(self, read_frame, send_command):
        self.read_frame = read_frame
        self.send_command = send_command
        self.last_input_at = None
        self.ui = LocalUI()

    async def step(self, command, after=0.12):
        await self.send_command(command)
        self.last_input_at = time.monotonic()
        await asyncio.sleep(after)

    async def tap(self, x, y, after=0.4):
        # Measured and physically checked in this session. Rehome every target
        # rather than depending on another chat's pointer position.
        await self.ready_frame()
        await self.step("home", 0.15)
        await self.step(f"move {round((x - 112) / 3.78)} {round(y / 3.78)}", 0.15)
        await self.ready_frame()
        await self.step("click", after)

    async def ready_frame(self, after=0):
        # OCR must observe the result of the last input, not a queued old frame.
        if self.last_input_at is not None:
            after = max(after, self.last_input_at + 0.1)
        return await self.read_frame(after)

    async def read(self, box=HEADER, neutral=False):
        _, raw = await self.ready_frame()
        return await self.ui.read(raw, box, neutral=neutral)

    async def wait_text(self, expected, box=HEADER, timeout=5):
        deadline = time.monotonic() + timeout
        previous = None
        consecutive = 0
        while time.monotonic() < deadline:
            seq, raw = await self.ready_frame(after=previous or 0)
            result = await self.ui.read(raw, box)
            if compact(expected) in compact(result["text"]):
                if seq != previous:
                    consecutive += 1
                    if consecutive >= 2:
                        return result
            else:
                consecutive = 0
            previous = seq
            await asyncio.sleep(0.16)
        raise RuntimeError(f"Expected UI state did not appear: {expected}")

    async def home_screen(self):
        async def home_visible():
            _, raw = await self.ready_frame()
            row = await self.ui.read(raw, [20, 964, 700, 1050])
            chatgpt = await self.ui.read(raw, [365, 1130, 515, 1210])
            return bool(locate(row, "设置")) and "ChatGPT" in compact(chatgpt["text"])

        if await home_visible():
            return
        # The tested iPhone route uses App Switcher then its blank bottom area.
        # Cmd+H / AC Home were not reliable across this phone's apps.
        # A Control Center overlay consumes the first gesture. Check its result
        # before making a second, bounded attempt from the underlying app.
        for _ in range(2):
            for cmd, gap in [("key esc", .15), ("home", .15), ("move 65 450", .15), ("press", .15), ("move 0 -180", .15), ("release", .5), ("move 0 200", .15), ("click", .8)]:
                await self.step(cmd, gap)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if await home_visible():
                    return
                await asyncio.sleep(.16)
        raise RuntimeError("Home screen was not verified; inspect the phone before proceeding")

    async def ensure_chinese(self):
        for _ in range(3):
            if "空格" in compact((await self.read(KEYBOARD_SPACE))["text"]):
                return
            await self.step("key ctrl+space", 1.25)
        raise RuntimeError("Chinese keyboard was not verified")

    async def type_segments(self, segments):
        for item in segments:
            if "literal" in item:
                text = item["literal"]
                if not text.isascii() or "\n" in text:
                    raise ValueError("literal supports single-line ASCII; Chinese uses checked pinyin segments")
                await self.step("type " + text)
                continue
            expected, pinyin = item["text"], item["pinyin"]
            if not pinyin.isascii() or not pinyin.isalpha():
                raise ValueError("Pinyin must be ASCII letters")
            await self.step("type " + pinyin, .2)
            point = None
            deadline = time.monotonic() + 2
            while point is None and time.monotonic() < deadline:
                _, raw = await self.ready_frame()
                result = await self.ui.read(raw, [0, 960, 718, 1052])
                point = locate(result, expected)
                if point is None:
                    for box in ([0, 973, 180, 1047], [0, 960, 718, 1052]):
                        point = locate(await self.ui.read(raw, box, scale=2), expected)
                        if point is not None:
                            break
                if point is None:
                    await asyncio.sleep(.1)
            if not point:
                raise RuntimeError("Intended Chinese candidate was not recognized; leave the composition unsent")
            if point[0] < 100:
                await self.step("key space", .12)
            else:
                await self.tap(*point, after=.15)

    async def wechat_page(self, raw):
        header = compact((await self.ui.read(raw, CHAT_TITLE))["text"])
        tabs = compact((await self.ui.read(raw, WECHAT_TABS))["text"])
        if "通讯录" in tabs and "发现" in tabs and ("WeChat" in tabs or "微信" in tabs):
            # The pointer can cover the search label. A recognized list title
            # plus the bottom tabs is enough to confirm the same layout.
            if header in {"WeChat", "微信"} or header.startswith("连接中"):
                return "list"
            if header in {"通讯录", "发现", "我"}:
                return "tab"
            if "搜索" in compact((await self.ui.read(raw, [100, 180, 620, 250]))["text"]):
                return "list"
        if header == ASSISTANT:
            return "helper-title"
        return None

    async def wait_wechat_page(self, allowed, timeout):
        deadline = time.monotonic() + timeout
        while True:
            _, raw = await self.ready_frame()
            page = await self.wechat_page(raw)
            if page in allowed or time.monotonic() >= deadline:
                return page if page in allowed else None
            await asyncio.sleep(.16)

    async def wechat_filehelper(self, segments, send=False):
        pages = {"list", "tab", "helper-title"}
        page = await self.wait_wechat_page(pages, 0)
        if page is None:
            await self.home_screen()
            profile_path = os.environ.get("MHXY_WECHAT_PROFILE")
            if not profile_path:
                raise RuntimeError("Open WeChat manually or configure MHXY_WECHAT_PROFILE before starting this recipe")
            profile = json.loads(Path(profile_path).read_text(encoding="utf-8-sig"))
            _, raw = await self.ready_frame()
            actual = Image.open(io.BytesIO(raw)).convert("RGB").crop(tuple(profile["wechat_icon_box"])).resize((16, 16))
            expected = profile["wechat_icon_rgb"]
            difference = sum(abs(a - b) for pixel, ref in zip(actual.get_flattened_data(), expected) for a, b in zip(pixel, ref)) / (256 * 3)
            if difference > 24:
                raise RuntimeError("The WeChat dock icon no longer matches this phone profile; inspect before opening")
            await self.tap(435, 1430, after=.7)
            page = await self.wait_wechat_page(pages, 3)
        # Reach the builtin Function search result even if the last chat happens
        # to use the same display name; a chat title alone is not identity proof.
        if page == "tab":
            await self.tap(90, 1440, after=.5)
            page = await self.wait_wechat_page({"list"}, 3)
        elif page == "helper-title":
            await self.tap(40, 135, after=.7)
            page = await self.wait_wechat_page({"list"}, 3)
        if page != "list":
            raise RuntimeError("WeChat chat list was not verified")
        await self.tap(220, 220, after=.5)
        await self.ensure_chinese()
        await self.type_segments([{"pinyin": "wenjian", "text": "文件"},
                                  {"pinyin": "chuanshu", "text": "传输"},
                                  {"pinyin": "zhushou", "text": "助手"}])
        await self.wait_text("功能", [20, 235, 690, 295])
        await self.wait_text(ASSISTANT, [20, 290, 690, 410])
        await self.tap(240, 350, after=.5)
        await self.wait_text(ASSISTANT, CHAT_TITLE)
        keyboard = "空格" in compact((await self.read(KEYBOARD_SPACE))["text"])
        await self.tap(300, 840 if keyboard else 1450, after=.4)
        await self.ensure_chinese()
        before = await self.read([85, 800, 560, 885], neutral=True)
        if compact(before["text"]) not in {"", "可按住转文字"}:
            raise RuntimeError("An existing message draft is present; preserve it and inspect")
        await self.type_segments(segments)
        expected = "".join(s.get("text", s.get("literal", "")) for s in segments)
        await asyncio.sleep(.25)
        body = compact((await self.read([85, 800, 560, 885], neutral=True))["text"])
        if body != compact(expected):
            raise RuntimeError("Draft readback differs; do not send. Inspect the current draft.")
        if compact((await self.read(CHAT_TITLE))["text"]) != ASSISTANT:
            raise RuntimeError("Recipient changed; do not send")
        if not send:
            return {"recipient_verified": True, "draft_verified": True, "sent": False}
        await self.tap(642, 1340, after=.7)
        after = compact((await self.read([85, 800, 560, 885], neutral=True))["text"])
        if after:
            raise RuntimeError("Send result is ambiguous; inspect before retrying")
        return {"recipient_verified": True, "draft_verified": True, "input_cleared_after_send": True, "sent": True}

    async def notes_create(self, title, paragraphs, rows):
        if rows and (not all(len(r) == 2 for r in rows)):
            raise ValueError("The tested Notes table recipe supports exactly two columns")
        for _ in range(3):
            if await self.notes_blank():
                break
            await asyncio.sleep(.16)
        else:
            await self.open_blank_note()
        keyboard = compact((await self.read(KEYBOARD_SPACE))["text"])
        if not ("空格" in keyboard or "space" in keyboard.lower()):
            await self.tap(65, 250, after=.4)
        await self.ensure_chinese()
        await self.type_segments(title)
        await self.step("key enter")
        for paragraph in paragraphs:
            await self.type_segments(paragraph)
            await self.step("key enter")
        if rows:
            await self.step("key enter")
            await self.tap(332, 835, after=.3)
            deadline = time.monotonic() + 2
            while True:
                _, raw = await self.ready_frame()
                if notes_table_visible(raw):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Native table grid was not verified; inspect this new note before continuing")
                await asyncio.sleep(.16)
            cells = [cell for row in rows for cell in row]
            for i, cell in enumerate(cells):
                await self.type_segments(cell)
                if i + 1 < len(cells):
                    await self.step("key tab")
        await self.tap(650, 150, after=.6)
        return {"created": True, "native_table_rows": len(rows), "native_table_columns": 2 if rows else 0, "visual_verification_required": True}

    async def notes_blank(self):
        _, raw = await self.ready_frame()
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        r, g, b = image.getpixel((650, 122))
        if r < 180 or g < 100 or r - b < 100:
            return False
        # The yellow Done control and the native formatting toolbar distinguish
        # the editor from an empty-looking screen while Notes is opening.
        fmt = False
        for box in ([20, 805, 122, 865], [45, 1430, 160, 1505]):
            if "格式" in compact((await self.ui.read(raw, box))["text"]):
                fmt = True
                break
        return fmt and not compact((await self.ui.read(raw, [40, 210, 690, 760]))["text"])

    async def open_blank_note(self):
        spotlight = "最佳搜索结果" in compact((await self.read([20, 120, 320, 174]))["text"])
        query = compact((await self.read([60, 805, 375, 870]))["text"]) if spotlight else ""
        if not query.startswith("Notes"):
            await self.home_screen()
            await self.step("key cmd+space", .5)
            await self.step("type Notes", .15)
            await self.step("key space", .5)
        await self.wait_text("最佳搜索结果", [20, 120, 320, 174])
        result = await self.wait_text("备忘录", [20, 310, 700, 385])
        point = locate(result, "备忘录")
        if not point or point[0] >= 195:
            raise RuntimeError("Notes app result was not located")
        await self.tap(point[0], point[1] - 78, after=.7)
        # Notes can resume either its list or the last note. The compose glyph
        # is shared by both; only use it after opening the verified Notes app.
        deadline = time.monotonic() + 3
        while True:
            if await self.notes_blank():
                return
            _, raw = await self.ready_frame()
            if notes_compose_visible(raw):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Notes compose control was not verified; leave the current note untouched")
            await asyncio.sleep(.16)
        await self.tap(627, 1470, after=.5)
        deadline = time.monotonic() + 3
        while not await self.notes_blank():
            if time.monotonic() >= deadline:
                raise RuntimeError("New blank note was not verified; preserve existing note")
            await asyncio.sleep(.16)
