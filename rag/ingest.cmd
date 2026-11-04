@echo off
setlocal
cd /d "%~dp0"
if "%HF_ENDPOINT%"=="" set "HF_ENDPOINT=https://hf-mirror.com"
uv run python ingest.py %*
