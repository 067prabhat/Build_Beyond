"""LangGraph checkpoint saver backed by the SAP Agent Memory Service.

Copied from agent-commons-sdk hana_memory_store/store.py.

Uses ``sap_cloud_sdk.agent_memory`` for storage. Sync methods call the SDK
client directly; async methods delegate via ``asyncio.to_thread``.

Checkpoints and pending writes are stored as Memory records using:
  - ``agent_id``   — identifies the LangGraph application (≤36 chars)
  - ``invoker_id`` — identifies the conversation thread (≤36 chars; long IDs are SHA-1 hashed)
  - ``content``    — human-readable record key (``_lg_checkpoint:…`` / ``_lg_write:…``)
  - ``metadata``   — all serialized state (base64-encoded msgpack blobs)
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import logging
import time
from collections import defaultdict
from typing import Any, AsyncIterator, Iterator, Optional, Sequence

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    SerializerProtocol,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sap_cloud_sdk.agent_memory import AgentMemoryClient, AgentMemoryConfig, FilterDefinition, Memory, create_client

# ── Inlined from negotiation-agent's app/utils.py ────────────────────────
# We inline these two helpers instead of importing from app.utils so this file
# has no external dependency on that module. DO NOT change the hash algorithm
# or _INVOKER_ID_HASH_LEN — every existing HANA record is partitioned by the
# output of to_invoker_id(), so any change silently orphans stored data.
import hashlib as _hashlib

_MAX_ID_LEN            = 36    # MAX_INVOKER_ID_LEN
_INVOKER_ID_HASH_LEN   = 36    # SHA-1 hex-digest slice length


def to_invoker_id(key: str) -> str:
    """Derive a stable invoker_id (≤36 chars) from a natural key.

    SHA-1 is used as a compact non-cryptographic hash for storage keying only
    (not security — usedforsecurity=False). 36 hex chars = 144 bits; collision
    probability is negligible at expected thread volumes.
    """
    if len(key) <= _INVOKER_ID_HASH_LEN:
        return key
    return _hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:_INVOKER_ID_HASH_LEN]

# ─────────────────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

_CHECKPOINT = "checkpoint"
_WRITE = "write"
_CHUNK = "chunk"
_PAGE_SIZE = 100
# 64 KB per chunk leaves headroom for the JSON envelope around the base64 blob
_CHUNK_SIZE = 65_536
# Shared across aput_writes and _aprune_old_records — keep well below the urllib3
# pool size (10) so parallel graph nodes can't exhaust it.
_WRITE_CONCURRENCY = 5


def _encode(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(gzip.compress(data)).decode("ascii")


def _decode(data: str) -> bytes:
    raw = base64.b64decode(data.encode("ascii"))
    # Transparently handle records written before compression was added.
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


class HanaMemorySaver(BaseCheckpointSaver):
    """LangGraph checkpointer backed by the SAP Agent Memory Service.

    Sync methods call ``sap_cloud_sdk.agent_memory.AgentMemoryClient`` directly.
    Async methods delegate via ``asyncio.to_thread`` so they never block the
    event loop.

    Usage (async — recommended for async runtimes)::

        async with HanaMemorySaver(agent_id="my-app") as saver:
            graph = builder.compile(checkpointer=saver)
            await graph.ainvoke(
                {"messages": [{"role": "user", "content": "hi"}]},
                {"configurable": {"thread_id": "1"}},
            )

    Usage (sync)::

        with HanaMemorySaver(agent_id="my-app") as saver:
            graph = builder.compile(checkpointer=saver)
            graph.invoke(
                {"messages": [{"role": "user", "content": "hi"}]},
                {"configurable": {"thread_id": "1"}},
            )

    Note on ``invoker_id``: it is derived from ``thread_id`` only (key-scoped).
    ``configurable["invoker_id"]`` is not read; user identity is not factored in.
    Two users sharing the same ``thread_id`` would map to the same HANA partition.
    In practice ``thread_id`` is a per-conversation A2A context ID so collisions
    are not expected, but the isolation is key-based, not user-based.

    Args:
        agent_id: Identifier for this LangGraph application (max 36 characters).
        config: Optional explicit SAP Agent Memory config. If ``None``,
            credentials are auto-detected from the BTP service binding volume
            or ``CLOUD_SDK_CFG_AGENT_MEMORY_DEFAULT_*`` environment variables.
        serde: Optional custom serializer. Defaults to ``JsonPlusSerializer``.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        config: Optional[AgentMemoryConfig] = None,
        serde: Optional[SerializerProtocol] = None,
    ) -> None:
        if len(agent_id) > _MAX_ID_LEN:
            raise ValueError(f"agent_id must be at most {_MAX_ID_LEN} characters, got {len(agent_id)}")
        super().__init__(serde=serde or JsonPlusSerializer())
        self.agent_id = agent_id
        self._sdk_config = config
        self._client: Optional[AgentMemoryClient] = None
        # Global semaphore shared across all async write/delete calls on this instance.
        # Caps total concurrent HTTP connections regardless of parallel graph nodes.
        self._http_sem = asyncio.Semaphore(_WRITE_CONCURRENCY)

    # ── context managers ───────────────────────────────────────────────────────

    def __enter__(self) -> "HanaMemorySaver":
        self._client = create_client(config=self._sdk_config)
        return self

    def __exit__(self, *_: Any) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    async def __aenter__(self) -> "HanaMemorySaver":
        return self.__enter__()

    async def __aexit__(self, *_: Any) -> None:
        self.__exit__(*_)

    # ── client access ──────────────────────────────────────────────────────────

    def _get_client(self) -> AgentMemoryClient:
        if self._client is None:
            self._client = create_client(config=self._sdk_config)
        return self._client

    def probe_client(self) -> AgentMemoryClient:
        """Return the SDK client, creating it if needed. Use to verify connectivity."""
        return self._get_client()

    # ── serialization helpers ──────────────────────────────────────────────────

    def _encode_obj(self, obj: Any) -> tuple[str, str]:
        type_, raw = self.serde.dumps_typed(obj)
        return type_, _encode(raw)

    def _decode_obj(self, type_: str, data: str) -> Any:
        return self.serde.loads_typed((type_, _decode(data)))

    # ── content key helpers ────────────────────────────────────────────────────

    @staticmethod
    def _checkpoint_content(checkpoint_ns: str, checkpoint_id: str) -> str:
        return f"_lg_checkpoint:{checkpoint_ns}:{checkpoint_id}"

    @staticmethod
    def _write_content(checkpoint_ns: str, checkpoint_id: str, task_id: str, idx: int) -> str:
        return f"_lg_write:{checkpoint_ns}:{checkpoint_id}:{task_id}:{idx}"

    @staticmethod
    def _chunk_content(checkpoint_ns: str, checkpoint_id: str, idx: int) -> str:
        return f"_lg_chunk:{checkpoint_ns}:{checkpoint_id}:{idx}"

    def _prune_old_records(
        self, client: AgentMemoryClient, invoker_id: str, checkpoint_ns: str, current_checkpoint_id: str, parent_checkpoint_id: Optional[str]
    ) -> None:
        """Delete checkpoint and chunk records for the previous checkpoint only.

        Uses a content-substring filter so only O(1) records are fetched instead
        of the full thread history — avoids an expensive list-all on every put.
        Falls back to full-scan when parent_checkpoint_id is unknown.
        """
        if not parent_checkpoint_id:
            return
        try:
            t0 = time.monotonic()
            # Filter to records whose content key contains the parent checkpoint ID.
            # Content keys are _lg_checkpoint:{ns}:{id} and _lg_chunk:{ns}:{id}:{idx},
            # so this returns only the stale checkpoint + its chunks — typically 1–3 records.
            stale = client.list_memories(
                self.agent_id,
                invoker_id,
                filters=[FilterDefinition(target="content", contains=parent_checkpoint_id)],
                limit=_PAGE_SIZE,
            )
            deleted = 0
            for m in stale:
                md = m.metadata or {}
                if md.get("_checkpoint_ns") != checkpoint_ns:
                    continue
                if md.get("_record_type") not in (_CHECKPOINT, _CHUNK, _WRITE):
                    continue
                if md.get("_checkpoint_id") == current_checkpoint_id:
                    continue
                if m.id:
                    client.delete_memory(m.id)
                    deleted += 1
            logger.debug("_prune_old_records: deleted=%d elapsed=%.0fms", deleted, (time.monotonic() - t0) * 1000)
        except Exception as e:
            logger.warning("Failed to prune old checkpoint records: %s", e)

    async def _aprune_old_records(
        self, invoker_id: str, checkpoint_ns: str, current_checkpoint_id: str, parent_checkpoint_id: Optional[str]
    ) -> None:
        """Async version of _prune_old_records — deletes stale records in parallel."""
        if not parent_checkpoint_id:
            return
        try:
            t0 = time.monotonic()
            client = self._get_client()
            stale = await asyncio.to_thread(
                client.list_memories,
                self.agent_id,
                invoker_id,
                filters=[FilterDefinition(target="content", contains=parent_checkpoint_id)],
                limit=_PAGE_SIZE,
            )
            to_delete = [
                m.id for m in stale
                if m.id
                and (m.metadata or {}).get("_checkpoint_ns") == checkpoint_ns
                and (m.metadata or {}).get("_record_type") in (_CHECKPOINT, _CHUNK, _WRITE)
                and (m.metadata or {}).get("_checkpoint_id") != current_checkpoint_id
            ]
            if to_delete:
                async def _delete_one(mid: str) -> None:
                    async with self._http_sem:
                        await asyncio.to_thread(client.delete_memory, mid)

                await asyncio.gather(*[_delete_one(mid) for mid in to_delete])
            logger.debug("_aprune_old_records: deleted=%d elapsed=%.0fms", len(to_delete), (time.monotonic() - t0) * 1000)
        except Exception as e:
            logger.warning("Failed to prune old checkpoint records: %s", e)

    # ── fetch helpers ──────────────────────────────────────────────────────────

    def _fetch_thread_memories(self, client: AgentMemoryClient, invoker_id: str) -> list[Memory]:
        """Fetch all LG memories for agent_id + invoker_id, newest first."""
        all_memories: list[Memory] = []
        offset = 0
        while True:
            batch = client.list_memories(self.agent_id, invoker_id, limit=_PAGE_SIZE, offset=offset)
            all_memories.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break
            offset += len(batch)
        all_memories.sort(key=lambda m: m.create_timestamp or "", reverse=True)
        return all_memories

    def _split_memories(
        self, memories: list[Memory], checkpoint_ns: str
    ) -> tuple[list[Memory], dict[str, list[Memory]], dict[str, list[Memory]]]:
        checkpoints: list[Memory] = []
        writes_by_cp: dict[str, list[Memory]] = defaultdict(list)
        chunks_by_cp: dict[str, list[Memory]] = defaultdict(list)
        for m in memories:
            md = m.metadata
            if not md or md.get("_checkpoint_ns") != checkpoint_ns:
                continue
            rec_type = md.get("_record_type")
            if rec_type == _CHECKPOINT:
                checkpoints.append(m)
            elif rec_type == _WRITE:
                writes_by_cp[md.get("_checkpoint_id", "")].append(m)
            elif rec_type == _CHUNK:
                chunks_by_cp[md.get("_checkpoint_id", "")].append(m)
        for cp_id in writes_by_cp:
            writes_by_cp[cp_id].sort(key=lambda m: m.metadata.get("_idx", 0))  # type: ignore[union-attr]
        for cp_id in chunks_by_cp:
            chunks_by_cp[cp_id].sort(key=lambda m: m.metadata.get("_chunk_idx", 0))  # type: ignore[union-attr]
        return checkpoints, writes_by_cp, chunks_by_cp

    def _build_writes(self, write_mems: list[Memory]) -> list[tuple[str, str, Any]]:
        result = []
        for m in write_mems:
            md = m.metadata
            if not md:
                continue
            channel_value = self._decode_obj(md["_type"], md["_data"])
            result.append((md["_task_id"], channel_value[0], channel_value[1]))
        return result

    def _memory_to_tuple(self, memory: Memory, writes: list[tuple[str, str, Any]], chunks: list[Memory]) -> CheckpointTuple:
        md = memory.metadata or {}
        thread_id: str = md["_thread_id"]
        checkpoint_ns: str = md["_checkpoint_ns"]
        checkpoint_id: str = md["_checkpoint_id"]
        parent_checkpoint_id: Optional[str] = md.get("_parent_checkpoint_id")

        if md.get("_chunked"):
            cp_data = "".join(c.metadata["_chunk_data"] for c in chunks)  # type: ignore[index]
        else:
            cp_data = md["_checkpoint_data"]

        checkpoint: Checkpoint = self._decode_obj(md["_checkpoint_type"], cp_data)
        metadata: CheckpointMetadata = self._decode_obj(md["_metadata_type"], md["_metadata_data"])

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_checkpoint_id,
                    }
                }
                if parent_checkpoint_id
                else None
            ),
            pending_writes=writes,
        )

    # ── sync interface ─────────────────────────────────────────────────────────

    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        configurable = config.get("configurable", {})
        thread_id: str = configurable["thread_id"]
        checkpoint_ns: str = configurable.get("checkpoint_ns", "")
        checkpoint_id: Optional[str] = configurable.get("checkpoint_id")
        invoker_id = to_invoker_id(thread_id)

        t0 = time.monotonic()
        client = self._get_client()
        all_memories = self._fetch_thread_memories(client, invoker_id)
        checkpoints, writes_by_cp, chunks_by_cp = self._split_memories(all_memories, checkpoint_ns)

        if checkpoint_id:
            checkpoints = [
                c for c in checkpoints
                if c.metadata and c.metadata.get("_checkpoint_id") == checkpoint_id
            ]
        if not checkpoints:
            logger.debug("get_tuple: no checkpoint found for thread=%s (%.0fms)", thread_id, (time.monotonic() - t0) * 1000)
            return None

        memory = checkpoints[0]  # newest-first from sort
        cp_id: str = memory.metadata["_checkpoint_id"]  # type: ignore[index]
        result = self._memory_to_tuple(memory, self._build_writes(writes_by_cp.get(cp_id, [])), chunks_by_cp.get(cp_id, []))
        logger.debug("get_tuple: thread=%s cp=%s records=%d elapsed=%.0fms", thread_id, cp_id, len(all_memories), (time.monotonic() - t0) * 1000)
        return result

    def _write_checkpoint_sync(
        self,
        client: AgentMemoryClient,
        invoker_id: str,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        parent_checkpoint_id: Optional[str],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
    ) -> None:
        """Write checkpoint and chunk records to HANA (no pruning)."""
        cp_type, cp_data = self._encode_obj(checkpoint)
        meta_type, meta_data = self._encode_obj(metadata)

        if len(cp_data) > _CHUNK_SIZE:
            parts = [cp_data[i:i + _CHUNK_SIZE] for i in range(0, len(cp_data), _CHUNK_SIZE)]
            for idx, part in enumerate(parts):
                client.add_memory(
                    self.agent_id,
                    invoker_id,
                    self._chunk_content(checkpoint_ns, checkpoint_id, idx),
                    metadata={
                        "_record_type": _CHUNK,
                        "_checkpoint_ns": checkpoint_ns,
                        "_checkpoint_id": checkpoint_id,
                        "_chunk_idx": idx,
                        "_chunk_data": part,
                    },
                )
            cp_record_meta: dict[str, Any] = {
                "_record_type": _CHECKPOINT,
                "_thread_id": thread_id,
                "_checkpoint_ns": checkpoint_ns,
                "_checkpoint_id": checkpoint_id,
                "_parent_checkpoint_id": parent_checkpoint_id,
                "_checkpoint_type": cp_type,
                "_chunked": True,
                "_metadata_type": meta_type,
                "_metadata_data": meta_data,
            }
            logger.debug("put: thread=%s cp=%s chunked=%d parts size=%d bytes", thread_id, checkpoint_id, len(parts), len(cp_data))
        else:
            cp_record_meta = {
                "_record_type": _CHECKPOINT,
                "_thread_id": thread_id,
                "_checkpoint_ns": checkpoint_ns,
                "_checkpoint_id": checkpoint_id,
                "_parent_checkpoint_id": parent_checkpoint_id,
                "_checkpoint_type": cp_type,
                "_checkpoint_data": cp_data,
                "_metadata_type": meta_type,
                "_metadata_data": meta_data,
            }
            logger.debug("put: thread=%s cp=%s inline size=%d bytes", thread_id, checkpoint_id, len(cp_data))

        client.add_memory(
            self.agent_id,
            invoker_id,
            self._checkpoint_content(checkpoint_ns, checkpoint_id),
            metadata=cp_record_meta,
        )

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        configurable = config.get("configurable", {})
        thread_id: str = configurable["thread_id"]
        checkpoint_ns: str = configurable.get("checkpoint_ns", "")
        checkpoint_id: str = checkpoint["id"]
        parent_checkpoint_id: Optional[str] = configurable.get("checkpoint_id")
        invoker_id = to_invoker_id(thread_id)

        t0 = time.monotonic()
        client = self._get_client()
        self._write_checkpoint_sync(client, invoker_id, thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, checkpoint, metadata)
        # Prune stale records after the new checkpoint is safely written.
        self._prune_old_records(client, invoker_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id)
        logger.debug("put: thread=%s cp=%s total elapsed=%.0fms", thread_id, checkpoint_id, (time.monotonic() - t0) * 1000)
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        configurable = config.get("configurable", {})
        thread_id: str = configurable["thread_id"]
        checkpoint_ns: str = configurable.get("checkpoint_ns", "")
        checkpoint_id: str = configurable["checkpoint_id"]
        invoker_id = to_invoker_id(thread_id)

        client = self._get_client()
        for idx, (channel, value) in enumerate(writes):
            type_, data = self._encode_obj((channel, value))
            client.add_memory(
                self.agent_id,
                invoker_id,
                self._write_content(checkpoint_ns, checkpoint_id, task_id, idx),
                metadata={
                    "_record_type": _WRITE,
                    "_thread_id": thread_id,
                    "_checkpoint_ns": checkpoint_ns,
                    "_checkpoint_id": checkpoint_id,
                    "_task_id": task_id,
                    "_task_path": task_path,
                    "_idx": idx,
                    "_type": type_,
                    "_data": data,
                },
            )

    def list(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        return iter(self._collect_list(config, filter=filter, before=before, limit=limit))

    def delete_thread(self, thread_id: str) -> None:
        invoker_id = to_invoker_id(thread_id)
        client = self._get_client()
        for memory in self._fetch_thread_memories(client, invoker_id):
            md = memory.metadata or {}
            if md.get("_record_type") in (_CHECKPOINT, _WRITE, _CHUNK) and memory.id:
                client.delete_memory(memory.id)

    # ── shared list implementation ─────────────────────────────────────────────

    def _fetch_all_memories(self, client: AgentMemoryClient, thread_id: Optional[str]) -> list[Memory]:
        """Fetch memories for a single thread or all threads (paginated)."""
        if thread_id:
            return self._fetch_thread_memories(client, to_invoker_id(thread_id))
        all_memories: list[Memory] = []
        offset = 0
        while True:
            batch = client.list_memories(self.agent_id, limit=_PAGE_SIZE, offset=offset)
            all_memories.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break
            offset += len(batch)
        all_memories.sort(key=lambda m: m.create_timestamp or "", reverse=True)
        return all_memories

    def _matches_filter(self, md: dict, filter: dict[str, Any]) -> bool:
        """Return True when the checkpoint metadata satisfies all filter predicates."""
        cp_meta: CheckpointMetadata = self._decode_obj(md["_metadata_type"], md["_metadata_data"])
        return all(cp_meta.get(k) == v for k, v in filter.items())  # type: ignore[union-attr]

    def _collect_list(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> list[CheckpointTuple]:
        configurable = (config or {}).get("configurable", {})
        thread_id: Optional[str] = configurable.get("thread_id")
        checkpoint_ns: str = configurable.get("checkpoint_ns", "")
        before_id: Optional[str] = (before or {}).get("configurable", {}).get("checkpoint_id")

        client = self._get_client()
        all_memories = self._fetch_all_memories(client, thread_id)
        checkpoints, writes_by_cp, chunks_by_cp = self._split_memories(all_memories, checkpoint_ns)

        results: list[CheckpointTuple] = []
        count = 0
        found_before = before_id is None
        for memory in checkpoints:
            md = memory.metadata or {}
            cp_id: str = md.get("_checkpoint_id", "")

            if not found_before:
                if cp_id == before_id:
                    found_before = True
                continue

            if limit is not None and count >= limit:
                break

            if filter and not self._matches_filter(md, filter):
                continue

            results.append(self._memory_to_tuple(memory, self._build_writes(writes_by_cp.get(cp_id, [])), chunks_by_cp.get(cp_id, [])))
            count += 1

        return results

    # ── async wrappers ─────────────────────────────────────────────────────────

    async def aget_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        t0 = time.monotonic()
        result = await asyncio.to_thread(self.get_tuple, config)
        logger.debug("[TIMING] aget_tuple elapsed=%.0fms thread=%s", (time.monotonic() - t0) * 1000, config.get("configurable", {}).get("thread_id", "?"))
        return result

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Async checkpoint write with parallel pruning.

        Writes the checkpoint via the sync put() path (in a thread), then prunes
        stale records for the parent checkpoint asynchronously with parallel deletes.
        This avoids the ~10s sequential-delete bottleneck on threads with many writes.
        """
        t0 = time.monotonic()
        configurable = config.get("configurable", {})
        thread_id: str = configurable["thread_id"]
        checkpoint_id: str = checkpoint["id"]
        parent_checkpoint_id: Optional[str] = configurable.get("checkpoint_id")
        checkpoint_ns: str = configurable.get("checkpoint_ns", "")
        invoker_id = to_invoker_id(thread_id)

        # Write the checkpoint (write-only, pruning handled separately below).
        client = self._get_client()
        await asyncio.to_thread(
            self._write_checkpoint_sync,
            client, invoker_id, thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, checkpoint, metadata,
        )
        write_elapsed = (time.monotonic() - t0) * 1000

        # Prune stale records for the parent checkpoint with parallel deletes.
        await self._aprune_old_records(invoker_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id)
        logger.debug("[TIMING] aput elapsed=%.0fms (write=%.0fms) thread=%s cp=%s", (time.monotonic() - t0) * 1000, write_elapsed, thread_id, checkpoint_id)
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        t0 = time.monotonic()
        configurable = config.get("configurable", {})
        thread_id: str = configurable["thread_id"]
        checkpoint_ns: str = configurable.get("checkpoint_ns", "")
        checkpoint_id: str = configurable["checkpoint_id"]
        invoker_id = to_invoker_id(thread_id)

        # Encode all writes first (CPU-bound, fast)
        encoded = []
        for idx, (channel, value) in enumerate(writes):
            type_, data = self._encode_obj((channel, value))
            encoded.append((idx, channel, type_, data))

        # Fire add_memory calls concurrently, bounded by the shared instance semaphore
        # (_WRITE_CONCURRENCY=5) to prevent pool exhaustion from parallel graph nodes.
        client = self._get_client()

        async def _write_one(idx: int, type_: str, data: str) -> None:
            async with self._http_sem:
                await asyncio.to_thread(
                    client.add_memory,
                    self.agent_id,
                    invoker_id,
                    self._write_content(checkpoint_ns, checkpoint_id, task_id, idx),
                    metadata={
                        "_record_type": _WRITE,
                        "_thread_id": thread_id,
                        "_checkpoint_ns": checkpoint_ns,
                        "_checkpoint_id": checkpoint_id,
                        "_task_id": task_id,
                        "_task_path": task_path,
                        "_idx": idx,
                        "_type": type_,
                        "_data": data,
                    },
                )

        await asyncio.gather(*[_write_one(idx, type_, data) for idx, _, type_, data in encoded])
        logger.debug("[TIMING] aput_writes elapsed=%.0fms thread=%s task=%s writes=%d", (time.monotonic() - t0) * 1000, thread_id, task_id, len(writes))

    async def alist(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for item in await asyncio.to_thread(
            self._collect_list, config, filter=filter, before=before, limit=limit
        ):
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)
