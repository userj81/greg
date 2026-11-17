// Fotografa as telas do painel de exemplo que docs/imagens/gerar.py sobe (dados fictícios). Lê o plano pelo stdin
// ({base, token, preferencias, layout, saida, capturas:[{nome, rota, pref, layout}]}), faz login uma vez, grava as
// preferências e a janela dividida (larguras; vazio = padrão) de cada captura antes de navegar e salva <saida>/<nome>.jpg a 1120×760 em 2× (retina). Usa o Playwright já instalado no Mac;
// nada é baixado, nada sai da máquina. Saída: 0 ok · 2 Playwright ausente.
import { createRequire } from 'node:module';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import os from 'node:os';

const plano = JSON.parse(readFileSync(0, 'utf8'));
const candidatos = [process.env.PLAYWRIGHT_DIR, join(os.homedir(), 'Downloads/portal-exemplogroup/node_modules/playwright')].filter(Boolean);
let chromium;
for (const c of candidatos) {
  if (existsSync(join(c, 'package.json'))) {
    try { chromium = createRequire(join(c, 'package.json'))(c).chromium; break; } catch { /* tenta o próximo */ }
  }
}
if (!chromium) { console.error('capturar: Playwright não encontrado (PLAYWRIGHT_DIR)'); process.exit(2); }

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1120, height: 760 }, deviceScaleFactor: 2 });
await page.goto(plano.base + '/demandas');
if (await page.$('input#token')) {
  await page.fill('input#token', plano.token);
  await page.click('button[type=submit]');
  await page.waitForLoadState('networkidle');
}
const ligado = () => page.evaluate(() => document.documentElement.hasAttribute('data-gravacao'));
for (const cap of plano.capturas) {
  writeFileSync(plano.preferencias, JSON.stringify({ fonte: 13, ...cap.pref }));
  if (plano.layout) writeFileSync(plano.layout, JSON.stringify(cap.layout || {}));
  let nota = '';
  if (cap.liberar) {
    // Envio liberado (pedido 50): o mesmo POST que o Touch ID faz, com o token sorteado desta execução (a Evolution aqui é a falsa).
    const lib = await page.request.post(plano.base + '/envio/liberar', { headers: { 'x-vigia-envio': plano.token_envio } });
    if (lib.status() !== 200) { console.error(`${cap.nome}: o envio não liberou (http ${lib.status()})`); process.exit(1); }
    nota = ', envio liberado';
  }
  const r = await page.goto(plano.base + cap.rota);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(600);
  if (cap.rascunho) await page.fill('#zap-texto', cap.rascunho);
  if (cap.gravacao) {
    // Modo de gravação (D-24, Papel): liga pelo interruptor de Ajustes, como o dono faria, volta para a rota e recarrega
    // para provar que o script do <head> religa antes de pintar (o estado vive no localStorage desta origem).
    await page.goto(plano.base + '/ajustes');
    await page.click('#vigia-gravar');
    if (!await ligado()) { console.error(`${cap.nome}: o interruptor não ligou o modo de gravação`); process.exit(1); }
    await page.goto(plano.base + cap.rota);
    await page.reload();
    await page.waitForTimeout(600);
    if (!await ligado()) { console.error(`${cap.nome}: o modo de gravação não sobreviveu ao recarregar`); process.exit(1); }
    nota = ', modo de gravação: ligou pelo interruptor, sobreviveu ao reload';
  }
  const arquivo = join(plano.saida, cap.nome + '.jpg');
  await page.screenshot({ path: arquivo, type: 'jpeg', quality: 88 });
  if (plano.html_saida) writeFileSync(join(plano.html_saida, cap.nome + '.html'), await page.content());
  if (cap.gravacao) {
    await page.goto(plano.base + '/ajustes');
    await page.click('#vigia-gravar');
    const sobra = await page.evaluate(() => document.documentElement.hasAttribute('data-gravacao') || localStorage.getItem('vigia-gravacao') !== null);
    if (sobra) { console.error(`${cap.nome}: o botão não desligou o modo de gravação`); process.exit(1); }
    nota += ', desligou';
  }
  console.log(`${cap.nome}: http ${r.status()}, ${Math.round(readFileSync(arquivo).length / 1024)} KB${nota}`);
}
await browser.close();
