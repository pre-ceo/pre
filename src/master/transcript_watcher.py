"""TranscriptWatcher — per-cwd singleton, asyncio Task 周期 stat+tail, fan-out 增量
行给 SSE 订阅者. 替代各 SSE 连接各自 polling 文件的重复 IO.

设计:
  - WATCHER_REGISTRY: transcript_path -> Watcher (singleton)
  - subscribe() -> (init_state, queue): asyncio.Queue 后续接 {event, ...} dict
  - 没订阅者超 IDLE_CLOSE_SEC 自动关闭, registry 自清
  - session 切换 (inode/ctime 变) 时 emit {event: session_change}, offset 重置
  - 慢消费者 (queue 满) 标 lagged 并踢出, 不阻塞其他订阅者

接口:
  await get_or_create(path, normalizer) -> Watcher
  init, q = await watcher.subscribe()
  evt = await q.get()    # {event: "message"|"session_change"|"lagged", ...}
  watcher.unsubscribe(q)
"""

import asyncio
import json
import os
import time
from typing import Callable, Optional


POLL_INTERVAL = 0.5       # 秒, 500ms 一次 tail
IDLE_CLOSE_SEC = 30.0     # 没订阅 30s 自动关
MAX_READ_BYTES_PER_TICK = 1024 * 1024  # 单次 tail 最多读 1MB
QUEUE_MAX = 1024          # 单订阅者 queue 上限, 满即标慢消费

WATCHER_REGISTRY: dict[str, "TranscriptWatcher"] = {}
_REGISTRY_LOCK = asyncio.Lock()


class TranscriptWatcher:
    """监一份 transcript JSONL 文件, 把新行 fan-out 给订阅者."""

    def __init__(self, transcript_path: str,
                 normalizer: Callable[[dict], Optional[dict]]):
        self.path = transcript_path
        self.normalize = normalizer
        self.subscribers: list[asyncio.Queue] = []
        self.offset = 0
        self.transcript_id = ""
        self._task: Optional[asyncio.Task] = None
        self._last_activity = time.time()

    async def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def subscribe(self, sync_to_eof: bool = True) -> tuple[dict, asyncio.Queue]:
        """加一个订阅. sync_to_eof=True 跳到当前 EOF (跟 SSE backfill 衔接)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self.subscribers.append(q)
        self._last_activity = time.time()
        # 初始 state — 给 SSE handler 用来对齐 watcher_offset
        try:
            st = os.stat(self.path)
            tid = f"{st.st_ino}:{int(st.st_ctime)}"
            size = st.st_size
        except OSError:
            tid = ""
            size = 0
        if sync_to_eof and size > self.offset:
            # 第一个订阅者来时, 把 offset 跳到当前 EOF, 避免重新解析整个文件
            self.offset = size
            self.transcript_id = tid
        await self._ensure_task()
        return ({"transcript_id": tid or self.transcript_id, "offset": self.offset,
                 "size": size}, q)

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self.subscribers.remove(q)
        except ValueError:
            pass
        self._last_activity = time.time()

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                if not self.subscribers:
                    if time.time() - self._last_activity > IDLE_CLOSE_SEC:
                        break
                    continue
                try:
                    await self._tail_once()
                except Exception as e:
                    # 兜底: tail 异常不杀整个 watcher, 留 next tick
                    await self._broadcast({"event": "watcher_error",
                                            "error": f"{type(e).__name__}: {e}"[:200]})
        except asyncio.CancelledError:
            pass
        finally:
            # 清 registry
            for k, v in list(WATCHER_REGISTRY.items()):
                if v is self:
                    WATCHER_REGISTRY.pop(k, None)

    async def _tail_once(self) -> None:
        try:
            st = os.stat(self.path)
        except OSError:
            return
        tid = f"{st.st_ino}:{int(st.st_ctime)}"
        if tid != self.transcript_id:
            self.transcript_id = tid
            self.offset = 0
            await self._broadcast({"event": "session_change",
                                    "transcript_id": tid, "size": st.st_size})
        if st.st_size <= self.offset:
            return
        to_read = min(st.st_size - self.offset, MAX_READ_BYTES_PER_TICK)
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read(to_read)
        except OSError:
            return
        # 按 \n 切; 尾部不完整片段留到下一轮
        nl_idx = chunk.rfind(b"\n")
        if nl_idx < 0:
            return  # 还没满一行
        complete = chunk[: nl_idx + 1]
        consumed = len(complete)
        self.offset += consumed
        try:
            text = complete.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            return
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            norm = self.normalize(obj)
            if norm is None:
                continue
            await self._broadcast({"event": "message", "msg": norm,
                                    "offset": self.offset})

    async def _broadcast(self, evt: dict) -> None:
        dead: list[asyncio.Queue] = []
        for q in list(self.subscribers):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)
            # 留一条 lagged 通知给慢消费者, 让 SSE handler 端能感知并关流
            try:
                # 已 unsubscribe, 但还在 SSE handler 持有, 直接 put 即可
                q.put_nowait({"event": "lagged", "reason": "queue_full"})
            except asyncio.QueueFull:
                pass


async def get_or_create(transcript_path: str,
                          normalizer: Callable[[dict], Optional[dict]]
                          ) -> TranscriptWatcher:
    """拿/建对应 path 的 watcher singleton."""
    async with _REGISTRY_LOCK:
        w = WATCHER_REGISTRY.get(transcript_path)
        if w is None or (w._task is not None and w._task.done()):
            w = TranscriptWatcher(transcript_path, normalizer)
            WATCHER_REGISTRY[transcript_path] = w
        return w


def registry_size() -> int:
    """debug / metric."""
    return len(WATCHER_REGISTRY)
