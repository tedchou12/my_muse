#!/usr/bin/env python3
"""Expose Chrome's loopback-only DevTools endpoint to the compose network.

Chrome 131 binds --remote-debugging-port to 127.0.0.1 regardless of
--remote-debugging-address. Muse runs in a different container, so this proxy:

* forwards HTTP requests to the local DevTools endpoint with the Host header
  Chrome expects;
* rewrites the WebSocket URL in /json/version so Playwright can reach it;
* tunnels the WebSocket upgrade bidirectionally.
"""
from __future__ import annotations

import asyncio
import os
import sys

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 9222
PUBLIC_HOST = os.environ.get("CDP_PUBLIC_HOST", "browser:9223").encode()
PRIVATE_ORIGIN = b"http://127.0.0.1:9222"


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def rewrite_request(header: bytes) -> bytes:
    lines = header.split(b"\r\n")
    rewritten = []
    for line in lines:
        lowered = line.lower()
        if lowered.startswith(b"host:"):
            rewritten.append(b"Host: 127.0.0.1:9222")
        elif lowered.startswith(b"origin:"):
            rewritten.append(b"Origin: " + PRIVATE_ORIGIN)
        else:
            rewritten.append(line)
    return b"\r\n".join(rewritten)


def header_value(header: bytes, name: bytes) -> bytes:
    for line in header.split(b"\r\n"):
        if line.lower().startswith(name.lower() + b":"):
            return line.split(b":", 1)[1].strip()
    return b""


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        header = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, ConnectionError):
        writer.close()
        return

    first_line = header.split(b"\r\n", 1)[0]
    parts = first_line.split(b" ")
    method = parts[0] if parts else b"GET"
    path = parts[1] if len(parts) > 1 else b"/"
    is_websocket = b"upgrade: websocket" in header.lower()

    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            UPSTREAM_HOST, UPSTREAM_PORT
        )
    except Exception as exc:
        sys.stderr.write("cdp-proxy: upstream unavailable: %s\n" % exc)
        writer.close()
        return

    if is_websocket:
        upstream_writer.write(rewrite_request(header))
        await upstream_writer.drain()
        try:
            response = await upstream_reader.readuntil(b"\r\n\r\n")
        except Exception:
            writer.close()
            upstream_writer.close()
            return
        writer.write(response)
        await writer.drain()
        if response.startswith(b"HTTP/1.1 101"):
            await asyncio.gather(
                pipe(reader, upstream_writer),
                pipe(upstream_reader, writer),
            )
        else:
            upstream_writer.close()
            writer.close()
        return

    # Plain HTTP: force a close so the body can be buffered and rewritten.
    header = rewrite_request(header)
    if b"\r\nconnection:" not in header.lower():
        header = header.replace(b"\r\n\r\n", b"\r\nConnection: close\r\n\r\n", 1)
    else:
        header = b"\r\n".join(
            b"Connection: close"
            if line.lower().startswith(b"connection:")
            else line
            for line in header.split(b"\r\n")
        )
    upstream_writer.write(header)
    await upstream_writer.drain()
    try:
        response_header = await upstream_reader.readuntil(b"\r\n\r\n")
    except Exception:
        upstream_writer.close()
        writer.close()
        return

    length = header_value(response_header, b"content-length")
    chunked = b"chunked" in header_value(response_header, b"transfer-encoding").lower()
    if chunked:
        body = await upstream_reader.read()
    elif length.isdigit():
        try:
            body = await upstream_reader.readexactly(int(length))
        except asyncio.IncompleteReadError as exc:
            body = exc.partial
    else:
        body = await upstream_reader.read()

    upstream_writer.close()
    body = body.replace(b"127.0.0.1:9222", PUBLIC_HOST)
    lines = [
        line
        for line in response_header.split(b"\r\n")
        if not line.lower().startswith(b"content-length:")
        and not line.lower().startswith(b"transfer-encoding:")
        and not line.lower().startswith(b"connection:")
    ]
    # Keep the terminating empty lines out of the rebuilt header.
    while lines and lines[-1] == b"":
        lines.pop()
    lines.append(b"Content-Length: %d" % len(body))
    lines.append(b"Connection: close")
    writer.write(b"\r\n".join(lines) + b"\r\n\r\n" + body)
    await writer.drain()
    writer.close()


async def main() -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", 9223)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
