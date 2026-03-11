#!/bin/bash
# ============================================
# LINE スタンプメーカー ワンクリック起動
# ============================================

cd "$(dirname "$0")"

# 最新コードを取得
echo "📥 最新コードを取得中..."
git pull

# 仮想環境を有効化
source .venv/bin/activate

# サーバー起動（バックグラウンド）
echo "🚀 サーバー起動中..."
uvicorn app:app --reload --host 0.0.0.0 --port 8000 &
SERVER_PID=$!
sleep 2

# ngrok起動
echo "🌐 ngrok起動中..."
echo ""
echo "========================================="
echo "  Ctrl+C で全て停止します"
echo "========================================="
echo ""
ngrok http 8000

# ngrokが止まったらサーバーも停止
kill $SERVER_PID 2>/dev/null
echo "👋 サーバーを停止しました"
