#!/bin/bash
set -e

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

if ! command -v python3 &> /dev/null; then
    echo "Python3 is not installed. Please install it first."
    exit 1
fi

if ! command -v pip &> /dev/null; then
    echo "pip is not installed. Please install it first."
    exit 1
fi

if [ ! -f ".env" ]; then
    echo "Creating .env file based on .env.example..."
    cp .env.example .env

    echo "Please enter your Telegram API credentials:"
    read -p "API_ID: " api_id
    read -p "API_HASH: " api_hash
    read -p "HANDLER (e.g., .saveit): " handler
    read -p "FORWARD_GROUP_IDS (optional, e.g. -1001234567890,@learnpython): " forward_group_ids

    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "s/API_ID=.*/API_ID=$api_id/" .env
        sed -i '' "s/API_HASH=.*/API_HASH=$api_hash/" .env
        sed -i '' "s/HANDLER=.*/HANDLER=$handler/" .env
        if [ -n "$forward_group_ids" ]; then
            sed -i '' "s/FORWARD_GROUP_IDS=.*/FORWARD_GROUP_IDS=$forward_group_ids/" .env
        fi
    else
        sed -i "s/API_ID=.*/API_ID=$api_id/" .env
        sed -i "s/API_HASH=.*/API_HASH=$api_hash/" .env
        sed -i "s/HANDLER=.*/HANDLER=$handler/" .env
        if [ -n "$forward_group_ids" ]; then
            sed -i "s/FORWARD_GROUP_IDS=.*/FORWARD_GROUP_IDS=$forward_group_ids/" .env
        fi
    fi
fi

git stash || true
git pull || true
git stash pop || true

pip install --upgrade telethon python-dotenv

echo "Running Saveit.py..."
python3 Saveit.py

echo "Done."
