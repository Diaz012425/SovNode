# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src\\ui\\sovnode_qt.py'],
    pathex=['src', 'src/core', 'src/tools', 'src/ui'],
    binaries=[],
    # 'logo.ico' vive en la RAÍZ del proyecto (junto a este .spec, ver
    # build.py::resolve_icon) — el mismo lugar de donde lo lee
    # get_resource_path() en sovnode_qt.py en modo NO empaquetado
    # (relativo a _PROJECT_ROOT). El destino '.' lo pone en la raíz del
    # bundle de PyInstaller (MEIPASS), así que get_resource_path()
    # resuelve exactamente igual (os.path.join(sys._MEIPASS, "logo.ico"))
    # en los dos modos. `icon=['logo.ico']` más abajo SOLO incrusta el
    # ícono en los metadatos del .exe (lo que muestra el Explorador/la
    # barra de tareas antes de que la ventana pinte nada) — NO agrega el
    # archivo al bundle; sin la entrada correspondiente acá en `datas`,
    # get_resource_path("logo.ico") no encontraba nada dentro de MEIPASS
    # en tiempo de ejecución y setWindowIcon()/el ícono de la bandeja
    # quedaban con un QIcon vacío (bug real, reportado: "no tiene icono").
    # 'assets/fonts' (los 4 .otf de Inter + su licencia OFL, ver
    # load_app_fonts() en sovnode_qt.py) sigue el mismo patrón que
    # logo.ico arriba: get_resource_path() en modo NO empaquetado los lee
    # relativos a _PROJECT_ROOT, así que el destino aquí tiene que ser el
    # mismo 'assets/fonts' dentro de MEIPASS para que ambos modos
    # resuelvan igual. Sin esta entrada, un build empaquetado arrancaría
    # con la fuente por defecto del sistema en vez de Inter -
    # degradación silenciosa (ver el BLINDAJE de load_app_fonts), no un
    # crash, pero perdería la tipografía propia del rediseño.
    datas=[
        ('logo.ico', '.'),
        ('src/logo.png', 'src'),
        ('assets/fonts', 'assets/fonts'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SovNode',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['logo.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SovNode',
)
