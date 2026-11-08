# Empaquetado con PyInstaller (carpeta, sin consola). Se ejecuta desde scripts/release.py.
a = Analysis(
    ["main.py"],
    datas=[("alpekabot/assets", "alpekabot/assets")],
    # keyring elige su backend en tiempo de ejecución: PyInstaller no lo detecta solo
    hiddenimports=["keyring.backends.Windows"],
    excludes=["pytest", "tests"],
)
# Las herramientas de desarrollo de WebEngine solo sirven para depurar páginas (83 MB)
a.datas = [d for d in a.datas if "qtwebengine_devtools_resources" not in d[0]]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AlpekaBOT", console=False, upx=False,
          icon="alpekabot/assets/app.ico")
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="AlpekaBOT")
