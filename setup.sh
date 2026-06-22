#!/bin/bash
# ─────────────────────────────────────────────────────────────
# CalorieTracker Setup Script
# Sets up directories, installs dependencies, and optionally
# registers the daily launchd scheduled task.
# ─────────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPORTS_DIR="$HOME/CalorieTracker/reports"
LOGS_DIR="$HOME/CalorieTracker/logs"
PLIST_NAME="com.calorie-tracker.daily.plist"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

echo "🍽️  CalorieTracker Setup"
echo "========================"

# 1. Create directories
echo "📁 Creating directories..."
mkdir -p "$REPORTS_DIR"
mkdir -p "$LOGS_DIR"
echo "   ✅ $REPORTS_DIR"
echo "   ✅ $LOGS_DIR"

# 2. Install Python dependencies
echo ""
echo "📦 Installing Python dependencies..."
pip3 install -r "$SCRIPT_DIR/requirements.txt"
echo "   ✅ Dependencies installed"

# 3. Check for OPENAI_API_KEY
echo ""
if [ -z "$GEMINI_API_KEY" ]; then
    echo "⚠️  GEMINI_API_KEY is not set."
    echo "   Add this to your ~/.zshrc or ~/.bash_profile:"
    echo ""
    echo "   export GEMINI_API_KEY='your-api-key-here'"
    echo ""
else
    echo "✅ GEMINI_API_KEY is set"
fi

# 4. Install launchd plist (optional)
echo ""
read -p "🕐 Install daily schedule (runs at 11 PM)? [y/N] " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    mkdir -p "$LAUNCH_AGENTS_DIR"
    cp "$SCRIPT_DIR/$PLIST_NAME" "$LAUNCH_AGENTS_DIR/$PLIST_NAME"

    # Update the plist with the correct Python path
    PYTHON_PATH=$(which python3)
    echo "   Using Python: $PYTHON_PATH"

    launchctl unload "$LAUNCH_AGENTS_DIR/$PLIST_NAME" 2>/dev/null || true
    launchctl load "$LAUNCH_AGENTS_DIR/$PLIST_NAME"
    echo "   ✅ Daily schedule installed and loaded"
    echo "   📋 To check: launchctl list | grep calorie"
    echo "   🗑️  To remove: launchctl unload ~/Library/LaunchAgents/$PLIST_NAME"
else
    echo "   ⏭️  Skipped. You can run manually:"
    echo "   cd $SCRIPT_DIR && python3 calorie_tracker.py"
fi

echo ""
echo "🎉 Setup complete!"
echo ""
echo "Quick start:"
echo "  1. Make sure GEMINI_API_KEY is set"
echo "  2. Run: cd $SCRIPT_DIR && python3 calorie_tracker.py"
echo "  3. Reports will appear in: $REPORTS_DIR"
