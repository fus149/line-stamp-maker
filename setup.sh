#!/bin/bash
# ============================================
# LINE スタンプメーカー 初回セットアップ
# 新しいPCで1回だけ実行してください
# ============================================

cd "$(dirname "$0")"

echo "🔧 初回セットアップを開始します..."
echo ""

# 1. Python仮想環境
if [ ! -d ".venv" ]; then
    echo "📦 Python仮想環境を作成中..."
    python3 -m venv .venv
fi
source .venv/bin/activate
echo "📦 パッケージをインストール中..."
pip install -r requirements.txt

# 2. Playwrightブラウザ
echo "🌐 Playwrightブラウザをインストール中..."
playwright install chromium

# 3. ngrok
if ! command -v ngrok &> /dev/null; then
    echo "🔧 ngrokをインストール中..."
    brew install ngrok/ngrok/ngrok
fi

# 4. ngrok認証トークン設定
if [ ! -f "$HOME/Library/Application Support/ngrok/ngrok.yml" ] && [ ! -f "$HOME/.config/ngrok/ngrok.yml" ]; then
    echo ""
    echo "⚠️  ngrokの認証トークンを入力してください"
    echo "   確認先: https://dashboard.ngrok.com/get-started/your-authtoken"
    echo ""
    read -p "トークン: " NGROK_TOKEN
    ngrok config add-authtoken "$NGROK_TOKEN"
fi

# 5. Claude Code ユーザー設定
CLAUDE_DIR="$HOME/.claude"
CLAUDE_SETTINGS="$CLAUDE_DIR/settings.json"
if [ ! -f "$CLAUDE_SETTINGS" ]; then
    echo "⚙️  Claude Code設定をセットアップ中..."
    mkdir -p "$CLAUDE_DIR"
    cat > "$CLAUDE_SETTINGS" << 'SETTINGS'
{
  "defaultMode": "bypassPermissions",
  "permissions": {
    "allow": [
      "Bash(*)",
      "Read",
      "Edit",
      "Write",
      "Glob",
      "Grep",
      "WebFetch",
      "WebSearch",
      "mcp__Claude_in_Chrome__*",
      "mcp__Claude_Preview__*",
      "mcp__mcp-registry__*",
      "mcp__scheduled-tasks__*"
    ]
  }
}
SETTINGS
    echo "   ✅ Claude Code許可設定を反映しました"
else
    echo "⚙️  Claude Code設定: 既に存在します（スキップ）"
fi

echo ""
echo "========================================="
echo "  ✅ セットアップ完了！"
echo ""
echo "  起動コマンド: ~/line-stamp-maker/start.sh"
echo "========================================="
