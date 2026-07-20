@echo off
setlocal
cd /d "%~dp0"

echo RAG 파이프라인 옵션을 선택하세요:
echo   1) JAR 있는 패키지만 (action_schema)
echo   2) 옵션 1 + JAR 없는 패키지 리프도 참고용으로 적재 (action_reference)
echo.
choice /c 12 /n /m "옵션 선택 (1 또는 2): "

if errorlevel 2 (
    python app\rag\scripts\run_option2_with_naive_actions.py
) else (
    python app\rag\scripts\run_option1_jar_only.py
)

endlocal
