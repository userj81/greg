// Instalador do Greg.app distribuído em DMG (pedido 49). Só roda com VigiaModo=docker no Info.plist (o DMG põe;
// o app pessoal do Mac, feito por bin/construir-app.sh, não tem a chave e segue igual).
// O que faz, na ordem, mostrando cada passo na própria janela do app:
//   1. acha o Docker (Docker Desktop, OrbStack ou colima); se não houver, baixa e instala o Docker Desktop; liga o motor;
//   2. copia o código que vem dentro do app para ~/Library/Application Support/Vigia/vigia (mantém docker.env e config.toml);
//   3. pede o número do WhatsApp e gera o docker.env com senhas aleatórias (bin/preparar-docker.sh);
//   4. com o Greg parado, confere as duas portas que o compose publica (painel e Evolution): a que estiver
//      ocupada por outro programa é trocada pela próxima livre e gravada no docker.env (--portas-livres);
//      docker compose up -d (com --build quando o app é novo ou foi atualizado);
//   5. espera o painel responder; 6. se o Claude Code do contêiner não tem login, abre o Terminal só para o /login;
//   7. entrega para a Janela, que entra no painel (Touch ID + token do docker.env, que nunca aparece na tela).
// O que não faz: não pede senha de administrador, não aceita termos por você (os do Docker ficam na janela do Docker),
// não manda nada para fora além do download do Docker Desktop (desktop.docker.com) e das imagens que o compose baixa.
import AppKit
import Foundation

final class Instalador {
    unowned let j: Janela
    let pasta = Instalador.pastaPadrao()
    var projeto: URL { pasta.appendingPathComponent("vigia") }
    var envDocker: URL { projeto.appendingPathComponent("docker.env") }
    let fila = DispatchQueue(label: "vigia.instalador")
    var docker: String?
    var precisaMontar = false
    var primeiraVez = false
    var esperandoClaude = false
    var instalando = false               // enquanto true, a Janela não tenta entrar no painel por conta própria
    var observacao: NSKeyValueObservation?

    init(janela: Janela) { j = janela }

    /// Pasta de dados do app (VIGIA_APP_DADOS sobrescreve, para teste).
    static func pastaPadrao() -> URL {
        if let p = ProcessInfo.processInfo.environment["VIGIA_APP_DADOS"], !p.isEmpty { return URL(fileURLWithPath: p) }
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Vigia")
    }
    static var versaoDoApp: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "0" }

    // MARK: fluxo
    func comecar() {
        instalando = true
        esperandoClaude = false
        tela("Preparando o Greg", "Conferindo o que já está instalado neste Mac.", carregando: true)
        fila.async { self.passoDocker() }
    }

    func passoDocker() {
        docker = acharDocker()
        guard let d = docker else {
            principal {
                self.tela("O Greg precisa do Docker",
                          "O Docker é o programa que roda a conexão com o WhatsApp aqui no seu Mac. O Greg baixa e "
                          + "instala o Docker Desktop para você (uns 600 MB). Ele é gratuito para uso pessoal e para "
                          + "empresas pequenas.",
                          botoes: [("Instalar o Docker", "baixar-docker", true), ("Já instalei, procurar de novo", "tentar", false)])
            }
            return
        }
        if !motorLigado(d) {
            principal { self.tela("Ligando o Docker",
                                  "Se for a primeira vez, o Docker abre uma janela pedindo para aceitar os termos dele e, "
                                  + "às vezes, a senha do Mac. Faça isso por lá; o Greg continua sozinho.", carregando: true) }
            ligarMotor()
            var ok = false
            for _ in 0..<120 {   // até 6 min: a primeira partida do Docker Desktop é lenta
                Thread.sleep(forTimeInterval: 3)
                if let d2 = acharDocker(), motorLigado(d2) { docker = d2; ok = true; break }
            }
            guard ok else {
                principal { self.tela("O Docker não ligou",
                                      "Abra o Docker pelo Launchpad, espere ele dizer que está rodando e tente de novo.",
                                      botoes: [("Tentar de novo", "tentar", true)]) }
                return
            }
        }
        guard rodar(docker!, ["compose", "version"]).0 == 0 else {
            principal { self.tela("Falta o Docker Compose",
                                  "O Docker deste Mac não tem o Compose. Instale o Docker Desktop (ele já traz) e tente de novo.",
                                  botoes: [("Tentar de novo", "tentar", true)]) }
            return
        }
        passoCodigo()
    }

    func passoCodigo() {
        let carimbo = projeto.appendingPathComponent(".versao-app")
        let atual = (try? String(contentsOf: carimbo, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
        primeiraVez = !FileManager.default.fileExists(atPath: envDocker.path)
        if atual != Instalador.versaoDoApp {
            do { try copiarCodigo() } catch {
                principal { self.tela("Não consegui copiar o Greg", "Erro ao gravar em \(self.projeto.path): \(error.localizedDescription)",
                                      botoes: [("Tentar de novo", "tentar", true)]) }
                return
            }
            precisaMontar = true
        }
        passoConfig()
    }

    func passoConfig() {
        let env = Ambiente.lerEnv(envDocker)
        if (env["DONO_WHATSAPP"] ?? "").isEmpty {
            principal { self.telaNumero(erro: nil) }
            return
        }
        passoSubir()
    }

    func receberNumero(_ cru: String) {
        let digitos = cru.filter(\.isNumber)
        guard (10...15).contains(digitos.count) else {
            telaNumero(erro: "Confira o número: só dígitos, com o código do país e o DDD (ex.: 5511987654321).")
            return
        }
        tela("Guardando as configurações", "Gerando as senhas deste Mac. Elas ficam só aqui.", carregando: true)
        fila.async {
            let (rc, saida) = self.rodar("/bin/bash", [self.projeto.appendingPathComponent("bin/preparar-docker.sh").path,
                                                        "--dono", digitos, "--tz", TimeZone.current.identifier])
            guard rc == 0 else {
                self.principal { self.tela("Não consegui gerar a configuração", saida, botoes: [("Tentar de novo", "tentar", true)]) }
                return
            }
            self.passoSubir()
        }
    }

    func passoSubir() {
        let texto = precisaMontar
            ? "Montando o Greg no Docker. Na primeira vez leva de 5 a 15 minutos: baixa uns 3 GB (a Evolution, o banco "
              + "e o modelo que transcreve áudio). Pode deixar a janela aberta e fazer outra coisa."
            : "Ligando o Greg."
        principal { self.tela(self.precisaMontar ? "Instalando o Greg" : "Abrindo o Greg", texto, carregando: true, log: true) }
        // Porta ocupada por outro programa derrubava o compose ("port is already allocated"). Só confere com o Greg
        // parado: com ele no ar, as portas dele mesmo aparecem ocupadas e seriam trocadas à toa.
        let noAr = rodar(docker!, ["compose", "ps", "-q"], limite: 30)
        if noAr.0 == 0 && noAr.1.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            let (rcPortas, saidaPortas) = rodar("/bin/bash", [projeto.appendingPathComponent("bin/preparar-docker.sh").path, "--portas-livres"])
            guard rcPortas == 0 else {
                principal { self.tela("Não achei porta livre neste Mac",
                                      "O Greg usa duas portas do próprio computador e não encontrou uma livre para cada. "
                                      + "Feche programas que você não está usando e tente de novo.",
                                      botoes: [("Tentar de novo", "tentar", true)], detalhe: saidaPortas) }
                return
            }
        }
        var args = ["compose", "up", "-d"]
        if precisaMontar { args.append("--build") }
        let (rc, saida) = rodar(docker!, args, aoLer: { linha in self.principal { self.mostrarLog(linha) } })
        guard rc == 0 else {
            let fim = saida.split(separator: "\n").suffix(12).joined(separator: "\n")
            principal { self.tela("A instalação parou", "O Docker devolveu um erro. As últimas linhas:", botoes: [("Tentar de novo", "tentar", true)],
                                  detalhe: fim) }
            return
        }
        if precisaMontar {
            try? Instalador.versaoDoApp.write(to: projeto.appendingPathComponent(".versao-app"), atomically: true, encoding: .utf8)
            precisaMontar = false
        }
        passoPainel()
    }

    func passoPainel() {
        principal { self.tela("Esperando o painel", "Na primeira vez a conexão com o WhatsApp leva uns minutos para ficar pronta.",
                              carregando: true) }
        for _ in 0..<200 {   // até 10 min
            if painelResponde() { passoClaude(); return }
            Thread.sleep(forTimeInterval: 3)
        }
        principal { self.tela("O painel não respondeu",
                              "O Greg subiu mas o painel não abriu em \(self.j.amb.base.absoluteString). Tente de novo; se repetir, "
                              + "reinicie o Docker.", botoes: [("Tentar de novo", "tentar", true)]) }
    }

    func passoClaude() {
        if claudeLogado() { concluir(); return }
        principal {
            self.tela("Falta entrar na sua conta do Claude",
                      "É o Claude que lê as mensagens e separa o que é pedido. Clique em Abrir o login: abre uma janela do "
                      + "Terminal. Digite /login, entre na sua conta pelo navegador e, quando terminar, digite /exit. "
                      + "O Greg percebe sozinho e continua.",
                      botoes: [("Abrir o login", "claude", true), ("Fazer depois", "pular-claude", false)])
        }
    }

    func concluir() {
        esperandoClaude = false
        instalando = false
        principal { self.j.instalacaoPronta(primeiraVez: self.primeiraVez) }
    }

    // MARK: ações vindas da página (vigia://instalar/<acao>)
    func acao(_ url: URL) {
        let nome = url.lastPathComponent
        switch nome {
        case "tentar": comecar()
        case "baixar-docker": baixarDocker()
        case "numero":
            let valor = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "valor" })?.value ?? ""
            receberNumero(valor)
        case "claude": abrirLoginClaude()
        case "pular-claude": concluir()
        default: break
        }
    }

    // MARK: Docker
    func acharDocker() -> String? {
        let casa = FileManager.default.homeDirectoryForCurrentUser.path
        if let p = ProcessInfo.processInfo.environment["VIGIA_DOCKER"], !p.isEmpty { return FileManager.default.isExecutableFile(atPath: p) ? p : nil }
        let candidatos = ["/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                          "/Applications/Docker.app/Contents/Resources/bin/docker", "\(casa)/.docker/bin/docker",
                          "\(casa)/.orbstack/bin/docker", "/Applications/OrbStack.app/Contents/MacOS/xbin/docker"]
        return candidatos.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    func motorLigado(_ d: String) -> Bool { rodar(d, ["info"], limite: 20).0 == 0 }

    func ligarMotor() {
        let fm = FileManager.default
        if fm.fileExists(atPath: "/Applications/Docker.app") {
            _ = rodar("/usr/bin/open", ["-g", "-a", "/Applications/Docker.app"])
        } else if fm.fileExists(atPath: "/Applications/OrbStack.app") {
            _ = rodar("/usr/bin/open", ["-g", "-a", "/Applications/OrbStack.app"])
        } else if let colima = ["/opt/homebrew/bin/colima", "/usr/local/bin/colima"].first(where: { fm.isExecutableFile(atPath: $0) }) {
            let p = Process()   // "colima start" demora; segue em paralelo e o laço de espera confere o motor
            p.executableURL = URL(fileURLWithPath: colima)
            p.arguments = ["start"]
            p.environment = ambiente()
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            try? p.run()
        }
    }

    func baixarDocker() {
        #if arch(arm64)
        let arq = "arm64"
        #else
        let arq = "amd64"
        #endif
        guard let url = URL(string: "https://desktop.docker.com/mac/main/\(arq)/Docker.dmg") else { return }
        tela("Baixando o Docker Desktop", "Uns 600 MB, direto do site do Docker.", carregando: true, barra: true)
        let tarefa = URLSession.shared.downloadTask(with: url) { local, resp, erro in
            self.observacao = nil
            let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
            guard let local = local, erro == nil, status == 200 else {
                self.principal { self.tela("O download não terminou",
                                           "Confira a internet e tente de novo. Se preferir, baixe em docker.com e instale como qualquer app.",
                                           botoes: [("Tentar de novo", "baixar-docker", true), ("Já instalei, procurar de novo", "tentar", false)]) }
                return
            }
            let dmg = self.pasta.appendingPathComponent("Docker.dmg")
            try? FileManager.default.createDirectory(at: self.pasta, withIntermediateDirectories: true)
            try? FileManager.default.removeItem(at: dmg)
            do { try FileManager.default.moveItem(at: local, to: dmg) } catch { return }
            self.fila.async { self.instalarDocker(dmg) }
        }
        observacao = tarefa.progress.observe(\.fractionCompleted) { p, _ in
            self.principal { self.mostrarBarra(p.fractionCompleted) }
        }
        tarefa.resume()
    }

    func instalarDocker(_ dmg: URL) {
        principal { self.tela("Instalando o Docker Desktop", "Copiando para a pasta Aplicativos.", carregando: true) }
        let ponto = pasta.appendingPathComponent("montagem-docker")
        try? FileManager.default.createDirectory(at: ponto, withIntermediateDirectories: true)
        let (rc, _) = rodar("/usr/bin/hdiutil", ["attach", dmg.path, "-nobrowse", "-noautoopen", "-readonly", "-mountpoint", ponto.path])
        let origem = ponto.appendingPathComponent("Docker.app")
        guard rc == 0, FileManager.default.fileExists(atPath: origem.path) else {
            principal { self.tela("Não consegui abrir o instalador do Docker", "Baixe em docker.com e instale como qualquer app; depois volte aqui.",
                                  botoes: [("Já instalei, procurar de novo", "tentar", true)]) }
            return
        }
        // segurança r01 (M2): só copia para Aplicativos o que o macOS aceita E que a Docker Inc assinou. Sem isto a
        // única garantia de origem seria o HTTPS do download.
        let (rcAssin, _) = rodar("/usr/bin/codesign", ["--verify", "--deep", "--strict", origem.path])
        let (rcPortao, _) = rodar("/usr/sbin/spctl", ["--assess", "--type", "execute", origem.path])
        let (_, quem) = rodar("/usr/bin/codesign", ["-dv", "--verbose=2", origem.path])
        guard rcAssin == 0, rcPortao == 0, quem.contains("Authority=Developer ID Application: Docker Inc") else {
            _ = rodar("/usr/bin/hdiutil", ["detach", ponto.path, "-quiet"])
            try? FileManager.default.removeItem(at: dmg)
            try? FileManager.default.removeItem(at: ponto)
            principal { self.tela("O Docker baixado não passou na conferência",
                                  "A assinatura do arquivo não é a da Docker Inc, então ele não foi instalado. Baixe em docker.com e instale como qualquer app; depois volte aqui.",
                                  botoes: [("Já instalei, procurar de novo", "tentar", true)]) }
            return
        }
        if FileManager.default.isWritableFile(atPath: "/Applications") {
            let (rcCopia, _) = rodar("/usr/bin/ditto", [origem.path, "/Applications/Docker.app"])
            _ = rodar("/usr/bin/hdiutil", ["detach", ponto.path, "-quiet"])
            try? FileManager.default.removeItem(at: dmg)
            try? FileManager.default.removeItem(at: ponto)
            guard rcCopia == 0 else {
                principal { self.tela("Não consegui copiar o Docker", "Instale pelo site docker.com e volte aqui.",
                                      botoes: [("Já instalei, procurar de novo", "tentar", true)]) }
                return
            }
            passoDocker()
        } else {
            // usuário sem permissão de administrador: abre o volume e deixa ele arrastar (pede a senha de um admin)
            _ = rodar("/usr/bin/open", [ponto.path])
            principal { self.tela("Arraste o Docker para Aplicativos",
                                  "Esta conta do Mac não pode instalar apps sozinha. Na janela que abriu, arraste o Docker para "
                                  + "Aplicativos (o Mac pede a senha de um administrador) e depois clique abaixo.",
                                  botoes: [("Pronto, continuar", "tentar", true)]) }
        }
    }

    // MARK: Claude Code dentro do contêiner
    func claudeLogado() -> Bool {
        rodar(docker!, ["compose", "exec", "-T", "vigia", "test", "-s", "/home/vigia/.claude/.credentials.json"], limite: 30).0 == 0
    }

    func abrirLoginClaude() {
        let arq = pasta.appendingPathComponent("Entrar no Claude.command")
        func q(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }
        let amb = ambiente()   // já traz as portas do docker.env: o compose do Terminal tem de enxergar as mesmas
        var exporta = "export PATH=\(q(amb["PATH"] ?? "/usr/bin:/bin"))\n"
        for chave in ["COMPOSE_PROJECT_NAME"] + Instalador.chavesDePorta {
            if let v = amb[chave], !v.isEmpty { exporta += "export \(chave)=\(q(v))\n" }
        }
        let script = """
        #!/bin/bash
        cd \(q(projeto.path)) || exit 1
        \(exporta)clear
        echo "Entrar no Claude: digite /login e siga o navegador. Quando terminar, digite /exit e feche esta janela."
        echo
        \(q(docker!)) compose exec -it vigia claude
        """
        do {
            try script.write(to: arq, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: arq.path)
        } catch { return }
        _ = rodar("/usr/bin/open", ["-a", "Terminal", arq.path])
        tela("Entrando no Claude", "Na janela do Terminal: digite /login, entre na sua conta e depois /exit. O Greg continua sozinho.",
             carregando: true, botoes: [("Fazer depois", "pular-claude", false)])
        esperandoClaude = true
        fila.async {
            while self.esperandoClaude {
                if self.claudeLogado() { self.concluir(); return }
                Thread.sleep(forTimeInterval: 3)
            }
        }
    }

    // MARK: arquivos
    func copiarCodigo() throws {
        let fm = FileManager.default
        guard let fonte = Bundle.main.resourceURL?.appendingPathComponent("vigia"), fm.fileExists(atPath: fonte.path) else {
            throw NSError(domain: "vigia", code: 1, userInfo: [NSLocalizedDescriptionKey: "o app veio sem o código (Resources/vigia)"])
        }
        try fm.createDirectory(at: projeto, withIntermediateDirectories: true)
        let guardar: Set<String> = ["docker.env", "config.toml", ".versao-app"]   // do usuário: nunca sobrescreve
        for item in try fm.contentsOfDirectory(atPath: fonte.path) {
            let alvo = projeto.appendingPathComponent(item)
            if guardar.contains(item) && fm.fileExists(atPath: alvo.path) { continue }
            if fm.fileExists(atPath: alvo.path) { try fm.removeItem(at: alvo) }
            try fm.copyItem(at: fonte.appendingPathComponent(item), to: alvo)
        }
    }

    func painelResponde() -> Bool {
        var req = URLRequest(url: j.amb.base.appendingPathComponent("/login"))
        req.timeoutInterval = 4
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: req) { _, r, _ in ok = (r as? HTTPURLResponse)?.statusCode == 200; sem.signal() }.resume()
        _ = sem.wait(timeout: .now() + 6)
        return ok
    }

    // MARK: processos
    /// Portas que o compose publica em 127.0.0.1 deste Mac. O valor mora no docker.env (quem grava é o
    /// preparar-docker.sh --portas-livres); sem a linha, vale o padrão do compose (8120, 8080). O Postgres não publica porta.
    static let chavesDePorta = ["VIGIA_PORTA_HOST", "EVOLUTION_PORTA_HOST"]

    func ambiente() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        // O docker-compose.yml da raiz interpola ${VIGIA_PORTA_HOST} do ambiente de quem chama, não do docker.env
        // (o env_file só entra no contêiner). Por isso as portas gravadas vão aqui. Variável já presente no ambiente
        // do app vence o arquivo (teste).
        let gravadas = Ambiente.lerEnv(envDocker)
        for chave in Instalador.chavesDePorta where (env[chave] ?? "").isEmpty {
            if let v = gravadas[chave], Int(v) != nil { env[chave] = v }
        }
        var dirs = ["/usr/local/bin", "/opt/homebrew/bin", "/Applications/Docker.app/Contents/Resources/bin",
                    "\(FileManager.default.homeDirectoryForCurrentUser.path)/.docker/bin",
                    "\(FileManager.default.homeDirectoryForCurrentUser.path)/.orbstack/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        if let d = docker { dirs.insert((d as NSString).deletingLastPathComponent, at: 0) }
        env["PATH"] = dirs.joined(separator: ":")
        return env
    }

    /// Roda um programa e devolve (código, saída+erro). `aoLer` recebe cada linha enquanto roda. `limite` em segundos.
    @discardableResult
    func rodar(_ prog: String, _ args: [String], limite: Double? = nil, aoLer: ((String) -> Void)? = nil) -> (Int32, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: prog)
        p.arguments = args
        p.currentDirectoryURL = FileManager.default.fileExists(atPath: projeto.path) ? projeto : pasta.deletingLastPathComponent()
        p.environment = ambiente()
        let cano = Pipe()
        p.standardOutput = cano
        p.standardError = cano
        p.standardInput = FileHandle.nullDevice
        var tudo = Data()
        var resto = ""
        let trava = NSLock()
        cano.fileHandleForReading.readabilityHandler = { h in
            let d = h.availableData
            guard !d.isEmpty else { return }
            trava.lock(); tudo.append(d); trava.unlock()
            if let aoLer = aoLer {
                resto += String(decoding: d, as: UTF8.self)
                let partes = resto.components(separatedBy: CharacterSet(charactersIn: "\r\n"))
                resto = partes.last ?? ""
                for l in partes.dropLast() where !l.trimmingCharacters(in: .whitespaces).isEmpty { aoLer(l) }
            }
        }
        do { try p.run() } catch { return (127, "não consegui rodar \(prog)") }
        if let limite = limite {
            let fim = Date().addingTimeInterval(limite)
            while p.isRunning && Date() < fim { Thread.sleep(forTimeInterval: 0.2) }
            if p.isRunning { p.terminate() }
        }
        p.waitUntilExit()
        cano.fileHandleForReading.readabilityHandler = nil
        trava.lock(); let saida = String(decoding: tudo, as: UTF8.self); trava.unlock()
        return (p.terminationStatus, saida)
    }

    // MARK: telas
    func principal(_ f: @escaping () -> Void) { DispatchQueue.main.async(execute: f) }

    func telaNumero(erro: String?) {
        tela("Qual é o seu WhatsApp?",
             "O número que o Greg vai ler. Use o código do país e o DDD, só os dígitos (ex.: 5511987654321). "
             + "Fica guardado só neste Mac.",
             campo: true, erro: erro)
    }

    static func esc(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;").replacingOccurrences(of: "\"", with: "&quot;")
    }

    func tela(_ titulo: String, _ texto: String, carregando: Bool = false, botoes: [(String, String, Bool)] = [],
              campo: Bool = false, erro: String? = nil, log: Bool = false, barra: Bool = false, detalhe: String? = nil) {
        let e = Instalador.esc
        let bts = botoes.map { "<a class=\"btn\($0.2 ? " primario" : "")\" href=\"vigia://instalar/\($0.1)\">\(e($0.0))</a>" }.joined()
        let formulario = !campo ? "" : """
            <form onsubmit="location.href='vigia://instalar/numero?valor='+encodeURIComponent(document.getElementById('n').value);return false">
            <input id=n inputmode=numeric autocomplete=tel placeholder="5511987654321" autofocus aria-label="Seu número do WhatsApp">
            <button class="btn primario" type=submit>Continuar</button></form>
            """
        let html = """
        <!doctype html><html lang=pt-BR><meta charset=utf-8><style>
        :root{--fundo:#f5f5f7;--cartao:#fff;--texto:#1d1d1f;--meta:#6e6e73;--linha:#d2d2d7;--acento:#0a66d6;--erro:#c9341f}
        *{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:32px 0;background:var(--fundo);color:var(--texto);
        font:15px/1.5 -apple-system,system-ui;-webkit-user-select:none;cursor:default}
        .c{width:min(520px,calc(100vw - 48px));background:var(--cartao);border:1px solid var(--linha);border-radius:14px;padding:32px}
        .marca{font-weight:600;color:var(--meta);font-size:13px;letter-spacing:.02em;margin:0 0 18px}
        h1{font-size:22px;line-height:1.25;margin:0 0 10px;text-wrap:balance}p{margin:0 0 18px;color:var(--meta)}
        .btn{display:inline-block;font:inherit;font-weight:500;padding:8px 16px;border-radius:8px;border:1px solid var(--linha);
        background:transparent;color:var(--texto);text-decoration:none;margin:0 8px 8px 0;cursor:default}
        .btn.primario{background:var(--acento);border-color:var(--acento);color:#fff}
        form{display:flex;gap:8px;flex-wrap:wrap}input{flex:1 1 220px;font:inherit;padding:8px 12px;border-radius:8px;
        border:1px solid var(--linha);background:var(--fundo);color:var(--texto);-webkit-user-select:text}
        input:focus,.btn:focus-visible{outline:3px solid color-mix(in srgb,var(--acento) 45%,transparent);outline-offset:1px}
        .erro{color:var(--erro);margin:-6px 0 14px}.roda{width:18px;height:18px;border:2px solid var(--linha);border-top-color:var(--acento);
        border-radius:50%;animation:g 1s linear infinite;margin:0 0 18px}@keyframes g{to{transform:rotate(360deg)}}
        @media(prefers-reduced-motion:reduce){.roda{animation:none}}
        .barra{height:6px;background:var(--linha);border-radius:3px;overflow:hidden;margin:0 0 18px}.barra i{display:block;height:100%;width:0;background:var(--acento)}
        #log{font:12px/1.4 ui-monospace,Menlo,monospace;color:var(--meta);white-space:pre-wrap;word-break:break-all;margin:0 0 12px;min-height:1.4em;max-height:38vh;overflow:auto;
        -webkit-user-select:text}</style>
        <div class=c role=main><p class=marca>Greg</p><h1>\(e(titulo))</h1><p>\(e(texto))</p>
        \(erro.map { "<p class=erro role=alert>\(e($0))</p>" } ?? "")
        \(barra ? "<div class=barra role=progressbar aria-label=Download><i id=barra></i></div>" : (carregando ? "<div class=roda aria-hidden=true></div>" : ""))
        \(log ? "<pre id=log aria-live=polite></pre>" : "")
        \(detalhe.map { "<pre id=log>\(e($0))</pre>" } ?? "")
        \(formulario)\(bts.isEmpty ? "" : "<div>\(bts)</div>")</div></html>
        """
        j.web.loadHTMLString(html, baseURL: nil)
    }

    func mostrarLog(_ linha: String) {
        let limpa = linha.replacingOccurrences(of: "\u{1b}\\[[0-9;]*[A-Za-z]", with: "", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
        guard let dados = try? JSONSerialization.data(withJSONObject: [String(limpa.prefix(160))]),
              let json = String(data: dados, encoding: .utf8) else { return }
        j.web.evaluateJavaScript("var l=document.getElementById('log');if(l)l.textContent=\(json)[0]", completionHandler: nil)
    }

    func mostrarBarra(_ f: Double) {
        j.web.evaluateJavaScript("var b=document.getElementById('barra');if(b)b.style.width='\(Int(max(0, min(1, f)) * 100))%'",
                                 completionHandler: nil)
    }
}
