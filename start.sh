#!/bin/bash
echo "Installing Python requirements..."
pip install -r requirements.txt

echo "Installing system tools..."
apt-get install -y ffmpeg aria2 p7zip-full > /dev/null 2>&1

echo "Installing Playwright..."
playwright install chromium
playwright install-deps chromium

echo "Starting bot..."
python bot.py
