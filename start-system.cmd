 @echo off
  start "FPT RAG Backend" /D "%~dp0backend" cmd /k ".venv\Scripts\python.exe -m uvicorn api.main:app --port 8000"
  start "FPT RAG Frontend" /D "%~dp0frontend" cmd /k "npm run dev"
  timeout /t 5 >nul
  start "" http://localhost:5173
