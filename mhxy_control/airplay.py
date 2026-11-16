"""Read UxPlay's local H.264 RTP output without relying on its RTP timestamps."""

import asyncio
import socket
import struct
import time

from .runtime import Sample


START_CODE = b"\x00\x00\x00\x01"
MAX_ACCESS_UNIT = 8 * 1024 * 1024


def rtp_payload(packet):
    if len(packet) < 13:
        return None
    flags, kind, sequence, _, ssrc = struct.unpack_from(">BBHII", packet)
    if flags >> 6 != 2 or kind & 127 != 96:
        return None
    start = 12 + 4 * (flags & 15)
    end = len(packet) - (packet[-1] if flags & 32 else 0)
    if flags & 16:
        if start + 4 > end:
            return None
        words = struct.unpack_from(">H", packet, start + 2)[0]
        start += 4 + 4 * words
    if start >= end:
        return None
    return sequence, ssrc, bool(kind & 128), packet[start:end]


class AccessUnits:
    """Assemble RFC 6184 single NAL, STAP-A and FU-A packets into Annex B frames."""

    def __init__(self):
        self.sequence = None
        self.ssrc = None
        self.sps = self.pps = b""
        self.needs_keyframe = True
        self.buffer = bytearray()
        self.fragment = None
        self.has_idr = False
        self.has_picture = False

    def reset_frame(self):
        self.buffer.clear()
        self.fragment = None
        self.has_idr = False
        self.has_picture = False

    def push(self, packet):
        parsed = rtp_payload(packet)
        if parsed is None:
            return None
        sequence, ssrc, marker, payload = parsed
        if ssrc != self.ssrc:
            self.ssrc = ssrc
            self.sps = self.pps = b""
            self.sequence = None
            self.needs_keyframe = True
            self.reset_frame()
        if self.sequence is not None:
            step = (sequence - self.sequence) & 65535
            if step == 0 or step >= 32768:
                return None
            if step != 1:
                self.reset_frame()
                self.needs_keyframe = True
        self.sequence = sequence
        try:
            self.append(payload)
            if len(self.buffer) + len(self.fragment or b"") > MAX_ACCESS_UNIT:
                raise ValueError("H.264 access unit is too large")
        except ValueError:
            self.reset_frame()
            self.needs_keyframe = True
            return None
        if not marker:
            return None
        result = None
        if self.fragment is None and self.has_picture:
            if self.has_idr and self.sps and self.pps:
                result = (self.sps + self.pps + bytes(self.buffer), True)
                self.needs_keyframe = False
            elif not self.needs_keyframe:
                result = (bytes(self.buffer), False)
        self.reset_frame()
        return result

    def append_nal(self, payload):
        kind = payload[0] & 31
        if kind == 7:
            self.sps = START_CODE + payload
        elif kind == 8:
            self.pps = START_CODE + payload
        else:
            self.has_idr |= kind == 5
            self.has_picture |= kind in (1, 5)
            self.buffer.extend(START_CODE)
            self.buffer.extend(payload)

    def append(self, payload):
        kind = payload[0] & 31
        if 1 <= kind <= 23:
            self.append_nal(payload)
        elif kind == 24:
            offset = 1
            while offset + 2 <= len(payload):
                size = struct.unpack_from(">H", payload, offset)[0]
                offset += 2
                if not size or offset + size > len(payload):
                    raise ValueError("Malformed STAP-A packet")
                self.append_nal(payload[offset:offset + size])
                offset += size
            if offset != len(payload):
                raise ValueError("Malformed STAP-A packet")
        elif kind == 28 and len(payload) > 2:
            header = payload[1]
            if header & 128:
                self.fragment = bytearray([(payload[0] & 224) | (header & 31)])
            if self.fragment is not None:
                self.fragment.extend(payload[2:])
                if header & 64:
                    self.append_nal(bytes(self.fragment))
                    self.fragment = None
        else:
            raise ValueError("Unsupported H.264 RTP packet")


class AirPlay:
    def __init__(self, port):
        self.port = port

    async def read(self, latest, trace, stopped):
        import av

        loop = asyncio.get_running_loop()
        units = AccessUnits()
        decoder = None
        last_frame = time.monotonic()
        packets = 0
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            sock.bind(("127.0.0.1", self.port))
            sock.setblocking(False)
            trace.event("rtp_listening", port=self.port)
            while not stopped.is_set():
                try:
                    packet, _ = await asyncio.wait_for(loop.sock_recvfrom(sock, 65535), 0.7)
                except TimeoutError:
                    packet = None
                if packet is not None:
                    packets += 1
                    assembled = units.push(packet)
                    if assembled is not None:
                        data, keyframe = assembled
                        if keyframe:
                            decoder = av.CodecContext.create("h264", "r")
                            decoder.thread_count = 1
                        try:
                            decoded = decoder.decode(av.Packet(data)) if decoder else []
                        except av.error.InvalidDataError:
                            units.needs_keyframe = True
                            decoded = []
                        for frame in decoded:
                            if latest.error is not None:
                                trace.event("video_recovered", packets=packets)
                            last_frame = time.monotonic()
                            latest.publish(Sample(frame.to_image(), last_frame))
                if time.monotonic() - last_frame > 2:
                    reason = ("Waiting for H.264 keyframe" if packets and units.needs_keyframe
                              else "No recent decoded AirPlay frame")
                    if latest.error != reason:
                        trace.event("video_unavailable", reason=reason, packets=packets)
                    latest.invalidate(reason)
