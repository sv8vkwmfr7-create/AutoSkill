"""文本向量化：provider（OpenAI 兼容 /embeddings）优先，本地 bigram 哈希兜底。

LocalHashEmbedder：词级 + 中文 bigram 哈希到 512 维并 L2 归一化。
在 ≤35 个技能的 MVP 规模下，配合 top-3 召回 + LLM 终审裁决，召回精度足够；
确定性输出、零网络依赖，保证测试可离线复现。
"""

from __future__ import annotations

import hashlib
import logging
import re

import numpy as np

from .config import Settings

logger = logging.getLogger("autoskill.embeddings")

EMBED_DIM = 512
_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-z0-9]+")


class LocalHashEmbedder:
    name = "local"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = np.zeros(EMBED_DIM, dtype=np.float32)
        text = (text or "").lower()
        # ascii 词（md5/sha256/base64 等技术术语）是最具区分度的信号，加权；
        # CJK bigram 承载中文语义，unigram 弱化避免常用字淹没信号
        for w in _WORD.findall(text):
            self._bump(vec, "w:" + w, 2.2)
        chars = [c for c in text if _CJK.match(c)]
        for c in chars:
            self._bump(vec, "c:" + c, 0.4)
        for a, b in zip(chars, chars[1:]):
            self._bump(vec, "b:" + a + b, 1.6)
        norm = float(np.linalg.norm(vec))
        if norm:
            vec /= norm
        return vec.tolist()

    @staticmethod
    def _bump(vec: np.ndarray, token: str, weight: float) -> None:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % EMBED_DIM
        vec[idx] += weight


class ProviderEmbedder:
    name = "provider"

    def __init__(self, settings: Settings, model: str):
        self.model = model
        import openai

        self._client = openai.OpenAI(
            base_url=settings.llm_base_url or None,
            api_key=settings.llm_api_key or "EMPTY",
            timeout=settings.llm_timeout_s,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(model=self.model, input=list(texts))
        ordered = sorted(resp.data, key=lambda d: d.index)
        return [d.embedding for d in ordered]


def make_embedder(settings: Settings):
    """auto：显式配置了 embedding 模型才走 provider，否则本地兜底。"""
    backend = settings.embedding_backend
    if backend == "provider" or (backend == "auto" and settings.embedding_model):
        try:
            emb = ProviderEmbedder(settings, settings.embedding_model)
            emb.embed(["warmup"])  # 连通性验证
            logger.info("embedding 后端: provider model=%s", settings.embedding_model)
            return emb
        except Exception as e:  # noqa: BLE001
            logger.warning("provider embedding 不可用（%s），降级本地哈希", e)
    logger.info("embedding 后端: local bigram-hash dim=%d", EMBED_DIM)
    return LocalHashEmbedder()
