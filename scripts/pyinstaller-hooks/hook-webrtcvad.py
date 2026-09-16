from PyInstaller.utils.hooks import copy_metadata


# webrtcvad-wheels provides the ``webrtcvad`` module, but its distribution
# metadata uses the project name ``webrtcvad-wheels``.  The upstream
# PyInstaller hook looks for metadata named ``webrtcvad`` and aborts the build.
datas = copy_metadata("webrtcvad-wheels")
hiddenimports = ["_webrtcvad"]
