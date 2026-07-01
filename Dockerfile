# Builds the Windows ER_Save_Watcher.exe from Linux/macOS via Wine.
#
# PyInstaller only ever produces an executable for the platform it runs on, so
# to get a Windows .exe without a Windows machine we run a Windows Python under
# Wine. tobix/pywine ships the official python.org installer (so tkinter -- which
# the overlay needs -- is bundled) inside a configured Wine prefix; everything
# Windows-side is invoked through `wine`.
#
# Build the image (compiles the exe into the image), then run it with a host
# directory mounted at /out to copy the exe back out:
#
#   docker build -t ersavewatcher-build .
#   docker run --rm -v "$(pwd)/dist:/out" ersavewatcher-build
#
# That leaves dist/ER_Save_Watcher.exe on your host. See README for the two
# alternative extraction methods (docker cp, and BuildKit --output).
#
# (The tag below pins Python 3.12 to match what the script is developed against.)
FROM tobix/pywine:3.12

WORKDIR /src

# Install the build toolchain into the Wine prefix's Python. cryptography is a
# runtime dependency for the dsr/ds3/ds2 save formats, so PyInstaller needs it
# importable at build time to bundle it into the exe.
RUN wine python -m pip install --no-cache-dir --upgrade pip && \
    wine python -m pip install --no-cache-dir pyinstaller cryptography

COPY er_save_watcher.py .

# --onefile: single self-contained exe; --windowed: no console window (it's a
# tkinter overlay app). Produces dist/ER_Save_Watcher.exe.
RUN wine python -m PyInstaller --onefile --windowed --name ER_Save_Watcher er_save_watcher.py

# Default action on `docker run`: copy the built exe into a directory the host
# has mounted at /out. Override with `docker cp` instead if you prefer.
CMD ["sh", "-c", "cp dist/ER_Save_Watcher.exe /out/ && echo 'Wrote ER_Save_Watcher.exe to the mounted /out directory'"]
