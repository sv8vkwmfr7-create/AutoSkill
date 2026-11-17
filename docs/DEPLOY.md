# 部署与运维手册

## 拓扑选择

| 拓扑 | 适用 | 沙箱隔离 |
|------|------|----------|
| A. 单机/云服务器直接跑（本仓库方式） | 开发、演示、单租户内部工具 | 五层纵深之①~④（用户态） |
| B. 服务器 + Docker 执行器（推荐生产） | 生产/多用户 | ①~④ + ⑤ 容器内核级隔离 |

生产推荐 B：服务进程跑在宿主机，技能沙箱以一次性容器执行（DockerExecutor），服务与 docker daemon 同主机。

## 方式 A：直接部署（10 分钟）

```bash
git clone https://github.com/sv8vkwmfr7-create/AutoSkill.git && cd AutoSkill
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env && vim .env                    # 填 LLM endpoint/key/model
python -m autoskill selfcheck                       # 一键功能自检（六项核心链路）
python -m autoskill eval                            # 批量评测（可选，约 10~15 分钟）
uvicorn 不用直接调；服务启动：
python -m autoskill serve --host 0.0.0.0 --port 8000
```

- 可执行入口 `autoskill` 由 `pyproject.toml [project.scripts]` 提供（`pip install -e .` 后生效）
- 启动即执行注册表重建与向量库对账（`/health` 的 `rebuild` 字段可查）
- LLM 配置缺失会**启动即失败**并给出可读指引（不会带病运行）

## 方式 B：服务器 + Docker 沙箱（生产推荐）

1. 服务器安装 Docker（Ubuntu: `curl -fsSL https://get.docker.com | sh`）
2. 按方式 A 部署代码，`.env` 中设置：

```ini
AUTOSKILL_EXECUTOR=docker
AUTOSKILL_DOCKER_IMAGE=python:3.14-slim
AUTOSKILL_DOCKER_MEMORY=256m
AUTOSKILL_DOCKER_PIDS_LIMIT=64
```

3. 验证沙箱后端：`curl :8000/health` → `executor` 字段应显示 `DockerExecutor(... daemon=<版本>)`
4. 可选：服务本身容器化（本仓库 Dockerfile），但技能沙箱需要访问宿主 docker（挂载 /var/run/docker.sock 或采用宿主服务进程拓扑，推荐后者）

### systemd 单元示例（方式 B）

```ini
# /etc/systemd/system/autoskill.service
[Unit]
Description=AutoSkill service
After=network.target docker.service

[Service]
WorkingDirectory=/opt/AutoSkill
ExecStart=/opt/AutoSkill/.venv/bin/python -m autoskill serve --host 127.0.0.1 --port 8000
EnvironmentFile=/opt/AutoSkill/.env
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

前置 Nginx 反代 + TLS 即为完整生产拓扑。

## API 面

| 端点 | 说明 |
|------|------|
| `POST /ensure` | 全链路：召回/生成 → 校验 → 入库 → 执行（结果式返回，ok=false 时带结构化 error） |
| `POST /skills/{name}/call` | 直接调用（未知技能返回结构化错误，不抛 500） |
| `GET /skills`、`GET /skills/{name}` | 注册表列表 / 单技能详情（含版本/状态/熔断计数/历史） |
| `GET /events?limit=N` | 事件审计轨迹（JSONL 尾部） |
| `POST /agent/chat` | Agent 对话（动态工具列表） |
| `GET /health` | 存活 + 后端组件 + 重建报告 |

未处理异常由全局兜底转为结构化 500（不泄漏裸栈）。

## 运维要点

- **持久化数据**：`skills/`（技能源码与元数据，唯一事实源）+ `data/`（向量库、事件日志）。备份 = 打包这两个目录
- **观测**：`/health`（存活与组件状态）、`/events`（逐请求审计）、`data/events.jsonl`（离线分析）
- **恢复**：进程重启自动对账；若向量库损坏，删除 `data/` 后重启即可从 `skills/` 全量重建
- **熔断治理**：`GET /skills` 观察 `status=fail_streak`；隔离技能由系统自动修复，无需人工干预
- **审计轨迹**：`ensure`/`call`/`rebuild` 每次调用均落 `events.jsonl`（时间戳、路径、技能、耗时、成败）
