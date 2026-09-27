"""Live end-to-end test: the real Python client stack against the REAL v2 server.

This is the first cross-implementation contact over the wire: the GTK
client's real ``NoteesClient`` + ``SyncEngine`` + ``LocalStore`` sync against
the actual TypeScript server process (``apps/server/dist/server.js`` from the
Notees monorepo) — HTTP batch/catch-up plus the WebSocket acceleration path.

Prerequisite (otherwise the module skips cleanly):

    cd $NOTEES_V2_ROOT            # default /etc/periphery/stacks/notees/.worktrees/greenfield-m1/v2
    pnpm --filter @notees/server build

The flow mirrors a two-device sync scenario:

1. spawn the server on an ephemeral port with a fixed API key (fresh data dir);
2. Device A locally applies + enqueues a small workspace (two classes with an
   extends edge, a page with two blocks — one carrying a mention token — and a
   property value) and pushes it over HTTP;
3. Device B catches up — B's store dump must equal A's for every derived table;
4. the server-side object API (``/api/v1/objects``, ``/api/v1/classes``) must
   agree with the client stores on names, node types, parents, classIds, and
   parentClassIds — three-way equality;
5. Device B subscribes over WS; Device A pushes another block — B must receive
   it through the live ops frame;
6. a fresh Device C re-pulls the whole log from seq 0 and must converge to the
   identical dump (replay determinism against the live server log).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import wait_until

from notees_gtk.core.api import NoteesClient
from notees_gtk.core.protocol.clock import Clock
from notees_gtk.core.protocol.models import new_envelope
from notees_gtk.core.sync import SyncEngine
from notees_gtk.data.store import LocalStore

V2_ROOT = Path(os.environ.get("NOTEES_V2_ROOT", "/etc/periphery/stacks/notees/.worktrees/greenfield-m1/v2"))
SERVER_JS = V2_ROOT / "apps" / "server" / "dist" / "server.js"

pytestmark = [
    pytest.mark.skipif(
        not SERVER_JS.exists(), reason="monorepo v2 server not built (run: pnpm --filter @notees/server build)"
    ),
    pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available"),
]

#: Fixed API key (nk_ + 32 chars); the actor id is derived from it server-side.
API_KEY = "nk_" + "k" * 32
BASE_URL_SUFFIX = "notees:workspace:default"


def _derive_uuid(namespace: str) -> str:
    """Port of the server's deriveUuid (sha256, uuid5 layout, version/variant bits)."""
    digest = bytearray(hashlib.sha256(namespace.encode()).digest())
    digest[6] = (digest[6] & 0x0F) | 0x50
    digest[8] = (digest[8] & 0x3F) | 0x80
    hex_str = bytes(digest[:16]).hex()
    return f"{hex_str[0:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:32]}"


WORKSPACE_ID = _derive_uuid(BASE_URL_SUFFIX)
ACTOR_ID = _derive_uuid(f"notees:actor:{API_KEY}")

#: Fixed node ids (a fresh server data dir per test run keeps them unique).
CLASS_SOURCE = "0192b000-0000-7000-8000-0000000000a1"
CLASS_ANNOTATED = "0192b000-0000-7000-8000-0000000000a2"
PAGE_MAIN = "0192b000-0000-7000-8000-0000000000b1"
PAGE_TARGET = "0192b000-0000-7000-8000-0000000000b2"
BLOCK_ONE = "0192b000-0000-7000-8000-0000000000c1"
BLOCK_TWO = "0192b000-0000-7000-8000-0000000000c2"
BLOCK_LIVE = "0192b000-0000-7000-8000-0000000000c3"
PROPERTY_SCHEMA = "0192b000-0000-7000-8000-0000000000d1"

#: Every derived-state table the two client stores must converge on.
#: (relay_operations.applied_at and sync_watermark are per-device bookkeeping
#: and legitimately differ; relay_outbox must simply be empty on both.)
DUMP_TABLES = (
    "nodes",
    "node_child_order",
    "class_member_set",
    "class_extends",
    "class_hierarchy",
    "class",
    "property_schema",
    "property_value",
    "property_value_tombstone",
    "node_asset",
    "collection_member",
    "trash",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_healthy(proc: subprocess.Popen[str], url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout is not None else ""
            pytest.fail(f"server exited early (code {proc.returncode}):\n{output}")
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.1)
    pytest.fail(f"server did not become healthy within {timeout}s: {last_error}")


@pytest.fixture
def server_url(tmp_path: Path) -> Iterator[str]:
    """Spawn the real v2 server on an ephemeral port; kill it in teardown."""
    port = _free_port()
    env = {
        **os.environ,
        "NOTEES_DATA_DIR": str(tmp_path / "server-data"),
        "NOTEES_PORT": str(port),
        "NOTEES_HOST": "127.0.0.1",
        "NOTEES_API_KEY": API_KEY,
        "NOTEES_LOG": "false",
    }
    proc = subprocess.Popen(
        ["node", str(SERVER_JS)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_until_healthy(proc, f"http://127.0.0.1:{port}/healthz")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()  # SIGTERM → the server shuts down cleanly.
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@dataclass
class Device:
    """One client instance: real REST client + engine + store on a temp dir."""

    name: str
    client: NoteesClient
    store: LocalStore
    engine: SyncEngine
    clock: Clock
    device_id: str

    def produce(self, op_type: str, payload: dict[str, Any], *, affected: tuple[str, ...] = ()) -> None:
        """Local-apply + enqueue, mirroring the editor's optimistic flow."""
        envelope = new_envelope(
            workspace_id=WORKSPACE_ID,
            actor_id=ACTOR_ID,
            device_id=self.device_id,
            op_type=op_type,
            payload=payload,
            clock=self.clock,
            client="gtk",
            affected_node_ids=affected,
        )
        self.store.apply_remote(envelope)
        self.store.enqueue(envelope)

    def close(self) -> None:
        self.engine.stop_realtime()
        self.store.close()


@pytest.fixture
def device_factory(server_url: str, tmp_path: Path) -> Iterator[Any]:
    devices: list[Device] = []

    def make_device(name: str) -> Device:
        directory = tmp_path / name
        directory.mkdir()
        client = NoteesClient(server_url, api_key=API_KEY)
        store = LocalStore(directory / "store.db")
        clock = Clock(device_id=f"{name}-device")
        engine = SyncEngine(client, store, actor_id=ACTOR_ID, workspace_id=WORKSPACE_ID, clock=clock)
        device = Device(name, client, store, engine, clock, f"{name}-device")
        devices.append(device)
        return device

    yield make_device
    for device in devices:
        device.close()


def _store_dump(store: LocalStore) -> dict[str, list[tuple[str, ...]]]:
    """All derived-state tables, rows stringified and sorted (order-independent)."""
    path = str(store._conn.execute("PRAGMA database_list").fetchone()[2])  # noqa: SLF001
    dump: dict[str, list[tuple[str, ...]]] = {}
    with sqlite3.connect(path) as raw:
        for table in DUMP_TABLES:
            rows = raw.execute(f'SELECT * FROM "{table}"').fetchall()
            dump[table] = sorted(tuple(str(value) for value in row) for row in rows)
    return dump


def _seed_device_a(device: Device) -> None:
    """The shared workspace: classes + extends, a page with two blocks (one
    carrying a mention token), class membership, and a property value."""
    device.produce("class.create", {"classId": CLASS_SOURCE, "name": "Source"}, affected=(CLASS_SOURCE,))
    device.produce("class.create", {"classId": CLASS_ANNOTATED, "name": "Annotated"}, affected=(CLASS_ANNOTATED,))
    device.produce(
        "class.setExtends",
        {"classId": CLASS_ANNOTATED, "parentClassIds": [CLASS_SOURCE]},
        affected=(CLASS_ANNOTATED,),
    )
    device.produce(
        "object.create",
        {"objectId": PAGE_MAIN, "nodeType": "page", "name": "Live Page", "classIds": [CLASS_SOURCE], "parentId": None},
        affected=(PAGE_MAIN,),
    )
    device.produce(
        "object.create",
        {"objectId": PAGE_TARGET, "nodeType": "page", "name": "Target Page", "parentId": None},
        affected=(PAGE_TARGET,),
    )
    device.produce(
        "object.create",
        {
            "objectId": BLOCK_ONE,
            "nodeType": "block",
            "parentId": PAGE_MAIN,
            "contentAst": [
                {"type": "text", "text": "See "},
                {"type": "mention", "targetNodeId": PAGE_TARGET, "text": "Target Page"},
                {"type": "text", "text": " for context."},
            ],
        },
        affected=(BLOCK_ONE, PAGE_TARGET),
    )
    device.produce(
        "object.create",
        {
            "objectId": BLOCK_TWO,
            "nodeType": "block",
            "parentId": PAGE_MAIN,
            "contentAst": [{"type": "text", "text": "Second block."}],
        },
        affected=(BLOCK_TWO,),
    )
    device.produce(
        "propertySchema.create",
        {"propertySchemaId": PROPERTY_SCHEMA, "name": "Status", "type": "select"},
    )
    device.produce(
        "property.set",
        {
            "objectId": PAGE_MAIN,
            "propertySchemaId": PROPERTY_SCHEMA,
            "value": {"label": "Open"},
            "idx": 0,
            "metadata": {"since": "2026"},
        },
        affected=(PAGE_MAIN,),
    )


def test_live_server_end_to_end(server_url: str, device_factory: Any) -> None:
    # --- 1+2: Device A seeds the workspace locally and pushes over HTTP. ---
    device_a = device_factory("device-a")
    _seed_device_a(device_a)
    push = device_a.engine.push()
    assert push.quarantined == 0
    assert push.sent == 9

    # --- 3: Device B catches up; both stores must converge byte-for-byte. ---
    device_b = device_factory("device-b")
    pull = device_b.engine.pull()
    assert pull.applied == 9
    assert device_b.engine.realtime_latest_seq is None
    assert _store_dump(device_a.store) == _store_dump(device_b.store)
    page_row = device_b.store.node(WORKSPACE_ID, PAGE_MAIN)
    assert page_row is not None
    assert page_row.name == "Live Page"
    assert page_row.class_ids == (CLASS_SOURCE,)
    block_one = device_b.store.node(WORKSPACE_ID, BLOCK_ONE)
    assert block_one is not None and block_one.parent_id == PAGE_MAIN
    assert block_one.content_plain == "See Target Page for context."

    # --- 4: the server's object API agrees — three-way equality. ---
    main = device_a.client._get_json(f"/api/v1/objects/{PAGE_MAIN}")["object"]
    assert main["nodeType"] == "page"
    assert main["name"] == "Live Page"
    assert main["classIds"] == [CLASS_SOURCE]
    assert main["parentId"] is None
    block = device_a.client._get_json(f"/api/v1/objects/{BLOCK_ONE}")["object"]
    assert block["nodeType"] == "block"
    assert block["parentId"] == PAGE_MAIN
    assert block["contentAst"] == [
        {"type": "text", "text": "See "},
        {"type": "mention", "targetNodeId": PAGE_TARGET, "text": "Target Page"},
        {"type": "text", "text": " for context."},
    ]
    pages = device_a.client._get_json("/api/v1/objects", params={"nodeType": "page"})["objects"]
    page_ids = {entry["id"] for entry in pages}
    assert {PAGE_MAIN, PAGE_TARGET} <= page_ids
    annotated = device_a.client._get_json(f"/api/v1/classes/{CLASS_ANNOTATED}")["class"]
    assert annotated["name"] == "Annotated"
    assert annotated["parentClassIds"] == [CLASS_SOURCE]
    # classIds OR-Set membership: PAGE_MAIN joined Source (Annotated has none).
    source = device_a.client._get_json(f"/api/v1/classes/{CLASS_SOURCE}")
    assert {entry["id"] for entry in source["members"]} == {PAGE_MAIN}
    assert device_a.client._get_json(f"/api/v1/classes/{CLASS_ANNOTATED}")["members"] == []

    # --- 5: realtime — B subscribes, A pushes a new block over HTTP. ---
    device_b.engine.start_realtime()
    assert wait_until(lambda: device_b.engine.realtime_latest_seq is not None, timeout=10)
    device_a.produce(
        "object.create",
        {
            "objectId": BLOCK_LIVE,
            "nodeType": "block",
            "parentId": PAGE_MAIN,
            "contentAst": [{"type": "text", "text": "Arrived over the socket."}],
        },
        affected=(BLOCK_LIVE,),
    )
    live_push = device_a.engine.push()
    assert live_push.sent == 1
    assert wait_until(lambda: device_b.store.node(WORKSPACE_ID, BLOCK_LIVE) is not None, timeout=10)
    live_row = device_b.store.node(WORKSPACE_ID, BLOCK_LIVE)
    assert live_row is not None and live_row.content_plain == "Arrived over the socket."
    device_b.engine.stop_realtime()

    # The live frame converged to the same state as the push path.
    assert _store_dump(device_a.store) == _store_dump(device_b.store)

    # --- 6: a fresh Device C replays the whole log from seq 0. ---
    device_c = device_factory("device-c")
    replay = device_c.engine.pull()
    assert replay.applied == 10
    assert _store_dump(device_a.store) == _store_dump(device_c.store)
