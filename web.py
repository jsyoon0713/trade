"""
KRX 자동매매 웹 대시보드 실행
http://localhost:5001 에서 접속
"""
import logging
import logging.config
import sys
from pathlib import Path

import yaml

# PyInstaller 단독 실행(.exe) 시 sys._MEIPASS 경로 사용
if getattr(sys, 'frozen', False):
    ROOT = Path(sys._MEIPASS)
    # exe와 같은 폴더에 있는 config/logs 사용
    WORK_DIR = Path(sys.executable).parent
else:
    ROOT = Path(__file__).parent
    WORK_DIR = ROOT

# logging 설정 파일
_log_cfg = WORK_DIR / "config" / "logging.yaml"
if not _log_cfg.exists():
    _log_cfg = ROOT / "config" / "logging.yaml"

with open(_log_cfg) as f:
    logging.config.dictConfig(yaml.safe_load(f))

from src.autotrader.web.app import create_app

if __name__ == "__main__":
    import os
    port = int(os.getenv("WEB_PORT", 5001))

    app = create_app()
    print("=" * 45)
    print("  KRX 자동매매 대시보드 시작")
    print(f"  http://localhost:{port}")
    print("  종료: Ctrl+C")
    print("=" * 45)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
