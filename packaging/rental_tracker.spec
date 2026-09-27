# PyInstaller recipe for the Windows app.
#
#   pip install -r packaging/requirements-build.txt && pip install .
#   pyinstaller packaging/rental_tracker.spec --noconfirm
#
# Produces two builds from one analysis:
#   dist/RentalTracker-Portable.exe   one file, runs from anywhere (starts a little slower)
#   dist/RentalTracker/               folder build that packaging/installer.iss turns into a setup .exe
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = SPECPATH  # noqa: F821 (defined by PyInstaller)
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)
import rental_tracker  # noqa: E402

VERSION = rental_tracker.__version__
ICON = os.path.join(HERE, "icon.ico")


def version_info():
    """File properties shown in Windows (right-click → Properties → Details)."""
    if sys.platform != "win32":
        return None
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)
    nums = tuple((list(map(int, VERSION.split("."))) + [0, 0, 0, 0])[:4])
    strings = [StringStruct("CompanyName", "Rental Tracker"),
               StringStruct("FileDescription", "Rental Tracker - offline rental property manager"),
               StringStruct("FileVersion", VERSION), StringStruct("InternalName", "RentalTracker"),
               StringStruct("OriginalFilename", "RentalTracker.exe"), StringStruct("ProductName", "Rental Tracker"),
               StringStruct("ProductVersion", VERSION)]
    return VSVersionInfo(ffi=FixedFileInfo(filevers=nums, prodvers=nums),
                         kids=[StringFileInfo([StringTable("040904B0", strings)]),
                               VarFileInfo([VarStruct("Translation", [1033, 1200])])])


a = Analysis(  # noqa: F821
    [os.path.join(HERE, "launch.py")],
    pathex=[SRC],
    datas=collect_data_files("rental_tracker"),          # templates, CSS/JS, SQL migrations
    hiddenimports=collect_submodules("rental_tracker"),
    excludes=["tkinter", "pytest", "_pytest", "hypothesis", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

portable = EXE(  # noqa: F821
    pyz, a.scripts, a.binaries, a.datas, [],
    name="RentalTracker-Portable", icon=ICON, version=version_info(),
    console=False, upx=False, runtime_tmpdir=None,
)

exe = EXE(  # noqa: F821
    pyz, a.scripts, [], exclude_binaries=True,
    name="RentalTracker", icon=ICON, version=version_info(),
    console=False, upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="RentalTracker", upx=False)  # noqa: F821
