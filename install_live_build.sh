#!/bin/bash
# Install the live-capable GUI build into your Kotak repo and push.
#   bash install_live_build.sh /Users/navneetsmac/Desktop/Kotak_api
set -e
REPO="${1:-/Users/navneetsmac/Desktop/Kotak_api}"
SRC="$(cd "$(dirname "$0")" && pwd)"

[ -d "$REPO" ] || { echo "Repo not found: $REPO"; exit 1; }

echo "Installing into: $REPO"
mkdir -p "$REPO/.github/workflows"
cp "$SRC/build.yml"          "$REPO/.github/workflows/build.yml"
cp "$SRC/app.py"             "$REPO/app.py"
cp "$SRC/paper_strategy.py"  "$REPO/paper_strategy.py"
cp "$SRC/strategy_config.py" "$REPO/strategy_config.py"
cp "$SRC/order_router.py"    "$REPO/order_router.py"

cd "$REPO"

# make sure requirements has darkdetect (customtkinter dep) for the build
grep -q "^darkdetect" requirements.txt 2>/dev/null || echo "darkdetect" >> requirements.txt

echo
echo "--- files the EXE needs ---"
for f in app.py config_loader.py kotak_ws_base.py paper_strategy.py \
         option_chain.py candle_engine.py strategy_config.py order_router.py; do
    [ -f "$f" ] && echo "  ok      $f" || echo "  MISSING $f"
done

echo
git add -A
git commit -m "Live-capable GUI: Rule B, per-side toggles, targets, order router" \
    || echo "nothing to commit"
git push
echo
echo "Build: $(git remote get-url origin | sed 's/\.git$//;s#git@github.com:#https://github.com/#')/actions"
echo "Artifact when green: BalfundKotakHA-Windows"
