"""测试夹具：离线 Fake（LLM/Store）与临时服务构建。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autoskill.config import Settings  # noqa: E402
from autoskill.service import AutoSkillService  # noqa: E402
from autoskill.vectorstore import VecHit  # noqa: E402


class FakeLLM:
    """脚本化 LLM：chat_json 按顺序弹出预设回复；脚本项为异常对象时抛出。"""

    name = "fake"

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []

    def chat_json(self, system, user, temperature=None, max_tokens=4000):
        self.calls.append({"system": system, "user": user})
        if not self.script:
            raise AssertionError("FakeLLM 脚本耗尽（收到意外调用）")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def chat_text(self, system, user, temperature=None, max_tokens=4000):
        raise NotImplementedError


class FakeStore:
    """罐头命中结果的向量库桩；记录 add/delete 以便断言。"""

    def __init__(self, hits=None):
        self.hits = list(hits or [])
        self.rows = {}
        self.deleted = []

    def backend_name(self):
        return "fake"

    def add(self, ids, embeddings, documents, metadatas):
        for i, rid in enumerate(ids):
            self.rows[str(rid)] = {"emb": embeddings[i], "doc": documents[i], "meta": dict(metadatas[i])}

    def query(self, embedding, k, where=None):
        out = []
        for h in self.hits:
            if where and any(h.metadata.get(key) != val for key, val in where.items()):
                continue
            out.append(h)
        return out[: max(k, 0)]

    def delete(self, ids):
        for rid in ids:
            self.rows.pop(str(rid), None)
            self.deleted.append(str(rid))

    def count(self):
        return len(self.rows)

    def list_ids(self):
        return list(self.rows.keys())


def make_service(tmp_path, llm=None, store=None, **overrides) -> AutoSkillService:
    """构建完全离线的服务：本地向量库 + 本地 embedding + 伪 LLM。"""
    s = Settings()
    s.skills_dir = tmp_path / "skills"
    s.data_dir = tmp_path / "data"
    s.vector_store = "local"
    s.embedding_backend = "local"
    s.llm_base_url = "http://localhost:9"
    s.llm_api_key = "test-key"
    for key, val in overrides.items():
        setattr(s, key, val)
    svc = AutoSkillService(s)
    if store is not None:
        svc.store = store
        svc.router.store = store
    if llm is not None:
        svc.set_llm(llm)
    return svc


TRIVIAL_IMPL = "def run(params):\n    return {'echo': params}\n"

ADD_IMPL = (
    "def run(params):\n"
    "    a = params['a']\n"
    "    b = params['b']\n"
    "    return {'sum': a + b}\n"
)
ADD_SKILL = {
    "name": "add_numbers",
    "description": "计算两个整数的和并返回对象",
    "tags": ["math"],
    "params": {
        "type": "object",
        "properties": {"a": {"type": "integer", "description": "加数"}, "b": {"type": "integer", "description": "被加数"}},
        "required": ["a", "b"],
    },
    "impl_code": ADD_IMPL,
    "tests": [
        {"input": {"a": 1, "b": 2}, "expected": {"sum": 3}},
        {"input": {"a": 0, "b": 0}, "expected": {"sum": 0}},
        {"input": {"a": -1, "b": 1}, "expected": {"sum": 0}},
    ],
}
