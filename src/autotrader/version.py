"""
앱 버전 관리
코드 변경 시 이 파일의 VERSION을 업데이트하세요.
"""

VERSION = "1.3.0"

CHANGELOG = {
    "1.3.0": "단타 타이밍 개선 — 30초 주기, 1분봉, 캐시 TTL 단축",
    "1.2.0": "VWAP 눌림 반등 전략 + 일봉 EMA/갭업 필터 추가",
    "1.1.0": "LS API 에러코드 감지 수정, 익절 로직 추가",
    "1.0.0": "최초 릴리즈 — RSI 스윙/단타 전략",
}

LATEST_CHANGE = CHANGELOG.get(VERSION, "")
