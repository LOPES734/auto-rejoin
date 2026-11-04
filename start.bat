@echo off
rem Inicia a versao Python (desenvolvimento). Duplo clique para executar.
cd /d "%~dp0"
title Roblox Auto Rejoin

where py >nul 2>nul
if errorlevel 1 goto nopython

py -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 goto piperror

py main.py
if errorlevel 1 goto apperror
exit /b 0

:nopython
echo [ERRO] Python nao encontrado. Instala o Python 3.9 ou superior a partir de https://www.python.org/downloads/
echo        e marca a opcao "Add python.exe to PATH" durante a instalacao.
pause
exit /b 1

:piperror
echo [ERRO] Nao foi possivel instalar as dependencias. Verifica a ligacao a Internet.
pause
exit /b 1

:apperror
echo [ERRO] O programa terminou com erro. Consulta o ficheiro logs.txt nesta pasta.
pause
exit /b 1
