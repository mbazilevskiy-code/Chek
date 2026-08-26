@echo off
setlocal
cd /d "%~dp0"

echo ==============================================
echo   Deploy bota "Chek" na server (odin klik)
echo ==============================================
echo.
echo Nuzhen VPS s Ubuntu (Timeweb/Aeza, zarubezhnaya lokaciya).
echo IP i parol root - v pisme ot hostera ili v ego paneli.
echo.

where ssh >nul 2>nul
if errorlevel 1 (
    echo [!] Komanda ssh ne naidena. Na Windows 10/11 ona vstroena:
    echo     Parametry - Prilozheniya - Dop. komponenty - OpenSSH Client.
    echo     Ustanovi i zapusti etot fail snova.
    pause
    exit /b
)

set /p IP=Vvedi IP servera i nazhmi Enter:
if "%IP%"=="" exit /b

echo.
echo Parol root sprositsya neskolko raz - eto normalno.
echo.

echo [1/3] Sozdayu papku na servere...
ssh -o StrictHostKeyChecking=accept-new root@%IP% "mkdir -p /opt/chek"
if errorlevel 1 goto :fail

set "FILES=bot.py analyzer.py config.py db.py nutrition.py web_dashboard.py dashboard.html coach.html requirements.txt .env deploy.sh"

rem -- Dnevnik kopiruem tolko pri PERVOM deploye, chtoby ne zateret serverniy --
if exist food_diary.db (
    ssh root@%IP% "test -f /opt/chek/food_diary.db" >nul 2>nul
    if errorlevel 1 set "FILES=%FILES% food_diary.db"
)

echo [2/3] Kopiruyu faily...
scp %FILES% root@%IP%:/opt/chek/
if errorlevel 1 goto :fail

echo [3/3] Nastraivayu server (1-2 minuty)...
ssh root@%IP% "bash /opt/chek/deploy.sh"
if errorlevel 1 goto :fail

echo.
echo ==============================================
echo  GOTOVO! Bot rabotaet na servere 24/7.
echo  Ssylka na dashbord napechatana vyshe - sohrani ee.
echo  VAZHNO: zakroj okno lokalnogo bota na etom kompyutere,
echo  inache oni budut meshat drug drugu.
echo ==============================================
pause
exit /b

:fail
echo.
echo [!] Chto-to poshlo ne tak. Prover IP i parol, zapusti snova.
echo     Esli ne poluchaetsya - prishli tekst oshibki v chat Cowork.
pause
