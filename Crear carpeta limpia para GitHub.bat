@echo off
REM Crea una copia LIMPIA de MonolitoPersonal en el Escritorio, lista para
REM subir a GitHub — sin __pycache__, venv, build/dist, memoria/DB del
REM usuario, indices .faiss, backups de parches, carpetas de trabajo
REM internas (_backups, _to_delete, sovnode_patch, Claude outputs), zips/
REM exe empaquetados, config de IDE, ni contenido generado por el LLM en
REM el sandbox (workspace).
REM
REM Ademas de copiar, esta version:
REM   - Vacia la carpeta "workspace" en el destino (ahi es donde el LLM
REM     escribe los programas que genera, p.ej. doom.py o agario_basico.py;
REM     no es codigo del proyecto, no debe ir al repo), dejando solo un
REM     .gitkeep para que la carpeta exista vacia.
REM   - Genera un .gitignore en el destino con las mismas exclusiones que
REM     usa esta copia, para que un "git add ." normal despues del primer
REM     commit siga respetando las mismas reglas.
REM   - Al final, escanea el destino por posibles claves de API sueltas en
REM     texto plano -- tanto el patron de Anthropic ("sk-ant-...") como el
REM     de Google/Gemini ("AIzaSy...") -- como red de seguridad antes de
REM     subir el repo. Actualizado 2026-09-20: SovNode ahora soporta Cloud
REM     con Gemini (Google) ademas de Anthropic (ver `cloud/api_key_gemini`
REM     / `cloud/api_key_anthropic` en QSettings), y el chequeo original
REM     solo cubria el formato de Anthropic -- una key de Gemini filtrada
REM     por accidente en algun archivo hubiera pasado este escaneo sin
REM     avisar nada. Nota: SovNode guarda ambas keys en el Registro de
REM     Windows via QSettings("SovNode","SovNode"), no en ningun archivo
REM     del proyecto, asi que este escaneo no deberia encontrar nada — es
REM     solo un chequeo extra.
REM
REM patch_bat2 (2026-09-20): el .gitignore generado ya cubria estos
REM archivos de memoria de forma INDIRECTA via patrones genericos por
REM extension (*.db, *.wal, *.faiss, *.meta.json, *.log), pero a pedido
REM del usuario ahora se listan tambien por su NOMBRE REAL explicito, en
REM su propia seccion "Memoria / RAG de SovNode": sovnode_memory.db
REM (MemoryGraph, historial + grafo de memoria, SQLite), sovnode.wal
REM (WriteAheadLog, JSONL append-only), sovnode_debug.log, y los indices
REM vectoriales workspace_vector_index.* / longterm_vector_index.*
REM (LocalVectorRAG: <nombre>.faiss + <nombre>.meta.json). Doble
REM cobertura a proposito: los patrones genericos siguen ahi por si
REM cambia el nombre de archivo interno, y los nombres explicitos dejan
REM claro, con solo mirar el .gitignore, que archivos son "memoria" del
REM usuario y no codigo fuente.
REM
REM Uso: doble clic. Toma como origen la carpeta donde vive ESTE .bat
REM (tiene que estar en la raiz de MonolitoPersonal, al lado de app.py).
REM El destino es una carpeta nueva en el Escritorio.

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "ORIGEN=%~dp0"
set "DESTINO=%USERPROFILE%\Desktop\MonolitoPersonal-GitHub"

echo ============================================================
echo  Copiando MonolitoPersonal a una carpeta limpia para GitHub
echo ============================================================
echo Origen:  %ORIGEN%
echo Destino: %DESTINO%
echo.

if exist "%DESTINO%" (
    echo La carpeta destino ya existe. Se va a SOBRESCRIBIR/actualizar su contenido.
    echo Presiona una tecla para continuar, o cierra esta ventana para cancelar.
    pause >nul
)

REM robocopy: /E copia subcarpetas (incluidas vacias), /XD excluye
REM carpetas por nombre, /XF excluye archivos por patron.
REM   - _backups, _to_delete, sovnode_patch, "Claude outputs": carpetas de
REM     trabajo interno de esta sesion de debugging, nunca deberian ir al
REM     repo (sovnode_patch en particular tiene decenas de backups .bakNN
REM     de orchestrator.py de casi 1 MB cada uno).
REM   - "src\core\workspace" YA NO se excluye: es un modulo real del
REM     proyecto (contiene system_health.py), no el sandbox. El sandbox
REM     real donde el LLM genera archivos es la carpeta "workspace" en la
REM     raiz (confirmado por el log: "Carpeta activa para herramientas:
REM     .../MonolitoPersonal/workspace") y se limpia aparte, mas abajo,
REM     porque robocopy /XD con un nombre sin ruta excluiria las dos
REM     carpetas "workspace" (la de la raiz Y la de src\core) por igual.
REM   - *.bak* en /XF atrapa cualquier archivo de backup suelto en
REM     cualquier carpeta (ARCHITECTURE.md.bak1, orchestrator.py.bakNN,
REM     test_regressions.py.bak1, etc.), no solo los de las carpetas ya
REM     excluidas arriba.
robocopy "%ORIGEN%." "%DESTINO%" /E ^
    /XD __pycache__ .git .claude .vscode .idea venv .venv env ENV build dist _backups _to_delete "Claude outputs" sovnode_patch ^
    /XF *.pyc *.pyo "*$py.class" *.so *.zip *.exe *.db *.db-journal *.db-shm *.db-wal *.wal *.faiss *.meta.json *.log *.tmp *.swp Thumbs.db desktop.ini ".DS_Store" *.bak* ^
    /NFL /NDL /NJH /NC /NS /NP

REM robocopy devuelve codigos >=8 solo en errores reales; 0-7 son exito
REM (con distintos matices de "hubo copias" / "hubo coincidencias").
if %ERRORLEVEL% GEQ 8 (
    echo.
    echo ============================================================
    echo  Hubo un error copiando los archivos. Revisa el mensaje de arriba.
    echo ============================================================
    pause
    exit /b 1
)

REM Vacia el sandbox (workspace en la raiz): ahi vive lo que el LLM va
REM generando durante el uso normal de la app (juegos de prueba, scripts,
REM etc.), no es codigo fuente del proyecto. Se deja la carpeta creada
REM con un .gitkeep para que exista vacia en el repo.
if exist "%DESTINO%\workspace" rd /s /q "%DESTINO%\workspace"
mkdir "%DESTINO%\workspace"
type nul > "%DESTINO%\workspace\.gitkeep"

REM Genera un .gitignore en el destino con las mismas reglas de esta
REM copia, para que despues del commit inicial un "git add ." normal siga
REM dejando afuera lo mismo que dejo afuera este script.
REM
REM IMPORTANTE: este bloque va con "disabledelayedexpansion" a proposito.
REM Con "enabledelayedexpansion" (activado al principio del script para el
REM chequeo de API keys mas abajo), un "echo !algo!" NO imprime el "!" —
REM cmd lo interpreta como el inicio/fin de una expansion de variable
REM retrasada y lo borra. Eso rompia la linea "!workspace/.gitkeep", que
REM terminaba escrita SIN el "!" inicial en el .gitignore generado — o
REM sea que la excepcion no funcionaba y "workspace/.gitkeep" quedaba
REM igual de ignorado que el resto de la carpeta.
setlocal disabledelayedexpansion
> "%DESTINO%\.gitignore" (
    echo # Generado automaticamente por "Crear carpeta limpia para GitHub.bat"
    echo __pycache__/
    echo .git/
    echo .claude/
    echo .vscode/
    echo .idea/
    echo venv/
    echo .venv/
    echo env/
    echo ENV/
    echo build/
    echo dist/
    echo _backups/
    echo _to_delete/
    echo Claude outputs/
    echo sovnode_patch/
    echo.
    echo # Sandbox del LLM: se versiona la carpeta pero no su contenido generado
    echo workspace/*
    echo !workspace/.gitkeep
    echo.
    echo # Memoria / RAG de SovNode -- listado explicito por nombre, ademas
    echo # de los patrones genericos de mas abajo ^(*.db, *.wal, *.faiss,
    echo # *.meta.json, *.log^). Estos son datos del USUARIO -- historial,
    echo # embeddings, log de auditoria -- nunca codigo del proyecto.
    echo sovnode_memory.db
    echo sovnode_memory.db-journal
    echo sovnode_memory.db-shm
    echo sovnode_memory.db-wal
    echo sovnode.wal
    echo sovnode_debug.log
    echo workspace_vector_index.*
    echo longterm_vector_index.*
    echo.
    echo *.pyc
    echo *.pyo
    echo *$py.class
    echo *.so
    echo *.zip
    echo *.exe
    echo *.db
    echo *.db-journal
    echo *.db-shm
    echo *.db-wal
    echo *.wal
    echo *.faiss
    echo *.meta.json
    echo *.log
    echo *.tmp
    echo *.swp
    echo Thumbs.db
    echo desktop.ini
    echo .DS_Store
    echo *.bak*
)
endlocal

echo.
echo ============================================================
echo  Verificando que no haya API keys sueltas en texto plano...
echo ============================================================
REM patch_bat1 (2026-09-20): se agrega el patron de Gemini/Google
REM ("AIzaSy...") ademas del de Anthropic -- ver el comentario del
REM encabezado de este archivo para el porque.
set "FOUND_KEY=0"
findstr /S /M /R /C:"sk-ant-[A-Za-z0-9_-]" "%DESTINO%\*.*" >nul 2>&1
if %ERRORLEVEL%==0 set "FOUND_KEY=1"
findstr /S /M /R /C:"AIzaSy[A-Za-z0-9_-]" "%DESTINO%\*.*" >nul 2>&1
if %ERRORLEVEL%==0 set "FOUND_KEY=1"

if "!FOUND_KEY!"=="1" (
    echo.
    echo  !!! ATENCION !!! Se encontro un patron parecido a una API key
    echo  ^(Anthropic "sk-ant-..." o Google/Gemini "AIzaSy..."^) en algun
    echo  archivo dentro de la carpeta destino. Revisa antes de subir esto
    echo  a GitHub. Ejecuta:
    echo    findstr /S /M /R /C:"sk-ant-[A-Za-z0-9_-]" "%DESTINO%\*.*"
    echo    findstr /S /M /R /C:"AIzaSy[A-Za-z0-9_-]" "%DESTINO%\*.*"
    echo  para ver en que archivo esta.
) else (
    echo  OK: no se encontro ningun patron de API key ^(Anthropic ni
    echo  Google/Gemini^) en el destino.
    echo  ^(Las keys de SovNode se guardan en el Registro de Windows, no en
    echo  archivos del proyecto, asi que esto era lo esperado.^)
)

echo.
echo ============================================================
echo  Listo. Carpeta limpia creada en:
echo  %DESTINO%
echo.
echo  Proximos pasos para subirla a GitHub (si todavia no es un repo):
echo    cd /d "%DESTINO%"
echo    git init
echo    git add .
echo    git commit -m "Version inicial"
echo    git branch -M main
echo    git remote add origin ^<URL-de-tu-repo-en-GitHub^>
echo    git push -u origin main
echo ============================================================
pause
