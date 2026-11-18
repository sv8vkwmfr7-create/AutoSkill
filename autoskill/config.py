"""运行配置：环境变量 + 项目根 .env 加载（不覆盖已有环境变量）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 加载器：KEY=VALUE，支持 # 注释，已存在的环境变量优先。"""
    path = path or PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    """全局配置（全部可通过 AUTOSKILL_* 环境变量覆盖）。"""

    # LLM（OpenAI 兼容协议）
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.2
    llm_timeout_s: int = 120
    llm_json_mode: bool = True  # provider 支持 response_format=json_object 时开启

    # 执行器与沙箱
    executor: str = "local"  # local | docker
    exec_timeout_s: float = 10.0
    # 本地沙箱资源围栏（Windows Job Object）
    sandbox_memory_mb: int = 256
    sandbox_max_processes: int = 32
    # Docker 后端（AUTOSKILL_EXECUTOR=docker 时生效）
    docker_image: str = "python:3.14-slim"
    docker_memory: str = "256m"
    docker_cpus: str = "0.5"
    docker_pids_limit: int = 64
    docker_network: str = "none"  # none=全禁网；bridge=容器可出站（域名白名单由 runner 审计钩子强制）

    # 存储与召回
    skills_dir: Path = PROJECT_ROOT / "skills"
    data_dir: Path = PROJECT_ROOT / "data"
    vector_store: str = "auto"  # auto | chroma | local
    embedding_backend: str = "auto"  # auto | provider | local
    embedding_model: str = ""
    top_k: int = 3
    min_candidate_sim: float = 0.06  # 进入 LLM 裁决的候选相似度下限（精度由 LLM 裁决保证）
    dedup_sim: float = 0.92  # 视为同一意图（触发版本升级）的相似度阈值

    # 生成与熔断
    max_repair_rounds: int = 3
    breaker_threshold: int = 3  # 连续失败次数 -> quarantined
    rollback_min_successes: int = 2  # 晋升后连续成功达标 -> 记为已验证稳定（自动回退目标）
    # egress 白名单（HTTP 类技能可访问的域名；默认空 = 全禁网，与既有安全模型一致）
    egress_hosts: list[str] = field(default_factory=list)

    # Agent
    agent_max_rounds: int = 5

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        return cls(
            llm_base_url=_env("AUTOSKILL_LLM_BASE_URL"),
            llm_api_key=_env("AUTOSKILL_LLM_API_KEY"),
            llm_model=_env("AUTOSKILL_LLM_MODEL", "deepseek-chat"),
            llm_temperature=_env_float("AUTOSKILL_LLM_TEMPERATURE", 0.2),
            llm_timeout_s=_env_int("AUTOSKILL_LLM_TIMEOUT_S", 120),
            llm_json_mode=_env("AUTOSKILL_LLM_JSON_MODE", "1") not in ("0", "false", "no"),
            executor=_env("AUTOSKILL_EXECUTOR", "local"),
            exec_timeout_s=_env_float("AUTOSKILL_EXEC_TIMEOUT_S", 10.0),
            sandbox_memory_mb=_env_int("AUTOSKILL_SANDBOX_MEMORY_MB", 256),
            sandbox_max_processes=_env_int("AUTOSKILL_SANDBOX_MAX_PROCESSES", 32),
            docker_image=_env("AUTOSKILL_DOCKER_IMAGE", "python:3.14-slim"),
            docker_memory=_env("AUTOSKILL_DOCKER_MEMORY", "256m"),
            docker_cpus=_env("AUTOSKILL_DOCKER_CPUS", "0.5"),
            docker_pids_limit=_env_int("AUTOSKILL_DOCKER_PIDS_LIMIT", 64),
            docker_network=_env("AUTOSKILL_DOCKER_NETWORK", "none"),
            skills_dir=Path(_env("AUTOSKILL_SKILLS_DIR", str(PROJECT_ROOT / "skills"))),
            data_dir=Path(_env("AUTOSKILL_DATA_DIR", str(PROJECT_ROOT / "data"))),
            vector_store=_env("AUTOSKILL_VECTOR_STORE", "auto"),
            embedding_backend=_env("AUTOSKILL_EMBEDDING", "auto"),
            embedding_model=_env("AUTOSKILL_EMBEDDING_MODEL", ""),
            top_k=_env_int("AUTOSKILL_TOP_K", 3),
            min_candidate_sim=_env_float("AUTOSKILL_MIN_CANDIDATE_SIM", 0.06),
            dedup_sim=_env_float("AUTOSKILL_DEDUP_SIM", 0.92),
            max_repair_rounds=_env_int("AUTOSKILL_MAX_REPAIR_ROUNDS", 3),
            breaker_threshold=_env_int("AUTOSKILL_BREAKER_THRESHOLD", 3),
            rollback_min_successes=_env_int("AUTOSKILL_ROLLBACK_MIN_SUCCESSES", 2),
            egress_hosts=[h.strip().lower() for h in _env("AUTOSKILL_EGRESS_HOSTS", "").split(",") if h.strip()],
            agent_max_rounds=_env_int("AUTOSKILL_AGENT_MAX_ROUNDS", 5),
        )
