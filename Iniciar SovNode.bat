@echo off
REM Lanzador de SovNode (MonolitoPersonal) - doble clic para abrir el programa
REM sin tener que escribir el comando "cd ..." + "set PYTHONPATH=..." +
REM "python src\ui\sovnode_qt.py" a mano cada vez.

REM %~dp0 = carpeta donde vive ESTE .bat, con barra final incluida.
REM "cd /d" entre comillas funciona aunque la ruta tenga espacios o parentesis.
cd /d "%~dp0"

REM BLINDAJE (2026-09-09 — "ModuleNotFoundError: No module named 'pipeline'"
REM al correr sovnode_qt.py con doble clic): sovnode_qt.py (en src\ui) hace
REM imports "planos" como `from pipeline import EventType`, pero pipeline.py
REM vive en src\core. Python solo agrega al sys.path la carpeta del script
REM que se ejecuta directamente (src\ui), no las carpetas hermanas -  sin
REM esto, cualquier import de un módulo de src\core, src\tools o src falla
REM apenas se abre a mano. El mismo set de carpetas ya está declarado en
REM SovNode.spec (pathex=['src', 'src/core', 'src/tools', 'src/ui']) para el
REM build empaquetado con PyInstaller; acá se replica igual para que correr
REM desde el código fuente funcione exactamente igual que el .exe compilado.
set "PYTHONPATH=%~dp0src;%~dp0src\core;%~dp0src\tools;%~dp0src\ui;%PYTHONPATH%"

echo Iniciando SovNode...
python src\ui\sovnode_qt.py

if errorlevel 1 (
    echo.
    echo ============================================================
    echo  Hubo un error al iniciar SovNode. Revisa el mensaje de arriba.
    echo  Verifica que Ollama este corriendo y que Python este instalado
    echo  y accesible desde la terminal ^(comando "python"^).
    echo ============================================================
    pause
)
