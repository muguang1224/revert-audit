@echo off
setlocal
cd /d "%~dp0"

if not exist .git (
  git init
)
git add -A
git -c user.name="muguang1224" -c user.email="muguang1224@users.noreply.github.com" commit -m "revert-audit v0.1.0"
git tag -a v0.1.0 -m "revert-audit v0.1.0" 2>nul
git branch -M main

where gh >nul 2>nul
if %ERRORLEVEL%==0 (
  gh repo create revert-audit --public --description "Measure how often CI was green on changes that were later reverted." 2>nul
)

git remote remove origin 2>nul
git remote add origin https://github.com/muguang1224/revert-audit.git
git push -u origin main
git push origin v0.1.0

where gh >nul 2>nul
if %ERRORLEVEL%==0 (
  gh release create v0.1.0 --title "revert-audit v0.1.0" --notes "CI false-negative rate on reverted changes. 90 tests; zero dependencies." 2>nul
)

echo.
echo Done. Verify:
echo   https://github.com/muguang1224/revert-audit
echo   https://github.com/muguang1224/revert-audit/releases/latest
pause
