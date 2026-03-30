"""
KRX 자동매매 웹 대시보드 실행
http://localhost:5001 에서 접속
"""
import logging
import logging.config
from pathlib import Path

import yaml

ROOT = Path(__file__).parent

with open(ROOT / "config" / "logging.yaml") as f:
    logging.config.dictConfig(yaml.safe_load(f))

from src.autotrader.web.app import create_app

if __name__ == "__main__":
    app = create_app()
    print("=" * 45)
    print("  KRX 자동매매 대시보드 시작")
    print("  브라우저에서 http://localhost:5001 접속")
    print("  종료: Ctrl+C")
    print("=" * 45)
    app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False)
