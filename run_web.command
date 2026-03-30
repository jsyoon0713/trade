#!/bin/bash
# KRX 자동매매 웹 대시보드
# 더블클릭하면 브라우저가 자동으로 열립니다

cd "$(dirname "$0")"

echo "======================================"
echo " KRX 자동매매 웹 대시보드"
echo " http://localhost:5000"
echo "======================================"
echo ""

# 잠깐 후 브라우저 자동 오픈
(sleep 2 && open "http://localhost:5001") &

.venv/bin/python web.py

echo ""
echo "서버가 종료되었습니다."
read -p "아무 키나 누르면 창이 닫힙니다..."
