#!/bin/bash
# KRX 자동매매 봇 실행 파일
# Finder에서 더블클릭하면 Terminal이 열리며 봇이 시작됩니다

# 이 스크립트가 있는 디렉토리(프로젝트 루트)로 이동
cd "$(dirname "$0")"

echo "======================================"
echo " KRX 자동매매 봇 시작"
echo " 종료하려면 Ctrl+C 를 누르세요"
echo "======================================"
echo ""

.venv/bin/python -m src.autotrader.main

echo ""
echo "봇이 종료되었습니다."
read -p "아무 키나 누르면 창이 닫힙니다..."
