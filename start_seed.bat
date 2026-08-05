@echo off
rem 내부망 업데이트 시드 시작 (개발 PC 전용) — 시작프로그램에 넣어두면 항상 켜져 있음
cd /d %~dp0
python seed_server.py
pause
