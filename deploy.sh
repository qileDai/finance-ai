#!/usr/bin/env bash
# Finance AI 一键部署脚本
# 用法: ./deploy.sh [--rag]
#   --rag  启用 qdrant 向量库（RAG profile）
set -euo pipefail

PROJECT_DIR="/home/admin/finance-ai"
RAG_FLAG=""

# 解析参数
for arg in "$@"; do
  case "$arg" in
    --rag) RAG_FLAG="--profile rag" ;;
    *) echo "未知参数: $arg"; exit 1 ;;
  esac
done

echo "===== Finance AI 一键部署 ====="
echo "项目目录: $PROJECT_DIR"
echo "RAG: ${RAG_FLAG:-否}"
echo ""

# 1) 进入项目目录
cd "$PROJECT_DIR"

# 2) git pull
echo "----- [1/3] git pull -----"
git pull --ff-only
echo ""

# 3) 构建 + 启动容器
echo "----- [2/3] docker compose build -----"
docker compose build --pull

echo ""
echo "----- [3/3] docker compose up -----"
docker compose $RAG_FLAG up -d --remove-orphans

# 4) 等待健康检查
echo ""
echo "----- 等待服务健康检查 -----"
sleep 3
docker compose ps

echo ""
echo "===== 部署完成 ====="
echo "Bot:   http://<server-ip>:8081"
echo "Admin: http://<server-ip>:8082"
echo ""
echo "查看日志: cd $PROJECT_DIR && docker compose logs -f"
