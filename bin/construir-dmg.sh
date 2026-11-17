#!/bin/bash
# Monta dist/Greg.dmg (pedido 49): o Greg.app instalador (roda o Greg no Docker) com o código do projeto dentro.
# Quem chama: você, para gerar o arquivo que as pessoas baixam. Precisa de: Xcode Command Line Tools (swiftc), git, hdiutil.
# Uso: bin/construir-dmg.sh [--fonte PASTA_GIT]
#   --fonte: repositório de onde sai TUDO (código copiado para o app e fontes Swift compiladas), sempre o commit HEAD,
#            nunca alterações locais. Padrão: dist/publico (a edição pública, se existir aqui), senão este repositório.
# Saída: dist/Greg.dmg (fora do git). Assinatura ad-hoc: sem a conta paga de desenvolvedor da Apple, o Mac de quem baixa
# pede "Abrir mesmo assim" em Ajustes do Sistema > Privacidade e Segurança na primeira vez.
set -euo pipefail
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FONTE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --fonte) FONTE="${2:-}"; shift 2 ;;
    *) echo "uso: $0 [--fonte PASTA_GIT]"; exit 2 ;;
  esac
done
if [ -z "$FONTE" ]; then
  if [ -d "$RAIZ/dist/publico/.git" ]; then FONTE="$RAIZ/dist/publico"; else FONTE="$RAIZ"; fi
fi
git -C "$FONTE" rev-parse --verify -q HEAD >/dev/null || { echo "construir-dmg: $FONTE não é um repositório git com commit"; exit 2; }
for p in swiftc swift iconutil lipo hdiutil codesign plutil; do
  command -v "$p" >/dev/null 2>&1 || { echo "construir-dmg: falta $p (xcode-select --install)"; exit 2; }
done

SHA="$(git -C "$FONTE" rev-parse --short HEAD)"
DIA="$(date +%Y.%m.%d)"
VERSAO="$DIA.$SHA"   # CFBundleVersion: mudou = o app instalado recopia o código e remonta a imagem
TRAB="$(mktemp -d "${TMPDIR:-/tmp}/vigia-dmg.XXXXXX")"
trap 'rm -rf "$TRAB"' EXIT
APP="$TRAB/Greg.app"
CODIGO="$APP/Contents/Resources/vigia"
mkdir -p "$APP/Contents/MacOS" "$CODIGO"

echo "== código (commit $SHA de $FONTE)"
git -C "$FONTE" archive --format=tar HEAD | tar -x -C "$CODIGO"
[ -f "$CODIGO/docker-compose.yml" ] && [ -f "$CODIGO/bin/preparar-docker.sh" ] \
  || { echo "construir-dmg: o commit não tem docker-compose.yml/bin/preparar-docker.sh"; exit 2; }

echo "== ícone"
swift "$CODIGO/macapp/icone.swift" "$TRAB/Vigia.iconset" >/dev/null
iconutil -c icns "$TRAB/Vigia.iconset" -o "$APP/Contents/Resources/Greg.icns"

echo "== compilando (Apple Silicon e Intel)"
for arq in arm64 x86_64; do
  swiftc -O -target "$arq-apple-macos13.0" -framework AppKit -framework WebKit -framework LocalAuthentication -framework UserNotifications \
    -o "$TRAB/Vigia-$arq" "$CODIGO"/macapp/Vigia/*.swift
done
lipo -create -output "$APP/Contents/MacOS/Vigia" "$TRAB/Vigia-arm64" "$TRAB/Vigia-x86_64"

PL="$APP/Contents/Info.plist"
cp "$CODIGO/macapp/Info.plist" "$PL"
plutil -remove VigiaRaiz "$PL" 2>/dev/null || true
plutil -replace VigiaModo -string docker "$PL"
plutil -replace CFBundleIdentifier -string io.vigia.app "$PL"
plutil -replace CFBundleVersion -string "$VERSAO" "$PL"
plutil -replace CFBundleShortVersionString -string "$DIA" "$PL"
plutil -replace NSHumanReadableCopyright -string "código aberto · tudo no seu Mac" "$PL"
plutil -lint "$PL" >/dev/null
printf 'APPL????' > "$APP/Contents/PkgInfo"
codesign --force --deep --sign - --identifier io.vigia.app "$APP" >/dev/null 2>&1
codesign --verify --deep "$APP"

echo "== DMG"
PALCO="$TRAB/palco"
mkdir -p "$PALCO" "$RAIZ/dist"
cp -R "$APP" "$PALCO/"
ln -s /Applications "$PALCO/Aplicativos"
SAIDA="$RAIZ/dist/Greg.dmg"
rm -f "$SAIDA"
hdiutil create -quiet -volname "Greg" -srcfolder "$PALCO" -fs HFS+ -format UDZO -ov "$SAIDA"
xattr -c "$SAIDA" 2>/dev/null || true
# segurança r01 (M3): o DMG sai sem notarização; o hash publicado junto é o que deixa quem baixa conferir o arquivo
( cd "$RAIZ/dist" && shasum -a 256 Greg.dmg > Greg.dmg.sha256 )
echo "sha256: $(cut -d' ' -f1 "$RAIZ/dist/Greg.dmg.sha256")  (publique dist/Greg.dmg.sha256 na mesma release)"
echo "ok: $SAIDA ($(du -h "$SAIDA" | cut -f1), versão $VERSAO)"
