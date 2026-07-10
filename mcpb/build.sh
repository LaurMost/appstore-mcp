#!/usr/bin/env bash
# Build the MCPB bundle (dist/appstore-mcp.mcpb) for Claude Desktop.
#
# The bundle is one-file installable: it ships its own CPython plus vendored
# dependencies, so end users need no Python, uv, or checkout. Everything is
# platform-specific (interpreter + native wheels like pydantic_core), so the
# bundle only runs on the platform it was built on: currently macOS arm64.
#
# Requires: uv, npx, and (to regenerate the icon) Google Chrome.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_VERSION=3.13

# 1. Bundled runtime: a relocatable python-build-standalone CPython.
rm -rf server/python server/runtime-tmp
UV_PYTHON_INSTALL_DIR="$PWD/server/runtime-tmp" uv python install "$PYTHON_VERSION"
mv "$(find server/runtime-tmp -mindepth 1 -maxdepth 1 -type d -name 'cpython-*')" server/python
rm -rf server/runtime-tmp

# 2. Vendor the package + deps, resolved AGAINST THE BUNDLED INTERPRETER.
# Resolving against any other Python silently vendors native wheels for the
# wrong ABI (pydantic_core then fails to import at runtime).
rm -rf server/vendor
uv pip install --python "server/python/bin/python$PYTHON_VERSION" --target server/vendor ..

# 3. Icon: render the packaged SVG to the 512x512 PNG the manifest points at.
# Scale via an <img> wrapper - editing the SVG's own width/height attributes
# is easy to get wrong (a naive substitution also resizes the background rect).
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ -x "$CHROME" ]; then
    TMP=$(mktemp -d)
    cp ../src/appstore_mcp/assets/icon.svg "$TMP/icon.svg"
    printf '<!doctype html><body style="margin:0"><img src="icon.svg" style="width:512px;height:512px;display:block"></body>' > "$TMP/wrap.html"
    "$CHROME" --headless=new --disable-gpu --default-background-color=00000000 \
        --window-size=512,512 --screenshot="$TMP/icon.png" "file://$TMP/wrap.html" >/dev/null 2>&1
    mv "$TMP/icon.png" icon.png
    rm -rf "$TMP"
elif [ ! -f icon.png ]; then
    echo "error: no Chrome to render icon.png and no existing icon.png" >&2
    exit 1
else
    echo "warning: Chrome not found - reusing existing icon.png" >&2
fi

# 4. Validate and pack.
npx --yes @anthropic-ai/mcpb validate manifest.json
mkdir -p ../dist
npx --yes @anthropic-ai/mcpb pack . ../dist/appstore-mcp.mcpb

echo
echo "Built dist/appstore-mcp.mcpb - install by opening it with Claude Desktop."
