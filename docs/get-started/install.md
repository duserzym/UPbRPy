# Install

Download Studio from [GitHub Releases](https://github.com/duserzym/UPbRPy/releases).
The macOS arm64 build is signed with the lab's own certificate and is not
notarized. Unzip it, move **UPbRPy Studio.app** to Applications, then launch it.
If macOS blocks the first launch, use **System Settings > Privacy & Security >
Open Anyway** after attempting to open it. Subsequent launches use the installed app.

## Install from source

Python 3.12 or newer is required. From a source checkout:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[gui]"
upbrpy-gui
```

On Windows, activate with `.venv\Scripts\activate`. `python -m GUI` also
starts Studio. For the engine alone use `python -m pip install -e .`.
Optional extras are `gui`, `database`, `notebook`, `export`, `docs`, `build`
and `dev`; install only those needed for the task. The database actions require
`.[gui,database]`. See [Python API](../reference/python-api.md) for engine use.
