from sys import platform as _platform
if _platform == "linux" or _platform == "linux2":
    # linux
    from PyInstaller.hooks.hookutils import collect_data_files

    datas = collect_data_files('osgeo')
