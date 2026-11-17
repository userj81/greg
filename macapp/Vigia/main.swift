// Greg.app — janela nativa macOS sobre o servidor local do painel (D-36).
// Quem chama: o usuário (Dock/Launchpad). Compilado por bin/construir-app.sh (swiftc, sem dependência).
// O que faz: pede Touch ID (ou a senha do Mac), lê VIGIA_TOKEN do .env 0600 (Keychain só se VIGIA_KEYCHAIN=on, D-45), entra no
// painel e mostra as telas num WKWebView preso a http://127.0.0.1:<porta>. Nada sai do Mac; nenhuma outra origem é aberta.
// r02 (D-39): quando a tela de Conversas pede para enviar com a trava fechada, pede o Touch ID de novo, lê
// VIGIA_TOKEN_ENVIO de ~/.vigia-envio.env 0600 (Keychain só se ligado) e abre a trava pelo tempo escolhido em Ajustes (POST /envio/liberar).
// O token não passa pelo JavaScript da página.
import AppKit
import WebKit
import LocalAuthentication
import UserNotifications

final class Ambiente {
    let raiz: URL
    private let portaFixa: Int
    /// No app do DMG a porta do painel é lida na hora: o instalador troca a 8120 por outra quando ela está ocupada
    /// neste Mac e grava VIGIA_PORTA_HOST no docker.env (bin/preparar-docker.sh --portas-livres). A variável de
    /// ambiente vence o arquivo (teste). No app pessoal a porta vem do .env, lida uma vez.
    var porta: Int {
        guard modoDocker else { return portaFixa }
        if let p = Int(ProcessInfo.processInfo.environment["VIGIA_PORTA_HOST"] ?? "") { return p }
        return Int(Ambiente.lerEnv(arquivoEnv)["VIGIA_PORTA_HOST"] ?? "") ?? portaFixa
    }
    /// Pedido 49: app do DMG (Info.plist VigiaModo=docker) roda o Greg no Docker, instalado pelo Instalador; o .env é o
    /// docker.env do projeto copiado para ~/Library/Application Support/Vigia/vigia.
    let modoDocker: Bool
    var arquivoEnv: URL { raiz.appendingPathComponent(modoDocker ? "docker.env" : ".env") }
    /// Lido na hora: no app do DMG o docker.env só nasce durante a instalação.
    var token: String? {
        Ambiente.segredo("VIGIA_TOKEN") ?? Ambiente.lerEnv(arquivoEnv)["VIGIA_TOKEN"].flatMap { $0.isEmpty ? nil : $0 }
    }

    init() {
        modoDocker = (Bundle.main.object(forInfoDictionaryKey: "VigiaModo") as? String) == "docker"
            || ProcessInfo.processInfo.environment["VIGIA_MODO"] == "docker"
        if modoDocker {
            raiz = Instalador.pastaPadrao().appendingPathComponent("vigia")
            portaFixa = 8120
            return
        }
        let padrao = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("vigia-whatsapp")
        if let r = ProcessInfo.processInfo.environment["VIGIA_RAIZ"], !r.isEmpty {
            raiz = URL(fileURLWithPath: r)
        } else if let r = Bundle.main.object(forInfoDictionaryKey: "VigiaRaiz") as? String, !r.isEmpty {
            raiz = URL(fileURLWithPath: (r as NSString).expandingTildeInPath)
        } else {
            raiz = padrao
        }
        let env = Ambiente.lerEnv(raiz.appendingPathComponent(".env"))
        portaFixa = Int(env["VIGIA_PORT"] ?? "") ?? 8120   // D-45: token vem do .env (0600); Keychain opt-in
    }

    var base: URL { URL(string: "http://127.0.0.1:\(porta)")! }

    /// Item genérico do Keychain (serviço io.vigia.vigia, conta = nome), lido pelo MESMO `/usr/bin/security`
    /// que o app/segredos.py usa: é o único programa na ACL do item, então não aparece a janela "quer acessar o
    /// Keychain" a cada rebuild do app (assinatura ad-hoc muda a identidade do binário). O valor vem pelo stdout do
    /// processo filho, nunca por argv.
    static func segredo(_ nome: String) -> String? {
        // Keychain é opt-in (decisão do dono, 07/11/2026): sem VIGIA_KEYCHAIN=on, o app nem chama o `security`.
        if ProcessInfo.processInfo.environment["VIGIA_KEYCHAIN"] != "on" { return nil }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        p.arguments = ["find-generic-password", "-s", "io.vigia.vigia", "-a", nome, "-w"]
        let saida = Pipe()
        p.standardOutput = saida
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return nil }
        p.waitUntilExit()
        guard p.terminationStatus == 0 else { return nil }
        let dados = saida.fileHandleForReading.readDataToEndOfFile()
        let valor = String(decoding: dados, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return valor.isEmpty ? nil : valor
    }

    // .env simples: CHAVE=valor, comentário com '#', aspas opcionais. Mesma regra do vigia.sh.
    static func lerEnv(_ arquivo: URL) -> [String: String] {
        guard let texto = try? String(contentsOf: arquivo, encoding: .utf8) else { return [:] }
        var saida: [String: String] = [:]
        for linhaCrua in texto.split(separator: "\n") {
            var linha = String(linhaCrua).trimmingCharacters(in: .whitespaces)
            if linha.isEmpty || linha.hasPrefix("#") { continue }
            if let h = linha.range(of: " #") { linha = String(linha[..<h.lowerBound]) }
            guard let eq = linha.firstIndex(of: "=") else { continue }
            let chave = linha[..<eq].trimmingCharacters(in: .whitespaces)
            var valor = linha[linha.index(after: eq)...].trimmingCharacters(in: .whitespaces)
            if valor.count >= 2, (valor.hasPrefix("\"") && valor.hasSuffix("\"")) || (valor.hasPrefix("'") && valor.hasSuffix("'")) {
                valor = String(valor.dropFirst().dropLast())
            }
            saida[chave] = valor
        }
        return saida
    }
}

/// Pedido 20: com a barra de título transparente o WKWebView cobre a janela inteira e engole o clique, então não dava
/// para arrastar. Esta faixa fica por cima do web (ver montarJanela): arrastar move a janela, duplo clique faz o que
/// o Mac manda em Ajustes do Sistema (ampliar ou minimizar). O CSS do painel não põe nada clicável embaixo dela.
final class AlcaDaJanela: NSView {
    override var mouseDownCanMoveWindow: Bool { true }
    override func mouseDown(with event: NSEvent) {
        guard let w = window else { return }
        if event.clickCount == 2 {
            let acao = UserDefaults.standard.string(forKey: "AppleActionOnDoubleClick") ?? "Maximize"
            if acao == "Minimize" { w.performMiniaturize(nil) } else if acao != "None" { w.performZoom(nil) }
            return
        }
        w.performDrag(with: event)
    }
}

final class Janela: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKDownloadDelegate {
    let amb = Ambiente()
    var janela: NSWindow!
    var web: WKWebView!
    var alcaLateral: AlcaDaJanela!
    /// Onde terminam os botões da janela (semáforo), com folga: é a largura da alça quando a lateral está recolhida e
    /// o ponto a partir do qual a página pode pôr o botão de mostrar a lateral. Medido na janela real em montarJanela.
    var semaforo: CGFloat = 78
    var tentativas = 0
    var instalador: Instalador?          // só no app do DMG (pedido 49)
    var destinoInicial: String?          // depois da primeira instalação, abre direto em Conexão (QR)

    func applicationDidFinishLaunching(_ n: Notification) {
        Avisos.pedirPermissao()
        montarMenu()
        montarJanela()
        if amb.modoDocker {
            instalador = Instalador(janela: self)
            instalador?.comecar()
        } else {
            autenticarEEntrar()
        }
    }

    /// O Instalador terminou: Docker no ar, painel respondendo. Entra como sempre (Touch ID + token).
    func instalacaoPronta(primeiraVez: Bool) {
        if primeiraVez { destinoInicial = "/conexao" }
        tentativas = 0
        autenticarEEntrar()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    // MARK: janela
    func montarJanela() {
        janela = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1120, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        janela.title = "Greg"
        janela.titlebarAppearsTransparent = true
        janela.titleVisibility = .hidden
        janela.minSize = NSSize(width: 820, height: 560)
        // Pedido 61: a barra da página tem 52 pt e os botões da janela ficavam no alto dos 28 pt padrão, fora da linha
        // do título e dos ícones. Uma barra de ferramentas vazia no estilo unificado dá à janela a mesma altura da barra
        // da página e o macOS centraliza os botões nela.
        let barra = NSToolbar(identifier: "greg.barra")
        janela.toolbar = barra
        janela.toolbarStyle = .unified
        janela.layoutIfNeeded()
        if let zoom = janela.standardWindowButton(.zoomButton) {
            semaforo = min(max((zoom.frame.maxX + 12).rounded(), 60), 120)
        }

        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .nonPersistent()   // sessão só em memória: fechar o app = sair
        cfg.preferences.isFraudulentWebsiteWarningEnabled = false
        cfg.userContentController.add(self, name: "vigia")   // Touch ID para liberar o envio; largura da lateral (alça)
        // marca a página como "dentro do app" (a lateral desce para não ficar sob os botões da janela) e diz o nome da
        // cor de destaque do Mac, que Ajustes mostra; a cor em si o CSS do painel lê de AccentColor (estilo Papel).
        // Pedido 48: data-alca avisa que a alça de arrastar acompanha a lateral (a página pode recolher e estreitar)
        // e --semaforo diz onde terminam os botões da janela.
        cfg.userContentController.addUserScript(WKUserScript(
            source: "document.documentElement.setAttribute('data-app', 'mac');"
                + "document.documentElement.setAttribute('data-alca', '1');"
                + "document.documentElement.style.setProperty('--semaforo', '\(Int(semaforo))px');"
                + "document.documentElement.setAttribute('data-acento-nome', '\(Janela.nomeDoAcento())');",
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = true
        web.setValue(false, forKey: "drawsBackground")

        let conteudo = NSView(frame: NSRect(x: 0, y: 0, width: 1120, height: 760))
        web.frame = conteudo.bounds
        web.autoresizingMask = [.width, .height]
        conteudo.addSubview(web)
        // Duas alças por cima do web, só onde o painel não põe nada clicável (estilo Papel, pedido 39): o topo da
        // lateral (212 × 46 de início, a faixa dos botões da janela) e uma tira de 10 pt no topo das toolbars (os
        // controles começam em 12 px). Uma faixa de 28 pt inteira engolia a metade de cima dos botões da toolbar.
        // Pedido 48: a página avisa a largura da lateral e a alça acompanha (ajustarAlca).
        alcaLateral = AlcaDaJanela(frame: NSRect(x: 0, y: conteudo.bounds.height - 46, width: 212, height: 46))
        alcaLateral.autoresizingMask = [.minYMargin]
        conteudo.addSubview(alcaLateral)
        let alcaTopo = AlcaDaJanela(frame: NSRect(x: 0, y: conteudo.bounds.height - 10, width: conteudo.bounds.width, height: 10))
        alcaTopo.autoresizingMask = [.width, .minYMargin]
        conteudo.addSubview(alcaTopo)
        janela.contentView = conteudo
        janela.setFrameAutosaveName("VigiaJanela")
        janela.center()
        janela.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func montarMenu() {
        let principal = NSMenu()
        let app = NSMenuItem(); principal.addItem(app)
        let mApp = NSMenu()
        mApp.addItem(withTitle: "Sobre o Greg", action: #selector(sobre), keyEquivalent: "")
        mApp.addItem(.separator())
        mApp.addItem(withTitle: "Ajustes…", action: #selector(irAjustes), keyEquivalent: ",")
        mApp.addItem(.separator())
        mApp.addItem(withTitle: "Ocultar Greg", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        mApp.addItem(.separator())
        mApp.addItem(withTitle: "Encerrar Greg", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        app.submenu = mApp

        let editar = NSMenuItem(); principal.addItem(editar)
        let mEd = NSMenu(title: "Editar")
        mEd.addItem(withTitle: "Desfazer", action: #selector(desfazer), keyEquivalent: "z")
        mEd.addItem(withTitle: "Refazer", action: Selector(("redo:")), keyEquivalent: "Z")
        mEd.addItem(.separator())
        mEd.addItem(withTitle: "Recortar", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        mEd.addItem(withTitle: "Copiar", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        mEd.addItem(withTitle: "Colar", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        mEd.addItem(withTitle: "Selecionar tudo", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editar.submenu = mEd

        let ver = NSMenuItem(); principal.addItem(ver)
        // Mesma ordem e mesmos atalhos da lateral do painel (estilo Papel: ⌘1–5 nas seções; Conexão fica no rodapé).
        let mVer = NSMenu(title: "Visualizar")
        mVer.addItem(withTitle: "Demandas", action: #selector(irDemandas), keyEquivalent: "1")
        mVer.addItem(withTitle: "Conversas", action: #selector(irConversas), keyEquivalent: "2")
        mVer.addItem(withTitle: "Frentes", action: #selector(irFrentes), keyEquivalent: "3")
        mVer.addItem(withTitle: "Envios", action: #selector(irEnvios), keyEquivalent: "4")
        mVer.addItem(withTitle: "Contatos", action: #selector(irContatos), keyEquivalent: "5")
        mVer.addItem(withTitle: "Histórico", action: #selector(irHistorico), keyEquivalent: "6")
        mVer.addItem(withTitle: "Conexão", action: #selector(irConexao), keyEquivalent: "")
        mVer.addItem(withTitle: "Métricas", action: #selector(irMetricas), keyEquivalent: "")
        mVer.addItem(withTitle: "Conversas bloqueadas", action: #selector(irBloqueios), keyEquivalent: "")
        mVer.addItem(.separator())
        let lateral = mVer.addItem(withTitle: "Mostrar ou ocultar a lateral", action: #selector(lateral), keyEquivalent: "s")
        lateral.keyEquivalentModifierMask = [.command, .control]
        mVer.addItem(.separator())
        mVer.addItem(withTitle: "Nova mensagem", action: #selector(irNova), keyEquivalent: "n")
        mVer.addItem(withTitle: "Buscar", action: #selector(buscar), keyEquivalent: "f")
        mVer.addItem(.separator())
        let pausar = mVer.addItem(withTitle: "Pausar ou retomar leituras", action: #selector(pausar), keyEquivalent: "P")
        pausar.keyEquivalentModifierMask = [.command, .shift]
        let gravar = mVer.addItem(withTitle: "Modo de gravação", action: #selector(gravar), keyEquivalent: "B")
        gravar.keyEquivalentModifierMask = [.command, .shift]
        mVer.addItem(.separator())
        mVer.addItem(withTitle: "Recarregar", action: #selector(recarregar), keyEquivalent: "r")
        ver.submenu = mVer

        let jan = NSMenuItem(); principal.addItem(jan)
        let mJan = NSMenu(title: "Janela")
        mJan.addItem(withTitle: "Minimizar", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        mJan.addItem(withTitle: "Fechar", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        jan.submenu = mJan
        NSApp.windowsMenu = mJan
        NSApp.mainMenu = principal
    }

    @objc func sobre() {
        let a = NSAlert()
        a.messageText = "Greg"
        a.informativeText = "Leitura do WhatsApp pessoal, só no Mac.\nServidor local: \(amb.base.absoluteString)\nPasta: \(amb.raiz.path)"
        a.runModal()
    }
    @objc func recarregar() { web.reload() }
    @objc func irConexao() { ir("/conexao") }
    @objc func irDemandas() { ir("/demandas") }
    @objc func irConversas() { ir("/conversas") }
    @objc func irFrentes() { ir("/frentes") }
    @objc func irEnvios() { ir("/envios") }
    @objc func irContatos() { ir("/contatos") }
    @objc func irHistorico() { ir("/historico") }
    @objc func irNova() { ir("/conversas/nova") }
    @objc func irAjustes() { ir("/ajustes") }
    @objc func irMetricas() { ir("/metricas") }
    @objc func irBloqueios() { ir("/bloqueios") }
    @objc func buscar() { atalho("buscar") }
    @objc func pausar() { atalho("pausar") }
    @objc func gravar() { atalho("gravar") }
    @objc func lateral() { atalho("lateral") }
    /// ⌘Z: primeiro o "Desfazer" do painel (toast da triagem, D-25+); se a página não tinha o que desfazer, o undo do texto.
    @objc func desfazer() {
        web.evaluateJavaScript("!!(window.vigiaAtalho && window.vigiaAtalho('desfazer'))") { valor, _ in
            if (valor as? Bool) != true { NSApp.sendAction(Selector(("undo:")), to: nil, from: self) }
        }
    }
    func atalho(_ nome: String) { web.evaluateJavaScript("window.vigiaAtalho && window.vigiaAtalho('\(nome)')", completionHandler: nil) }
    func ir(_ caminho: String) { web.load(URLRequest(url: amb.base.appendingPathComponent(caminho))) }

    /// Nome da cor de destaque escolhida em Ajustes do Sistema (AppleAccentColor no domínio global; ausente = azul).
    static func nomeDoAcento() -> String {
        let nomes = [-1: "Grafite", 0: "Vermelho", 1: "Laranja", 2: "Amarelo", 3: "Verde", 4: "Azul", 5: "Roxo", 6: "Rosa"]
        guard let v = UserDefaults.standard.object(forKey: "AppleAccentColor") as? Int else { return "Azul" }
        return nomes[v] ?? "Azul"
    }

    // MARK: autenticação local (Touch ID ou senha do Mac) e login no painel
    func autenticarEEntrar() {
        let ctx = LAContext()
        ctx.localizedCancelTitle = "Cancelar"
        var erro: NSError?
        if ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &erro) {
            ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: "abrir o Greg") { ok, _ in
                DispatchQueue.main.async {
                    if ok { self.entrar(); return }
                    // lote 5: cancelar o Touch ID fechava o app sem dizer nada
                    let a = NSAlert()
                    a.messageText = "O Greg não abriu"
                    a.informativeText = "A confirmação do Touch ID foi cancelada. As leituras automáticas continuam rodando."
                    a.addButton(withTitle: "Tentar de novo")
                    a.addButton(withTitle: "Fechar")
                    if a.runModal() == .alertFirstButtonReturn { self.autenticarEEntrar() } else { NSApp.terminate(nil) }
                }
            }
        } else {
            entrar()   // sem Touch ID/senha disponíveis: o token do .env (0600) continua sendo a trava
        }
    }

    func entrar() {
        guard let token = amb.token else {
            mostrarAviso("Sem VIGIA_TOKEN", "Não achei VIGIA_TOKEN em \(amb.arquivoEnv.path) (chmod 600). Confira o arquivo e abra de novo.")
            return
        }
        var req = URLRequest(url: amb.base.appendingPathComponent("/login"))
        req.httpMethod = "POST"
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        var cs = CharacterSet.urlQueryAllowed; cs.remove(charactersIn: "&+=")
        let codificado = token.addingPercentEncoding(withAllowedCharacters: cs) ?? token
        req.httpBody = "token=\(codificado)".data(using: .utf8)
        web.load(req)
    }

    func mostrarAviso(_ titulo: String, _ texto: String) {
        let html = """
        <!doctype html><meta charset=utf-8><style>body{font:15px -apple-system,system-ui;display:grid;place-items:center;height:100vh;margin:0;color:#1d1d1f;background:#f2f2f7}
        .c{max-width:420px;text-align:center}h1{font-size:20px}
        button{font:inherit;padding:8px 16px;border-radius:8px;border:0;background:#0a66d6;color:#fff}</style>
        <div class=c><h1>\(titulo)</h1><p>\(texto)</p><p><button onclick="location.href='vigia://tentar'">Tentar de novo</button></p></div>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    // MARK: alça de arrastar a janela acompanha a lateral (pedido 48)
    /// `largura` = largura da lateral em pontos; 0 = lateral recolhida (a alça cobre só a faixa do semáforo, e o botão
    /// de mostrar a lateral, que a página põe logo depois dele, fica clicável).
    func ajustarAlca(_ largura: CGFloat) {
        var f = alcaLateral.frame
        f.size.width = largura <= 0 ? semaforo : min(max(largura, 120), 600)
        alcaLateral.frame = f
    }

    // MARK: mensagens da página: largura da lateral (pedido 48) e Touch ID para liberar o envio (r02)
    func userContentController(_ uc: WKUserContentController, didReceive msg: WKScriptMessage) {
        guard msg.name == "vigia", let corpo = msg.body as? [String: Any], let acao = corpo["acao"] as? String else { return }
        // só a página do próprio painel pode pedir
        guard let url = msg.frameInfo.request.url, url.scheme == "http",
              url.host == "127.0.0.1" || url.host == "localhost", (url.port ?? 80) == amb.porta else { return }
        if acao == "lateral" {
            ajustarAlca(CGFloat((corpo["largura"] as? NSNumber)?.doubleValue ?? 212))
            return
        }
        guard acao == "liberarEnvio" else { return }
        let ctx = LAContext()
        ctx.localizedCancelTitle = "Cancelar"
        var erro: NSError?
        guard ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &erro) else { responderLiberacao(false, "sem_touch"); return }
        ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: "liberar o envio de mensagens") { ok, _ in
            DispatchQueue.main.async { ok ? self.pedirLiberacao() : self.responderLiberacao(false, "cancelado") }
        }
    }

    func tokenEnvio() -> String? {
        if let t = Ambiente.segredo("VIGIA_TOKEN_ENVIO") { return t }   // D-45: Keychain só se ligado
        let arq = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".vigia-envio.env")
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: arq.path),
              let modo = attrs[.posixPermissions] as? NSNumber, modo.intValue & 0o077 == 0 else { return nil }
        return Ambiente.lerEnv(arq)["VIGIA_TOKEN_ENVIO"].flatMap { $0.isEmpty ? nil : $0 }
    }

    func pedirLiberacao() {
        guard let token = tokenEnvio() else {
            let a = NSAlert()
            a.messageText = "Envio desligado"
            a.informativeText = "Não achei VIGIA_TOKEN_ENVIO em ~/.vigia-envio.env (chmod 600)."
            a.runModal()
            responderLiberacao(false, "sem_token")
            return
        }
        web.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in
            let doPainel = cookies.filter { $0.domain == "127.0.0.1" || $0.domain == "localhost" }
            var req = URLRequest(url: self.amb.base.appendingPathComponent("/envio/liberar"))
            req.httpMethod = "POST"
            for (k, v) in HTTPCookie.requestHeaderFields(with: doPainel) { req.setValue(v, forHTTPHeaderField: k) }
            req.setValue(token, forHTTPHeaderField: "X-Vigia-Envio")
            req.setValue("application/json", forHTTPHeaderField: "Accept")
            let cfg = URLSessionConfiguration.ephemeral
            cfg.connectionProxyDictionary = [:]     // 127.0.0.1 sempre direto, nunca por proxy
            cfg.httpShouldSetCookies = false
            URLSession(configuration: cfg).dataTask(with: req) { _, resp, _ in
                let ok = (resp as? HTTPURLResponse)?.statusCode == 200
                let motivo = resp == nil ? "rede" : "recusado"     // auditoria C2: a página diz qual das causas foi
                DispatchQueue.main.async { self.responderLiberacao(ok, motivo) }
            }.resume()
        }
    }

    func responderLiberacao(_ ok: Bool, _ motivo: String = "") {
        web.evaluateJavaScript("window.vigiaEnvioLiberado && window.vigiaEnvioLiberado(\(ok ? "true" : "false"), '\(motivo)')", completionHandler: nil)
    }

    // MARK: WKNavigationDelegate — só o servidor local
    func webView(_ w: WKWebView, decidePolicyFor acao: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = acao.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "vigia" {
            decisionHandler(.cancel)
            // segurança r01 (B1): o esquema só vale nas telas do próprio app (instalador, erro); página do painel não o aciona
            if ehDoPainel(w.url) { return }
            tentativas = 0
            if url.host == "instalar" { instalador?.acao(url) }
            else if amb.modoDocker { instalador?.comecar() }   // "Tentar de novo" refaz a conferência do Docker
            else { autenticarEEntrar() }
            return
        }
        if url.scheme == "about" { decisionHandler(.allow); return }
        let mesmaOrigem = ehDoPainel(url)
        // pedido 50: link com `download` (Baixar foto, áudio, vídeo, arquivo) vira download nativo, só do próprio painel
        if mesmaOrigem && acao.shouldPerformDownload { decisionHandler(.download); return }
        // pedido 50, rodada 2: link de uma mensagem (http/https, clicado pela pessoa) abre no navegador padrão, nunca aqui dentro
        if !mesmaOrigem && acao.navigationType == .linkActivated && (url.scheme == "https" || url.scheme == "http") {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(mesmaOrigem ? .allow : .cancel)
    }

    func ehDoPainel(_ url: URL?) -> Bool {
        guard let url = url else { return false }
        return url.scheme == "http" && (url.host == "127.0.0.1" || url.host == "localhost") && (url.port ?? 80) == amb.porta
    }

    // resposta que o painel marcou como anexo (Content-Disposition: attachment) ou que a página não sabe mostrar
    func webView(_ w: WKWebView, decidePolicyFor resp: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let anexo = ((resp.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? "").lowercased().hasPrefix("attachment")
        if ehDoPainel(resp.response.url) && (anexo || !resp.canShowMIMEType) { decisionHandler(.download); return }
        decisionHandler(.allow)
    }

    // MARK: downloads (pedido 50) — sempre em ~/Downloads, sem sobrescrever; ao terminar, mostra no Finder
    var baixados: [ObjectIdentifier: URL] = [:]
    func webView(_ w: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ w: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }

    func download(_ d: WKDownload, decideDestinationUsing r: URLResponse, suggestedFilename nome: String, completionHandler: @escaping (URL?) -> Void) {
        let pasta = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads")
        let limpo = (nome as NSString).lastPathComponent.replacingOccurrences(of: "/", with: "_")
        let base = ((limpo.isEmpty ? "arquivo" : limpo) as NSString)
        let (raiz, ext) = (base.deletingPathExtension, base.pathExtension)
        var destino = pasta.appendingPathComponent(base as String), n = 2
        while FileManager.default.fileExists(atPath: destino.path) {
            destino = pasta.appendingPathComponent(ext.isEmpty ? "\(raiz) \(n)" : "\(raiz) \(n).\(ext)"); n += 1
        }
        baixados[ObjectIdentifier(d)] = destino
        completionHandler(destino)
    }
    func downloadDidFinish(_ d: WKDownload) {
        if var u = baixados.removeValue(forKey: ObjectIdentifier(d)) {
            // segurança r01 (M1): arquivo que veio de um contato leva a marca de quarentena, como no Safari e no Mail;
            // sem ela o macOS não avalia nem avisa quando o dono abre um executável recebido.
            var v = URLResourceValues()
            v.quarantineProperties = [kLSQuarantineTypeKey as String: kLSQuarantineTypeOtherDownload,
                                      kLSQuarantineAgentNameKey as String: "Greg"]
            try? u.setResourceValues(v)
            NSWorkspace.shared.activateFileViewerSelecting([u])
        }
    }
    func download(_ d: WKDownload, didFailWithError e: Error, resumeData: Data?) {
        baixados.removeValue(forKey: ObjectIdentifier(d))
        mostrarAlerta("Não consegui baixar o arquivo. Tente de novo.")
    }
    func mostrarAlerta(_ texto: String) { let a = NSAlert(); a.messageText = texto; a.runModal() }

    // MARK: anexar e gravar (pedido 50)
    // <input type="file"> da conversa abre o seletor nativo (um arquivo por vez)
    func webView(_ w: WKWebView, runOpenPanelWith p: WKOpenPanelParameters, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let painel = NSOpenPanel()
        painel.canChooseFiles = true; painel.canChooseDirectories = false; painel.allowsMultipleSelection = false
        painel.beginSheetModal(for: janela) { r in completionHandler(r == .OK ? painel.urls : nil) }
    }
    // microfone: só para a página do próprio painel e só áudio (câmera nunca). O macOS ainda pede a permissão dele.
    func webView(_ w: WKWebView, requestMediaCapturePermissionFor origem: WKSecurityOrigin, initiatedByFrame f: WKFrameInfo,
                 type: WKMediaCaptureType, decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        let doPainel = origem.protocol == "http" && (origem.host == "127.0.0.1" || origem.host == "localhost") && origem.port == amb.porta
        decisionHandler(doPainel && type == .microphone ? .grant : .deny)
    }

    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        // carga trocada por outra (a tela do instalador muda rápido) ou link vigia:// cancelado: não é painel fora do ar
        let ns = e as NSError
        if ns.code == NSURLErrorCancelled || (ns.domain == "WebKitErrorDomain" && ns.code == 102) { return }
        if instalador?.instalando == true { return }
        tentativas += 1
        if tentativas <= 10 {
            // o painel sobe pelo LaunchAgent (KeepAlive); espera e tenta de novo
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.entrar() }
        } else {
            mostrarAviso("Painel fora do ar", amb.modoDocker
                ? "Não consegui falar com \(amb.base.absoluteString). Confira se o Docker está ligado e tente de novo."
                : "Não consegui falar com \(amb.base.absoluteString). Rode `bin/vigia.sh status` em \(amb.raiz.path).")
        }
    }

    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        tentativas = 0
        // segurança r01 (B8): o login do painel vence em 30 dias; com o app aberto, ele entra de novo sozinho.
        // Só nesse aviso do painel: depois de Sair ou de token errado a tela de login fica como está.
        if ehDoPainel(w.url), w.url?.path == "/login", w.url?.query == "venceu=1" { entrar(); return }
        if let d = destinoInicial, let u = w.url, u.scheme == "http", u.path != "/login" {
            destinoInicial = nil
            ir(d)
        }
    }

    // confirm() do HTML (Reiniciar Evolution) vira diálogo nativo
    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = m; a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancelar")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }
    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = m; a.runModal(); completionHandler()
    }
}

/// Pedido 78: as notificações do Greg saem pelo próprio app, para levarem o nome e o ícone dele (pelo osascript o
/// macOS mostra o Editor de Scripts). O painel e o coletor chamam este mesmo executável com
/// `--notificar <título> <texto>`: ele emite a notificação e sai, sem abrir janela. Só texto fixo do catálogo chega aqui.
/// Códigos de saída: 0 emitida · 2 a pessoa não autorizou · 3 o sistema recusou · 4 sem resposta (quem chamou usa a reserva).
final class Avisos: NSObject, UNUserNotificationCenterDelegate {
    static let unico = Avisos()

    /// Com o Greg na frente, o macOS só mostra a notificação se o app disser que sim.
    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler h: @escaping (UNNotificationPresentationOptions) -> Void) {
        h([.banner, .list, .sound])
    }

    /// Na abertura do app: o pedido de permissão aparece com a pessoa na frente do Mac, não no meio da madrugada.
    static func pedirPermissao() {
        let c = UNUserNotificationCenter.current()
        c.delegate = unico
        c.requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    /// Não pede permissão aqui: o pedido do macOS some quando o processo termina e fica valendo como recusa. Quem
    /// pede é a janela do Greg, na abertura. Sem permissão, sai com 2 e quem chamou usa a reserva.
    static func emitir(titulo: String, texto: String) -> Int32 {
        let c = UNUserNotificationCenter.current()
        let fim = DispatchSemaphore(value: 0)
        var codigo: Int32 = 3
        c.getNotificationSettings { s in
            guard s.authorizationStatus == .authorized || s.authorizationStatus == .provisional else {
                FileHandle.standardError.write("notificação não autorizada para o Greg nos Ajustes do Sistema\n".data(using: .utf8)!)
                codigo = 2; fim.signal(); return
            }
            let m = UNMutableNotificationContent()
            m.title = String(titulo.prefix(120))
            m.body = String(texto.prefix(300))
            m.sound = .default
            c.add(UNNotificationRequest(identifier: UUID().uuidString, content: m, trigger: nil)) { erro in
                codigo = erro == nil ? 0 : 3
                fim.signal()
            }
        }
        if fim.wait(timeout: .now() + 12) == .timedOut { return 4 }
        Thread.sleep(forTimeInterval: 0.4)      // dá tempo de o sistema receber antes de o processo sair
        return codigo
    }
}

let argumentos = CommandLine.arguments
if let i = argumentos.firstIndex(of: "--notificar"), argumentos.count > i + 2 {
    exit(Avisos.emitir(titulo: argumentos[i + 1], texto: argumentos[i + 2]))
}

let app = NSApplication.shared
let delegado = Janela()
app.delegate = delegado
app.setActivationPolicy(.regular)
app.run()
