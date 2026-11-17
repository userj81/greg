// Gera o ícone do Greg.app (PNGs do iconset) com AppKit — sem arquivo binário no git.
// Squircle branco em gradiente 145°, aro escuro a 14% e o "G." da marca (docs/design/marca-greg):
// G da Instrument Serif itálica em contorno, sem depender da fonte instalada, e o ponto na cor de destaque.
// Uso: swift macapp/icone.swift <pasta.iconset>
import AppKit

let destino = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "Vigia.iconset"
try? FileManager.default.createDirectory(atPath: destino, withIntermediateDirectories: true)

func cor(_ hex: UInt32, _ alfa: CGFloat = 1) -> NSColor {
    NSColor(calibratedRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: alfa)
}

// Contornos em sintaxe de caminho SVG (só M, L, H, V, Q e Z absolutos), unidades da fonte (1000 por eme).
let letraG = "M236 9Q150 9 101 -58Q52 -126 52 -253Q52 -306 66 -370Q79 -434 106 -498Q134 -561 175 -614Q216 -666 272 -698Q328 -730 398 -730Q442 -730 473 -722Q504 -715 525 -703Q534 -698 531 -682L501 -530Q498 -513 485 -513Q473 -513 473 -526L472 -555Q469 -641 450 -672Q432 -704 393 -704Q347 -704 310 -679Q272 -654 242 -612Q211 -570 188 -519Q166 -468 152 -415Q137 -362 130 -315Q122 -268 122 -235Q122 -126 154 -72Q187 -17 239 -17Q271 -17 296 -36Q321 -54 332 -103L382 -322Q387 -342 384 -350Q380 -359 357 -361L322 -365Q309 -366 309 -376Q309 -388 327 -388H508Q521 -388 521 -378Q521 -367 506 -365L491 -363Q469 -360 462 -351Q456 -342 451 -321L393 -70Q391 -59 382 -49Q374 -39 356 -28Q331 -13 302 -2Q273 9 236 9Z"
let ponto = "M607 9Q586 9 573 -4Q560 -18 560 -38Q560 -64 578 -80Q596 -96 616 -96Q636 -96 650 -82Q663 -69 663 -49Q663 -23 645 -7Q627 9 607 9Z"

/// Converte o caminho SVG em CGPath.
func contorno(_ d: String) -> CGPath {
    let p = CGMutablePath()
    var numeros: [CGFloat] = []
    var comando: Character = " "
    var atual = CGPoint.zero
    func aplicar() {
        switch comando {
        case "M": atual = CGPoint(x: numeros[0], y: numeros[1]); p.move(to: atual)
        case "L": atual = CGPoint(x: numeros[0], y: numeros[1]); p.addLine(to: atual)
        case "H": atual = CGPoint(x: numeros[0], y: atual.y); p.addLine(to: atual)
        case "V": atual = CGPoint(x: atual.x, y: numeros[0]); p.addLine(to: atual)
        case "Q": atual = CGPoint(x: numeros[2], y: numeros[3]); p.addQuadCurve(to: atual, control: CGPoint(x: numeros[0], y: numeros[1]))
        case "Z": p.closeSubpath()
        default: break
        }
        numeros = []
    }
    var texto = ""
    func fecharNumero() { if !texto.isEmpty { numeros.append(CGFloat(Double(texto)!)); texto = "" } }
    for c in d {
        if c.isLetter { fecharNumero(); if comando != " " { aplicar() }; comando = c }
        else if c == " " || c == "," { fecharNumero() }
        else if c == "-" && !texto.isEmpty { fecharNumero(); texto = "-" }
        else { texto.append(c) }
    }
    fecharNumero(); aplicar()
    return p
}

/// Reforço do contorno, em unidades da fonte: engrossa o traço fino da serifa nos tamanhos pequenos para não sumir.
func reforco(_ px: CGFloat) -> CGFloat {
    switch px {
    case ..<24: return 46
    case ..<48: return 30
    case ..<96: return 14
    default: return 0
    }
}

func desenhar(_ lado: CGFloat) -> NSImage {
    let img = NSImage(size: NSSize(width: lado, height: lado))
    img.lockFocus()
    let ctx = NSGraphicsContext.current!.cgContext
    let r = CGRect(x: 0, y: 0, width: lado, height: lado).insetBy(dx: lado * 0.04, dy: lado * 0.04)
    let raio = lado * 0.225
    let squircle = NSBezierPath(roundedRect: r, xRadius: raio, yRadius: raio)
    // fundo: branco ("quero o ícone no formato branco", 12/11/2026), luz vinda de cima à esquerda (gradiente a 145°)
    let grad = NSGradient(colorsAndLocations: (cor(0xffffff), 0.0), (cor(0xf6f6f8), 0.55), (cor(0xe6e7ec), 1.0))!
    grad.draw(in: squircle, angle: -45)
    // aro: fio escuro bem leve, para o branco não sumir em fundo claro
    let aro = NSBezierPath(roundedRect: r.insetBy(dx: lado * 0.012, dy: lado * 0.012), xRadius: raio - lado * 0.012, yRadius: raio - lado * 0.012)
    aro.lineWidth = max(1, lado * 0.02)
    cor(0x1d1d1f, 0.14).setStroke()
    aro.stroke()
    // "G." da marca: o G itálico em grafite e o ponto na cor de destaque, 56% do lado, centrado.
    // Os contornos estão em coordenadas de SVG (y para baixo), por isso a escala em y é negativa.
    let escala = lado * 0.00092
    ctx.saveGState()
    ctx.translateBy(x: lado / 2 - 357.5 * escala, y: lado / 2 - 360.5 * escala)
    ctx.scaleBy(x: escala, y: -escala)
    ctx.addPath(contorno(letraG))
    ctx.setFillColor(cor(0x1d1d1f).cgColor)
    ctx.setStrokeColor(cor(0x1d1d1f).cgColor)
    ctx.setLineWidth(reforco(lado))
    ctx.setLineJoin(.round)
    ctx.drawPath(using: reforco(lado) > 0 ? .fillStroke : .fill)
    ctx.addPath(contorno(ponto))
    ctx.setFillColor(cor(0x0a84ff).cgColor)
    ctx.setStrokeColor(cor(0x0a84ff).cgColor)
    ctx.drawPath(using: reforco(lado) > 0 ? .fillStroke : .fill)
    ctx.restoreGState()
    img.unlockFocus()
    return img
}

func salvar(_ img: NSImage, _ nome: String, _ px: Int) {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8, samplesPerPixel: 4,
                               hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    img.draw(in: NSRect(x: 0, y: 0, width: px, height: px))
    NSGraphicsContext.restoreGraphicsState()
    let dados = rep.representation(using: .png, properties: [:])!
    try! dados.write(to: URL(fileURLWithPath: "\(destino)/\(nome)"))
}

for (base, escala) in [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)] {
    let px = base * escala
    let nome = escala == 1 ? "icon_\(base)x\(base).png" : "icon_\(base)x\(base)@2x.png"
    salvar(desenhar(CGFloat(px)), nome, px)
}
print("iconset em \(destino)")
