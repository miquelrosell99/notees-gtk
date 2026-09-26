"""Realtime WebSocket client — the relay's acceleration path (WIRE.md §2).

Built on the ``websockets`` library's SYNCHRONOUS client API so it fits this
repo's threading model: the engine runs on worker threads with a blocking
REST client, so the realtime reader runs on its own daemon thread with a
blocking ``recv()`` loop rather than an asyncio event loop.

Framing contract (v2):

- Server → client ``hello`` (``wsProtocolVersion``/``restoreEpoch``/
  ``latestSeq``), ``ops`` (``envelopes`` + ``seqs`` map), ``ack``
  (``savedIds``), ``error`` (``message``); unknown frame types are ignored.
- A ``hello``/``ops`` with a NEWER framing version fails loud:
  :class:`ProtocolVersionError` via the error callback, the socket is closed
  and the client NEVER reconnects (silently applying newer framing is the
  failure mode WIRE.md §2 forbids).
- Malformed JSON answers the error callback and keeps the connection.
- Abnormal closes reconnect on the backoff schedule [1s, 2s, 5s, 10s, 30s],
  reset after a successful ``hello``; :meth:`stop` closes cleanly (code 1000)
  and never reconnects.

The socket is only an accelerator: the seq cursor on catch-up responses stays
the authoritative recovery mechanism — :attr:`latest_seq`/
:attr:`restore_epoch` expose the last hello so the engine can decide when a
catch-up pull is needed after a reconnect.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import quote

from pydantic import ValidationError
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import ClientConnection, connect

from notees_gtk.core.api.client import NoteesClient
from notees_gtk.core.protocol.models import (
    WS_PROTOCOL_VERSION,
    RelayEnvelope,
    WsBatchMessage,
)

__all__ = [
    "DEFAULT_RECONNECT_DELAYS",
    "HelloInfo",
    "ProtocolVersionError",
    "RealtimeClient",
    "RealtimeProtocolError",
    "build_ws_url",
]

_log = logging.getLogger(__name__)

#: Default reconnect backoff (seconds) after abnormal closes.
DEFAULT_RECONNECT_DELAYS: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)


class ProtocolVersionError(RuntimeError):
    """The relay speaks a NEWER WS framing version: fail loud, never reconnect."""


class RealtimeProtocolError(RuntimeError):
    """A malformed frame or envelope from the relay (connection stays open)."""


@dataclass(frozen=True)
class HelloInfo:
    """Server greeting metadata (advertised again on every reconnect)."""

    latest_seq: int
    restore_epoch: int


def build_ws_url(base_url: str, workspace_id: str, token: str) -> str:
    """Build the relay WS URL from the REST base URL (``http(s)`` → ``ws(s)``)."""
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/api/relay/v2/ws/{quote(workspace_id, safe='')}?token={quote(token, safe='')}"


class RealtimeClient:
    """Threaded realtime subscriber for one workspace.

    Args:
        client: REST client supplying the base URL and the WS credential
            (:meth:`NoteesClient.ws_token` — the API key, or the bearer token
            as fallback). The token is REQUIRED: without a credential the
            relay rejects the handshake.
        workspace_id: Workspace to subscribe to.
        on_hello: Called with :class:`HelloInfo` on every (re)connect, after
            the framing version validated.
        on_ops: Called with ``(envelopes, seqs)`` for committed batches
            broadcast by the relay.
        on_ack: Called with the saved ids of a batch submitted over the socket.
        on_error: Called with every protocol/connection error; a raising
            callback is logged and never kills the reader thread.
        reconnect_delays: Backoff schedule (seconds) for abnormal closes.

    Lifecycle: :meth:`start` spawns the daemon reader thread; :meth:`stop`
    closes the socket cleanly (code 1000), interrupts a pending backoff, joins
    the reader, and never reconnects.
    """

    def __init__(
        self,
        client: NoteesClient,
        workspace_id: str,
        *,
        on_hello: Callable[[HelloInfo], None] | None = None,
        on_ops: Callable[[list[RelayEnvelope], dict[str, int]], None] | None = None,
        on_ack: Callable[[list[str]], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        reconnect_delays: Sequence[float] = DEFAULT_RECONNECT_DELAYS,
    ) -> None:
        token = client.ws_token()
        if not token:
            raise ValueError("RealtimeClient: no API key or bearer token available for the WS handshake")
        self._url = build_ws_url(client.base_url, workspace_id, token)
        self._on_hello_cb = on_hello
        self._on_ops_cb = on_ops
        self._on_ack_cb = on_ack
        self._on_error_cb = on_error
        self._delays = tuple(reconnect_delays)
        self._stopped = threading.Event()
        self._send_lock = threading.Lock()
        self._conn: ClientConnection | None = None
        self._thread: threading.Thread | None = None
        self._attempt = 0
        self._failed = False
        self._hello_latest_seq = 0
        self._hello_restore_epoch = 0

    # ------------------------------------------------------------------ state

    @property
    def latest_seq(self) -> int:
        """Highest server seq advertised by the last ``hello``."""
        return self._hello_latest_seq

    @property
    def restore_epoch(self) -> int:
        """Restore epoch advertised by the last ``hello``."""
        return self._hello_restore_epoch

    @property
    def failed(self) -> bool:
        """True after a fail-loud framing rejection (never reconnects)."""
        return self._failed

    @property
    def running(self) -> bool:
        """True between :meth:`start` and :meth:`stop`."""
        return self._thread is not None and self._thread.is_alive()

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Start the daemon reader thread (idempotent while running)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopped.clear()
        self._thread = threading.Thread(target=self._run, name="notees-ws-reader", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the stream: clean close (code 1000), interrupt backoff, join.

        Never reconnects. Safe to call more than once and from a different
        thread than the reader.
        """
        self._stopped.set()
        with self._send_lock:
            conn = self._conn
        if conn is not None:
            with contextlib.suppress(OSError, WebSocketException):  # Already closing/closed.
                conn.close(code=1000)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    # -------------------------------------------------------------------- send

    def send_batch(self, envelopes: list[RelayEnvelope]) -> bool:
        """Submit a batch over the socket (the acceleration path).

        Returns ``True`` when the frame was written; ``False`` when no socket
        is open (callers fall back to HTTP push, which stays authoritative).
        """
        frame = WsBatchMessage(envelopes=envelopes).model_dump(mode="json", by_alias=True)
        with self._send_lock:
            conn = self._conn
            if conn is None:
                return False
            try:
                conn.send(json.dumps(frame))
            except (OSError, WebSocketException):
                return False
        return True

    # ----------------------------------------------------------------- reader

    def _run(self) -> None:
        """Connect → read frames → dispatch, reconnecting on abnormal closes."""
        while not self._stopped.is_set() and not self._failed:
            try:
                with connect(self._url) as conn:
                    with self._send_lock:
                        self._conn = conn
                    while True:
                        self._handle_frame(conn.recv())
            except ConnectionClosed:
                pass  # Abnormal or clean close: fall through to reconnect.
            except (OSError, WebSocketException) as exc:
                self._emit_error(exc)
            finally:
                with self._send_lock:
                    self._conn = None
            if self._stopped.is_set() or self._failed:
                break
            delay = self._delays[min(self._attempt, len(self._delays) - 1)]
            self._attempt += 1
            _log.info("Realtime reconnecting in %.1fs (attempt %d)", delay, self._attempt)
            self._stopped.wait(delay)  # Interruptible: stop() wakes this immediately.

    def _handle_frame(self, raw: object) -> None:
        """Parse and dispatch one frame; malformed frames answer the error
        callback and keep the connection (WIRE.md §2 unknown-frame semantics)."""
        try:
            if not isinstance(raw, str):
                raise RealtimeProtocolError(f"frame is not text: {type(raw).__name__}")
            frame = json.loads(raw)
            if not isinstance(frame, dict):
                raise RealtimeProtocolError("frame is not a JSON object")
        except (ValueError, RealtimeProtocolError) as exc:
            self._emit_error(RealtimeProtocolError(f"malformed frame: {exc}"))
            return

        frame_type = frame.get("type")
        if frame_type == "hello":
            self._on_hello_frame(frame)
        elif frame_type == "ops":
            self._on_ops_frame(frame)
        elif frame_type == "ack":
            saved_ids = frame.get("savedIds")
            if self._on_ack_cb is not None and isinstance(saved_ids, list):
                self._emit(self._on_ack_cb, [str(item) for item in saved_ids])
        elif frame_type == "error":
            message = frame.get("message")
            self._emit_error(RuntimeError(message if isinstance(message, str) else "relay error"))
        # Unknown frame types are ignored (WIRE.md §2).

    def _on_hello_frame(self, frame: dict[str, object]) -> None:
        if self._check_framing_version(frame):
            return
        self._attempt = 0  # Successful handshake: reset the backoff schedule.
        self._hello_latest_seq = _int_or_zero(frame.get("latestSeq"))
        self._hello_restore_epoch = _int_or_zero(frame.get("restoreEpoch"))
        if self._on_hello_cb is not None:
            self._emit(
                self._on_hello_cb, HelloInfo(latest_seq=self._hello_latest_seq, restore_epoch=self._hello_restore_epoch)
            )

    def _on_ops_frame(self, frame: dict[str, object]) -> None:
        if self._check_framing_version(frame):
            return
        raw_envelopes = frame.get("envelopes")
        envelopes: list[RelayEnvelope] = []
        if isinstance(raw_envelopes, list):
            for raw_env in raw_envelopes:
                try:
                    envelopes.append(RelayEnvelope.model_validate(raw_env))
                except ValidationError as exc:
                    self._emit_error(RealtimeProtocolError(f"invalid envelope in ops frame: {exc}"))
                    return  # Drop the frame; the connection stays.
        raw_seqs = frame.get("seqs")
        seqs = (
            {str(key): int(value) for key, value in raw_seqs.items() if isinstance(value, int)}
            if isinstance(raw_seqs, dict)
            else {}
        )
        if self._on_ops_cb is not None:
            self._emit(self._on_ops_cb, envelopes, seqs)

    def _check_framing_version(self, frame: dict[str, object]) -> bool:
        """Fail loud on a newer framing version; True when the frame was rejected."""
        version = frame.get("wsProtocolVersion")
        if not isinstance(version, int) or version <= WS_PROTOCOL_VERSION:
            return False
        self._failed = True
        self._emit_error(
            ProtocolVersionError(f"relay WS framing version {version} is newer than supported {WS_PROTOCOL_VERSION}")
        )
        with self._send_lock:
            conn = self._conn
        if conn is not None:
            with contextlib.suppress(OSError, WebSocketException):
                conn.close(code=1000)
        return True

    # --------------------------------------------------------------- callbacks

    def _emit_error(self, error: BaseException) -> None:
        if self._on_error_cb is not None:
            self._emit(self._on_error_cb, error)

    def _emit(self, callback: Callable[..., None], *args: object) -> None:
        """Invoke a consumer callback; a raising consumer never kills the reader."""
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 — consumer bugs must not kill the stream
            _log.exception("Realtime callback %r raised; continuing", callback)


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) else 0
