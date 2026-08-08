#!/usr/bin/env bash
# ============================================================
# Grid-V2.0 一键部署脚本 (Docker Compose, 2 容器)
# 用途: 在装有 Docker 的云服务器上一键构建+启动+健康检查
# 用法:  bash deploy.sh          (或 ./deploy.sh)
# 要求:  docker + docker compose 已装; .env.wecom 已放到项目根目录
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

echo "=============================================="
echo "  Grid-V2.0 一键部署"
echo "=============================================="

# --- [1/4] 前置检查 ---
echo ""
echo "==> [1/4] 前置检查"
if ! command -v docker >/dev/null 2>&1; then
  echo "❌ 未检测到 docker，请先安装: https://docs.docker.com/engine/install/"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "❌ 未检测到 docker compose 插件，请先安装。"
  exit 1
fi
if [ ! -f .env.wecom ]; then
  echo "❌ 缺少 .env.wecom（企业微信凭证+OpenRouter key）。"
  echo "   请从旧服务器 /mnt/shared/code/grid-V2.0/.env.wecom 拷贝到本项目根目录后再部署。"
  exit 1
fi
echo "   ✅ docker / compose / .env.wecom 齐全"

# --- [2/4] 数据迁移提示 ---
echo ""
echo "==> [2/4] 运行数据"
echo "   数据卷: grid-v2-data (挂载到容器 /app/data，重启不丢)"
if [ -d /app/data ] && [ -n "$(ls -A /app/data 2>/dev/null)" ]; then
  echo "   ℹ️ 检测到旧数据目录 /app/data。如需迁移，请手动拷贝进卷:"
  echo "      docker run --rm -v grid-v2-data:/dst -v /app/data:/src alpine sh -c 'cp -a /src/. /dst/'"
fi

# --- [3/4] 构建并启动 ---
echo ""
echo "==> [3/4] 构建镜像并启动容器 (docker compose up -d --build)"
docker compose up -d --build

# --- [4/4] 健康检查 ---
echo ""
echo "==> [4/4] 等待后端就绪 (最多 30s)..."
ready=0
for i in $(seq 1 15); do
  if curl -sf http://localhost:8001/health >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done

echo ""
echo "=============================================="
if [ "$ready" = "1" ]; then
  echo "  ✅ 部署成功！后端健康检查通过。"
else
  echo "  ⚠️ 后端未在预期内就绪，请排查日志:"
  echo "      docker compose logs -f grid-v2-app"
fi
echo "----------------------------------------------"
echo "  前端:  http://<服务器IP或域名>/W2/   (经 nginx 反代 8001)"
echo "         http://localhost:8001/W2/     (本机直连)"
echo "  容器:  grid-v2-app + grid-v2-notify"
echo "  状态:  docker compose ps"
echo "  日志:  docker compose logs -f grid-v2-app"
echo "  停止:  docker compose down"
echo "=============================================="
