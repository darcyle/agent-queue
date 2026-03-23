@echo off
cd /d C:\Users\jkern\Documents\Dev\agent-queue2
.\.venv\Scripts\activate
python -m pytest tests/ -v --tb=short
pause