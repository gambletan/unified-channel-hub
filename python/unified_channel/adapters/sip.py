"""SIP adapter — direct voice calls via SIP protocol (no Twilio).

Pure Python implementation using socket/asyncio for SIP signaling.
This is a foundational stub with SIP state machine basics — not a full VoIP stack.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import socket
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator

from ..adapter import ChannelAdapter
from ..types import (
    ChannelStatus,
    ContentType,
    Identity,
    MessageContent,
    OutboundMessage,
    UnifiedMessage,
)

logger = logging.getLogger(__name__)


class SIPState(Enum):
    """SIP call state machine."""

    IDLE = "idle"
    REGISTERING = "registering"
    REGISTERED = "registered"
    INVITING = "inviting"
    RINGING = "ringing"
    IN_CALL = "in_call"
    DISCONNECTED = "disconnected"


def parse_sip_uri(uri: str) -> dict[str, str]:
    """Parse a SIP URI into components.

    Examples:
        sip:user@host:port -> {scheme: sip, user: user, host: host, port: port}
        sip:user@host      -> {scheme: sip, user: user, host: host, port: 5060}
        sips:user@host     -> {scheme: sips, user: user, host: host, port: 5061}
    """
    result: dict[str, str] = {}

    # Extract scheme
    if ":" not in uri:
        raise ValueError(f"invalid SIP URI: {uri}")

    scheme, rest = uri.split(":", 1)
    scheme = scheme.lower()
    if scheme not in ("sip", "sips"):
        raise ValueError(f"invalid SIP scheme: {scheme}")
    result["scheme"] = scheme

    # Extract user@host:port
    if "@" in rest:
        result["user"], hostport = rest.split("@", 1)
    else:
        result["user"] = ""
        hostport = rest

    if ":" in hostport:
        result["host"], result["port"] = hostport.rsplit(":", 1)
    else:
        result["host"] = hostport
        result["port"] = "5061" if scheme == "sips" else "5060"

    return result


def build_register_message(
    sip_uri: str,
    username: str,
    *,
    call_id: str | None = None,
    cseq: int = 1,
    local_host: str = "0.0.0.0",
    local_port: int = 5060,
    expires: int = 3600,
) -> str:
    """Build a SIP REGISTER request message."""
    parsed = parse_sip_uri(sip_uri)
    domain = parsed["host"]
    call_id = call_id or str(uuid.uuid4())
    branch = f"z9hG4bK{uuid.uuid4().hex[:16]}"
    tag = uuid.uuid4().hex[:8]

    lines = [
        f"REGISTER sip:{domain} SIP/2.0",
        f"Via: SIP/2.0/UDP {local_host}:{local_port};branch={branch}",
        f"From: <sip:{username}@{domain}>;tag={tag}",
        f"To: <sip:{username}@{domain}>",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} REGISTER",
        f"Contact: <sip:{username}@{local_host}:{local_port}>",
        f"Expires: {expires}",
        "Max-Forwards: 70",
        "Content-Length: 0",
        "",
        "",
    ]
    return "\r\n".join(lines)


def build_invite_message(
    target_uri: str,
    from_uri: str,
    username: str,
    *,
    call_id: str | None = None,
    cseq: int = 1,
    local_host: str = "0.0.0.0",
    local_port: int = 5060,
) -> str:
    """Build a SIP INVITE request message."""
    parsed = parse_sip_uri(target_uri)
    from_parsed = parse_sip_uri(from_uri)
    call_id = call_id or str(uuid.uuid4())
    branch = f"z9hG4bK{uuid.uuid4().hex[:16]}"
    tag = uuid.uuid4().hex[:8]

    lines = [
        f"INVITE {target_uri} SIP/2.0",
        f"Via: SIP/2.0/UDP {local_host}:{local_port};branch={branch}",
        f"From: <{from_uri}>;tag={tag}",
        f"To: <{target_uri}>",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} INVITE",
        f"Contact: <sip:{username}@{local_host}:{local_port}>",
        "Max-Forwards: 70",
        "Content-Type: application/sdp",
        "Content-Length: 0",
        "",
        "",
    ]
    return "\r\n".join(lines)


def build_bye_message(
    target_uri: str,
    from_uri: str,
    *,
    call_id: str,
    cseq: int = 2,
    local_host: str = "0.0.0.0",
    local_port: int = 5060,
) -> str:
    """Build a SIP BYE request message."""
    branch = f"z9hG4bK{uuid.uuid4().hex[:16]}"
    tag = uuid.uuid4().hex[:8]

    lines = [
        f"BYE {target_uri} SIP/2.0",
        f"Via: SIP/2.0/UDP {local_host}:{local_port};branch={branch}",
        f"From: <{from_uri}>;tag={tag}",
        f"To: <{target_uri}>",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} BYE",
        "Max-Forwards: 70",
        "Content-Length: 0",
        "",
        "",
    ]
    return "\r\n".join(lines)


def build_ack_message(
    target_uri: str,
    from_uri: str,
    *,
    call_id: str,
    cseq: int = 1,
    local_host: str = "0.0.0.0",
    local_port: int = 5060,
) -> str:
    """Build a SIP ACK request message."""
    branch = f"z9hG4bK{uuid.uuid4().hex[:16]}"
    tag = uuid.uuid4().hex[:8]

    lines = [
        f"ACK {target_uri} SIP/2.0",
        f"Via: SIP/2.0/UDP {local_host}:{local_port};branch={branch}",
        f"From: <{from_uri}>;tag={tag}",
        f"To: <{target_uri}>",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} ACK",
        "Max-Forwards: 70",
        "Content-Length: 0",
        "",
        "",
    ]
    return "\r\n".join(lines)


def _parse_sip_response(data: bytes) -> dict[str, Any]:
    """Parse a raw SIP response into a dict with status_code, headers, body."""
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\r\n")
    if not lines:
        return {"status_code": 0, "headers": {}, "body": ""}

    # First line: SIP/2.0 200 OK
    first = lines[0]
    parts = first.split(" ", 2)
    status_code = int(parts[1]) if len(parts) >= 2 else 0

    headers: dict[str, str] = {}
    body_start = len(lines)
    for i, line in enumerate(lines[1:], 1):
        if line == "":
            body_start = i + 1
            break
        if ":" in line:
            key, val = line.split(":", 1)
            headers[key.strip()] = val.strip()

    body = "\r\n".join(lines[body_start:])
    return {"status_code": status_code, "headers": headers, "body": body}


class SIPAdapter(ChannelAdapter):
    """SIP protocol adapter for direct voice calls without Twilio."""

    channel_id = "sip"

    def __init__(
        self,
        sip_uri: str,
        username: str,
        password: str,
        *,
        local_port: int = 5060,
        codec: str = "PCMU",
        stun_server: str | None = None,
    ) -> None:
        self._sip_uri = sip_uri
        self._username = username
        self._password = password
        self._local_port = local_port
        self._codec = codec
        self._stun_server = stun_server

        self._queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue()
        self._connected = False
        self._last_activity: datetime | None = None
        self._state = SIPState.IDLE
        self._listener_task: asyncio.Task | None = None
        self._sock: socket.socket | None = None
        self._call_id: str | None = None
        self._cseq = 1

    async def connect(self) -> None:
        """Create UDP socket, bind to local port, and REGISTER with the SIP server."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._sock.bind(("0.0.0.0", self._local_port))

        self._state = SIPState.REGISTERING
        await self._register()
        self._connected = True
        self._state = SIPState.REGISTERED
        self._listener_task = asyncio.create_task(self._listen_loop())
        logger.info("sip connected: %s as %s on :%d", self._sip_uri, self._username, self._local_port)

    async def _register(self) -> None:
        """Send SIP REGISTER to the server."""
        parsed = parse_sip_uri(self._sip_uri)
        msg = build_register_message(
            self._sip_uri,
            self._username,
            local_port=self._local_port,
        )
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(
            self._sock,
            msg.encode("utf-8"),
            (parsed["host"], int(parsed["port"])),
        )
        self._last_activity = datetime.now()

    async def disconnect(self) -> None:
        self._connected = False
        self._state = SIPState.DISCONNECTED
        if self._listener_task:
            self._listener_task.cancel()
        if self._sock:
            self._sock.close()
            self._sock = None
        logger.info("sip disconnected")

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        while self._connected:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield msg
            except asyncio.TimeoutError:
                continue

    async def send(self, msg: OutboundMessage) -> str | None:
        """Initiate an outbound SIP call.

        msg.chat_id should be a SIP URI (e.g. sip:user@host).
        msg.text is TTS text or audio path (passed in metadata for RTP layer).
        """
        if not self._connected or not self._sock:
            raise RuntimeError("sip not connected")

        target_uri = msg.chat_id
        parsed = parse_sip_uri(target_uri)
        self._call_id = str(uuid.uuid4())
        self._cseq = 1

        invite = build_invite_message(
            target_uri,
            self._sip_uri,
            self._username,
            call_id=self._call_id,
            cseq=self._cseq,
            local_port=self._local_port,
        )

        loop = asyncio.get_running_loop()
        await loop.sock_sendto(
            self._sock,
            invite.encode("utf-8"),
            (parsed["host"], int(parsed["port"])),
        )
        self._state = SIPState.INVITING
        self._last_activity = datetime.now()
        return self._call_id

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(
            connected=self._connected,
            channel="sip",
            account_id=self._sip_uri,
            last_activity=self._last_activity,
        )

    async def _listen_loop(self) -> None:
        """Listen for incoming SIP messages on the UDP socket."""
        loop = asyncio.get_running_loop()
        while self._connected and self._sock:
            try:
                data, addr = await asyncio.wait_for(
                    loop.sock_recvfrom(self._sock, 65535),
                    timeout=1.0,
                )
                await self._handle_incoming(data, addr)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                if self._connected:
                    logger.warning("sip listen error: %s", e)

    async def _handle_incoming(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle an incoming SIP message."""
        text = data.decode("utf-8", errors="replace")
        self._last_activity = datetime.now()

        # Check if this is a request (INVITE, BYE, etc.) or response (SIP/2.0 ...)
        first_line = text.split("\r\n", 1)[0] if "\r\n" in text else text.split("\n", 1)[0]

        if first_line.startswith("INVITE"):
            await self._handle_invite(text, addr)
        elif first_line.startswith("BYE"):
            self._state = SIPState.IDLE
            logger.info("sip: received BYE from %s", addr)
        elif first_line.startswith("ACK"):
            if self._state == SIPState.RINGING:
                self._state = SIPState.IN_CALL
        elif first_line.startswith("SIP/2.0"):
            # Response to our request
            resp = _parse_sip_response(data)
            status = resp["status_code"]
            if status == 180:
                self._state = SIPState.RINGING
            elif status == 200:
                if self._state in (SIPState.INVITING, SIPState.RINGING):
                    self._state = SIPState.IN_CALL
                    # Send ACK
                    if self._call_id and self._sock:
                        ack = build_ack_message(
                            f"sip:{addr[0]}:{addr[1]}",
                            self._sip_uri,
                            call_id=self._call_id,
                            local_port=self._local_port,
                        )
                        loop = asyncio.get_running_loop()
                        await loop.sock_sendto(self._sock, ack.encode("utf-8"), addr)

    async def _handle_invite(self, text: str, addr: tuple[str, int]) -> None:
        """Handle an incoming SIP INVITE — enqueue as UnifiedMessage."""
        # Extract From header
        caller = "unknown"
        call_id = str(uuid.uuid4())
        for line in text.split("\r\n"):
            if line.lower().startswith("from:"):
                caller = line.split(":", 1)[1].strip()
                # Extract URI from angle brackets if present
                if "<" in caller and ">" in caller:
                    caller = caller[caller.index("<") + 1 : caller.index(">")]
                break
            if line.lower().startswith("call-id:"):
                call_id = line.split(":", 1)[1].strip()

        self._state = SIPState.RINGING
        msg = UnifiedMessage(
            id=call_id,
            channel="sip",
            sender=Identity(id=caller, username=caller, display_name=caller),
            content=MessageContent(type=ContentType.TEXT, text="[incoming call]"),
            chat_id=caller,
            raw=text,
        )
        await self._queue.put(msg)
