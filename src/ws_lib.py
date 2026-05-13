"""
pre Message Bus — WebSocket 协议库 (RFC 6455 简化版)

服务端和客户端共用的 frame encode/decode + 握手辅助。
- 仅文本帧 (opcode=0x1) 和 close (0x8) / ping (0x9) / pong (0xA)
- 不实现分片 (FIN 必须为 1, 单次发送上限受 64-bit length, 实际限制由用户)
- Server side: 接收 masked frame, 发送 unmasked frame
- Client side: 发 masked frame, 接收 unmasked frame

跟 cdp_probe.py 的 client 是配套设计 (cdp_probe 处理 client 一侧, 这里实现两侧通用 frame 工具)。
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import os
import struct
from typing import Optional


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------- Handshake (server side) ----------

def compute_accept_key(client_key: str) -> str:
    """根据客户端 Sec-WebSocket-Key 计算 server 应返回的 Sec-WebSocket-Accept"""
    raw = (client_key + WS_GUID).encode("ascii")
    return base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")


def parse_http_request(raw: bytes) -> tuple[str, str, dict]:
    """解析 HTTP 请求 (request line + headers), 返回 (method, path, headers)"""
    text = raw.decode("iso-8859-1")
    lines = text.split("\r\n")
    if not lines:
        raise ValueError("empty HTTP request")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2:
        raise ValueError(f"bad request line: {lines[0]!r}")
    method, path = parts[0], parts[1]
    headers = {}
    for line in lines[1:]:
        if not line:
            break
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return method, path, headers


def build_handshake_response(client_key: str) -> bytes:
    """生成 101 Switching Protocols 响应"""
    accept = compute_accept_key(client_key)
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    ).encode("ascii")


# ---------- Frame (双向通用) ----------

OPCODE_TEXT = 0x1
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


def encode_frame(payload: bytes, opcode: int = OPCODE_TEXT, masked: bool = False) -> bytes:
    """
    编码单个 WS 帧.
    masked=True 仅 client → server 时使用 (RFC 强制要求)
    masked=False 用于 server → client
    """
    fin = 0x80
    b1 = fin | (opcode & 0x0F)
    ln = len(payload)
    mask_bit = 0x80 if masked else 0x00
    if ln < 126:
        header = struct.pack("!BB", b1, mask_bit | ln)
    elif ln < 65536:
        header = struct.pack("!BBH", b1, mask_bit | 126, ln)
    else:
        header = struct.pack("!BBQ", b1, mask_bit | 127, ln)
    if masked:
        mask = os.urandom(4)
        masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return header + mask + masked_payload
    return header + payload


async def read_exactly(reader: asyncio.StreamReader, n: int) -> bytes:
    """从 stream 读 n 字节, 不够抛 ConnectionError"""
    data = await reader.readexactly(n)
    return data


async def read_frame(reader: asyncio.StreamReader,
                     expect_masked: bool = True) -> tuple[int, bytes]:
    """
    读单个 WS 帧.
    expect_masked: server 端读 client 帧应为 True; client 端读 server 帧应为 False
    返回 (opcode, payload_bytes)
    """
    head = await read_exactly(reader, 2)
    b1, b2 = head[0], head[1]
    opcode = b1 & 0x0F
    masked = (b2 & 0x80) != 0
    ln = b2 & 0x7F
    if ln == 126:
        (ln,) = struct.unpack("!H", await read_exactly(reader, 2))
    elif ln == 127:
        (ln,) = struct.unpack("!Q", await read_exactly(reader, 8))

    mask_key = await read_exactly(reader, 4) if masked else None
    payload = await read_exactly(reader, ln) if ln else b""
    if masked and mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

    if expect_masked and not masked:
        # RFC 6455: client → server 必须 masked. 严格模式下应断开.
        # MVP 容忍, 仅警告.
        pass

    return opcode, payload


async def send_text(writer: asyncio.StreamWriter, text: str, masked: bool = False):
    """发送文本帧"""
    frame = encode_frame(text.encode("utf-8"), OPCODE_TEXT, masked=masked)
    writer.write(frame)
    await writer.drain()


async def send_to_writer(writer: asyncio.StreamWriter, text: str):
    """发文本帧, 自动 detect mask 方向 — writer 上挂 `_pre_send_masked`
    属性 (master-connect mode 下 master 端 client side 标 True), 没属性默认 False (server 端).
    用于 master 端给 node ws_writer 发命令时不需手动判断 transport 方向."""
    masked = bool(getattr(writer, "_pre_send_masked", False))
    frame = encode_frame(text.encode("utf-8"), OPCODE_TEXT, masked=masked)
    writer.write(frame)
    await writer.drain()


async def send_close(writer: asyncio.StreamWriter, masked: bool = False):
    frame = encode_frame(b"", OPCODE_CLOSE, masked=masked)
    writer.write(frame)
    await writer.drain()


# ---------- Client-side handshake (master → node, ) ----------

async def ws_client_handshake(host: str, port: int, path: str = "/node",
                              extra_headers: Optional[dict] = None,
                              timeout: float = 10.0
                              ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """连远端 ws server, 完成 HTTP Upgrade 握手. 返回 (reader, writer).
    master 主动 connect node 用. node 端的 ws server (NodeServer) accept 后跑此协议."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout
    )
    key = base64.b64encode(os.urandom(16)).decode()
    req_lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if extra_headers:
        for k, v in extra_headers.items():
            req_lines.append(f"{k}: {v}")
    req = "\r\n".join(req_lines) + "\r\n\r\n"
    writer.write(req.encode())
    await writer.drain()

    # 读 101 response — 用 readuntil 仅吃到 \r\n\r\n 截断, 不消费后续 ws frame bytes
    # (server 收到 GET 后会写 101 然后立即发 register_node frame, 一次 read 可能两者都拿到)
    try:
        head_bytes = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout=timeout
        )
    except asyncio.IncompleteReadError as e:
        writer.close()
        raise ConnectionError(f"ws_client_handshake: server closed mid-handshake "
                              f"(got {len(e.partial)} bytes)")
    head = head_bytes[:-4].decode("iso-8859-1")  # strip 末尾 \r\n\r\n
    first = head.split("\r\n")[0]
    if "101" not in first:
        writer.close()
        raise ConnectionError(f"ws_client_handshake failed: {first}")
    return reader, writer
