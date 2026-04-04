# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 스펙 파일
KRX 자동매매 봇 → 단독 실행파일(.exe) 빌드
"""
import os
from pathlib import Path

ROOT = Path(SPECPATH)

block_cipher = None

a = Analysis(
    ['web.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # Flask 템플릿
        ('src/autotrader/web/templates', 'src/autotrader/web/templates'),
        # 설정 파일
        ('config/settings.yaml', 'config'),
        # .env.example (실제 .env는 사용자가 직접 제공)
        ('.env.example', '.'),
    ],
    hiddenimports=[
        # Flask 관련
        'flask',
        'flask.templating',
        'jinja2',
        'werkzeug',
        'werkzeug.serving',
        'werkzeug.middleware',
        # 스케줄러
        'apscheduler',
        'apscheduler.schedulers.background',
        'apscheduler.triggers.cron',
        'apscheduler.triggers.interval',
        # 데이터
        'pandas',
        'numpy',
        'ta',
        'ta.momentum',
        'ta.trend',
        'ta.volatility',
        # 금융 데이터
        'yfinance',
        'yfinance.base',
        # 텔레그램
        'telegram',
        'telegram.ext',
        # AI
        'anthropic',
        'google.genai',
        # 기타
        'yaml',
        'dotenv',
        'requests',
        'beautifulsoup4',
        'bs4',
        'lxml',
        'lxml.etree',
        # 표준 라이브러리
        'queue',
        'threading',
        'pathlib',
        'select',
        # 내부 모듈
        'src.autotrader',
        'src.autotrader.broker',
        'src.autotrader.broker.ls_broker',
        'src.autotrader.broker.models',
        'src.autotrader.monitor',
        'src.autotrader.monitor.portfolio',
        'src.autotrader.monitor.risk_manager',
        'src.autotrader.notification',
        'src.autotrader.notification.telegram_notifier',
        'src.autotrader.strategy',
        'src.autotrader.strategy.base',
        'src.autotrader.strategy.rsi_strategy',
        'src.autotrader.strategy.daytrading_strategy',
        'src.autotrader.strategy.combined_strategy',
        'src.autotrader.strategy.indicators',
        'src.autotrader.trader',
        'src.autotrader.trader.swing_trader',
        'src.autotrader.trader.daytrader',
        'src.autotrader.scanner',
        'src.autotrader.scanner.stock_scanner',
        'src.autotrader.pipeline',
        'src.autotrader.pipeline.stock_analyzer',
        'src.autotrader.pipeline.strategy_recommender',
        'src.autotrader.backtest',
        'src.autotrader.backtest.engine',
        'src.autotrader.news',
        'src.autotrader.news.naver_news',
        'src.autotrader.news.dart_fetcher',
        'src.autotrader.news.claude_analyzer',
        'src.autotrader.news.gemini_analyzer',
        'src.autotrader.news.factory',
        'src.autotrader.news.base_analyzer',
        'src.autotrader.macro',
        'src.autotrader.macro.analyzer',
        'src.autotrader.macro.data_fetcher',
        'src.autotrader.macro.adjuster',
        'src.autotrader.macro.historical',
        'src.autotrader.macro.models',
        'src.autotrader.web',
        'src.autotrader.web.app',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'test', 'unittest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='KRX자동매매',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,     # True = 콘솔창 표시 (로그 확인용)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,        # 아이콘 파일 경로 (예: 'icon.ico')
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='KRX자동매매',
)
