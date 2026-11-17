"""向量技能池：Chroma 优先，numpy 本地兜底，统一 cosine 相似度语义（sim = 1 - distance）。"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .config import Settings

logger = logging.getLogger("autoskill.vectorstore")

COLLECTION = "autoskill_skills"


@dataclass
class VecHit:
    id: str
    document: str
    metadata: dict
    sim: float  # 1 - cosine distance，越大越相似


class BaseStore:
    def add(self, ids, embeddings, documents, metadatas) -> None: ...
    def query(self, embedding, k: int, where: dict | None = None) -> list[VecHit]: ...
    def delete(self, ids) -> None: ...
    def count(self) -> int: ...
    def list_ids(self) -> list[str]: ...
    def backend_name(self) -> str: ...


class ChromaStore(BaseStore):
    def __init__(self, persist_dir: Path):
        import chromadb

        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._col = self._client.get_or_create_collection(
            COLLECTION, metadata={"hnsw:space": "cosine"}
        )

    def backend_name(self) -> str:
        return "chroma"

    def add(self, ids, embeddings, documents, metadatas) -> None:
        self._col.upsert(ids=list(ids), embeddings=list(embeddings), documents=list(documents), metadatas=list(metadatas))

    def query(self, embedding, k: int, where: dict | None = None) -> list[VecHit]:
        if self.count() == 0:
            return []
        kwargs: dict[str, Any] = {
            "query_embeddings": [embedding],
            "n_results": min(k, self.count()),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        res = self._col.query(**kwargs)
        hits = []
        for i in range(len(res["ids"][0])):
            hits.append(
                VecHit(
                    id=res["ids"][0][i],
                    document=res["documents"][0][i] or "",
                    metadata=dict(res["metadatas"][0][i] or {}),
                    sim=1.0 - float(res["distances"][0][i]),
                )
            )
        return hits

    def delete(self, ids) -> None:
        if list(ids):
            self._col.delete(ids=list(ids))

    def count(self) -> int:
        return self._col.count()

    def list_ids(self) -> list[str]:
        return list(self._col.get(include=[])["ids"])


class LocalNumpyStore(BaseStore):
    """零外部依赖兜底：JSON 持久化 + numpy 余弦相似度。"""

    def __init__(self, persist_dir: Path):
        persist_dir.mkdir(parents=True, exist_ok=True)
        self._path = persist_dir / "local_store.json"
        self._lock = threading.RLock()
        self._data: dict[str, dict] = {}
        if self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))

    def backend_name(self) -> str:
        return "local-numpy"

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    def add(self, ids, embeddings, documents, metadatas) -> None:
        with self._lock:
            for i, rid in enumerate(ids):
                self._data[str(rid)] = {
                    "emb": list(embeddings[i]),
                    "doc": documents[i],
                    "meta": dict(metadatas[i]),
                }
            self._save()

    def query(self, embedding, k: int, where: dict | None = None) -> list[VecHit]:
        with self._lock:
            q = np.asarray(embedding, dtype=np.float32)
            scored = []
            for rid, row in self._data.items():
                if where and any(row["meta"].get(key) != val for key, val in where.items()):
                    continue
                v = np.asarray(row["emb"], dtype=np.float32)
                denom = float(np.linalg.norm(q) * np.linalg.norm(v)) or 1.0
                scored.append((float(np.dot(q, v) / denom), rid))
            scored.sort(reverse=True)
            hits = []
            for sim, rid in scored[: max(k, 0)]:
                row = self._data[rid]
                hits.append(VecHit(id=rid, document=row["doc"], metadata=dict(row["meta"]), sim=sim))
            return hits

    def delete(self, ids) -> None:
        with self._lock:
            for rid in ids:
                self._data.pop(str(rid), None)
            self._save()

    def count(self) -> int:
        return len(self._data)

    def list_ids(self) -> list[str]:
        return list(self._data.keys())


def make_vector_store(settings: Settings) -> BaseStore:
    choice = settings.vector_store
    if choice in ("auto", "chroma"):
        try:
            store = ChromaStore(settings.data_dir / "chroma")
            logger.info("向量库后端: chroma (%s 条)", store.count())
            return store
        except Exception as e:  # noqa: BLE001
            if choice == "chroma":
                raise
            logger.warning("chroma 不可用（%s），降级本地 numpy 向量库", e)
    store = LocalNumpyStore(settings.data_dir / "localvec")
    logger.info("向量库后端: local-numpy (%s 条)", store.count())
    return store
