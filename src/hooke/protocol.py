"""Wire protocol: JSON lines over a unix domain socket.

Connect-time handshake: the daemon immediately sends one JSON line
``{"type":"hello","protocol":1,"version":"..."}``. A protocol mismatch is a
hard error telling the CLI side to upgrade. After that it is strictly
request → response, one round-trip per connection.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

from .config import HOOKE_VERSION, PROTOCOL_VERSION


class ProtocolError(Exception):
    pass


class VersionMismatch(ProtocolError):
    pass


def hello_message() -> dict:
    return {"type": "hello", "protocol": PROTOCOL_VERSION, "version": HOOKE_VERSION}


async def send_json(writer: asyncio.StreamWriter, msg: dict) -> None:
    writer.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
    await writer.drain()


async def recv_json(reader: asyncio.StreamReader, *, first: bool = False) -> dict:
    line = await reader.readline()
    if not line:
        raise ProtocolError("connection closed" if not first else "no handshake received")
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"invalid JSON frame: {e}") from e
    if not isinstance(msg, dict):
        raise ProtocolError("frame must be a JSON object")
    if first:
        if msg.get("type") != "hello":
            raise ProtocolError("expected hello handshake")
        if msg.get("protocol") != PROTOCOL_VERSION:
            raise VersionMismatch(
                f"daemon speaks protocol {msg.get('protocol')}, "
                f"CLI speaks {PROTOCOL_VERSION} — upgrade your daemon"
            )
    return msg


async def request(socket: pathlib.Path, op: dict, timeout: float = 30.0) -> dict:
    """One-shot client round-trip: connect, handshake, send, await reply."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket)), timeout
        )
    except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
        raise ProtocolError(
            f"cannot reach hooke daemon at {socket} ({e.__class__.__name__}) — is it running?"
        ) from e
    try:
        await asyncio.wait_for(recv_json(reader, first=True), timeout)
        await send_json(writer, op)
        reply = await asyncio.wait_for(recv_json(reader), timeout)
        return reply
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
