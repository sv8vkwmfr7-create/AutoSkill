# AutoSkill 服务镜像
# 构建: docker build -t autoskill .
# 运行: docker run --rm -p 8000:8000 --env-file .env autoskill
# 说明: 服务容器本身负责 API/编排；技能沙箱默认走 DockerExecutor（宿主 docker），
#       或在容器内以 local 审计沙箱运行（AUTOSKILL_EXECUTOR=local）。
FROM python:3.14-slim

WORKDIR /app
ENV PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY autoskill ./autoskill
COPY eval ./eval
COPY tests ./tests
COPY README.md 最终目标与达标要求.md ./

# 构建期自检：离线测试套件必须全过才算构建成功
RUN pytest tests/ -q --basetemp=.pytest_tmp

EXPOSE 8000
CMD ["python", "-m", "autoskill", "serve", "--host", "0.0.0.0", "--port", "8000"]
