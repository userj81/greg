#!/bin/bash
# Compila o Greg.app (Swift/AppKit/WKWebView, sem dependência) e instala em ~/Applications.
# Uso: bin/construir-app.sh [--sem-instalar]
# Saída: dist/Greg.app (fora do git). Assinatura ad-hoc (codesign -s -): roda local sem Gatekeeper reclamar.
set -euo pipefail
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$RAIZ/dist"
APP="$DIST/Greg.app"
rm -rf "$APP" "$DIST/Vigia.iconset"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "== ícone"
swift "$RAIZ/macapp/icone.swift" "$DIST/Vigia.iconset" >/dev/null
iconutil -c icns "$DIST/Vigia.iconset" -o "$APP/Contents/Resources/Greg.icns"
rm -rf "$DIST/Vigia.iconset"

echo "== compilando"
swiftc -O -target arm64-apple-macos13.0 \
  -framework AppKit -framework WebKit -framework LocalAuthentication -framework UserNotifications \
  -o "$APP/Contents/MacOS/Vigia" "$RAIZ"/macapp/Vigia/*.swift

cp "$RAIZ/macapp/Info.plist" "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist" >/dev/null
printf 'APPL????' > "$APP/Contents/PkgInfo"
codesign --force --sign - --identifier io.vigia.vigia "$APP" >/dev/null 2>&1
echo "ok: $APP"

if [ "${1:-}" != "--sem-instalar" ]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/Greg.app"
  rm -rf "$HOME/Applications/Vigia.app"   # nome antigo do app (até 11/11/2026): sai para não ficarem dois no Launchpad
  cp -R "$APP" "$HOME/Applications/Greg.app"
  echo "instalado: $HOME/Applications/Greg.app"
fi
