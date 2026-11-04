@echo off
rem Compila o Roblox Auto Rejoin para dist\RobloxAutoRejoin.exe (PyInstaller)
rem e cria a versao portatil em dist\RobloxAutoRejoin_Portable.
setlocal EnableExtensions
cd /d "%~dp0"
title Roblox Auto Rejoin - Build

echo ================================================
echo  Roblox Auto Rejoin - compilacao do executavel
echo ================================================
echo.

where py >nul 2>nul
if errorlevel 1 goto nopython

echo [1/6] A instalar dependencias...
py -m pip install --upgrade pip --quiet --disable-pip-version-check
py -m pip install -r requirements.txt -r requirements-build.txt --disable-pip-version-check
if errorlevel 1 goto fail

echo.
echo [2/6] A executar os testes automatizados...
py -m unittest discover -s tests
if errorlevel 1 goto fail

echo.
echo [3/6] A compilar com PyInstaller...
if exist dist\RobloxAutoRejoin.exe del /q dist\RobloxAutoRejoin.exe
py -m PyInstaller --noconfirm --clean --onefile --windowed --name RobloxAutoRejoin --collect-all customtkinter --hidden-import psutil._pswindows main.py
if errorlevel 1 goto fail
if not exist dist\RobloxAutoRejoin.exe goto fail

echo.
echo [4/6] A verificar o executavel ^(--selftest^)...
if exist "%APPDATA%\RobloxAutoRejoin\selftest.txt" del /q "%APPDATA%\RobloxAutoRejoin\selftest.txt"
start /wait "" "dist\RobloxAutoRejoin.exe" --selftest
if errorlevel 1 goto selftestfail
if exist "%APPDATA%\RobloxAutoRejoin\selftest.txt" type "%APPDATA%\RobloxAutoRejoin\selftest.txt"

echo.
echo [5/6] A criar a versao portatil...
if exist dist\RobloxAutoRejoin_Portable rmdir /s /q dist\RobloxAutoRejoin_Portable
mkdir dist\RobloxAutoRejoin_Portable
copy /y dist\RobloxAutoRejoin.exe dist\RobloxAutoRejoin_Portable\ >nul
rem O ficheiro portable.flag faz o programa guardar config.json e logs.txt na subpasta "data" ao lado do .exe
type nul > dist\RobloxAutoRejoin_Portable\portable.flag
copy /y README.md dist\RobloxAutoRejoin_Portable\ >nul

echo.
echo [6/6] A criar o ZIP da versao portatil...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\RobloxAutoRejoin_Portable\*' -DestinationPath 'dist\RobloxAutoRejoin_Portable.zip' -Force"

echo.
echo ================================================
echo  Concluido.
echo  Executavel: %~dp0dist\RobloxAutoRejoin.exe
echo  Portatil:   %~dp0dist\RobloxAutoRejoin_Portable
echo ================================================
pause
exit /b 0

:nopython
echo [ERRO] Python nao encontrado. Instala o Python 3.9 ou superior a partir de https://www.python.org/downloads/
echo        e marca a opcao "Add python.exe to PATH" durante a instalacao.
pause
exit /b 1

:selftestfail
echo [ERRO] O executavel foi criado mas falhou a auto-verificacao.
if exist "%APPDATA%\RobloxAutoRejoin\selftest.txt" type "%APPDATA%\RobloxAutoRejoin\selftest.txt"
pause
exit /b 1

:fail
echo.
echo [ERRO] A compilacao falhou. Le as mensagens acima.
pause
exit /b 1
