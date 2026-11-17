"""Página /conversas (r02, D-39): todas as conversas do WhatsApp pessoal (pessoas e grupos) com foto, nome e não lidas,
a conversa aberta ao lado (texto, imagem, áudio, vídeo, documento, selo da IA) e o envio pelo próprio Greg, de
texto e, desde o pedido 50 (D-60, D-61), de foto, vídeo, arquivo, áudio de voz, resposta citando, reação, edição, encaminhamento (D-62) e apagar para todos (D-63); /conversas/nova manda mensagem para número novo. Envio só com a trava aberta pelo Touch ID (Greg.app) ou pelo token de envio (navegador).
Quem chama: app/server.py (via ROTAS); o Greg.app abre /conversas e /conversas/nova e responde ao pedido de Touch ID.
O que não faz: não marca nada como lido no celular (R-6), não mostra nada de antes do pareamento (R-2), não guarda o
texto nem o arquivo enviado (só o hash), não envia sem clique do dono (R-9) e não loga texto de mensagem.
"""
import hashlib
import json
import logging
import mimetypes
import re
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app import agendados, lotes, preferencias as pf, whatsapp as wa
from app.cursor import Cursor
from app.envio import Liberacao
from app.erros import EnvioIncerto, MidiaInvalida, VigiaErro, causa
from app.http import Resposta, json_resposta
from app.paginas import _comum as c

log = logging.getLogger("vigia.painel.conversas")
_TRAVA = threading.Lock()
FILTROS = (("todas", "Todas"), ("pessoas", "Pessoas"), ("grupos", "Grupos"), ("naolidas", "Não lidas"))
DIAS_CURTOS = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
DIAS_LONGOS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")

_I = '<svg viewBox="0 0 24 24" width="{t}" height="{t}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{p}</svg>'
# ícones só das mensagens (ticks, anexos, cadeado); os da toolbar vêm de _comum.icone (Papel)
ICO = {
    "check": '<polyline points="4 13 9 18 20 6"/>',
    "check2": '<polyline points="1 13 5 17 13 7"/><polyline points="9 15 11 17 19 7"/>',
    "relogio": '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/>',
    "play": '<polygon points="7 4 20 12 7 20 7 4" fill="currentColor"/>',
    "pausa": '<rect x="6" y="4" width="4" height="16" fill="currentColor"/><rect x="14" y="4" width="4" height="16" fill="currentColor"/>',
    "expandir": '<polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/>',
    "video": '<polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2"/>',
    "doc": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
    "baixar": '<path d="M12 4v11"/><polyline points="7 11 12 16 17 11"/><path d="M5 20h14"/>',
    "clipe": '<path d="M20.5 11.5l-8.6 8.6a5.5 5.5 0 0 1-7.8-7.8l8.6-8.6a3.7 3.7 0 0 1 5.2 5.2l-8.5 8.5a1.8 1.8 0 0 1-2.6-2.6l7.9-7.9"/>',
    "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0"/><path d="M12 17.5V21"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "rosto": '<circle cx="12" cy="12" r="9"/><path d="M8.5 14.5a4.5 4.5 0 0 0 7 0"/><path d="M9 9.5h.01M15 9.5h.01"/>',
    "responder": '<polyline points="9 14 4 9 9 4"/><path d="M4 9h10a6 6 0 0 1 6 6v4"/>',
    "encaminhar": '<polyline points="15 14 20 9 15 4"/><path d="M20 9H10a6 6 0 0 0-6 6v4"/>',
    "lapis": '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    "lixeira": '<path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="M6 7l1 13h10l1-13"/>',
    "pin": '<path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>',
    "pessoa": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "enquete": '<path d="M5 20V10M12 20V4M19 20v-7"/>',
    "relogio": '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/>',
    "cadeado": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
}


def ico(nome: str, t: int = 16) -> str:
    return _I.format(t=t, p=ICO[nome])


# ------------------------------------------------------------------ pele da página (estilo Papel, pedido 39)
# A base só tem o que é comum (painéis, controles, menus, itens de lista). O que é só desta tela mora aqui: a linha de
# conversa com foto, as bolhas, a nota da IA, o rodapé de envio e os menus de passos (D-48) sobre os .menu-itens da base.
_CSS = """<style>
.zap-lista { position: relative; }
.zap-busca-linha { flex: none; display: flex; padding: 8px 10px; border-bottom: 1px solid var(--fio-fraco); margin: 0; }
.zap-busca-linha .busca-caixa { flex: 1; display: flex; }
.zap-busca-linha .campo.busca { width: 100%; }
.zap-item { flex-direction: row; align-items: center; gap: 10px; padding: 9px 12px; position: relative; }
.zap-item .zap-corpo { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.zap-item .cab time { font-size: .8846rem; }
.zap-item .meta { gap: 8px; }
.zap-item .texto { display: block; flex: 1; -webkit-line-clamp: unset; white-space: nowrap; text-overflow: ellipsis; font: var(--t-meta) var(--fonte); }
.zap-item .classe { font-size: .8846rem; }
.zap-item .zap-mais { position: absolute; right: 8px; top: 50%; transform: translateY(-50%); width: 24px; height: 24px; border-radius: var(--raio-controle);
  display: grid; place-items: center; background: var(--botao-fundo); color: var(--texto); box-shadow: var(--bezel); opacity: 0; }
.zap-item .zap-mais svg { width: 14px; height: 14px; }
.zap-item:hover .zap-mais, .zap-item.menu-aberto .zap-mais, .zap-item .zap-mais:focus-visible { opacity: 1; }
.zap-item.menu-aberto { background: var(--fundo-2); }
.zap-item:hover .zap-corpo, .zap-item.menu-aberto .zap-corpo { padding-right: 28px; }
.zap-item .zap-marca { display: none; }
.zap-lista.selecionando .zap-item .zap-mais { display: none; }
.zap-lista.selecionando .zap-item .zap-marca { display: inline-block; width: 16px; height: 16px; margin: 0; accent-color: var(--acento); flex: none; }
.zap-lista.selecionando .zap-item.marcada { background: var(--selecao); }
.zap-lista.selecionando .toolbar :is(#zap-filtro, #zap-nova-btn, #zap-menu-topo) { display: none; }
.zap-aviso-lista { margin: 10px 12px 2px; padding: 6px 10px; border-radius: var(--raio-controle); font: var(--t-meta) var(--fonte); }
.zap-aviso-lista.ruim { background: var(--atrasado-fundo); color: var(--atrasado-texto); }
.zap-erro { padding: 14px 12px; }
.zap-barra { flex: none; display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-top: 1px solid var(--fio); background: var(--fundo-2); margin: 0; }
.zap-barra .n { flex: 1; font: var(--t-meta) var(--fonte); color: var(--meta); white-space: nowrap; }
.zap-barra[hidden], .zap-menu-lista[hidden], .zap-passo[hidden], #zap-sel-cancelar[hidden] { display: none !important; }
/* menus de passos (D-48): itens dentro de .zap-passo com a mesma cara dos itens diretos da base */
.zap-menu-lista { min-width: 250px; }
.zap-passo > [role=menuitem] { position: relative; display: flex; align-items: center; gap: 14px; width: 100%; padding: 6px 10px 6px 24px; border: 0;
  border-radius: 5px; background: transparent; color: var(--texto); font: var(--t-corpo) var(--fonte); text-align: left; text-decoration: none; cursor: default; white-space: nowrap; }
.zap-passo > [role=menuitem] > span { flex: 1; }
.zap-passo > [role=menuitem]:hover, .zap-passo > [role=menuitem]:focus-visible { background: var(--acento); color: var(--sobre-acento); outline: 0; box-shadow: none; text-decoration: none; }
.zap-passo > .destrutivo { color: var(--destrutivo); }
.zap-passo > .destrutivo:hover { background: var(--destrutivo); color: var(--sobre-acento); }
.zap-passo hr { border: 0; border-top: 1px solid var(--fio); margin: 4px 6px; }
.zap-passo-nome { padding: 6px 8px 4px; }
.zap-passo-nome form { display: flex; gap: 6px; margin: 0 0 6px; }
.zap-passo-nome .campo { flex: 1; }
.zap-passo-nome .btn.sm { height: var(--controle); }
.zap-passo-nome p { white-space: normal; max-width: 260px; }
.zap-confirma { padding: 8px 10px 6px; white-space: normal; max-width: 290px; }
.zap-confirma strong { display: block; font-weight: 600; margin-bottom: 4px; }
.zap-confirma p { margin: 0 0 10px; font: var(--t-meta) var(--fonte); color: var(--meta); }
.zap-confirma form { margin: 0; }
#zap-menu-linha { top: 0; left: 0; }
/* conversa aberta */
.zap-toolbar { gap: 10px; padding: 0 16px; }
.zap-perfil-abrir { flex: 1; min-width: 0; display: flex; align-items: center; gap: 10px; margin: 0 0 0 -8px; padding: 4px 8px; border: 0; border-radius: 8px; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.zap-perfil-abrir:hover { background: var(--fio-fraco); }
.zap-perfil-abrir:focus-visible { outline: 2px solid var(--acento); outline-offset: 1px; }
.zap-perfil { position: absolute; top: 0; right: 0; bottom: 0; z-index: 30; width: min(360px, 100%); display: flex; flex-direction: column; background: var(--fundo); border-left: 1px solid var(--fio); box-shadow: -10px 0 28px color-mix(in srgb, #000 12%, transparent); }
.zap-perfil[hidden] { display: none; }
.zap-perfil > .toolbar { justify-content: space-between; padding: 0 12px 0 16px; }
.zap-perfil-fechar { display: grid; place-items: center; width: 28px; height: 28px; border: 0; border-radius: 6px; background: none; color: var(--meta); cursor: pointer; }
.zap-perfil-fechar:hover { background: var(--fio-fraco); color: var(--texto); }
.zap-perfil-corpo { flex: 1; min-height: 0; overflow-y: auto; padding: 22px 18px 18px; }
.zap-perfil-cab { display: flex; flex-direction: column; align-items: center; gap: 3px; text-align: center; padding-bottom: 16px; }
.zap-perfil-cab h3 { margin: 10px 0 0; font: 700 17px/1.25 var(--fonte); overflow-wrap: anywhere; }
.zap-perfil-cab p { margin: 0; color: var(--meta); font: var(--t-meta) var(--fonte); }
.zap-perfil-foto { display: grid; place-items: center; width: 112px; height: 112px; padding: 0; border: 0; border-radius: 50%; overflow: hidden; background: var(--fio-fraco); color: var(--meta); font: 600 34px var(--fonte); cursor: zoom-in; }
.zap-perfil-foto.sem { cursor: default; }
.zap-perfil-foto img { width: 100%; height: 100%; object-fit: cover; }
.zap-perfil-selo { margin-top: 6px; padding: 2px 9px; border-radius: 999px; background: var(--fio-fraco); color: var(--meta); font: var(--t-mini) var(--fonte); }
.zap-perfil-bloco { padding: 12px 0; border-top: 1px solid var(--fio); }
.zap-perfil-bloco h4 { margin: 0 0 5px; color: var(--meta); font: 600 var(--t-mini) var(--fonte); text-transform: none; }
.zap-perfil-bloco small, .zap-perfil-rodape { display: block; margin-top: 4px; color: var(--meta); font: var(--t-mini) var(--fonte); }
.zap-perfil-aviso, .zap-perfil-carga { margin: 0 0 12px; padding: 8px 10px; border-radius: 8px; background: var(--fio-fraco); color: var(--meta); font: var(--t-meta) var(--fonte); }
.zap-perfil-resumo { margin: 0; }
.zap-perfil-grade { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; margin-top: 8px; }
.zap-perfil-grade button { aspect-ratio: 1; padding: 0; border: 0; border-radius: 6px; overflow: hidden; background: var(--fio-fraco); cursor: zoom-in; }
.zap-perfil-grade img { width: 100%; height: 100%; object-fit: cover; display: block; }
.zap-perfil-horas { width: 100%; border-collapse: collapse; font: var(--t-meta) var(--fonte); }
.zap-perfil-horas th { text-align: left; font-weight: 400; color: var(--meta); padding: 2px 0; }
.zap-perfil-horas td { text-align: right; padding: 2px 0; }
.zap-perfil-gente { list-style: none; margin: 0; padding: 0; }
.zap-perfil-gente li { display: flex; justify-content: space-between; gap: 8px; padding: 4px 0; }
.zap-perfil-gente li span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.zap-perfil-gente li small { margin: 0; flex: none; }
.zap-perfil-nome { display: flex; gap: 6px; }
.zap-perfil-nome .campo { flex: 1; min-width: 0; }
.zap-perfil-rodape { padding-top: 12px; border-top: 1px solid var(--fio); }
.zap-tg { flex: 1; min-width: 0; display: flex; flex-direction: column; line-height: 1.3; }
.zap-tg .nome { font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.zap-tg .zap-sub { font: var(--t-mini) var(--fonte); color: var(--meta); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.zap-msgs { flex: 1; min-height: 0; overflow: auto; padding: 16px 20px; display: flex; flex-direction: column; gap: 8px; }
.zap-dia { align-self: center; font: var(--t-rotulo) var(--fonte); color: var(--meta); margin: 4px 0; }
.zap-dia:first-child { margin-top: 0; }
.zap-msg { max-width: min(72%, 560px); padding: 7px 11px; border-radius: var(--raio-bolha); background: var(--bolha-fundo); color: var(--texto);
  align-self: flex-start; display: flex; flex-direction: column; gap: 3px; overflow-wrap: anywhere; }
.zap-msg.entrada { border-bottom-left-radius: 4px; }
.zap-msg.saida { align-self: flex-end; background: var(--bolha-saida); color: var(--sobre-acento); border-bottom-right-radius: 4px; }
.zap-msg.saida a { color: var(--sobre-acento); text-decoration: underline; }
.zap-msg.cont { margin-top: -4px; }
.zap-msg { position: relative; }
.zap-msg:target { outline: 2px solid var(--acento); outline-offset: 2px; }
.zap-txt a { color: var(--acento); text-decoration: underline; }
.zap-citada { display: flex; flex-direction: column; gap: 1px; padding: 4px 8px; border-left: 3px solid var(--acento); border-radius: 6px; background: var(--fio-fraco); color: inherit; text-decoration: none; font: var(--t-meta) var(--fonte); min-width: 0; }
.zap-citada b { font-weight: 600; }
.zap-citada.mini { flex-direction: row; align-items: center; gap: 8px; padding-right: 4px; }
.zap-cit-txt { display: flex; flex-direction: column; gap: 1px; flex: 1; min-width: 0; font-style: normal; }
.zap-cit-mini { width: 44px; height: 44px; flex: none; border-radius: 5px; object-fit: cover; background: var(--fio-fraco); }
.zap-cit-mini.fig { object-fit: contain; background: none; }
a.zap-citada { cursor: pointer; }
.zap-msg.alvo { animation: zap-alvo 1.8s ease-out; }
@keyframes zap-alvo { 0%, 45% { box-shadow: 0 0 0 3px var(--acento); } 100% { box-shadow: 0 0 0 3px transparent; } }
@media (prefers-reduced-motion: reduce) { .zap-msg.alvo { animation-duration: .01s; outline: 2px solid var(--acento); } }
.zap-citada span { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; color: var(--meta); }
.zap-msg.saida .zap-citada { border-left-color: var(--sobre-acento); background: color-mix(in srgb, var(--sobre-acento) 16%, transparent); text-decoration: none; }
.zap-msg.saida .zap-citada span { color: var(--sobre-acento); opacity: .85; }
.zap-editada { font-style: italic; margin-right: 2px; }
.zap-encaminhada { display: inline-flex; align-items: center; gap: 4px; font: italic var(--t-mini) var(--fonte); color: var(--meta); }
.zap-encaminhada svg { width: 12px; height: 12px; }
.zap-msg.saida .zap-encaminhada { color: var(--sobre-acento); opacity: .85; }
.zap-apagada { font-style: italic; color: var(--meta); }
.zap-msg.saida .zap-apagada { color: var(--sobre-acento); opacity: .85; }
.zap-acoes button.armado, .zap-acoes button.armado:hover { background: var(--destrutivo); color: var(--sobre-acento); }
.zap-reacoes { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 1px; }
.zap-reacao { display: inline-flex; align-items: center; gap: 3px; height: 22px; padding: 0 7px; border: 1px solid var(--fio); border-radius: 11px; background: var(--fundo); color: var(--texto); font: var(--t-mini) var(--fonte); }
button.zap-reacao { cursor: pointer; }
.zap-reacao.minha { border-color: var(--acento); }
.zap-acoes { position: absolute; top: 2px; left: 100%; display: flex; gap: 2px; padding-left: 6px; opacity: 0; }
.zap-msg.saida .zap-acoes { left: auto; right: 100%; padding: 0 6px 0 0; }
.zap-msg:hover .zap-acoes, .zap-msg:focus-within .zap-acoes, .zap-msg.reagindo .zap-acoes { opacity: 1; }
.zap-acoes button { width: 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center; border: 0; border-radius: 7px; background: none; color: var(--meta); cursor: pointer; }
.zap-acoes button:hover { background: var(--fio); color: var(--texto); }
.zap-acoes button:focus-visible, button.zap-reacao:focus-visible, .zap-emojis button:focus-visible { outline: 2px solid var(--acento); outline-offset: 1px; }
.zap-emojis { position: fixed; z-index: 30; display: flex; gap: 2px; padding: 4px; border-radius: 18px; background: var(--fundo); box-shadow: var(--sombra-flutuante); }
.zap-emojis.grade { display: grid; grid-template-columns: repeat(8, 32px); width: max-content; max-height: 232px; overflow: auto; border-radius: 12px; padding: 6px; }
.zap-emojis button.mais { color: var(--meta); font: 400 1.25rem/1 var(--fonte); }
.zap-emojis button { width: 32px; height: 32px; border: 0; border-radius: 50%; background: none; font-size: 1.25rem; line-height: 1; cursor: pointer; }
.zap-emojis button:hover { background: var(--fio); }
.zap-anexo a.zap-mapa { margin-left: auto; color: var(--acento); text-decoration: underline; white-space: nowrap; }
.zap-msg.saida .zap-anexo a.zap-mapa { color: var(--sobre-acento); }
.zap-anexo + .zap-anexo { margin-top: 4px; }
.zap-enquete { margin: 2px 0 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 4px; }
.zap-enquete li { padding: 4px 8px; border: 1px solid var(--fio); border-radius: 7px; }
.zap-msg.saida .zap-enquete li { border-color: color-mix(in srgb, var(--sobre-acento) 40%, transparent); }
.zap-anteriores { align-self: center; height: 26px; padding: 0 12px; border: 1px solid var(--fio); border-radius: 13px; background: var(--fundo); color: var(--texto); font: var(--t-meta) var(--fonte); cursor: pointer; }
.zap-anteriores:hover { background: var(--fio-fraco); }
.zap-teto { align-self: center; font: var(--t-mini) var(--fonte); color: var(--meta); }
dialog.zap-enc { width: min(420px, 92vw); max-height: 80vh; padding: 14px; border: 0; border-radius: 12px; background: var(--fundo); color: var(--texto); box-shadow: var(--sombra-flutuante); }
dialog.zap-enc::backdrop { background: color-mix(in srgb, #000 40%, transparent); }
dialog.zap-enc[open] { display: flex; flex-direction: column; gap: 10px; }
.zap-enc h2 { margin: 0; font: 700 1rem/1.3 var(--fonte); }
.zap-enc p { margin: 0; font: var(--t-meta) var(--fonte); color: var(--meta); }
.zap-enc-lista { flex: 1 1 auto; min-height: 120px; max-height: 320px; overflow: auto; display: flex; flex-direction: column; gap: 2px; }
.zap-enc-lista button { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; width: 100%; padding: 5px 10px; border: 0; border-radius: 7px; background: none; color: var(--texto); font: inherit; text-align: left; cursor: pointer; }
/* pedido 84: a foto da própria linha da lista (mesma rota e mesmo cache); sem foto, a silhueta da lista */
.zap-enc-lista .zap-av { width: 28px; height: 28px; }
.zap-enc-lista .zap-av svg { width: 15px; height: 15px; }
.zap-enc-nome { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.zap-enc-lista button:hover { background: var(--fio-fraco); }
.zap-enc-lista button[aria-pressed="true"] { background: var(--acento); color: var(--sobre-acento); }
.zap-enc-barra { display: flex; justify-content: flex-end; gap: 8px; }
.zap-citando { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-left: 3px solid var(--acento); border-radius: 6px; background: var(--fundo); font: var(--t-meta) var(--fonte); }
.zap-citando[hidden] { display: none; }
.zap-citando-txt { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.zap-citando-txt span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--meta); }
.zap-citando .btn-icone { flex: none; width: 24px; height: 24px; display: inline-flex; align-items: center; justify-content: center; border: 0; background: none; border-radius: 6px; color: var(--meta); cursor: pointer; }
.zap-quem { font: 600 .9231rem/1.3 var(--fonte); color: var(--meta); }
.zap-txt { white-space: pre-wrap; }
.zap-meta { align-self: flex-end; display: inline-flex; align-items: center; gap: 3px; font: var(--t-mini) var(--fonte); color: var(--meta); margin-top: -1px; }
.zap-meta svg { width: 13px; height: 13px; }
.zap-msg.saida .zap-meta { color: var(--sobre-acento); }
.zap-img { display: block; max-width: 320px; max-height: 320px; border-radius: 10px; }
.zap-img.fig { max-width: 150px; max-height: 150px; }
.zap-abrir { display: block; padding: 0; border: 0; background: none; border-radius: 10px; cursor: zoom-in; }
.zap-abrir:focus-visible { outline: 2px solid var(--acento); outline-offset: 2px; }
.zap-vid { position: relative; display: block; width: 320px; max-width: 100%; aspect-ratio: 16 / 9; border-radius: 10px; overflow: hidden; background: #000; margin-bottom: 6px; }
.zap-vid.vert { width: 190px; max-height: 340px; }
.zap-video { display: block; width: 100%; height: 100%; object-fit: cover; cursor: pointer; }
.zap-vid button { position: absolute; display: grid; place-items: center; border: 0; border-radius: 50%; color: #fff; background: color-mix(in srgb, #000 55%, transparent); cursor: pointer; transition: opacity .15s; }
.zap-vid-tocar { left: 50%; top: 50%; width: 52px; height: 52px; margin: -26px 0 0 -26px; }
.zap-vid-tocar i { display: grid; place-items: center; font-style: normal; }
.zap-vid-tocar .ic-pausa, .zap-vid.tocando .ic-play { display: none; }
.zap-vid.tocando .ic-pausa { display: grid; }
.zap-vid.tocando .zap-vid-tocar { opacity: 0; }
.zap-vid.tocando:hover .zap-vid-tocar, .zap-vid-tocar:focus-visible { opacity: 1; }
.zap-vid-expandir { right: 8px; top: 8px; width: 30px; height: 30px; }
.zap-vid button:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
dialog.zap-ampliada video { display: block; max-width: 96vw; max-height: calc(96vh - 52px); border-radius: 10px; background: #000; }
.zap-baixar { flex: none; display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; margin-left: auto; border-radius: 7px; color: var(--meta); }
.zap-baixar:hover { background: var(--fio); color: var(--texto); }
.zap-produto { display: flex; flex-direction: column; gap: 8px; max-width: 320px; }
.zap-produto .zap-img { width: 100%; max-width: 320px; max-height: 320px; object-fit: cover; }
.zap-prod-info { display: flex; flex-direction: column; gap: 2px; }
.zap-prod-info small { color: var(--meta); font-size: 12px; }
.zap-prod-info b { font-weight: 600; line-height: 1.3; }
.zap-prod-preco { font-size: 15px; font-weight: 600; }
.zap-prod-desc summary { cursor: pointer; color: var(--acento); font-size: 13px; list-style: none; }
.zap-prod-desc summary::-webkit-details-marker { display: none; }
.zap-prod-desc[open] summary { margin-bottom: 6px; }
.zap-msg.saida .zap-prod-info small, .zap-msg.saida .zap-prod-desc summary { color: var(--sobre-acento); opacity: .85; }
.zap-nao-abre { color: var(--meta); }
.zap-foto { position: relative; display: inline-block; max-width: 100%; }
.zap-previa-foto { display: block; max-width: min(280px, 100%); max-height: 280px; border-radius: 10px; margin-bottom: 4px; }
.zap-foto .zap-baixar, .zap-msg.saida .zap-foto .zap-baixar { position: absolute; right: 8px; bottom: 8px; margin: 0; width: 30px; height: 30px; border-radius: 50%; background: var(--acento); color: var(--sobre-acento); opacity: 0; transition: opacity .12s; }
.zap-foto:hover .zap-baixar, .zap-foto .zap-baixar:focus-visible { opacity: 1; }
@media (hover: none) { .zap-foto .zap-baixar { opacity: .92; } }
.zap-msg.saida .zap-baixar { color: var(--sobre-acento); }
.zap-msg.saida .zap-baixar:hover { background: transparent; opacity: .75; color: var(--sobre-acento); }
dialog.zap-ampliada { padding: 0; border: 0; background: transparent; max-width: 96vw; max-height: 96vh; }
dialog.zap-ampliada::backdrop { background: color-mix(in srgb, #000 72%, transparent); }
dialog.zap-ampliada img { display: block; max-width: 96vw; max-height: calc(96vh - 52px); border-radius: 10px; }
.zap-ampliada-barra { display: flex; justify-content: flex-end; gap: 8px; padding-top: 10px; }
.zap-pendente, .zap-gravando { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border: 1px solid var(--fio); border-radius: 8px; background: var(--fundo); font: var(--t-meta) var(--fonte); }
.zap-pendente[hidden], .zap-gravando[hidden] { display: none; }
.zap-pendente-nome { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.zap-pendente .btn-icone { flex: none; width: 24px; height: 24px; display: inline-flex; align-items: center; justify-content: center; border: 0; background: none; border-radius: 6px; color: var(--meta); cursor: pointer; }
.zap-gravando-dica { flex: 1; min-width: 0; color: var(--meta); }
.zap-ponto { flex: none; width: 9px; height: 9px; border-radius: 50%; background: var(--destrutivo); animation: zap-pulso 1.1s ease-in-out infinite; }
@keyframes zap-pulso { 50% { opacity: .25; } }
@media (prefers-reduced-motion: reduce) { .zap-ponto { animation: none; } }
/* pedido 84: sem tranco. Bolha nova entra em 160 ms; abrir outra conversa troca a tela com um esmaecer de 150 ms (a
   lista fica parada); com "reduzir movimento" nada disso roda (a base também zera animação nesse caso). */
@keyframes zap-chega { from { opacity: 0; transform: translateY(4px); } }
.zap-msg.chegou { animation: zap-chega .16s ease-out; }
@media (prefers-reduced-motion: no-preference) {
  @view-transition { navigation: auto; }
  .zap-lista { view-transition-name: zap-lista; }
  ::view-transition-group(*) { animation-duration: .15s; }
}
@media (prefers-reduced-motion: reduce) { .zap-msg.chegou { animation: none; } }
.zap-pendente { flex-direction: column; align-items: stretch; }
.zap-pend-barra { display: flex; align-items: center; gap: 8px; min-width: 0; }
.zap-pend-previa:empty { display: none; }
.zap-pend-previa { display: flex; justify-content: center; }
.zap-pend-previa img { max-width: 100%; max-height: 200px; border-radius: 8px; display: block; }
.zap-pend-minis { display: flex; gap: 6px; overflow-x: auto; padding: 2px; }
.zap-pend-minis[hidden] { display: none; }
.zap-mini { flex: none; width: 44px; height: 44px; border: 0; border-radius: 6px; padding: 0; background: var(--fundo-2) center / cover no-repeat;
  color: var(--meta); font: var(--t-mini) var(--fonte); box-shadow: inset 0 0 0 1px var(--fio); display: inline-flex; align-items: center; justify-content: center; overflow: hidden; }
.zap-mini.com-foto span { display: none; }
.zap-mini.on { box-shadow: 0 0 0 2px var(--acento); }
.zap-mini:focus-visible { outline: 3px solid var(--foco); outline-offset: 2px; }
.zap-solte { position: fixed; z-index: 40; display: flex; align-items: center; justify-content: center; pointer-events: none; box-sizing: border-box;
  border: 2px dashed var(--acento); border-radius: 12px; background: color-mix(in srgb, var(--fundo) 86%, var(--acento)); color: var(--texto); font: var(--t-vazio) var(--fonte); }
.zap-solte[hidden] { display: none; }
.zap-audio { display: flex; align-items: center; gap: 8px; }
.zap-audio audio { height: 32px; max-width: 260px; }
.zap-audio > span { font: var(--t-mini) var(--fonte); color: var(--meta); }
.zap-anexo { display: flex; align-items: center; gap: 8px; }
.zap-midia-erro { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 4px; padding: 6px 8px; border-radius: var(--raio-controle); background: var(--citacao-fundo); font: var(--t-meta) var(--fonte); color: var(--texto-2); max-width: 320px; }
.zap-midia-erro .btn { height: 24px; padding: 0 9px; font: var(--t-meta) var(--fonte); }
.zap-foto.zap-midia-falhou, img.zap-img.zap-midia-falhou { display: none; }
.zap-anexo svg { flex: none; color: var(--meta); }
.zap-anexo small { display: block; font: var(--t-mini) var(--fonte); color: var(--meta); }
.zap-msg.saida :is(.zap-quem, .zap-audio > span, .zap-anexo svg, .zap-anexo small) { color: var(--sobre-acento); }
.zap-ia { display: flex; align-items: center; gap: 8px; padding-left: 4px; max-width: min(72%, 560px); font: var(--t-meta) var(--fonte); color: var(--meta); }
.zap-ia.saida { align-self: flex-end; }
.zap-ia a.classe { color: var(--meta); }
.zap-ia a.classe:hover { text-decoration: underline; }
.zap-ia-txt { min-width: 0; overflow-wrap: anywhere; }
.zap-vazio-fio { margin: auto; color: var(--meta); text-align: center; }
/* D-47: transcrição do áudio abaixo do player (herda a cor da bolha) */
.zap-transcricao { margin: 4px 0 0; font-size: .9231rem; line-height: 1.45; max-width: 48ch; }
.zap-transcricao small { display: block; margin-top: 3px; font-size: .8462rem; opacity: .7; }
details.zap-transcricao > summary { cursor: default; list-style: none; }
details.zap-transcricao > summary::-webkit-details-marker { display: none; }
details.zap-transcricao > summary b { font-weight: 600; text-decoration: underline; }
details.zap-transcricao[open] > summary { display: none; }
details.zap-transcricao > p { margin: 0; }
/* rodapé de envio */
.zap-rodape-envio { flex: none; display: flex; flex-direction: column; gap: 6px; padding: 10px 16px 12px; border-top: 1px solid var(--fio); background: var(--fundo-2); margin: 0; }
.zap-rodape-envio.nota { color: var(--meta); font: var(--t-meta) var(--fonte); }
.zap-caixa { display: flex; align-items: flex-end; gap: 8px; }
#zap-texto { flex: 1; height: 32px; min-height: 32px; max-height: 160px; padding: 7px 10px; line-height: 1.35; resize: none; }
.zap-caixa .btn { height: 32px; }
.zap-caixa .btn.zap-enviar, .zap-caixa .btn.zap-extra { width: 32px; padding: 0; flex: none; }
.zap-caixa .btn.zap-extra svg { width: 16px; height: 16px; }
.zap-caixa .btn.zap-extra.ativo { color: var(--destrutivo); }
.zap-caixa .btn.zap-enviar svg { width: 15px; height: 15px; }
.zap-agendados { display: flex; flex-direction: column; gap: 4px; margin-bottom: 6px; }
.zap-agendados:empty { display: none; }
.zap-agendado { display: flex; align-items: center; gap: 8px; padding: 5px 8px 5px 10px; border-radius: var(--raio-controle); background: var(--citacao-fundo); font: var(--t-meta) var(--fonte); color: var(--texto); }
.zap-agendado svg { flex: none; color: var(--meta); }
.zap-agendado b { font-weight: 600; white-space: nowrap; }
.zap-agendado .zap-ag-txt { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--meta); }
.zap-agendado .btn { height: 24px; padding: 0 8px; font: var(--t-meta) var(--fonte); }
.zap-caixa { position: relative; }
.zap-agenda-pop { position: absolute; right: 0; bottom: 40px; z-index: 30; width: 268px; padding: 10px; border-radius: var(--raio-grupo); background: var(--menu-fundo); box-shadow: var(--sombra-flutuante); display: flex; flex-direction: column; gap: 8px; }
.zap-agenda-pop[hidden] { display: none; }
.zap-agenda-pop .zap-ag-tit { font: 600 var(--t-meta) var(--fonte); color: var(--meta); }
.zap-agenda-pop .zap-ag-atalhos { display: flex; flex-wrap: wrap; gap: 6px; }
.zap-agenda-pop .zap-ag-atalhos .btn { height: 26px; padding: 0 9px; font: var(--t-meta) var(--fonte); }
.zap-agenda-pop input { width: 100%; box-sizing: border-box; }
.zap-agenda-pop .zap-ag-nota { font: var(--t-meta) var(--fonte); color: var(--meta); margin: 0; }
.zap-incerto { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; padding: 6px 8px 6px 10px; border-radius: var(--raio-controle); background: var(--atrasado-fundo); color: var(--atrasado-texto); font: var(--t-meta) var(--fonte); }
.zap-incerto[hidden] { display: none; }
/* pedido 84 (D-87): a bolha provisória diz o que houve. Não enviada e travada: Tentar de novo e Descartar. Pode ter
   saído: sem repetir (a faixa de cima segura os envios até "Já conferi"). */
.zap-msg.saida.otimista:is(.falhou, .travado, .incerta) { background: var(--atrasado-fundo); color: var(--atrasado-texto); }
.zap-msg.saida.otimista:is(.falhou, .travado, .incerta) .zap-meta { color: inherit; }
.zap-falha { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 6px; font: var(--t-mini) var(--fonte); }
.zap-falha span { flex: 1 1 100%; }
.zap-falha button { padding: 2px 8px; border: 1px solid currentColor; border-radius: var(--raio-controle); background: none; color: inherit; font: inherit; cursor: pointer; }
.zap-item.enviando .zap-previa > [title="saindo"] { display: inline; }
.zap-item.enviando .zap-previa svg { display: inline-block; width: 12px; height: 12px; margin-right: 3px; vertical-align: -2px; }
.zap-incerto .zap-incerto-txt { flex: 1; min-width: 0; }
.zap-incerto .btn { flex: none; height: 24px; padding: 0 9px; font: var(--t-meta) var(--fonte); }
.zap-incerto.pisca { animation: zap-incerto-pisca .5s ease-out 1; }
@keyframes zap-incerto-pisca { 0% { box-shadow: 0 0 0 0 var(--atrasado-texto); } 100% { box-shadow: 0 0 0 6px transparent; } }
@media (prefers-reduced-motion: reduce) { .zap-incerto.pisca { animation: none; } }
.zap-aviso { font: var(--t-meta) var(--fonte); color: var(--meta); }
.zap-aviso.ruim { color: var(--destrutivo); }
.zap-aviso[hidden] { display: none; }
/* nova mensagem */
.zap-nova fieldset { border: 0; padding: 0; margin: 0; min-width: 0; }
.zap-nova fieldset[disabled] { opacity: .55; }
.zap-nova .linha-campos .campo { flex: 1; width: auto; }
.zap-nova .zap-res:empty { display: none; }
.zap-nova .zap-res { margin-top: 6px; }
.zap-nova .zap-res.ruim { color: var(--destrutivo); }
.zap-nova .zap-res.ok { color: var(--ok); }
.zap-chave { display: inline-flex; align-items: center; gap: 8px; }
.zap-chave input { width: 14px; height: 14px; margin: 0; accent-color: var(--acento); }
.zap-nova .zap-acoes { margin-top: 4px; }
.zap-nova .zap-acoes .zap-aviso { flex: 1; min-width: 0; }
.zap-vazio-acao { margin-top: 12px; }
</style>"""

_TEXTO_FORA = ("Apaga o que já guardou dela, não guarda mais nada que chegar e a IA não lê. Ela some da lista. "
               "No celular nada muda.")
_CANCELAR = '<button type="button" class="btn sm" data-passo-ir="menu">Cancelar</button>'


def _menu(itens: str, id_: str = "", classe: str = "") -> str:
    """Menu ··· da toolbar (details.menu da base) cujos itens vêm em passos (D-48); `itens` já escapado."""
    i = f' id="{id_}"' if id_ else ""
    return (f'<details class="menu"{i}><summary class="btn icone" aria-haspopup="menu" aria-label="Mais opções" '
            f'title="Mais opções">{c.icone("mais")}</summary>'
            f'<div class="menu-itens direita zap-menu-lista{(" " + classe) if classe else ""}" role="menu">{itens}</div></details>')


def _form_fora(jid: str, extra: str = "") -> str:
    """Formulário de 'Deixar fora' (D-41) com volta para a lista; `extra` já escapado vai dentro do .acoes."""
    return (f'<form method="post" action="/bloqueios"><input type="hidden" name="acao" value="bloquear">'
            f'<input type="hidden" name="c" value="{c.esc(jid)}"><input type="hidden" name="volta" value="/conversas">'
            f'<div class="acoes"><button type="submit" class="btn sm destrutivo">Deixar fora</button>{extra}</div></form>')


# ------------------------------------------------------------------ serviços do contexto

def servicos(ctx) -> SimpleNamespace:
    """Grupos, fotos e trava de envio: um de cada por servidor (criados no primeiro uso)."""
    with _TRAVA:
        s = getattr(ctx, "zap", None)
        if s is None:
            dados = c.dados_dir(ctx)
            s = SimpleNamespace(grupos=wa.Grupos(dados / "grupos.json", ctx.evo),
                                avatares=wa.Avatares(dados / "avatares", ctx.evo), liberacao=Liberacao(minutos=lambda: pf.ler()["envio_min"]),
                                perfis=wa.Perfis(dados / "perfis.json", ctx.evo), vistas=wa.Vistas(dados / "vistas.json"))
            ctx.zap = s
    return s


def desde_leitura(ctx) -> int | None:
    try:
        # D-53: pareado_em − 1 dia; depois de um repareamento, a janela que as telas já mostravam (pedido 75, D-74)
        return Cursor.carregar(c.dados_dir(ctx) / "cursor.json", reparar=False).desde_tela
    except Exception:  # cursor ilegível: a Conexão mostra; aqui só não lê nada (R-2)
        return None


def _q(v: str) -> str:
    return urllib.parse.quote(str(v), safe="")


def quando(ts: int) -> str:
    try:
        d = datetime.fromtimestamp(int(ts))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    hoje = date.today()
    if d.date() == hoje:
        return d.strftime("%H:%M")
    if d.date() == hoje - timedelta(days=1):
        return "ontem"
    if 0 < (hoje - d.date()).days < 7:
        return DIAS_CURTOS[d.weekday()]
    return d.strftime("%d/%m")


def _hhmm(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def dia(ts: int) -> str:
    try:
        d = datetime.fromtimestamp(int(ts)).date()
    except (TypeError, ValueError, OSError, OverflowError):   # timestamp fora da faixa não derruba o fio inteiro
        return ""
    hoje = date.today()
    if d == hoje:
        return "Hoje"
    if d == hoje - timedelta(days=1):
        return "Ontem"
    if 0 < (hoje - d).days < 7:
        return DIAS_LONGOS[d.weekday()].capitalize()
    return d.strftime("%d/%m/%Y")


def _sem_acento(txt: str) -> str:
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFD", txt.lower()) if unicodedata.category(ch) != "Mn")


# ------------------------------------------------------------------ dados

def carregar_lista(ctx) -> tuple[list[dict], str]:
    """(conversas, problema). problema: '' | 'nao_pareado' | 'sem_banco'."""
    return carregar(ctx)[:2]


def carregar(ctx, vista: str = "") -> tuple[list[dict], str, set | None]:
    """(conversas, problema, bloqueados). problema: '' | 'nao_pareado' | 'sem_banco'. `bloqueados` é a lista que já
    veio do banco (None quando não houve consulta), para o fio da conversa aberta não repetir o mesmo psql.
    `vista` (pedido 60): a conversa que o dono está abrindo agora; as não lidas dela até este momento deixam de
    contar no Greg. O WhatsApp não é avisado."""
    par = desde_leitura(ctx)
    if par is None:
        return [], "nao_pareado", None
    psql = getattr(ctx, "psql", None)
    if psql is None:
        return [], "sem_banco", None
    try:
        linhas = wa.listar(psql, desde=par)
        bloq = wa.bloqueados(psql)  # D-41: o banco já tira o conteúdo; aqui a conversa nem aparece
    except (VigiaErro, ValueError) as e:
        log.warning("lista de conversas indisponível: %s", causa(e))
        return [], "sem_banco", None
    linhas = [ln for ln in linhas if ln["jid"] not in bloq]
    svc = servicos(ctx)
    if vista:
        aberta = next((ln for ln in linhas if ln["jid"] == vista), None)
        if aberta and aberta.get("nao_lida_ts"):
            svc.vistas.marcar(vista, int(aberta["nao_lida_ts"]))
    vistas = svc.vistas.todas()
    for ln in linhas:      # não lida que chegou antes da última vez que a conversa foi aberta no Greg não acende a bolinha
        if ln["nao_lidas"] and 0 < int(ln.get("nao_lida_ts") or 0) <= vistas.get(ln["jid"], 0):
            ln["nao_lidas"] = 0
    apelidos = ctx.banco.apelidos()
    grupos = svc.grupos.nomes([ln["jid"] for ln in linhas])
    classes = {r["conversa"]: r["ultima_classe"] for r in ctx.banco.conversas_recentes(dias=7)}
    pedidos = []
    saida = []
    for ln in linhas:
        jid = ln["jid"]
        tipo = wa.tipo_de(jid)
        g = grupos.get(jid) or {}
        nome = wa.nome_da_conversa(jid, apelido=apelidos.get(jid, ""), contato=ln["contato"], push=ln["push"],
                                   grupo=g.get("subject", ""))
        url = ln["foto_url"] or g.get("foto", "")
        if url or tipo != "grupo":   # pedido 54: pessoa sem URL de foto no banco é perguntada ao WhatsApp
            pedidos.append((jid, url))
        cont = wa.conteudo(ln["message"], ln["tipo_msg"])
        prev = wa.APAGADA if ln.get("apagada") else wa.previa(cont)
        if wa._de_mim(ln["key"]):
            prev = "Você: " + prev
        elif tipo == "grupo":
            quem = wa.nome_do_remetente(wa.remetente(ln["key"], jid), ln["push_ultima"], apelidos)
            prev = f"{quem.split()[0] if quem else 'Alguém'}: {prev}"
        saida.append({"jid": jid, "tipo": tipo, "nome": nome, "ts": ln["ts"], "nao_lidas": ln["nao_lidas"],
                      "previa": prev, "classe": classes.get(jid, ""), "membros": g.get("membros", 0),
                      "busca": _sem_acento(f"{nome} {wa.digitos(jid)}")})
    if pedidos:
        svc.avatares.pedir(pedidos)
    ind = svc.avatares.indice()
    for s in saida:
        s["foto"] = svc.avatares.arquivo(s["jid"], ind) is not None
    return saida, "", set(bloq)


def filtrar(conversas: list[dict], f: str, q: str) -> list[dict]:
    if f == "pessoas":
        conversas = [x for x in conversas if x["tipo"] != "grupo"]
    elif f == "grupos":
        conversas = [x for x in conversas if x["tipo"] == "grupo"]
    elif f == "naolidas":
        conversas = [x for x in conversas if x["nao_lidas"]]
    termo = _sem_acento(q.strip())
    if termo:
        so_num = re.sub(r"\D", "", termo)
        conversas = [x for x in conversas if termo in x["busca"] or (so_num and len(so_num) >= 3 and so_num in x["busca"])]
    return conversas


# ------------------------------------------------------------------ pedaços de HTML

def avatar(jid: str, nome: str, foto: bool, classe: str = "") -> str:
    """Foto guardada em disco (servida por /conversas/avatar) ou a silhueta cinza da base. Sem iniciais coloridas (Papel)."""
    cls = ("avatar zap-av " + classe).strip()
    if foto:
        return f'<img class="{cls}" src="/conversas/avatar?j={c.esc(_q(jid))}" alt="">'
    return c.avatar(wa.tipo_de(jid) == "grupo", classe=("zap-av " + classe).strip())


def _classe_linha(classe: str) -> str:
    """Ponto + palavra na linha da lista; "lembrando" mostra só "lembrar" (a linha tem 300 px), o resto fica no tooltip."""
    if classe not in ("atencao", "demanda", "lembrar", "lembrando"):
        return ""
    if classe == "lembrando":
        return f'<span class="classe lembrar" title="{c.rotulo_classe(classe)}">lembrar</span>'
    return c.classe_html(classe)


def html_item(x: dict, ativo: str, f: str) -> str:
    nl = f'<span class="nao-lida" role="img" aria-label="{x["nao_lidas"]} não lidas"></span>' if x["nao_lidas"] else ""
    cls = "item-lista zap-item" + (" ativo" if x["jid"] == ativo else "") + (" nl" if x["nao_lidas"] else "")
    atual = ' aria-current="true"' if x["jid"] == ativo else ""
    # D-48: a caixa de marcar pertence ao formulário da barra (atributo form) e só aparece no modo Selecionar; o ⋯ abre o
    # menu da linha (o botão direito também). data-jid/data-nome alimentam o menu; a confirmação é feita no servidor (D-41).
    return (f'<a class="{cls}"{atual} href="/conversas?c={c.esc(_q(x["jid"]))}&amp;f={c.esc(f)}" data-busca="{c.esc(x["busca"])}" '
            f'data-jid="{c.esc(x["jid"])}" data-nome="{c.esc(x["nome"])}">'
            f'<input type="checkbox" class="zap-marca" name="j:{c.esc(x["jid"])}" value="1" form="form-fora-varios" tabindex="-1" '
            f'aria-label="Marcar {c.esc(x["nome"])}">'
            f'{avatar(x["jid"], x["nome"], x["foto"])}'
            f'<span class="zap-corpo"><span class="cab"><span class="nome zap-nome">{c.esc(x["nome"])}</span>'
            f'<time>{c.esc(quando(x["ts"]))}</time></span>'
            f'<span class="meta zap-l2"><span class="texto zap-previa">{c.esc(x["previa"])}</span>{_classe_linha(x["classe"])}{nl}</span></span>'
            f'<span class="zap-mais" role="button" tabindex="0" aria-label="Mais opções">{c.icone("mais")}</span></a>')


def _status_lista(todas: list[dict], problema: str) -> str:
    if problema == "nao_pareado":
        return '<a href="/conexao">Ir para Conexão</a>'
    if problema == "sem_banco":
        return 'Confira a <a href="/conexao">Conexão</a>'
    n, nl = len(todas), sum(1 for x in todas if x["nao_lidas"])
    if not n:
        return "Nenhuma conversa"
    s = f"{n} conversa" + ("s" if n != 1 else "")
    if nl:
        s += f", {nl} não lida" + ("s" if nl != 1 else "")
    return s


def html_lista(conversas: list[dict], problema: str, ativo: str, f: str, q: str, todas: list[dict], aviso_html: str = "") -> str:
    """Painel estreito (300 px): toolbar com título, pull-down de filtro, nova mensagem e ···; busca; linhas; rodapé.
    `aviso_html`: já escapado, decidido pelo chamador (falha do banco ao deixar fora); sucesso vai em toast."""
    nl = sum(1 for x in todas if x["nao_lidas"])
    volta_c = "&amp;c=" + c.esc(_q(ativo)) if ativo else ""
    itens_filtro = "".join(
        f'<a role="menuitem" href="/conversas?f={k}{volta_c}"{" class=" + chr(34) + "on" + chr(34) if k == f else ""}>'
        f'<span>{r}{f" <b>{nl}</b>" if k == "naolidas" and nl else ""}</span></a>' for k, r in FILTROS)
    filtro = c.pulldown(f'<span>{dict(FILTROS)[f]}</span>', itens_filtro, id_="zap-filtro", direita=True, aria="Filtro")
    if problema == "nao_pareado":
        itens = c.vazio("O número ainda não foi pareado", "As conversas aparecem a partir do pareamento, sem histórico antigo")
    elif problema == "sem_banco":
        itens = f'<div class="zap-erro">{c.erro_inline("O banco da Evolution não respondeu agora.", f"<a class={chr(34)}btn{chr(34)} href={chr(34)}{c.esc(_href_lista(f, q, ativo))}{chr(34)}>Tentar de novo</a>")}</div>'
    elif not conversas:
        itens = (c.vazio("Nenhuma conversa com esse filtro") if todas else
                 c.vazio("Nenhuma mensagem desde o pareamento", "As conversas aparecem aqui conforme chegam"))
    else:
        itens = "".join(html_item(x, ativo, f) for x in conversas)
    menu_topo = _menu(f'<div class="zap-passo" data-passo="menu"><button type="button" role="menuitem" id="zap-selecionar"><span>Selecionar…</span></button>'
                      f'<button type="button" role="menuitem" id="zap-ler-agora"><span>Ler agora</span></button>'
                      f'{c.item_menu("Quem está fora do Greg", href="/bloqueios")}</div>', "zap-menu-topo")
    # Menu de UMA linha (compartilhado; o JS posiciona junto da linha clicada ou do botão direito). Dois passos: itens e
    # confirmação. O "Abrir" recebe o href da linha; o formulário manda bloquear com volta=/conversas (D-41).
    menu_linha = (f'<div class="menu-itens zap-menu-lista" id="zap-menu-linha" role="menu" hidden>'
                  f'<div class="zap-passo" data-passo="menu"><a role="menuitem" id="zap-ml-abrir" href="/conversas"><span>Abrir</span></a>'
                  f'<button type="button" role="menuitem" class="destrutivo" data-passo-ir="fora"><span>Deixar fora do Greg…</span></button></div>'
                  f'<div class="zap-passo zap-confirma" data-passo="fora" hidden><strong id="zap-ml-titulo">Deixar fora do Greg?</strong>'
                  f'<p>{_TEXTO_FORA}</p>{_form_fora("", _CANCELAR)}</div></div>')
    # Barra do modo Selecionar: é o formulário que recebe as caixas das linhas (form="form-fora-varios"). A confirmação é
    # em dois cliques no próprio botão (JS da página), não em modal (guia Papel).
    barra = (f'<form class="zap-barra" id="form-fora-varios" method="post" action="/bloqueios" hidden>'
             f'<input type="hidden" name="acao" value="bloquear_varios"><input type="hidden" name="volta" value="/conversas">'
             f'<span class="n" id="zap-sel-conta">Nenhuma marcada</span>'
             f'<button type="submit" class="btn destrutivo" id="zap-sel-fora" disabled data-rotulo="Deixar fora do Greg" '
             f'data-confirma="Confirmar: deixar fora">Deixar fora do Greg</button></form>')
    toolbar = c.toolbar('<b class="titulo" id="zap-titulo-lista">Conversas</b>', c.espaco(), filtro,
                        c.botao_icone("nova", "Nova mensagem (⌘N)", href="/conversas/nova", attrs='id="zap-nova-btn"'), menu_topo,
                        '<button type="button" class="btn sm" id="zap-sel-cancelar" hidden>Cancelar</button>')
    busca = (f'<form class="zap-busca-linha" method="get" action="/conversas" role="search">'
             f'{c.busca("q", q, placeholder="Buscar", id_="zap-busca", largura="100%")}'
             f'<input type="hidden" name="f" value="{c.esc(f)}"></form>')
    return (f'<section class="painel lista estreita zap-lista" aria-label="Conversas">{toolbar}{busca}'
            f'<div class="corpo" id="zap-itens">{aviso_html}{itens}</div>{menu_linha}{barra}'
            f'{c.rodape(_status_lista(todas, problema), "zap-status-lista")}</section>')


def _href_lista(f: str, q: str, ativo: str) -> str:
    partes = [f"f={f}"] + ([f"q={_q(q)}"] if q else []) + ([f"c={_q(ativo)}"] if ativo else [])
    return "/conversas?" + "&".join(partes)


ESPERA_CONFIRMACAO_S = 60


def _ticks(status: str, ts: int = 0, agora: float | None = None) -> str:
    """Relógio só no primeiro minuto. A linha em "Message" existe porque a Evolution entregou a mensagem ao WhatsApp e
    recebeu o id; a confirmação do servidor às vezes chega minutos depois ou nunca (medido em 13/11/2026 na conversa
    com o próprio número: 17 s, 5 min, 26 min e sem confirmação). Passado o minuto, mostra como enviada."""
    if status in ("READ", "PLAYED"):
        return f'<span class="lido" title="lida">{ico("check2", 14)}</span>'
    if status == "DELIVERY_ACK":
        return f'<span title="entregue">{ico("check2", 14)}</span>'
    if status == "SERVER_ACK":
        return f'<span title="enviada">{ico("check", 13)}</span>'
    if status in ("PENDING", ""):
        if ts and (time.time() if agora is None else agora) - int(ts) > ESPERA_CONFIRMACAO_S:
            return f'<span title="enviada; o WhatsApp ainda não confirmou">{ico("check", 13)}</span>'
        return f'<span title="saindo">{ico("relogio", 12)}</span>'
    return ""


_TRANSCRICAO_RESUMO = 280


def html_transcricao(t: dict | None) -> str:
    """D-47: o texto do áudio abaixo do player. Curto vai inteiro; longo mostra o começo e abre em "ver tudo".
    Sem transcrição, falhou ou pulado = nada (o player fica como sempre)."""
    if not t or t.get("estado") not in ("ok", "cortado"):
        return ""
    texto = " ".join(str(t.get("texto") or "").split())
    if not texto:
        return ""
    nota = (f'<small>só os primeiros {c.esc(lotes._duracao(t.get("segundos_lidos")))}</small>'
            if t.get("estado") == "cortado" else "")
    if len(texto) <= _TRANSCRICAO_RESUMO:
        return f'<p class="zap-transcricao">{c.esc(texto)}{nota}</p>'
    corte = texto.rfind(" ", 0, _TRANSCRICAO_RESUMO)
    corte = corte if corte > 40 else _TRANSCRICAO_RESUMO
    return (f'<details class="zap-transcricao"><summary>{c.esc(texto[:corte])}… <b>ver tudo</b></summary>'
            f'<p>{c.esc(texto)}</p>{nota}</details>')


def _baixar(src: str, rotulo: str) -> str:
    """Link de download de uma mídia (pedido 50). `src` já vem escapado; o servidor responde com attachment."""
    return f'<a class="zap-baixar" href="{src}&amp;baixar=1" download aria-label="{c.esc(rotulo)}" title="{c.esc(rotulo)}">{ico("baixar", 15)}</a>'


_URL = re.compile(r"""(?:https?://[^\s<>"']+|www\.[^\s<>"']+\.[^\s<>"']+)""", re.I)


def com_links(texto: str) -> str:
    """Texto escapado com os endereços http(s) e www clicáveis (pedido 50, rodada 2). O link abre fora do Greg e não
    leva o endereço do painel junto (noreferrer): a query de /conversas tem o jid da conversa."""
    partes, pos = [], 0
    for m in _URL.finditer(texto or ""):
        url = m.group(0).rstrip(".,;:!?…")
        if url.endswith(")") and "(" not in url:
            url = url[:-1]
        if len(url) < 9:
            continue
        href = url if url[:4].lower() == "http" else "https://" + url
        partes.append(c.esc(texto[pos:m.start()]))
        partes.append(f'<a href="{c.esc(href)}" target="_blank" rel="noopener noreferrer">{c.esc(url)}</a>')
        pos = m.start() + len(url)
    partes.append(c.esc((texto or "")[pos:]))
    return "".join(partes)


def html_citada(m: dict, por_chave: dict, jid: str, apelidos: dict) -> str:
    """O bloco da mensagem citada, acima da resposta. Se a original está no fio, o bloco leva até ela."""
    q = m.get("citada")
    if not q:
        return ""
    orig = por_chave.get(q["id"])
    previa = wa.previa(wa.conteudo(q.get("msg") or {}, "")) or "Mensagem"
    if orig is not None and orig.get("apagada"):
        previa = wa.APAGADA                 # a cópia que veio dentro da resposta não é mostrada (D-63)
    de = str(q.get("de") or "")
    if orig is not None:
        quem = "Você" if wa._de_mim(orig["key"]) else wa.nome_do_remetente(wa.remetente(orig["key"], jid), orig["push"], apelidos)
    elif wa.tipo_de(jid) == "grupo":
        quem = wa.nome_do_remetente(de, "", apelidos) if de else ""
    elif de and not de.endswith("@lid"):
        quem = wa.nome_do_remetente(jid, "", apelidos) if wa.digitos(de) == wa.digitos(jid) else "Você"
    else:
        quem = ""
    miolo = (f"<b>{c.esc(quem)}</b>" if quem else "") + f"<span>{c.esc(previa)}</span>"
    mini = "" if (orig is not None and orig.get("apagada")) else _mini_da_citada(q.get("msg") or {}, orig)
    cls = "zap-citada"
    if mini:                                # pedido 58: foto, figurinha e vídeo citados levam a miniatura à direita
        miolo, cls = f'<i class="zap-cit-txt">{miolo}</i>{mini}', "zap-citada mini"
    if orig is not None:
        return f'<a class="{cls}" href="#m-{c.esc(orig["id"])}">{miolo}</a>'
    return f'<div class="{cls}">{miolo}</div>'


_B64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")


def _mini_da_citada(msg: dict, orig: dict | None) -> str:
    """A miniatura da mensagem citada. Foto e figurinha que ainda estão no fio usam a própria mídia; fora do fio
    (ou vídeo), vale a miniatura que o WhatsApp manda dentro da resposta. Sem nenhuma das duas, fica só o texto."""
    tipo = wa.conteudo(msg, "")["tipo"]
    if tipo not in ("imagem", "figurinha", "video"):
        return ""
    if orig is not None and tipo in ("imagem", "figurinha"):
        fig = " fig" if tipo == "figurinha" else ""
        return f'<img class="zap-cit-mini{fig}" loading="lazy" src="/conversas/midia?m={c.esc(orig["id"])}" alt="">'
    interna = lotes._desembrulhar(msg)
    for chave in ("imageMessage", "videoMessage"):
        b64 = (interna.get(chave) or {}).get("jpegThumbnail") if isinstance(interna.get(chave), dict) else None
        if isinstance(b64, str) and 0 < len(b64) <= 24000 and _B64.fullmatch(b64):
            return f'<img class="zap-cit-mini" src="data:image/jpeg;base64,{b64}" alt="">'
    return ""


def html_reacoes(lista: list[dict], apelidos: dict, acoes: bool) -> str:
    """Reações de uma mensagem, agrupadas por emoji; o nome de quem reagiu fica na dica. A minha é um botão que a tira."""
    if not lista:
        return ""
    grupos: dict = {}
    for r in lista:
        g = grupos.setdefault(r["emoji"], {"nomes": [], "minha": False})
        if r["quem"] == "eu":
            g["minha"] = True
            g["nomes"].append("Você")
        else:
            g["nomes"].append(wa.nome_do_remetente(r["quem"], r.get("push", ""), apelidos))
    partes = []
    for emoji, g in grupos.items():
        n = len(g["nomes"])
        miolo = c.esc(emoji) + (f"<span>{n}</span>" if n > 1 else "")
        titulo = c.esc(", ".join(g["nomes"]))
        if g["minha"] and acoes:
            partes.append(f'<button type="button" class="zap-reacao minha" data-tirar-reacao title="{titulo}. Clique para tirar a sua." '
                          f'aria-label="Tirar a sua reação {c.esc(emoji)}">{miolo}</button>')
        else:
            partes.append(f'<span class="zap-reacao{" minha" if g["minha"] else ""}" title="{titulo}">{miolo}</span>')
    return f'<div class="zap-reacoes">{"".join(partes)}</div>'


# Pedido 51: o "+" do Reagir abre esta grade; as usadas por último ficam na tira (guardadas no navegador).
EMOJIS_MAIS = ("👍 ❤️ 😂 😮 😢 🙏 👏 🔥 🎉 💯 ✅ 👀 🤝 💪 🙌 👌 "
               "😀 😁 😅 🤣 😊 😉 😍 😘 🥰 😎 🤩 🥳 🤔 🤨 😐 🙄 "
               "😬 😴 😔 😕 😟 😭 😤 😡 🤯 😱 🥺 😳 🤗 🤫 🤭 🫡 "
               "🤝 👋 ✌️ 🤞 🤙 👊 ✊ 👎 🫶 🤌 👉 👈 ☝️ 👇 🙋 🤷 "
               "💙 💚 💛 🧡 💜 🖤 🤍 💔 ⭐ ✨ ⚡ 💥 💡 📌 📎 📞 "
               "💰 💸 📈 📉 🗓️ ⏰ ⌛ 🚀 🎯 🏆 🥇 🎁 ☕ 🍻 🍕 🙃 "
               "❗ ❓ ⚠️ 🚫 ❌ ⭕ 🆗 🔝 🔜 ➡️ ⬅️ ⬆️ ⬇️ 🔁 🤡 💀")


JANELA_EDICAO = 15 * 60      # o WhatsApp só aceita editar até 15 min depois do envio
_MSG_TEXTO = ("conversation", "extendedTextMessage")
_ENCAMINHAVEL = ("texto", "imagem", "audio", "video", "documento")


def _acoes(editavel: bool, encaminhavel: bool, minha: bool = False) -> str:
    return (f'<span class="zap-acoes"><button type="button" data-reagir aria-label="Reagir" title="Reagir">{ico("rosto", 15)}</button>'
            f'<button type="button" data-responder aria-label="Responder" title="Responder">{ico("responder", 15)}</button>'
            + (f'<button type="button" data-encaminhar aria-label="Encaminhar" title="Encaminhar">{ico("encaminhar", 15)}</button>' if encaminhavel else "")
            + (f'<button type="button" data-editar aria-label="Editar" title="Editar (até 15 min depois de enviar)">{ico("lapis", 15)}</button>' if editavel else "")
            + (f'<button type="button" data-apagar aria-label="Apagar para todos" title="Apagar para todos">{ico("lixeira", 15)}</button>' if minha else "")
            + '</span>')


def html_conteudo(m: dict, cont: dict, transcricao: dict | None = None) -> str:
    t = cont["tipo"]
    src = f'/conversas/midia?m={c.esc(m["id"])}'
    legenda = f'<div class="zap-txt">{com_links(cont["texto"])}</div>' if cont["texto"] else ""
    if t == "texto":
        return f'<div class="zap-txt">{com_links(cont["texto"])}</div>'
    if t in ("imagem", "figurinha"):
        cls = "zap-img fig" if t == "figurinha" else "zap-img"
        if t == "figurinha":
            return f'<img class="{cls}" loading="lazy" src="{src}" alt="Figurinha">'
        # pedido 56: baixar a foto direto (vai para Downloads), sem precisar abrir em tamanho grande
        return (f'<span class="zap-foto"><button type="button" class="zap-abrir" data-ampliar="{src}" aria-label="Abrir a foto em tamanho grande">'
                f'<img class="{cls}" loading="lazy" src="{src}" alt="Foto"></button>{_baixar(src, "Baixar a foto")}</span>{legenda}')
    if t == "produto":
        foto = (f'<button type="button" class="zap-abrir" data-ampliar="{src}" aria-label="Abrir a foto do produto em tamanho grande">'
                f'<img class="zap-img" loading="lazy" src="{src}" alt="Foto do produto"></button>') if cont.get("foto") else ""
        preco = f'<span class="zap-prod-preco">{c.esc(cont["preco"])}</span>' if cont.get("preco") else ""
        desc = (f'<details class="zap-prod-desc"><summary>Ver descrição</summary><div class="zap-txt">{com_links(cont["descricao"])}</div></details>'
                if cont.get("descricao") else "")
        return (f'<div class="zap-produto">{foto}<div class="zap-prod-info"><small>Produto do catálogo</small>'
                f'<b>{c.esc(cont["titulo"])}</b>{preco}</div>{desc}</div>')
    if t == "audio":
        return (f'<div class="zap-audio"><audio controls preload="none" src="{src}"></audio>'
                f'<span>{c.esc(cont["extra"])}</span>{_baixar(src, "Baixar o áudio")}</div>{html_transcricao(transcricao)}')
    if t == "video":
        # pedido 56: foto com movimento é um vídeo de poucos segundos; carrega o primeiro quadro para não ficar um
        # retângulo preto e diz o que é. Vídeo comum continua sem carregar nada até o clique.
        mov = bool(cont.get("movimento"))
        rotulo = f'Foto com movimento · {c.esc(cont["extra"])}' if mov else f'Vídeo de {c.esc(cont["extra"])}'
        # pedido 59: o vídeo nasce no formato em que foi gravado (em pé = estreito, como no celular), com a capa que
        # já está no banco. O botão do meio toca e pausa ali mesmo; o do canto abre grande.
        larg, alt = cont.get("larg") or 0, cont.get("alt") or 0
        forma = f' style="aspect-ratio: {int(larg)} / {int(alt)}"' if larg and alt else ""
        vert = " vert" if larg and alt and alt > larg else ""
        return (f'<span class="zap-vid{vert}"{forma}><video class="zap-video" preload="{"metadata" if mov else "none"}" playsinline '
                f'poster="{src}&amp;capa=1" src="{src}{"#t=0.1" if mov else ""}"></video>'
                f'<button type="button" class="zap-vid-tocar" data-tocar aria-label="Reproduzir ou pausar o vídeo">'
                f'<i class="ic-play">{ico("play", 22)}</i><i class="ic-pausa">{ico("pausa", 22)}</i></button>'
                f'<button type="button" class="zap-vid-expandir" data-expandir aria-label="Abrir o vídeo em tamanho grande" '
                f'title="Abrir grande">{ico("expandir", 15)}</button></span>'
                f'<div class="zap-anexo">{ico("video")}<span>{rotulo}</span>{_baixar(src, "Baixar o vídeo")}</div>{legenda}')
    if t == "documento":
        nome = cont["extra"] or "Documento"
        return (f'<div class="zap-anexo">{ico("doc")}<span>{c.esc(nome)}</span>'
                f'{_baixar(src + "&amp;n=" + c.esc(_q(nome)), "Baixar o arquivo")}</div>{legenda}')
    if t == "local":
        rotulo = "Localização ao vivo" if cont.get("vivo") else "Localização"
        detalhe = f'<small>{c.esc(cont["texto"])}</small>' if cont["texto"] else ""
        mapa = (f'<a class="zap-mapa" href="https://www.google.com/maps?q={c.esc(cont["extra"])}" target="_blank" '
                f'rel="noopener noreferrer">Abrir no mapa</a>') if cont["extra"] else ""
        return f'<div class="zap-anexo">{ico("pin")}<span>{rotulo}{detalhe}</span>{mapa}</div>'
    if t == "contato":
        return "".join(f'<div class="zap-anexo">{ico("pessoa")}<span>{c.esc(x["nome"])}'
                       f'<small>{c.esc(" · ".join(x["numeros"]) or "sem número")}</small></span></div>' for x in cont.get("contatos") or [])
    if t == "enquete":
        opcoes = "".join(f"<li>{c.esc(o)}</li>" for o in cont.get("opcoes") or [])
        return (f'<div class="zap-anexo">{ico("enquete")}<span>Enquete<small>os votos aparecem só no celular</small></span></div>'
                f'<div class="zap-txt">{c.esc(cont["texto"])}</div><ul class="zap-enquete zap-txt">{opcoes}</ul>')
    # pedido 57: o que o Greg não sabe mostrar ganha um aviso só, sem o nome técnico do tipo
    return (f'<div class="zap-anexo zap-nao-abre">{ico("cadeado")}<span>Esta mensagem só abre no celular'
            '<small>O Greg ainda não mostra este tipo de conteúdo</small></span></div>')


def html_mensagens(msgs: list[dict], jid: str, apelidos: dict, classes: dict, transcricoes: dict | None = None,
                   reacoes: dict | None = None, acoes: bool = False) -> str:
    """`reacoes`: {key.id: [...]} de wa.fio. `acoes`: a conversa aceita envio, então cada bolha ganha Reagir e
    Responder (o servidor confere a trava em cada um)."""
    transcricoes = transcricoes or {}
    reacoes = reacoes or {}
    por_chave = {m["key"].get("id"): m for m in msgs if m["key"].get("id")}
    agora = time.time()
    partes = []
    ultimo_dia = ultimo_quem = None
    grupo = wa.tipo_de(jid) == "grupo"
    for m in msgs:
        d = dia(m["ts"])
        if d != ultimo_dia:
            partes.append(f'<div class="zap-dia">{c.esc(d)}</div>')
            ultimo_dia, ultimo_quem = d, None
        mim = wa._de_mim(m["key"])
        quem_jid = "eu" if mim else wa.remetente(m["key"], jid)
        cont_ = wa.conteudo(m["message"], m["tipo_msg"])
        seguida = quem_jid == ultimo_quem
        ultimo_quem = quem_jid
        cab = ""
        if grupo and not mim and not seguida:
            nome = wa.nome_do_remetente(quem_jid, m["push"], apelidos)
            cab = f'<span class="zap-quem">{c.esc(nome)}</span>'   # cinza: só o acento é colorido (Papel)
        editada = '<span class="zap-editada">editada</span>' if m["status"] == "EDITED" else ""
        meta = f'<span class="zap-meta">{editada}{_hhmm(m["ts"])}{_ticks(m["status"], m["ts"]) if mim else ""}</span>'
        cls = "zap-msg " + ("saida" if mim else "entrada") + (" cont" if seguida else "")
        dados = botoes = ""
        if m.get("apagada"):
            # D-63: o remetente apagou. Como no WhatsApp, fica só o aviso: sem texto, mídia, citação, reação nem ações.
            partes.append(f'<div class="{cls}" id="m-{c.esc(m["id"])}">{cab}<div class="zap-apagada">{wa.APAGADA}</div>'
                          f'<span class="zap-meta">{_hhmm(m["ts"])}</span></div>')
            continue
        if acoes:
            editavel = mim and m["tipo_msg"] in _MSG_TEXTO and 0 <= agora - m["ts"] <= JANELA_EDICAO
            botoes = _acoes(editavel, cont_["tipo"] in _ENCAMINHAVEL, mim)
            autor = "Você" if mim else wa.nome_do_remetente(quem_jid, m["push"], apelidos)
            dados = f' data-id="{c.esc(m["id"])}" data-quem="{c.esc(autor)}" data-previa="{c.esc(wa.previa(cont_))}"'
        selo = f'<span class="zap-encaminhada">{ico("encaminhar", 12)}Encaminhada</span>' if m.get("encaminhada") else ""
        partes.append(f'<div class="{cls}" id="m-{c.esc(m["id"])}"{dados}>{cab}{selo}{html_citada(m, por_chave, jid, apelidos)}'
                      f'{html_conteudo(m, cont_, transcricoes.get(m["id"]))}{meta}'
                      f'{html_reacoes(reacoes.get(m["key"].get("id")) or [], apelidos, acoes)}{botoes}</div>')
        cl = None if m.get("apagada") else classes.get(m["id"])      # o resumo da IA também é conteúdo (D-63)
        if cl and cl.get("classe") not in (None, "", "sem_acao"):
            dec = {"confirmado": ", confirmada", "recusado": ", recusada"}.get(cl.get("decisao") or "", "")
            href = f'/demandas?conversa={c.esc(_q(jid))}&amp;item={c.esc(cl.get("id", ""))}'
            base_cls = "lembrar" if cl["classe"] == "lembrando" else cl["classe"]
            partes.append(f'<div class="zap-ia{" saida" if mim else ""}"><a class="classe {c.esc(base_cls)}" href="{href}">{c.rotulo_classe(cl["classe"])}</a>'
                          f'<span class="zap-ia-txt">{c.esc(cl.get("resumo") or "")}{dec}</span></div>')   # o modo de gravação borra só o texto
    if not partes:
        partes.append('<div class="zap-vazio-fio">Sem mensagens desde o pareamento nesta conversa.</div>')
    return "".join(partes)


def _vale() -> str:
    """Por quanto tempo a trava abre, como a pessoa escolheu em Ajustes (pedido 70): '8 horas', '10 minutos'."""
    return pf.ENVIO_MIN.get(pf.ler()["envio_min"], pf.ENVIO_MIN[pf.PADRAO["envio_min"]])


def _travado() -> str:
    return f"Envio travado. Libere para escrever, vale {_vale()}."


_DIAS_SEM = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
_ROTULO_AG = {"foto": "Foto", "video": "Vídeo", "arquivo": "Arquivo"}


def quando_agendado(ts: int, agora: datetime | None = None) -> str:
    """'hoje 18:00', 'amanhã 09:00' ou 'seg 17/11 09:00' (hora do Mac)."""
    d, hoje = datetime.fromtimestamp(ts), (agora or datetime.now()).date()
    dias = (d.date() - hoje).days
    if dias == 0:
        return f"hoje {d:%H:%M}"
    if dias == 1:
        return f"amanhã {d:%H:%M}"
    return f"{_DIAS_SEM[d.weekday()]} {d:%d/%m %H:%M}"


def oque_agendado(it: dict) -> str:
    """O que vai sair, em uma linha: a prévia do texto, ou 'Foto: nome' com a legenda."""
    if it["tipo"] == "texto":
        return it.get("previa", "")
    base = f'{_ROTULO_AG.get(it["tipo"], "Arquivo")}: {it.get("nome", "")}'
    return f'{base} · {it["previa"]}' if it.get("previa") else base


def html_agendados(ctx, jid: str) -> str:
    """Faixa em cima do campo: o que está agendado para esta conversa, com Cancelar; e o que não saiu nas últimas 24 h."""
    try:
        itens = agendados.da(ctx).listar(para=jid)
    except (VigiaErro, OSError, ValueError) as e:
        log.warning("agendados indisponíveis: %s", causa(e))
        return '<div class="zap-agendado"><span class="zap-ag-txt">Não consegui ler os envios agendados.</span></div>'
    agora, partes = time.time(), []
    for it in itens:
        if it["estado"] == "agendado":
            partes.append(f'<div class="zap-agendado">{ico("relogio", 14)}<b>{c.esc(quando_agendado(it["quando"]))}</b>'
                          f'<span class="zap-ag-txt">{c.esc(oque_agendado(it))}</span>'
                          f'<button type="button" class="btn" data-ag-cancelar="{it["id"]}">Cancelar</button></div>')
        elif it["estado"] in ("vencido", "falhou", "incerto") and agora - it["resolvido_em"] < 86400:
            partes.append(f'<div class="zap-agendado">{ico("relogio", 14)}<b>{c.esc("Não saiu" if it["estado"] != "incerto" else "Pode ter saído")}</b>'
                          f'<span class="zap-ag-txt">{c.esc("agendado para " + quando_agendado(it["quando"]) + ": " + it["motivo"])}</span></div>')
    return "".join(partes)


def html_compor(ctx, jid: str, nome: str) -> str:
    if wa.tipo_de(jid) == "lid":
        return '<div class="zap-rodape-envio nota">Sem número visível nesta conversa: responda pelo celular.</div>'
    if not getattr(getattr(ctx, "tokens", None), "envio", ""):
        return '<div class="zap-rodape-envio nota">Envio desligado: o token de envio não está configurado (~/.vigia-envio.env).</div>'
    liberado = servicos(ctx).liberacao.liberada()
    return (f'<form class="zap-rodape-envio" id="zap-compor" data-para="{c.esc(jid)}" data-nome="{c.esc(nome)}" data-vale="{c.esc(_vale())}">'
            f'<div class="zap-agendados" id="zap-agendados" aria-label="Envios agendados para esta conversa">{html_agendados(ctx, jid)}</div>'
            f'<div class="zap-aviso" id="zap-aviso" role="status" hidden></div>'
            f'<div class="zap-citando" id="zap-citando" hidden><span class="zap-citando-txt"><b id="zap-citando-quem"></b><span id="zap-citando-previa"></span></span>'
            f'<button type="button" class="btn-icone" id="zap-citando-tirar" aria-label="Não responder a essa mensagem" title="Não responder a essa mensagem">{ico("x", 14)}</button></div>'
            f'<div class="zap-pendente" id="zap-pendente" hidden><div class="zap-pend-previa" id="zap-pend-previa"></div>'
            f'<div class="zap-pend-barra"><span class="zap-pendente-nome" id="zap-pendente-nome"></span>'
            f'<button type="button" class="btn-icone" id="zap-pendente-tirar" aria-label="Tirar este anexo" title="Tirar este anexo">{ico("x", 14)}</button></div>'
            f'<div class="zap-pend-minis" id="zap-pend-minis" aria-label="Anexos escolhidos" hidden></div></div>'
            f'<div class="zap-gravando" id="zap-gravando" hidden><span class="zap-ponto" aria-hidden="true"></span>'
            f'<span id="zap-gravando-tempo" role="timer">0:00</span><span class="zap-gravando-dica">Gravando. Enviar manda como áudio de voz.</span>'
            f'<button type="button" class="btn" id="zap-gravando-cancelar">Cancelar</button></div>'
            f'<input type="file" id="zap-arquivo" hidden>'
            f'<div class="zap-caixa">'
            f'<button type="button" class="btn zap-extra" id="zap-anexar" aria-label="Anexar foto, vídeo ou arquivo" title="Anexar foto, vídeo ou arquivo"{"" if liberado else " hidden"}>{ico("clipe", 16)}</button>'
            f'<textarea id="zap-texto" class="campo" rows="1" maxlength="{wa.LIMITE_TEXTO}" aria-label="Mensagem" '
            f'placeholder="{"Escreva para " + c.esc(nome) if liberado else _travado()}"{"" if liberado else " disabled"}></textarea>'
            f'<button type="button" class="btn primario" id="zap-liberar"{" hidden" if liberado else ""}>{c.icone("touchid")}Liberar com Touch ID</button>'
            f'<button type="button" class="btn zap-extra" id="zap-gravar" aria-label="Gravar áudio" title="Gravar áudio"{"" if liberado else " hidden"}>{ico("mic", 16)}</button>'
            f'<button type="button" class="btn zap-extra" id="zap-agendar" aria-label="Agendar o envio" title="Agendar o envio" aria-haspopup="dialog"{"" if liberado else " hidden"}>{ico("relogio", 16)}</button>'
            f'<button type="submit" class="btn primario zap-enviar" aria-label="Enviar" title="Enviar (Enter)"{"" if liberado else " hidden"}>{c.icone("enviar")}</button>'
            f'<div class="zap-agenda-pop" id="zap-agenda-pop" role="dialog" aria-label="Agendar o envio" hidden>'
            f'<span class="zap-ag-tit">Enviar depois</span><div class="zap-ag-atalhos" id="zap-ag-atalhos"></div>'
            f'<input type="datetime-local" class="campo" id="zap-ag-quando" aria-label="Dia e hora do envio">'
            f'<button type="button" class="btn primario" id="zap-ag-ok">Agendar</button>'
            f'<p class="zap-ag-nota">Sai sozinho na hora, com o Mac ligado e o Greg aberto. Se atrasar mais de 1 hora, não sai e o Greg avisa aqui.</p></div>'
            f'</div></form>')


def _toolbar_fio(jid: str, nome: str, foto: bool, sub: str, extra: str = "", perfil: bool = False) -> str:
    miolo = (f'{avatar(jid, nome, foto, "p")}'
             f'<div class="zap-tg"><div class="nome">{c.esc(nome)}</div><div class="zap-sub">{c.esc(sub)}</div></div>')
    if perfil:      # pedido 57: tocar no nome ou na foto abre as informações da conversa
        miolo = (f'<button type="button" class="zap-perfil-abrir" data-perfil-abrir aria-expanded="false" aria-controls="zap-perfil" '
                 f'title="Ver informações">{miolo}</button>')
    return f'<header class="toolbar zap-toolbar">{miolo}{extra}</header>'


_DIAS = {"sun": "Domingo", "mon": "Segunda", "tue": "Terça", "wed": "Quarta", "thu": "Quinta", "fri": "Sexta", "sat": "Sábado"}


def _hora_comercial(v: str) -> str:
    """A Evolution manda minutos desde a meia-noite ("540") ou já "09:00"."""
    if v.isdigit() and int(v) < 1440:
        return f"{int(v) // 60:02d}:{int(v) % 60:02d}"
    return v if re.fullmatch(r"\d{1,2}:\d{2}", v) else ""


def _horario(h: dict) -> str:
    if h.get("modo") == "open_24h":
        return "24 horas"
    if h.get("modo") == "appointment_only":
        return "Só com hora marcada"
    abre, fecha = _hora_comercial(h.get("abre", "")), _hora_comercial(h.get("fecha", ""))
    return f"{abre} às {fecha}" if abre and fecha else ""


def _data_curta(v) -> str:
    try:
        d = datetime.fromtimestamp(int(v)) if isinstance(v, (int, float)) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    return d.strftime("%d/%m/%Y") if d.year > 2009 else ""


def _plural(n: int, um: str, varios: str) -> str:
    return f"{n} {um if n == 1 else varios}"


_ESTADO_PERFIL = {"teto": "Muitas consultas ao WhatsApp nesta hora. O Greg volta a perguntar daqui a pouco.",
                  "indisponivel": "O WhatsApp não respondeu agora. Abaixo, só o que o Greg já tinha."}


def html_perfil(jid: str, nome: str, foto: bool, apelido: str, perfil: dict, estado: str, midias: dict | None,
                nomes: dict | None = None, apelidos: dict | None = None) -> str:
    """O miolo do painel de informações (pedido 57). Tudo que vem do WhatsApp passa por c.esc."""
    tipo, nomes, apelidos = wa.tipo_de(jid), nomes or {}, apelidos or {}
    rosto = (f'<button type="button" class="zap-perfil-foto" data-ampliar="/conversas/avatar?j={c.esc(_q(jid))}" '
             f'aria-label="Abrir a foto em tamanho grande"><img src="/conversas/avatar?j={c.esc(_q(jid))}" alt=""></button>'
             if foto else f'<span class="zap-perfil-foto sem" aria-hidden="true">{c.esc(wa.iniciais(nome))}</span>')
    linhas = []
    if tipo == "grupo":
        n = perfil.get("membros") or 0
        linhas.append("Comunidade" if perfil.get("comunidade") else "Grupo")
        if n:
            linhas[-1] += f" · {_plural(n, 'participante', 'participantes')}"
    elif tipo == "lid":
        linhas.append("Número escondido pelo WhatsApp")
    else:
        linhas.append(wa.numero_formatado(jid))
        if perfil.get("nome") and perfil["nome"] != nome:
            linhas.append(f'No WhatsApp: ~{perfil["nome"]}')
    cab = (f'<div class="zap-perfil-cab">{rosto}<h3>{c.esc(nome)}</h3>'
           + "".join(f'<p>{c.esc(x)}</p>' for x in linhas if x)
           + ('<span class="zap-perfil-selo">Conta comercial</span>' if perfil.get("comercial") else "") + '</div>')
    blocos = []
    if estado in _ESTADO_PERFIL:
        blocos.append(f'<p class="zap-perfil-aviso">{_ESTADO_PERFIL[estado]}</p>')
    if tipo == "lid":
        blocos.append('<p class="zap-perfil-aviso">O WhatsApp esconde o número desta pessoa, então não há perfil para consultar.</p>')

    def bloco(titulo: str, miolo: str) -> None:
        blocos.append(f'<section class="zap-perfil-bloco"><h4>{c.esc(titulo)}</h4>{miolo}</section>')

    if perfil.get("recado"):
        desde = _data_curta(perfil.get("recado_em"))
        bloco("Recado", f'<div class="zap-txt">{c.esc(perfil["recado"])}</div>' + (f'<small>desde {desde}</small>' if desde else ""))
    if perfil.get("categoria"):
        bloco("Categoria", f'<div class="zap-txt">{c.esc(perfil["categoria"])}</div>')
    if perfil.get("descricao"):
        bloco("Descrição", f'<div class="zap-txt">{com_links(perfil["descricao"])}</div>')
    if perfil.get("sites"):
        bloco("Site", "".join(f'<div class="zap-txt">{com_links(x)}</div>' for x in perfil["sites"]))
    horas = [(_DIAS.get(str(h.get("dia", "")).lower()[:3], ""), _horario(h)) for h in perfil.get("horario") or []]
    horas = [(d, t) for d, t in horas if d and t]
    if horas:
        bloco("Horário de funcionamento",
              '<table class="zap-perfil-horas">' + "".join(f'<tr><th scope="row">{d}</th><td>{c.esc(t)}</td></tr>' for d, t in horas) + '</table>')
    if perfil.get("criado") and tipo == "grupo":
        criado = _data_curta(perfil["criado"])
        if criado:
            bloco("Criado em", f'<div class="zap-txt">{criado}</div>')
    if perfil.get("so_admin_fala"):
        blocos.append('<p class="zap-perfil-aviso">Só administradores enviam mensagens neste grupo.</p>')
    if midias is not None:
        partes = [x for x in (_plural(len(midias["fotos"]), "foto", "fotos") if midias["fotos"] else "",
                              _plural(midias["videos"], "vídeo", "vídeos") if midias["videos"] else "",
                              _plural(midias["documentos"], "documento", "documentos") if midias["documentos"] else "") if x]
        grade = "".join(f'<button type="button" class="zap-abrir" data-ampliar="/conversas/midia?m={c.esc(i)}" aria-label="Abrir a foto em tamanho grande">'
                        f'<img loading="lazy" src="/conversas/midia?m={c.esc(i)}" alt=""></button>' for i in midias["fotos"][:9])
        resumo = (" · ".join(partes) + (" ou mais" if midias.get("cheio") else "")) if partes else "Nenhuma mídia no período que o Greg guarda"
        bloco("Mídia desta conversa", f'<p class="zap-perfil-resumo">{resumo}</p>' + (f'<div class="zap-perfil-grade">{grade}</div>' if grade else ""))
    gente = perfil.get("participantes") or []
    if gente:
        ordem = sorted(gente, key=lambda x: not x.get("admin"))[:60]
        itens = "".join(
            f'<li><span>{c.esc(nomes.get(x["jid"]) or wa.nome_do_remetente(x["jid"], "", apelidos))}</span>'
            + ('<small>admin</small>' if x.get("admin") else "") + '</li>' for x in ordem)
        resto = len(gente) - len(ordem)
        bloco(f"Participantes ({len(gente)})", f'<ul class="zap-perfil-gente">{itens}</ul>'
              + (f'<small>e mais {resto}</small>' if resto > 0 else ""))
    if tipo != "grupo":
        bloco("Nome no Greg",
              f'<form method="post" action="/conversas/apelido" class="zap-perfil-nome"><input type="hidden" name="c" value="{c.esc(jid)}">'
              f'<input class="campo" name="nome" maxlength="80" value="{c.esc(apelido)}" placeholder="{c.esc(nome)}" aria-label="Nome no Greg">'
              f'<button type="submit" class="btn sm">Salvar</button></form>'
              f'<small>Só no Greg; a agenda do celular não muda.</small>')
    rodape = ""
    if perfil.get("em"):
        rodape = (f'<p class="zap-perfil-rodape">Consultado no WhatsApp em {datetime.fromtimestamp(float(perfil["em"])).strftime("%d/%m às %H:%M")}. '
                  f'O Greg pergunta de novo depois de 24 horas.</p>')
    return cab + "".join(blocos) + rodape


def perfil_get(req):
    """GET /conversas/perfil?c=<jid>: o miolo do painel de informações (pedido 57). Conversa bloqueada não é consultada
    nem mostrada (D-41)."""
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    ctx, jid = req.ctx, c.query(req, "c")
    psql, par = getattr(ctx, "psql", None), desde_leitura(ctx)
    if not wa.JID_OK.match(jid) or psql is None:
        return c.texto_simples("não encontrado", 404)
    try:
        if jid in wa.bloqueados(psql):
            return c.texto_simples("não encontrado", 404)
        salvos = wa.nomes_agenda(psql, [jid])
    except (VigiaErro, ValueError):
        return c.texto_simples("banco indisponível", 503)
    s = servicos(ctx)
    apelidos = ctx.banco.apelidos()
    perfil, estado = s.perfis.obter(jid)
    guardado = (s.grupos.nomes([]).get(jid) or {}).get("subject", "")
    nome = wa.nome_da_conversa(jid, apelido=apelidos.get(jid, ""), contato=salvos.get(jid, ""),
                               push=perfil.get("nome", ""), grupo=perfil.get("assunto") or guardado)
    midias = nomes = None
    try:
        if par is not None:
            midias = wa.midias(psql, jid, desde=par)
        gente = [x["jid"] for x in (perfil.get("participantes") or []) if not x["jid"].endswith("@lid")][:500]
        nomes = wa.nomes_agenda(psql, gente) if gente else {}
    except (VigiaErro, ValueError) as e:
        log.warning("mídias do perfil indisponíveis: %s", causa(e))
    foto = s.avatares.arquivo(jid) is not None
    corpo = html_perfil(jid, nome, foto, apelidos.get(jid, ""), perfil, estado, midias, nomes, apelidos)
    return Resposta(200, corpo.encode("utf-8"), "text/html; charset=utf-8", [("Cache-Control", "no-store")])


def _rodape_fio(base: str) -> str:
    return c.rodape(f'<span id="zap-envio-estado"></span><span>{c.esc(base)}</span>', "zap-status-fio")


LIMITES_FIO = (200, 400, 800, 2000)      # "Ver mensagens anteriores" sobe um degrau por clique (pedido 50, rodada 3)


def limite_fio(bruto) -> int:
    try:
        n = int(bruto)
    except (TypeError, ValueError):
        return LIMITES_FIO[0]
    return next((x for x in LIMITES_FIO if n <= x), LIMITES_FIO[-1])


def html_fio(ctx, jid: str, conversas: list[dict], bloqueados: set | None = None, limite: int = LIMITES_FIO[0]) -> str:
    """`bloqueados`: a lista que `carregar` já trouxe do banco; None = consultar aqui (cada psql é um subprocesso)."""
    par = desde_leitura(ctx)
    psql = getattr(ctx, "psql", None)
    info = next((x for x in conversas if x["jid"] == jid), None)
    apelidos = ctx.banco.apelidos()
    if psql is not None:
        # Antes de qualquer consulta: conversa bloqueada não pede nem o nome do grupo ao WhatsApp (só o cache local).
        try:
            if jid in (bloqueados if bloqueados is not None else wa.bloqueados(psql)):
                guardado = (servicos(ctx).grupos.nomes([]).get(jid) or {}).get("subject", "")
                return html_fio_bloqueado(jid, wa.nome_da_conversa(jid, apelido=apelidos.get(jid, ""), grupo=guardado))
        except (VigiaErro, ValueError) as e:
            log.warning("bloqueios indisponíveis: %s", causa(e))
            return c.painel(f'<div class="miolo">{c.erro_inline("O banco da Evolution não respondeu agora.", f"<a class={chr(34)}btn{chr(34)} href={chr(34)}/conversas?c={c.esc(_q(jid))}{chr(34)}>Tentar de novo</a>")}</div>',
                            toolbar_html=c.toolbar(), status_html=c.rodape(""), aria="Conversa aberta")
    if info is None:
        grupos = servicos(ctx).grupos.nomes([jid])
        nome = wa.nome_da_conversa(jid, apelido=apelidos.get(jid, ""), grupo=(grupos.get(jid) or {}).get("subject", ""))
        info = {"jid": jid, "tipo": wa.tipo_de(jid), "nome": nome, "foto": False, "membros": 0}
    msgs, reacoes = [], {}
    erro = ""
    if par is not None and psql is not None:
        try:
            msgs, reacoes = wa.fio(psql, jid, desde=par, limite=limite)   # reações na mesma consulta (pedido 50, rodada 2)
        except (VigiaErro, ValueError) as e:
            log.warning("mensagens indisponíveis: %s", causa(e))
            erro = f'<div class="zap-vazio-fio">{c.erro_inline("O banco da Evolution não respondeu agora.", f"<a class={chr(34)}btn{chr(34)} href={chr(34)}/conversas?c={c.esc(_q(jid))}{chr(34)}>Tentar de novo</a>")}</div>'
    classes = {r["msg_id"]: r for r in ctx.banco.classificacoes(conversa=jid, limite=500)}
    # D-47: transcrições dos áudios desta conversa (pelo id da mensagem; o ciclo grava, aqui só se lê).
    ler_transcricoes = getattr(ctx.banco, "transcricoes", None)
    ids_audio = [m["id"] for m in msgs if m.get("tipo_msg") in ("audioMessage", "pttMessage")]
    transcricoes = ler_transcricoes(ids_audio) if callable(ler_transcricoes) and ids_audio else {}
    pode_enviar = info["tipo"] != "lid" and bool(getattr(getattr(ctx, "tokens", None), "envio", ""))
    for m in msgs:
        if m.get("apagada"):
            _esquecer_midia(ctx, m["id"])
    anteriores = ""
    if len(msgs) >= limite:
        maior = next((x for x in LIMITES_FIO if x > limite), None)
        anteriores = (f'<button type="button" class="zap-anteriores" data-n="{maior}">Ver mensagens anteriores</button>' if maior else
                      f'<div class="zap-teto">O Greg mostra as {LIMITES_FIO[-1]} mensagens mais recentes de cada conversa.</div>')
    dialogo_enc = ""
    if pode_enviar:
        dialogo_enc = ('<dialog class="zap-enc" id="zap-enc" aria-labelledby="zap-enc-t"><h2 id="zap-enc-t">Encaminhar para</h2>'
                       '<p>Escolha a conversa. Chega como mensagem nova, sem o selo de encaminhada.</p>'
                       '<input class="campo" id="zap-enc-busca" type="search" placeholder="Buscar conversa" aria-label="Buscar conversa">'
                       '<div class="zap-enc-lista" id="zap-enc-lista"></div>'
                       '<div class="zap-enc-barra"><button type="button" class="btn" data-fechar>Cancelar</button>'
                       '<button type="button" class="btn primario" id="zap-enc-ok" disabled>Encaminhar</button></div></dialog>'
                       f'<template id="zap-tpl-relogio"><span title="saindo">{ico("relogio", 12)}</span></template>'
                       f'<script type="application/json" id="zap-emojis-todos">{json.dumps(EMOJIS_MAIS.split())}</script>')
    if info["tipo"] == "grupo":
        sub = f'Grupo com {info["membros"]} participantes' if info.get("membros") else "Grupo"
    elif info["tipo"] == "lid":
        sub = "Número escondido pelo WhatsApp"
    else:
        num = wa.numero_formatado(jid)
        sub = num if num != info["nome"] else "Sem nome salvo"
    # A assinatura também conta as transcrições: a que chega no próximo ciclo troca o fio sem recarregar a página.
    # Reação e edição também trocam o fio: entram como um resumo curto do estado delas.
    extra = hashlib.sha256(repr((sorted((a, sorted((r["quem"], r["emoji"]) for r in rs)) for a, rs in reacoes.items()),
                                 [(m["id"], m["ts"], m.get("apagada")) for m in msgs if m["status"] == "EDITED" or m.get("apagada")],
                                 [m["status"] for m in msgs if wa._de_mim(m["key"])])).encode()).hexdigest()[:10]
    assinatura = f'{len(msgs)}:{msgs[-1]["id"] if msgs else ""}:{msgs[-1]["status"] if msgs else ""}:t{len(transcricoes)}:{extra}'
    # D-48: os três botões do cabeçalho viram um menu ⋯ de três passos (itens · nome no Greg · confirmar "fora").
    nome_item = nome_passo = ""
    if info["tipo"] != "grupo":
        nome_item = '<button type="button" role="menuitem" data-passo-ir="nome"><span>Nome no Greg…</span></button>'
        nome_passo = (f'<div class="zap-passo zap-passo-nome" data-passo="nome" hidden>'
                      f'<form method="post" action="/conversas/apelido"><input type="hidden" name="c" value="{c.esc(jid)}">'
                      f'<input class="campo" name="nome" maxlength="80" value="{c.esc(apelidos.get(jid, ""))}" placeholder="{c.esc(info["nome"])}" aria-label="Nome no Greg">'
                      f'<button type="submit" class="btn sm">Salvar</button></form>'
                      f'<p class="meta-linha">Só no Greg; a agenda do celular não muda. Vazio volta ao nome do WhatsApp.</p></div>')
    demandas = c.item_menu("Ver demandas desta conversa", href=f"/demandas?conversa={_q(jid)}") if classes else ""
    menu_cab = _menu(
        f'<div class="zap-passo" data-passo="menu">{demandas}{nome_item}{"<hr>" if demandas or nome_item else ""}'
        f'<button type="button" role="menuitem" class="destrutivo" data-passo-ir="fora"><span>Deixar fora do Greg…</span></button></div>{nome_passo}'
        f'<div class="zap-passo zap-confirma" data-passo="fora" hidden><strong>Deixar “{c.esc(info["nome"])}” fora do Greg?</strong>'
        f'<p>{_TEXTO_FORA}</p>{_form_fora(jid, _CANCELAR)}</div>',
        "zap-menu-cab")
    return (f'<section class="painel detalhe zap-fio" aria-label="Conversa aberta">'
            f'{_toolbar_fio(jid, info["nome"], info["foto"], sub, menu_cab, perfil=True)}'
            f'<aside class="zap-perfil" id="zap-perfil" data-c="{c.esc(jid)}" aria-label="Informações da conversa" hidden>'
            f'<header class="toolbar"><strong>Informações</strong><button type="button" class="zap-perfil-fechar" data-perfil-fechar '
            f'aria-label="Fechar as informações">{ico("x", 16)}</button></header><div class="zap-perfil-corpo" id="zap-perfil-corpo"></div></aside>'
            f'<div class="zap-msgs" id="zap-msgs" data-assinatura="{c.esc(assinatura)}">{erro or (anteriores + html_mensagens(msgs, jid, apelidos, classes, transcricoes, reacoes, pode_enviar))}</div>{dialogo_enc}'
            f'<dialog class="zap-ampliada" id="zap-ampliada" aria-label="Foto em tamanho grande"><img alt="Foto em tamanho grande">'
            f'<div class="zap-ampliada-barra"><a class="btn" download>{ico("baixar", 15)}Baixar</a>'
            f'<button type="button" class="btn" data-fechar>Fechar</button></div></dialog>'
            f'<dialog class="zap-ampliada" id="zap-video-grande" aria-label="Vídeo em tamanho grande"><video controls playsinline></video>'
            f'<div class="zap-ampliada-barra"><a class="btn" download>{ico("baixar", 15)}Baixar</a>'
            f'<button type="button" class="btn" data-fechar>Fechar</button></div></dialog>'
            f'{html_compor(ctx, jid, info["nome"])}{_rodape_fio("Ler aqui não marca como lida no celular")}</section>')


def html_fio_bloqueado(jid: str, nome: str) -> str:
    corpo = (f'<div class="vazio"><b>Conversa bloqueada</b><span>O Greg não guarda o conteúdo, a IA não lê e ela não aparece na lista</span>'
             f'<form class="zap-vazio-acao" method="post" action="/bloqueios"><input type="hidden" name="acao" value="desbloquear">'
             f'<input type="hidden" name="c" value="{c.esc(jid)}"><input type="hidden" name="volta" value="/conversas">'
             f'<button type="submit" class="btn">Desbloquear</button></form></div>')
    return (f'<section class="painel detalhe zap-fio" aria-label="Conversa bloqueada">{_toolbar_fio(jid, nome, False, "Fora do Greg")}'
            f'<div class="corpo">{corpo}</div>{c.rodape("Desbloquear vale só para o que chegar depois; o que foi apagado não volta")}</section>')


def html_vazio() -> str:
    return c.painel(c.vazio("Nenhuma conversa aberta", "Escolha uma à esquerda ou comece uma nova"),
                    toolbar_html=c.toolbar(), status_html=c.rodape(""), aria="Conversa aberta")


def html_nova(ctx) -> str:
    if not getattr(getattr(ctx, "tokens", None), "envio", ""):
        aviso = c.erro_inline("Envio desligado: o token de envio não está configurado (~/.vigia-envio.env).")
        desab = " disabled"
    else:
        aviso, desab = "", ""
    form = (f'<form class="zap-nova" id="zap-nova">{aviso}<fieldset class="form-coluna"{desab}>'
            f'<div><label class="campo-rotulo" for="nova-num">Para</label><div class="linha-campos">'
            f'<input id="nova-num" class="campo" inputmode="tel" autocomplete="off" placeholder="92 99123 4567 ou +1 415 555 0100">'
            f'<button type="button" class="btn" id="nova-conferir">Conferir</button></div>'
            f'<div id="nova-res" class="zap-res meta-linha" role="status"></div></div>'
            f'<label class="zap-chave"><input type="checkbox" id="nova-salvar"> Salvar como contato no Greg</label>'
            f'<div id="nova-nome-l" hidden><label class="campo-rotulo" for="nova-nome">Nome</label><input id="nova-nome" class="campo" maxlength="80">'
            f'<p class="meta-linha">Fica só no Greg. A agenda do celular não muda.</p></div>'
            f'<div><label class="campo-rotulo" for="nova-texto">Mensagem</label><textarea id="nova-texto" class="campo" maxlength="{wa.LIMITE_TEXTO}"></textarea></div>'
            f'<div class="acoes zap-acoes"><div class="zap-aviso" id="zap-aviso" role="status" hidden></div>'
            f'<a class="btn" href="/conversas">Cancelar</a>'
            f'<button type="submit" class="btn primario" id="nova-enviar">{c.icone("enviar")}Enviar</button></div>'
            f'</fieldset></form>')
    toolbar = c.toolbar(c.botao_icone("voltar", "Voltar para as conversas", href="/conversas"), c.titulo("Nova mensagem"), c.espaco())
    return (f'<section class="painel detalhe zap-fio" aria-label="Nova mensagem">{toolbar}<div class="corpo">{c.miolo(form)}</div>'
            f'{_rodape_fio("Pelo seu número pessoal, para qualquer número com WhatsApp")}</section>')


_JS = """<script>
(function () {
  var aviso = document.getElementById('zap-aviso');
  function dizer(t, ruim) {
    if (!aviso) { return; }
    aviso.textContent = t || ''; aviso.hidden = !t; aviso.className = 'zap-aviso' + (ruim ? ' ruim' : '');
  }
  function pedirJson(url, opc) {
    opc = opc || {}; opc.credentials = 'same-origin';
    opc.headers = Object.assign({'Accept': 'application/json'}, opc.headers || {});
    return fetch(url, opc).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) { return {s: r.status, d: d}; });
    });
  }
  function postJson(url, corpo, cab) {
    return pedirJson(url, {method: 'POST', headers: Object.assign({'Content-Type': 'application/json'}, cab || {}),
                           body: JSON.stringify(corpo || {})});
  }
  // rodapé de envio: travado = campo desabilitado + "Liberar com Touch ID"; liberado = campo livre + Enviar
  var f = document.getElementById('zap-compor'), txt = document.getElementById('zap-texto');
  var btLiberar = document.getElementById('zap-liberar'), btEnviar = f ? f.querySelector('.zap-enviar') : null;
  var envioEstado = document.getElementById('zap-envio-estado');
  var btAnexar = document.getElementById('zap-anexar'), btGravar = document.getElementById('zap-gravar');
  var btAgendar = document.getElementById('zap-agendar'), popAg = document.getElementById('zap-agenda-pop');
  var podeGravar = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  if (btGravar && !podeGravar) { btGravar.hidden = true; }
  var liberado = !!(txt && !txt.disabled);
  function pintarTrava(ok, restante) {
    liberado = !!ok;
    if (txt) {
      txt.disabled = !ok;
      txt.placeholder = ok ? 'Escreva para ' + (f.getAttribute('data-nome') || '') : 'Envio travado. Libere para escrever, vale ' + (f.getAttribute('data-vale') || '8 horas') + '.';
    }
    if (btLiberar) { btLiberar.hidden = !!ok; }
    if (btEnviar) { btEnviar.hidden = !ok; }
    if (btAnexar) { btAnexar.hidden = !ok; }
    if (btGravar) { btGravar.hidden = !ok || !podeGravar; }
    if (btAgendar) { btAgendar.hidden = !ok; if (!ok && popAg) { popAg.hidden = true; } }
    if (envioEstado) {
      var mi = Math.max(1, Math.ceil((restante || 0) / 60)), ho = Math.floor(mi / 60);
      envioEstado.textContent = ok ? 'Envio liberado por ' + (ho ? ho + ' h' + (mi % 60 ? ' ' + (mi % 60) + ' min' : '') : mi + ' min') + '.' : '';
    }
  }
  function estado() {
    if (!envioEstado) { return; }
    pedirJson('/envio/estado').then(function (x) {
      if (x.s !== 200 || !x.d.configurado) { return; }
      pintarTrava(x.d.liberado, x.d.restante_s);
    }).catch(function () {});
  }
  // auditoria C2 (lote 5): "Envio não liberado" juntava causas com saídas diferentes; o app manda o motivo
  var MOTIVO_TRAVA = {
    cancelado: 'Envio não liberado: a confirmação do Touch ID foi cancelada.',
    sem_touch: 'Envio não liberado: este Mac não tem Touch ID nem senha para confirmar.',
    sem_token: 'Envio não liberado: o token de envio não está configurado neste Mac.',
    recusado: 'Envio não liberado: o painel recusou o token de envio.',
    rede: 'Envio não liberado: o painel não respondeu. Tente de novo.'
  };
  function liberar(depois) {
    var h = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.vigia;
    if (h) {
      window.vigiaEnvioLiberado = function (ok, motivo) {
        window.vigiaEnvioLiberado = null;
        if (ok) { dizer(''); estado(); depois(); } else { dizer(MOTIVO_TRAVA[motivo] || 'Envio não liberado.', true); }
      };
      dizer('Confirme com o Touch ID…');
      h.postMessage({acao: 'liberarEnvio'});
      return;
    }
    var tk = window.prompt('Token de envio (fica liberado por ' + ((f && f.getAttribute('data-vale')) || '8 horas') + '):');
    if (!tk) { dizer('Envio não liberado: nenhum token informado.', true); return; }
    postJson('/envio/liberar', {}, {'X-Vigia-Envio': tk}).then(function (x) {
      if (x.s === 200) { dizer(''); pintarTrava(true, x.d.restante_s); depois(); } else { dizer(MOTIVO_TRAVA.recusado, true); }
    }).catch(function () { dizer(MOTIVO_TRAVA.rede, true); });
  }
  var enviando = false, incerto = false, faixaInc = null;
  // Auditoria C1 (lote 3 do pedido 63): depois de um "pode ter saído" a tela segura os envios para ninguém mandar em
  // dobro. Antes segurava calada; agora uma faixa fixa diz por quê e o botão "Já conferi" libera de novo. Ela não é o
  // aviso de uma linha (que some quando a pessoa anexa ou digita): fica até a pessoa responder.
  function mostrarIncerto(texto, piscar) {
    if (!aviso || !aviso.parentNode) { dizer(texto || 'Confira no celular antes de continuar.', true); return; }
    if (!faixaInc) {
      faixaInc = document.createElement('div'); faixaInc.className = 'zap-incerto'; faixaInc.setAttribute('role', 'alert');
      var t = document.createElement('span'); t.className = 'zap-incerto-txt'; faixaInc.appendChild(t);
      var b = document.createElement('button'); b.type = 'button'; b.className = 'btn'; b.textContent = 'Já conferi';
      b.addEventListener('click', function () {
        incerto = false; faixaInc.hidden = true; dizer('');
        document.querySelectorAll('.zap-msg.otimista.incerta').forEach(function (x) { x.remove(); });   // pedido 84
        if (txt) { txt.focus(); } bombear();
      });
      faixaInc.appendChild(b);
      var onde = aviso.parentNode.classList.contains('zap-acoes') ? aviso.parentNode : aviso;   // Nova mensagem: acima da linha de botões
      onde.parentNode.insertBefore(faixaInc, onde);
    }
    if (texto) { faixaInc.firstChild.textContent = texto + ' Os envios desta tela ficam parados até você conferir.'; }
    faixaInc.hidden = false;
    if (piscar) { faixaInc.classList.remove('pisca'); void faixaInc.offsetWidth; faixaInc.classList.add('pisca'); }
  }
  function marcarIncerto(texto) { incerto = true; dizer(''); mostrarIncerto(texto, false); }
  function barradoPorIncerto() { if (!incerto) { return false; } mostrarIncerto('', true); return true; }
  function enviar(corpo, ok, rota) {
    if (barradoPorIncerto() || enviando) { return; }
    enviando = true; dizer('Enviando…');
    postJson(rota || '/conversas/enviar', corpo).then(function (x) {
      enviando = false;
      if (x.s === 200) { dizer(''); ok(x.d); }
      else if (x.s === 423) { dizer(''); pintarTrava(false, 0); liberar(function () { enviar(corpo, ok, rota); }); }
      else if (x.s === 202) { marcarIncerto((x.d && x.d.mensagem) || 'Pode ter saído. Confira no celular antes de repetir.'); }
      else { dizer((x.d && x.d.mensagem) || ('Falhou (' + x.s + ').'), true); }
    }).catch(function () { enviando = false; dizer('Falha de rede local. Confira no celular antes de repetir.', true); });
  }
  // ---- pedido 52: a mensagem aparece na hora em que ele manda (como no WhatsApp) e o envio corre atrás. A bolha
  // provisória não tem id; some quando a de verdade chega na leitura seguinte (trocarFio). Texto vai em fila: dá para
  // mandar várias seguidas sem esperar a anterior. Falhou = as bolhas saem e o texto volta para o campo.
  function bolhaNaHora(rotulo, foto, legenda) {
    var m = document.getElementById('zap-msgs');
    if (!m) { return null; }
    var ult = m.lastElementChild, b = document.createElement('div'), d = new Date();
    b.className = 'zap-msg saida otimista' + (ult && ult.classList.contains('saida') ? ' cont' : '');
    var tx = document.createElement('div'); tx.className = 'zap-txt'; tx.textContent = rotulo;
    var me = document.createElement('span'); me.className = 'zap-meta';
    me.textContent = ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
    var rel = document.getElementById('zap-tpl-relogio');
    if (rel && rel.content) { me.appendChild(rel.content.cloneNode(true)); }
    b.setAttribute('data-em', String(Date.now())); b.appendChild(tx); b.appendChild(me); chegou(b); m.appendChild(b); m.scrollTop = m.scrollHeight;
    // pedido 64: a foto aparece na bolha provisória (miniatura feita na página, em data:, sem sair do Mac)
    if (foto && window.createImageBitmap) {
      window.createImageBitmap(foto).then(function (bm) {
        var e = Math.min(1, 280 / Math.max(bm.width, bm.height)), c = document.createElement('canvas');
        c.width = Math.max(1, Math.round(bm.width * e)); c.height = Math.max(1, Math.round(bm.height * e));
        c.getContext('2d').drawImage(bm, 0, 0, c.width, c.height);
        var im = document.createElement('img'); im.className = 'zap-previa-foto'; im.alt = 'Foto'; im.src = c.toDataURL('image/jpeg', .72);
        if (!b.parentNode) { return; }
        b.insertBefore(im, tx); if (legenda) { tx.textContent = legenda; } else { tx.remove(); }
        m.scrollTop = m.scrollHeight;
      }).catch(function () {});
    }
    return b;
  }
  // ---- pedido 84 (D-87): envio otimista em todos os caminhos. A bolha aparece com o relógio, a linha da conversa sobe
  // para o topo na hora, e só a confirmação do servidor tira o relógio (o tique vem da leitura seguinte). Falha certa
  // (o servidor disse que não saiu): a bolha fica "Não enviada" com Tentar de novo e Descartar. Travado: idem, e a
  // liberação abre. Pode ter saído (202 ou rede): a bolha diz isso, sem repetir, e a faixa segura os envios.
  var MSG_INCERTO = 'Pode ter saído. Confira no celular antes de repetir.';
  function seloRelogio() { var r = document.getElementById('zap-tpl-relogio'); return r && r.content ? r.content.cloneNode(true) : document.createTextNode(''); }
  function voltarAEnviar(b) {
    if (!b) { return; }
    b.classList.remove('falhou', 'travado', 'incerta');
    var p = b.querySelector('.zap-falha'); if (p) { p.remove(); }
    var me = b.querySelector('.zap-meta'); if (me && !me.querySelector('[title="saindo"]')) { me.appendChild(seloRelogio()); }
  }
  function estadoBolha(b, tipo, texto, tentar) {
    if (!b) { return; }
    voltarAEnviar(b); b.classList.add(tipo);
    var rel = b.querySelector('.zap-meta [title="saindo"]'); if (rel) { rel.remove(); }
    var p = document.createElement('div'), s = document.createElement('span');
    p.className = 'zap-falha'; p.setAttribute('role', 'status'); s.textContent = texto; p.appendChild(s);
    function botao(rotulo, fazer) {
      var bt = document.createElement('button'); bt.type = 'button'; bt.textContent = rotulo;
      bt.addEventListener('click', fazer); p.appendChild(bt);
    }
    if (tipo !== 'incerta') {
      if (tentar) { botao('Tentar de novo', function () { if (barradoPorIncerto()) { return; } tentar(); }); }
      botao('Descartar', function () { b.remove(); });
    }
    b.appendChild(p);
  }
  function subirNaLista(jid, previa) {
    var lt = document.getElementById('zap-itens');
    if (!lt || !jid || document.querySelector('.zap-lista.selecionando')) { return; }
    var a = null;
    lt.querySelectorAll(':scope > .zap-item').forEach(function (x) { if (x.getAttribute('data-jid') === jid) { a = x; } });
    if (!a) { return; }
    var pv = a.querySelector('.zap-previa'), tm = a.querySelector('time'), d = new Date();
    if (pv) { pv.textContent = 'Você: ' + previa; pv.insertBefore(seloRelogio(), pv.firstChild); }
    if (tm) { tm.textContent = ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2); }
    a.classList.add('enviando');
    var prim = lt.querySelector(':scope > .zap-item');
    if (prim !== a) { lt.insertBefore(a, prim); }
  }
  // pedido 64: texto e anexo saem pela MESMA fila, na ordem em que ele mandou. O campo e a faixa do anexo esvaziam na
  // hora do envio (antes ficavam cheios até o servidor responder). Falhou = o texto e o primeiro anexo voltam.
  var filaTxt = [], mandando = false, repender = null, temPendente = null, soltarPendentes = null;
  function devolverFila() {
    var textos = [], perdidos = 0;
    filaTxt.forEach(function (i) {
      if (i.b) { i.b.remove(); }
      if (!i.arq) { textos.push(i.corpo.texto); return; }
      if (i.legenda) { textos.push(i.legenda); }
      if (!(repender && repender(i.arq))) { perdidos++; }       // pedido 71: todos os anexos voltam para a faixa
    });
    filaTxt = [];
    if (txt && textos.length) { txt.value = textos.concat(txt.value ? [txt.value] : []).join('\\n'); txt.dispatchEvent(new Event('input')); }
    return perdidos ? ' ' + perdidos + (perdidos > 1 ? ' anexos não voltaram' : ' anexo não voltou') + ' para o campo; anexe de novo.' : '';
  }
  function bombear() {
    if (mandando || incerto || !filaTxt.length) { return; }
    var it = filaTxt[0]; mandando = true;
    (it.arq ? pedirJson('/conversas/enviar-midia?' + it.q, {method: 'POST', headers: {'Content-Type': 'application/octet-stream'}, body: it.arq})
            : postJson('/conversas/enviar', it.corpo)).then(function (x) {
      mandando = false;
      if (x.s === 200) { filaTxt.shift(); if (it.b) { it.b.classList.add('saiu'); } dizer(''); atualizar(true); bombear(); }
      else if (x.s === 423) {
        var travadas = pararFila('travado', 'Envio travado. Libere o envio para mandar.');
        pintarTrava(false, 0);
        liberar(function () { travadas.forEach(function (i) { voltarAEnviar(i.b); filaTxt.push(i); }); bombear(); });
      }
      else if (x.s === 202) {
        filaTxt.shift(); var m2 = (x.d && x.d.mensagem) || MSG_INCERTO; estadoBolha(it.b, 'incerta', m2);
        pararFila('falhou', 'Não enviada: a anterior pode ter saído. Confira no celular.'); marcarIncerto(m2);
      }
      else { pararFila('falhou', 'Não enviada. ' + ((x.d && x.d.mensagem) || ('Falhou (' + x.s + ').'))); }
    }).catch(function () {
      mandando = false; filaTxt.shift();
      var m4 = 'O painel não respondeu a tempo e a mensagem pode ter saído. Confira no celular antes de repetir.';
      estadoBolha(it.b, 'incerta', m4); pararFila('falhou', 'Não enviada: a anterior pode ter saído. Confira no celular.'); marcarIncerto(m4);
    });
  }
  // pedido 84: a fila para na falha (a ordem das mensagens não se inverte). Cada bolha que ficou diz o que houve e
  // tem Tentar de novo; o texto não volta ao campo. Item sem bolha (tela sem fio) volta ao campo, como antes.
  function pararFila(tipo, texto) {
    var itens = filaTxt.filter(function (i) { return i.b; }), sem = filaTxt.filter(function (i) { return !i.b; });
    filaTxt = sem;
    var extra = sem.length ? devolverFila() : '';
    itens.forEach(function (i) { estadoBolha(i.b, tipo, texto, function () { voltarAEnviar(i.b); filaTxt.push(i); bombear(); }); });
    dizer(extra ? extra.trim() : '', !!extra);
    atualizar(true, true, true);   // a linha que subiu com o relógio volta a mostrar o que de fato está na conversa
    return itens;
  }
  function enviarTexto(corpo) {
    if (barradoPorIncerto()) { return false; }
    filaTxt.push({corpo: corpo, b: bolhaNaHora(corpo.texto)}); subirNaLista(f.dataset.para, corpo.texto); bombear();
    return true;
  }
  // pedido 50: o arquivo vai cru no corpo; destino, tipo, nome e legenda na query (o servidor não loga query).
  var LIMITE_MIDIA = 16 * 1024 * 1024;
  // pedido 64: foto, vídeo e arquivo entram na fila do texto; devolve false se não entrou (o campo fica como está)
  function enviarAnexo(arq, tipo, legenda) {
    if (barradoPorIncerto()) { return false; }
    if (!arq || !arq.size) { dizer('Arquivo vazio.', true); return false; }
    if (arq.size > LIMITE_MIDIA) { dizer('Arquivo acima de 16 MB. O Greg não envia arquivos maiores.', true); return false; }
    var q = 'para=' + encodeURIComponent(f.dataset.para) + '&tipo=' + tipo + '&nome=' + encodeURIComponent(arq.name || '') +
            '&legenda=' + encodeURIComponent(legenda || '') + (citando ? '&citar=' + encodeURIComponent(citando) : '');
    var rot = ({foto: 'Foto', video: 'Vídeo'}[tipo] || (arq.name || 'Arquivo')) + (legenda ? '\\n' + legenda : '');
    filaTxt.push({arq: arq, legenda: legenda || '', q: q, b: bolhaNaHora(rot, tipo === 'foto' ? arq : null, legenda || '')});
    subirNaLista(f.dataset.para, rot); bombear();
    return true;
  }
  // pedido 84: o áudio chega aqui com a bolha já na tela (posta quando a gravação parou); `prov` é ela
  function enviarMidia(arq, tipo, legenda, ok, prov) {
    function deNovo() { voltarAEnviar(prov); enviarMidia(arq, tipo, legenda, ok, prov); }
    if (barradoPorIncerto()) { if (prov) { estadoBolha(prov, 'falhou', 'Não enviada: confira o envio anterior no celular.', deNovo); } return; }
    if (enviando) { if (prov) { estadoBolha(prov, 'falhou', 'Não enviada: outro envio estava em andamento.', deNovo); } return; }
    if (!arq || !arq.size) { if (prov) { estadoBolha(prov, 'falhou', 'Arquivo vazio. Nada foi enviado.'); } else { dizer('Arquivo vazio.', true); } return; }
    if (arq.size > LIMITE_MIDIA) { var gr = 'Arquivo acima de 16 MB. O Greg não envia arquivos maiores.'; if (prov) { estadoBolha(prov, 'falhou', gr); } else { dizer(gr, true); } return; }
    var q = 'para=' + encodeURIComponent(f.dataset.para) + '&tipo=' + tipo + '&nome=' + encodeURIComponent(arq.name || '') +
            '&legenda=' + encodeURIComponent(legenda || '') + (citando ? '&citar=' + encodeURIComponent(citando) : '');
    enviando = true; dizer('');
    var rot = tipo === 'voz' ? 'Áudio de voz' : ({foto: 'Foto', video: 'Vídeo'}[tipo] || (arq.name || 'Arquivo'));
    if (!prov) { prov = bolhaNaHora(rot + (legenda ? '\\n' + legenda : '')); subirNaLista(f.dataset.para, rot); }
    pedirJson('/conversas/enviar-midia?' + q, {method: 'POST', headers: {'Content-Type': 'application/octet-stream'}, body: arq}).then(function (x) {
      enviando = false;
      if (x.s === 200) { if (prov) { prov.classList.add('saiu'); } dizer(''); ok(x.d); }
      else if (x.s === 423) { estadoBolha(prov, 'travado', 'Envio travado. Libere o envio para mandar.', deNovo); pintarTrava(false, 0); liberar(deNovo); }
      else if (x.s === 202) { var m2 = (x.d && x.d.mensagem) || MSG_INCERTO; estadoBolha(prov, 'incerta', m2); marcarIncerto(m2); }
      else { var m3 = 'Não enviada. ' + ((x.d && x.d.mensagem) || ('Falhou (' + x.s + ').')); if (prov) { estadoBolha(prov, 'falhou', m3, deNovo); } else { dizer(m3, true); } atualizar(true, true, true); }
    }).catch(function () {
      enviando = false;
      var m4 = 'O painel não respondeu a tempo e o áudio pode ter saído. Confira no celular antes de repetir.';
      estadoBolha(prov, 'incerta', m4); marcarIncerto(m4);   // pedido 84: rede caída é "pode ter saído", como na fila
    });
  }
  // ---- pedido 50, rodada 2: responder citando e reagir. `citando` é o id da mensagem escolhida (ou null).
  // rodada 3: `editando` é o id da mensagem própria cujo texto está no campo; a mesma faixa mostra qual é.
  var citando = null, editando = null, faixaCit = document.getElementById('zap-citando');
  function citar(bolha, editar) {
    var id = bolha ? bolha.getAttribute('data-id') : null;
    if (editando && !editar && txt) { txt.value = ''; }
    citando = editar ? null : id; editando = editar ? id : null;
    if (!faixaCit) { return; }
    faixaCit.hidden = !id;
    if (id) {
      document.getElementById('zap-citando-quem').textContent = editar ? 'Editando a mensagem' : (bolha.getAttribute('data-quem') || '');
      document.getElementById('zap-citando-previa').textContent = bolha.getAttribute('data-previa') || '';
    }
  }
  var armadoApagar = null, apagando = false;
  function desarmarApagar() {
    clearTimeout(armadoApagar); armadoApagar = null;
    document.querySelectorAll('[data-apagar].armado').forEach(function (b) {
      b.classList.remove('armado'); b.setAttribute('title', 'Apagar para todos');
      var bo = b.closest('.zap-msg'); if (bo) { bo.classList.remove('reagindo'); }
    });
  }
  function apagar(id) {
    if (barradoPorIncerto() || apagando || !f) { return; }
    apagando = true; dizer('Apagando…');
    postJson('/conversas/apagar', {para: f.dataset.para, id: id}).then(function (x) {
      apagando = false;
      if (x.s === 200) { dizer(''); setTimeout(function () { atualizar(true, true); }, 700); }
      else if (x.s === 423) { dizer(''); pintarTrava(false, 0); liberar(function () { apagar(id); }); }
      else if (x.s === 202) { marcarIncerto((x.d && x.d.mensagem) || 'Pode ter saído. Confira no celular antes de repetir.'); }
      else { dizer((x.d && x.d.mensagem) || ('Falhou (' + x.s + ').'), true); }
    }).catch(function () { apagando = false; dizer('Falha de rede local. Confira no celular antes de repetir.', true); });
  }
  var reagindo = false;
  function reagir(id, emoji) {
    if (barradoPorIncerto() || reagindo || !f) { return; }
    reagindo = true;
    postJson('/conversas/reagir', {para: f.dataset.para, id: id, emoji: emoji}).then(function (x) {
      reagindo = false;
      if (x.s === 200) { dizer(''); setTimeout(function () { atualizar(true, true); }, 700); }
      else if (x.s === 423) { pintarTrava(false, 0); liberar(function () { reagir(id, emoji); }); }
      else if (x.s === 202) { marcarIncerto((x.d && x.d.mensagem) || 'Pode ter saído. Confira no celular antes de repetir.'); }
      else { dizer((x.d && x.d.mensagem) || ('Falhou (' + x.s + ').'), true); }
    }).catch(function () { reagindo = false; dizer('Falha de rede local. Confira no celular antes de repetir.', true); });
  }
  window.vigiaZap = {enviar: enviar, postJson: postJson, dizer: dizer};

  var msgs = document.getElementById('zap-msgs');
  // Auditoria C5 (lote 3 do pedido 63): foto, vídeo ou áudio que não carrega deixava ícone quebrado, vídeo parado ou
  // áudio mudo, sem dizer nada. Agora a bolha diz por quê (o servidor já sabia) e oferece tentar de novo.
  var MOTIVO_MIDIA = {
    404: 'Esta mídia não está mais disponível. O WhatsApp guarda o arquivo por tempo limitado; peça para reenviarem.',
    502: 'A ponte com o WhatsApp não respondeu ao baixar esta mídia.',
    503: 'O banco das mensagens não respondeu ao buscar esta mídia.',
    401: 'Sua sessão terminou. Entre de novo para ver esta mídia.'
  };
  function notaDeMidia(el, texto, podeTentar) {
    var dono = el.closest('.zap-foto, .zap-vid, .zap-audio, .zap-produto') || el, velha = dono.nextElementSibling;
    if (velha && velha.classList && velha.classList.contains('zap-midia-erro')) { velha.remove(); }
    var n = document.createElement('div'); n.className = 'zap-midia-erro'; n.setAttribute('role', 'status');
    var t = document.createElement('span'); t.textContent = texto; n.appendChild(t);
    if (podeTentar) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'btn'; b.textContent = 'Tentar de novo';
      b.addEventListener('click', function () {
        var base = (el.getAttribute('data-src-midia') || '').replace(/[&?]r=[0-9]+/, '');
        n.remove(); dono.classList.remove('zap-midia-falhou');
        el.setAttribute('src', base + '&r=' + Date.now()); if (el.load && el.tagName !== 'IMG') { el.load(); }
      });
      n.appendChild(b);
    }
    dono.classList.add('zap-midia-falhou');
    dono.parentNode.insertBefore(n, dono.nextSibling);
  }
  document.addEventListener('error', function (ev) {
    var el = ev.target, src = el && el.getAttribute && el.getAttribute('src');
    if (!el || !/^(IMG|VIDEO|AUDIO)$/.test(el.tagName) || !src || src.indexOf('/conversas/midia?') !== 0) { return; }
    el.setAttribute('data-src-midia', src.split('#')[0]);
    // o elemento não diz o motivo; pergunta ao servidor só o cabeçalho (o painel não baixa de novo o que está em cache)
    fetch(src.split('#')[0], {method: 'HEAD', credentials: 'same-origin'}).then(function (r) {
      if (r.ok) { notaDeMidia(el, 'O arquivo chegou, mas este formato não abre aqui. Use Baixar.', false); return; }
      notaDeMidia(el, MOTIVO_MIDIA[r.status] || ('Não consegui carregar esta mídia (' + r.status + ').'), r.status !== 404 && r.status !== 401);
    }).catch(function () { notaDeMidia(el, 'O painel não respondeu ao carregar esta mídia.', true); });
  }, true);
  function rolar() { if (msgs) { msgs.scrollTop = msgs.scrollHeight; } }
  // imagem e áudio carregam depois: enquanto ele está no fim da conversa, cada carga empurra a rolagem de novo
  var colado = true;
  if (msgs) {
    msgs.addEventListener('scroll', function () { colado = msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 80; });
    msgs.addEventListener('load', function () { if (colado) { rolar(); } }, true);
    msgs.addEventListener('loadedmetadata', function () { if (colado) { rolar(); } }, true);
  }
  rolar();
  // pedido 58: tocar na citação leva até a mensagem citada e a destaca por um instante (sem mexer no endereço)
  if (msgs) {
    msgs.addEventListener('click', function (ev) {
      var a = ev.target.closest ? ev.target.closest('a.zap-citada') : null;
      if (!a) { return; }
      var alvo = document.getElementById((a.getAttribute('href') || '').slice(1));
      if (!alvo) { return; }
      ev.preventDefault();
      colado = false;
      var calmo = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      alvo.scrollIntoView({block: 'center', behavior: calmo ? 'auto' : 'smooth'});
      alvo.classList.remove('alvo'); void alvo.offsetWidth; alvo.classList.add('alvo');
      setTimeout(function () { alvo.classList.remove('alvo'); }, 2000);
    });
  }
  var ampliada = document.getElementById('zap-ampliada');
  if (msgs && ampliada && ampliada.showModal) {
    document.addEventListener('click', function (ev) {
      var b = ev.target.closest ? ev.target.closest('[data-ampliar]') : null;
      if (!b) { return; }
      var src = b.getAttribute('data-ampliar');
      ampliada.querySelector('img').src = src;
      ampliada.querySelector('a').href = src + '&baixar=1';
      ampliada.showModal();
    });
    ampliada.addEventListener('click', function (ev) { if (ev.target === ampliada || ev.target.closest('[data-fechar]')) { ampliada.close(); } });
    ampliada.addEventListener('close', function () { ampliada.querySelector('img').removeAttribute('src'); });
  }
  // pedido 57: tocar no nome ou na foto do cabeçalho abre as informações da conversa num painel à direita. O miolo
  // vem pronto do servidor (uma vez por abertura de página); Esc ou o X fecham.
  var perfil = document.getElementById('zap-perfil');
  if (perfil) {
    var perfilCorpo = document.getElementById('zap-perfil-corpo'), perfilBotao = document.querySelector('[data-perfil-abrir]');
    var perfilCarregado = false;
    var avisoPerfil = function (t) { var p = document.createElement('p'); p.className = 'zap-perfil-carga'; p.textContent = t; perfilCorpo.textContent = ''; perfilCorpo.appendChild(p); };
    var fecharPerfil = function () {
      if (perfil.hidden) { return; }
      perfil.hidden = true;
      if (perfilBotao) { perfilBotao.setAttribute('aria-expanded', 'false'); perfilBotao.focus(); }
    };
    var abrirPerfil = function () {
      perfil.hidden = false;
      if (perfilBotao) { perfilBotao.setAttribute('aria-expanded', 'true'); }
      perfil.querySelector('[data-perfil-fechar]').focus();
      if (perfilCarregado) { return; }
      avisoPerfil('Carregando as informações…');
      fetch('/conversas/perfil?c=' + encodeURIComponent(perfil.getAttribute('data-c')), {credentials: 'same-origin'})
        .then(function (r) { if (!r.ok) { throw new Error('perfil'); } return r.text(); })
        .then(function (h) { perfilCorpo.innerHTML = h; perfilCarregado = true; })
        .catch(function () { avisoPerfil('Não foi possível carregar agora. Feche e abra de novo para tentar.'); });
    };
    document.addEventListener('click', function (ev) {
      if (!ev.target.closest) { return; }
      if (ev.target.closest('[data-perfil-abrir]')) { if (perfil.hidden) { abrirPerfil(); } else { fecharPerfil(); } }
      else if (ev.target.closest('[data-perfil-fechar]')) { fecharPerfil(); }
    });
    document.addEventListener('keydown', function (ev) {
      var aberto = document.querySelector('dialog[open]');
      if (ev.key === 'Escape' && !perfil.hidden && !aberto) { fecharPerfil(); }
    });
  }
  // pedido 59: o vídeo toca e pausa na própria bolha (botão do meio ou toque no vídeo); o botão do canto abre grande,
  // continuando do ponto em que estava
  if (msgs) {
    var marcar = function (ev) {
      var cx = ev.target && ev.target.closest ? ev.target.closest('.zap-vid') : null;
      if (cx) { cx.classList.toggle('tocando', ev.type === 'play'); }
    };
    ['play', 'pause', 'ended'].forEach(function (n) { msgs.addEventListener(n, marcar, true); });
    var grande = document.getElementById('zap-video-grande');
    msgs.addEventListener('click', function (ev) {
      var cx = ev.target.closest ? ev.target.closest('.zap-vid') : null;
      if (!cx) { return; }
      var v = cx.querySelector('video');
      if (ev.target.closest('[data-expandir]') && grande && grande.showModal) {
        var em = v.currentTime || 0, src = v.getAttribute('src').split('#')[0], g = grande.querySelector('video');
        v.pause();
        g.src = src;
        g.addEventListener('loadedmetadata', function () { if (em > 0 && em < g.duration) { g.currentTime = em; } }, {once: true});
        grande.querySelector('a').href = src + '&baixar=1';
        grande.showModal();
        var p = g.play(); if (p && p.catch) { p.catch(function () {}); }
        return;
      }
      if (v.paused) { var q = v.play(); if (q && q.catch) { q.catch(function () {}); } } else { v.pause(); }
    });
    if (grande) {
      grande.addEventListener('click', function (ev) { if (ev.target === grande || ev.target.closest('[data-fechar]')) { grande.close(); } });
      grande.addEventListener('close', function () { var g = grande.querySelector('video'); g.pause(); g.removeAttribute('src'); g.load(); });
    }
  }
  // Reagir abre seis emojis em cima da bolha; Responder põe a mensagem na faixa acima do campo; a minha reação sai no clique.
  var EMOJIS = ['\\uD83D\\uDC4D', '\\u2764\\uFE0F', '\\uD83D\\uDE02', '\\uD83D\\uDE2E', '\\uD83D\\uDE22', '\\uD83D\\uDE4F'];
  // pedido 51: a tira mostra primeiro as reações usadas por último (ficam só neste navegador); "+" abre a grade inteira
  function recentes() {
    try { var r = JSON.parse(localStorage.getItem('greg.reacoes') || '[]'); return Array.isArray(r) ? r.filter(function (e) { return typeof e === 'string' && e && e.length <= 16; }) : []; }
    catch (e) { return []; }
  }
  function lembrarReacao(e) {
    if (!e) { return; }
    try { localStorage.setItem('greg.reacoes', JSON.stringify([e].concat(recentes().filter(function (x) { return x !== e; })).slice(0, 12))); } catch (er) {}
  }
  function daTira() {
    var v = [];
    recentes().concat(EMOJIS).forEach(function (e) { if (v.indexOf(e) < 0 && v.length < 6) { v.push(e); } });
    return v;
  }
  function todosEmojis() {
    var el = document.getElementById('zap-emojis-todos'), v = [];
    try { JSON.parse(el ? el.textContent : '[]').forEach(function (e) { if (v.indexOf(e) < 0) { v.push(e); } }); } catch (e) {}
    return v.length ? v : EMOJIS;
  }
  function botaoEmoji(e) {
    var b = document.createElement('button');
    b.type = 'button'; b.textContent = e; b.setAttribute('data-emoji', e); b.setAttribute('aria-label', 'Reagir com ' + e);
    return b;
  }
  // a caixa é fixa na janela (não corta no topo da conversa): acima da bolha; sem espaço, abaixo
  function posicionarEmojis(cx, bolha) {
    var r = bolha.getBoundingClientRect(), w = cx.offsetWidth, h = cx.offsetHeight;
    var x = bolha.classList.contains('saida') ? r.right - w : r.left, y = r.top - h - 4;
    if (y < 8) { y = Math.min(r.bottom + 4, window.innerHeight - h - 8); }
    cx.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + 'px'; cx.style.top = Math.max(8, y) + 'px';
  }
  function fecharEmojis() {
    document.querySelectorAll('.zap-emojis').forEach(function (e) { e.remove(); });
    document.querySelectorAll('.zap-msg.reagindo').forEach(function (b) { b.classList.remove('reagindo'); });
  }
  if (msgs && f) {
    msgs.addEventListener('click', function (ev) {
      var alvo = ev.target.closest ? ev.target.closest('[data-responder], [data-reagir], [data-emoji], [data-tirar-reacao], [data-editar], [data-encaminhar], [data-apagar]') : null;
      if (!alvo) { return; }
      var bolha = alvo.closest('.zap-msg');
      if (!bolha || !bolha.getAttribute('data-id')) { return; }
      if (alvo.hasAttribute('data-responder')) { fecharEmojis(); citar(bolha); if (txt && !txt.disabled) { txt.focus(); } return; }
      if (alvo.hasAttribute('data-editar')) {
        fecharEmojis(); if (temPendente && temPendente()) { soltarPendentes(); }
        citar(bolha, true);
        var corpoTxt = bolha.querySelector('.zap-txt');
        if (txt) { txt.value = corpoTxt ? corpoTxt.textContent : ''; txt.dispatchEvent(new Event('input')); if (!txt.disabled) { txt.focus(); } }
        return;
      }
      if (alvo.hasAttribute('data-encaminhar')) { fecharEmojis(); abrirEncaminhar(bolha); return; }
      if (alvo.hasAttribute('data-apagar')) {
        // dois cliques no mesmo botão (como o "fora" da lista): o primeiro arma por 6 s, o segundo apaga
        fecharEmojis();
        if (alvo.classList.contains('armado')) { desarmarApagar(); apagar(bolha.getAttribute('data-id')); return; }
        desarmarApagar(); alvo.classList.add('armado'); bolha.classList.add('reagindo');
        alvo.setAttribute('title', 'Clique de novo para apagar para todos');
        dizer('Apagar para todos? Clique de novo na lixeira. Não tem volta.', true);
        armadoApagar = setTimeout(function () { desarmarApagar(); dizer(''); }, 6000);
        return;
      }
      if (alvo.hasAttribute('data-tirar-reacao')) { fecharEmojis(); reagir(bolha.getAttribute('data-id'), ''); return; }
      if (alvo.hasAttribute('data-emoji')) { var e = alvo.getAttribute('data-emoji'); fecharEmojis(); lembrarReacao(e); reagir(bolha.getAttribute('data-id'), e); return; }
      var aberta = bolha.querySelector('.zap-emojis');
      fecharEmojis();
      if (aberta) { return; }
      var cx = document.createElement('div');
      cx.className = 'zap-emojis'; cx.setAttribute('role', 'group'); cx.setAttribute('aria-label', 'Escolha a reação');
      daTira().forEach(function (e) { cx.appendChild(botaoEmoji(e)); });
      var mais = document.createElement('button');
      mais.type = 'button'; mais.className = 'mais'; mais.textContent = '+'; mais.setAttribute('aria-label', 'Outra reação'); mais.title = 'Outra reação';
      mais.addEventListener('click', function (ev2) {
        ev2.stopPropagation();
        cx.textContent = ''; cx.classList.add('grade');
        todosEmojis().forEach(function (e) { cx.appendChild(botaoEmoji(e)); });
        posicionarEmojis(cx, bolha); cx.firstChild.focus();
      });
      cx.appendChild(mais);
      bolha.classList.add('reagindo'); bolha.appendChild(cx); posicionarEmojis(cx, bolha); cx.firstChild.focus();
    });
    msgs.addEventListener('scroll', function () { if (document.querySelector('.zap-emojis')) { fecharEmojis(); } });
    document.addEventListener('click', function (ev) { if (!ev.target.closest('.zap-emojis, [data-reagir]')) { fecharEmojis(); } });
    document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') { fecharEmojis(); } });
    var tirarCit = document.getElementById('zap-citando-tirar');
    if (tirarCit) { tirarCit.addEventListener('click', function () { citar(null); if (txt) { txt.dispatchEvent(new Event('input')); } }); }
  }
  // ---- rodada 3: encaminhar. O destino sai da lista de conversas já carregada ao lado (sem conversa de número escondido).
  var enc = document.getElementById('zap-enc'), encLista = document.getElementById('zap-enc-lista');
  var encBusca = document.getElementById('zap-enc-busca'), encOk = document.getElementById('zap-enc-ok');
  var encId = null, encPara = null, encNome = '', encaminhando = false;
  var encFonte = null, encParcial = false;   // auditoria C4: com filtro na lista, os destinos vêm da lista inteira
  function pintarDestinos() {
    var t = semAcento(encBusca.value.trim());
    encLista.textContent = ''; encPara = null; encOk.disabled = true; encOk.textContent = 'Encaminhar';
    (encFonte || document).querySelectorAll('#zap-itens .zap-item').forEach(function (a) {
      var jid = a.getAttribute('data-jid') || '', nome = a.getAttribute('data-nome') || '';
      if (/@lid$/.test(jid) || (t && (a.getAttribute('data-busca') || '').indexOf(t) < 0)) { return; }
      var b = document.createElement('button'), av = a.querySelector('.zap-av'), n = document.createElement('span');
      b.type = 'button'; b.setAttribute('data-jid', jid); b.setAttribute('data-nome', nome); b.setAttribute('aria-pressed', 'false');
      // pedido 84: a foto vem da linha da lista (mesmo endereço, já no cache do navegador); sem foto, a silhueta dela
      if (av) {
        av = document.importNode(av, true);
        if (av.tagName === 'IMG') { av.loading = 'lazy'; av.decoding = 'async'; }
        b.appendChild(av);
      }
      n.className = 'zap-enc-nome'; n.textContent = nome; b.appendChild(n);
      encLista.appendChild(b);
    });
    if (!encLista.firstChild) { var v = document.createElement('p'); v.textContent = 'Nenhuma conversa com esse nome.'; encLista.appendChild(v); }
    if (encParcial) { var av = document.createElement('p'); av.textContent = 'Só as conversas do filtro atual aparecem aqui. Para ver todas, feche e mude o filtro da lista para Todas.'; encLista.appendChild(av); }
  }
  function esqueleto(n) {
    var e = document.createElement('div'); e.className = 'esqueleto'; e.setAttribute('aria-hidden', 'true');
    for (var i = 0; i < n; i++) { e.insertAdjacentHTML('beforeend', '<div class="esq-linha"><i style="width:38%"></i><i style="width:82%"></i></div>'); }
    return e;
  }
  function abrirEncaminhar(bolha) {
    if (!enc || !enc.showModal) { return; }
    encId = bolha.getAttribute('data-id'); encBusca.value = ''; pintarDestinos(); enc.showModal(); encBusca.focus();
    var filtroAtual = new URL(location.href).searchParams.get('f') || 'todas';
    if (filtroAtual === 'todas' || encFonte) { return; }
    var esq = esqueleto(3); encLista.appendChild(esq);   // pedido 84: enquanto a lista inteira chega
    fetch('/conversas?f=todas', {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.text() : ''; }).then(function (h) {
      var d = h ? new DOMParser().parseFromString(h, 'text/html') : null;
      if (d && d.querySelector('#zap-itens .zap-item')) { encFonte = d; encParcial = false; } else { encParcial = true; }
      if (enc.open) { var escolhido = encPara; pintarDestinos(); if (escolhido) { var bt = encLista.querySelector('button[data-jid="' + escolhido.replace(/"/g, '') + '"]'); if (bt) { bt.click(); } } }
    }).catch(function () { encParcial = true; esq.remove(); if (enc.open && !encPara) { pintarDestinos(); } });
  }
  function encaminhar() {
    if (barradoPorIncerto() || encaminhando || !encPara) { return; }
    var corpo = {de: f.dataset.para, id: encId, para: encPara}, nome = encNome, prov = null;
    enc.close();
    // pedido 84: na hora do clique a linha do destino sobe; se o destino é a conversa aberta, a bolha aparece também
    if (corpo.para === corpo.de && msgs) {
      var orig = null;
      msgs.querySelectorAll('.zap-msg[data-id]').forEach(function (x) { if (x.getAttribute('data-id') === corpo.id) { orig = x; } });
      var ot = orig && orig.querySelector('.zap-txt');
      prov = bolhaNaHora((ot && ot.textContent.trim()) || 'Mensagem encaminhada');
    }
    mandarEncaminhar(corpo, nome, prov);
  }
  function mandarEncaminhar(corpo, nome, prov) {
    function deNovo() { voltarAEnviar(prov); mandarEncaminhar(corpo, nome, prov); }
    encaminhando = true; encOk.disabled = true;
    subirNaLista(corpo.para, 'Encaminhando…'); dizer('Encaminhando para ' + nome + '…');
    postJson('/conversas/encaminhar', corpo).then(function (x) {
      encaminhando = false;
      if (x.s === 200) { if (prov) { prov.classList.add('saiu'); } dizer('Encaminhada para ' + nome + '.'); atualizar(true); }
      else if (x.s === 423) { estadoBolha(prov, 'travado', 'Envio travado. Libere o envio para mandar.', deNovo); dizer(''); pintarTrava(false, 0); liberar(deNovo); }
      else if (x.s === 202) { var m2 = (x.d && x.d.mensagem) || MSG_INCERTO; estadoBolha(prov, 'incerta', m2); marcarIncerto(m2); }
      else {
        var m3 = (x.d && x.d.mensagem) || ('Falhou (' + x.s + ').');
        if (prov) { estadoBolha(prov, 'falhou', 'Não enviada. ' + m3, deNovo); dizer(''); } else { dizer('Não foi encaminhada para ' + nome + '. ' + m3, true); }
        atualizar(true);   // a linha que subiu volta ao lugar
      }
    }).catch(function () {
      encaminhando = false;
      var m4 = 'O painel não respondeu a tempo e a mensagem pode ter saído para ' + nome + '. Confira no celular antes de repetir.';
      estadoBolha(prov, 'incerta', m4); marcarIncerto(m4);   // pedido 84: rede caída é "pode ter saído", como na fila
    });
  }
  if (enc && f) {
    encBusca.addEventListener('input', pintarDestinos);
    encLista.addEventListener('click', function (ev) {
      var b = ev.target.closest('button[data-jid]');
      if (!b) { return; }
      encLista.querySelectorAll('button').forEach(function (o) { o.setAttribute('aria-pressed', o === b ? 'true' : 'false'); });
      encPara = b.getAttribute('data-jid'); encNome = b.getAttribute('data-nome') || b.textContent; encOk.disabled = false; encOk.textContent = 'Encaminhar para ' + encNome;
    });
    encOk.addEventListener('click', encaminhar);
    enc.addEventListener('click', function (ev) { if (ev.target === enc || ev.target.closest('[data-fechar]')) { enc.close(); } });
  }
  // ---- rodada 3: "Ver mensagens anteriores" troca o fio pelo degrau seguinte sem perder o ponto da leitura
  if (msgs) {
    msgs.addEventListener('click', function (ev) {
      var b = ev.target.closest ? ev.target.closest('.zap-anteriores') : null;
      if (!b) { return; }
      var u = new URL(location.href); u.searchParams.set('n', b.getAttribute('data-n'));
      b.disabled = true; b.textContent = 'Carregando…';
      var esqA = esqueleto(2); b.parentNode.insertBefore(esqA, b.nextSibling);   // pedido 84
      fetch(u.href, {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.text() : ''; }).then(function (h) {
        var mn = h ? new DOMParser().parseFromString(h, 'text/html').getElementById('zap-msgs') : null;
        if (!mn) { esqA.remove(); b.disabled = false; b.textContent = 'Ver mensagens anteriores'; dizer('Não consegui carregar as mensagens anteriores. Tente de novo.', true); return; }
        var doFim = msgs.scrollHeight - msgs.scrollTop;
        msgs.innerHTML = mn.innerHTML; msgs.setAttribute('data-assinatura', mn.getAttribute('data-assinatura'));
        msgs.scrollTop = msgs.scrollHeight - doFim;
        history.replaceState(null, '', u.pathname + u.search);
      }).catch(function () { esqA.remove(); b.disabled = false; b.textContent = 'Ver mensagens anteriores'; dizer('O painel não respondeu. As mensagens anteriores não foram carregadas.', true); });
    });
  }
  if (f && txt) {
    function crescer() { txt.style.height = 'auto'; txt.style.height = Math.max(32, Math.min(txt.scrollHeight, 160)) + 'px'; }
    txt.addEventListener('input', crescer);
    txt.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); f.requestSubmit(); }
    });
    // ---- pedido 50: anexo pendente (escolhido, colado ou arrastado); o texto do campo vira a legenda.
    // Pedido 71: vários anexos de uma vez (até 10), com prévia do escolhido e miniaturas para navegar; e a conversa
    // INTEIRA aceita o arquivo arrastado, não só a caixa de escrever.
    var entrada = document.getElementById('zap-arquivo'), faixa = document.getElementById('zap-pendente');
    var faixaNome = document.getElementById('zap-pendente-nome'), previa = document.getElementById('zap-pend-previa');
    var minis = document.getElementById('zap-pend-minis'), pendentes = [], sel = 0, LIMITE_ANEXOS = 10, figuras = new Map();
    function tamanho(n) { return n >= 1048576 ? (n / 1048576).toFixed(1).replace('.', ',') + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB'; }
    function tipoDe(a) {
      if (/^image\\/(jpeg|png|webp)$/.test(a.type)) { return 'foto'; }
      if (/^video\\/(mp4|quicktime)$/.test(a.type)) { return 'video'; }
      return 'arquivo';
    }
    function rotuloDe(a) { return {foto: 'Foto', video: 'Vídeo', arquivo: 'Arquivo'}[tipoDe(a)]; }
    // miniatura feita na página (data:, nada sai do Mac); só foto. Uma por arquivo, guardada enquanto ele está pendente.
    function figura(a, pronto) {
      if (figuras.has(a)) { pronto(figuras.get(a)); return; }
      if (tipoDe(a) !== 'foto' || !window.createImageBitmap) { pronto(''); return; }
      window.createImageBitmap(a).then(function (bm) {
        var e = Math.min(1, 480 / Math.max(bm.width, bm.height)), cv = document.createElement('canvas');
        cv.width = Math.max(1, Math.round(bm.width * e)); cv.height = Math.max(1, Math.round(bm.height * e));
        cv.getContext('2d').drawImage(bm, 0, 0, cv.width, cv.height);
        var u = cv.toDataURL('image/jpeg', .75); figuras.set(a, u); pronto(u);
      }).catch(function () { pronto(''); });
    }
    function pintarPendentes() {
      var n = pendentes.length;
      if (sel >= n) { sel = Math.max(0, n - 1); }
      if (faixa) { faixa.hidden = !n; }
      if (txt) { txt.placeholder = n ? (n > 1 ? 'Legenda da primeira (opcional)' : 'Legenda (opcional)') : 'Escreva para ' + (f.getAttribute('data-nome') || ''); }
      if (!n) { figuras.clear(); if (previa) { previa.textContent = ''; } if (minis) { minis.textContent = ''; } return; }
      var atual = pendentes[sel];
      if (faixaNome) { faixaNome.textContent = (n > 1 ? (sel + 1) + ' de ' + n + ' · ' : '') + rotuloDe(atual) + ': ' + (atual.name || 'sem nome') + ' · ' + tamanho(atual.size); }
      if (previa) {
        previa.textContent = '';
        figura(atual, function (u) {
          if (pendentes[sel] !== atual) { return; }
          previa.textContent = '';
          if (u) { var im = document.createElement('img'); im.alt = 'Prévia de ' + (atual.name || 'foto'); im.src = u; previa.appendChild(im); }
        });
      }
      if (minis) {
        minis.textContent = ''; minis.hidden = n < 2;
        pendentes.forEach(function (a, i) {
          var b = document.createElement('button'); b.type = 'button'; b.className = 'zap-mini' + (i === sel ? ' on' : '');
          b.setAttribute('aria-label', 'Anexo ' + (i + 1) + ' de ' + n + ': ' + (a.name || rotuloDe(a)));
          b.setAttribute('aria-pressed', i === sel ? 'true' : 'false'); b.setAttribute('data-i', String(i));
          var r = document.createElement('span'); r.textContent = tipoDe(a) === 'arquivo' ? ((a.name || '').split('.').pop() || 'arq').slice(0, 4).toUpperCase() : rotuloDe(a);
          b.appendChild(r);
          figura(a, function (u) { if (u) { b.style.backgroundImage = 'url(' + u + ')'; b.classList.add('com-foto'); } });
          minis.appendChild(b);
        });
      }
    }
    // junta arquivos aos pendentes; devolve quantos entraram. Arquivo vazio ou acima de 16 MB não entra, e a tela diz.
    function anexar(lista) {
      var entrou = 0, grandes = 0, sobra = 0;
      Array.prototype.forEach.call(lista || [], function (a) {
        if (!a || !a.size) { return; }
        if (a.size > LIMITE_MIDIA) { grandes++; return; }
        if (pendentes.length >= LIMITE_ANEXOS) { sobra++; return; }
        pendentes.push(a); entrou++;
      });
      if (entrou) { sel = pendentes.length - entrou; }
      var avisos = [];
      if (grandes) { avisos.push(grandes > 1 ? grandes + ' arquivos acima de 16 MB ficaram de fora.' : 'Arquivo acima de 16 MB. O Greg não envia arquivos maiores.'); }
      if (sobra) { avisos.push('O Greg anexa até ' + LIMITE_ANEXOS + ' por vez; ' + sobra + (sobra > 1 ? ' ficaram' : ' ficou') + ' de fora.'); }
      dizer(avisos.join(' '), !!avisos.length);
      pintarPendentes(); if (entrou && txt && !txt.disabled) { txt.focus(); }
      if (entrada) { entrada.value = ''; }
      return entrou;
    }
    function pendurar(a) { pendentes = []; sel = 0; if (a) { anexar([a]); } else { pintarPendentes(); if (entrada) { entrada.value = ''; } } }
    if (btAnexar && entrada) {
      entrada.multiple = true;
      btAnexar.addEventListener('click', function () { entrada.click(); });
      entrada.addEventListener('change', function () { if (entrada.files && entrada.files.length) { anexar(entrada.files); } });
    }
    repender = function (a) { var antes = pendentes.length; anexar([a]); return pendentes.length > antes; };
    temPendente = function () { return pendentes.length > 0; }; soltarPendentes = function () { pendurar(null); };
    var tirar = document.getElementById('zap-pendente-tirar');
    if (tirar) { tirar.addEventListener('click', function () { figuras.delete(pendentes[sel]); pendentes.splice(sel, 1); dizer(''); pintarPendentes(); }); }
    if (minis) {
      minis.addEventListener('click', function (ev) {
        var b = ev.target.closest ? ev.target.closest('.zap-mini') : null;
        if (b) { sel = +b.getAttribute('data-i') || 0; pintarPendentes(); }
      });
    }
    txt.addEventListener('paste', function (ev) {
      var arqs = ev.clipboardData && ev.clipboardData.files;
      if (liberado && arqs && arqs.length) { ev.preventDefault(); anexar(arqs); }
    });
    // a área de soltar é a conversa inteira (mensagens e caixa de escrever); a faixa por cima diz o que vai acontecer
    var areaFio = document.querySelector('.zap-fio') || f, solte = document.createElement('div'), entradas = 0;
    solte.className = 'zap-solte'; solte.hidden = true; solte.setAttribute('aria-hidden', 'true'); document.body.appendChild(solte);
    function temArquivo(ev) { return !!ev.dataTransfer && Array.prototype.indexOf.call(ev.dataTransfer.types || [], 'Files') >= 0; }
    function mostrarSolte(on) {
      if (on) {
        var r = areaFio.getBoundingClientRect();
        solte.style.left = r.left + 'px'; solte.style.top = r.top + 'px'; solte.style.width = r.width + 'px'; solte.style.height = r.height + 'px';
        solte.textContent = liberado ? 'Solte para anexar' : 'Solte para liberar o envio e anexar';
      }
      solte.hidden = !on;
    }
    areaFio.addEventListener('dragenter', function (ev) { if (temArquivo(ev)) { ev.preventDefault(); entradas++; mostrarSolte(true); } });
    areaFio.addEventListener('dragover', function (ev) { if (temArquivo(ev)) { ev.preventDefault(); ev.dataTransfer.dropEffect = 'copy'; } });
    areaFio.addEventListener('dragleave', function (ev) { if (temArquivo(ev)) { entradas = Math.max(0, entradas - 1); if (!entradas) { mostrarSolte(false); } } });
    areaFio.addEventListener('drop', function (ev) {
      entradas = 0; mostrarSolte(false);
      var arqs = ev.dataTransfer && ev.dataTransfer.files;
      if (!arqs || !arqs.length) { return; }
      ev.preventDefault();
      var lista = Array.prototype.slice.call(arqs);
      if (liberado) { anexar(lista); } else { liberar(function () { anexar(lista); }); }
    });

    // ---- pedido 50: gravar áudio. O navegador grava (MP4/AAC no WebKit, WebM/Opus no Chrome) e a Evolution converte
    // para áudio de voz. Enviar para a gravação e manda; Cancelar descarta. Nada fica guardado no navegador.
    // Pedido 51: o MP4 em fragmentos do WebKit sai da conversão da Evolution 2.3.7 com 1 s (medido: 12 s viram 0,99 s).
    // Por isso a gravação é decodificada aqui e vai em WAV (mono, 16 kHz; 12 kHz acima de 8 min para caber em 16 MB),
    // que a Evolution converte inteiro. Se o que foi decodificado é mais curto que o tempo gravado, nada é enviado.
    function paraWav(blob) {
      var AC = window.AudioContext || window.webkitAudioContext, OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
      if (!AC || !OAC || !blob.arrayBuffer) { return Promise.reject(new Error('sem decodificador')); }
      return blob.arrayBuffer().then(function (ab) {
        var ac = new AC();
        return new Promise(function (ok, nao) { ac.decodeAudioData(ab, ok, nao); }).then(function (buf) {
          try { ac.close(); } catch (e) {}
          var taxa = buf.duration <= 480 ? 16000 : 12000, n = Math.max(1, Math.ceil(buf.duration * taxa));
          var off = new OAC(1, n, taxa), fonte = off.createBufferSource();
          fonte.buffer = buf; fonte.connect(off.destination); fonte.start(0);
          return off.startRendering().then(function (r) {
            var pcm = r.getChannelData(0), dv = new DataView(new ArrayBuffer(44 + pcm.length * 2)), i, s;
            function asc(p, tx) { for (var k = 0; k < tx.length; k++) { dv.setUint8(p + k, tx.charCodeAt(k)); } }
            asc(0, 'RIFF'); dv.setUint32(4, 36 + pcm.length * 2, true); asc(8, 'WAVE'); asc(12, 'fmt ');
            dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true); dv.setUint32(24, taxa, true);
            dv.setUint32(28, taxa * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
            asc(36, 'data'); dv.setUint32(40, pcm.length * 2, true);
            for (i = 0; i < pcm.length; i++) { s = Math.max(-1, Math.min(1, pcm[i])); dv.setInt16(44 + i * 2, s < 0 ? s * 32768 : s * 32767, true); }
            return {wav: new Blob([dv.buffer], {type: 'audio/wav'}), seg: buf.duration};
          });
        });
      });
    }
    function mmss(s) { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2); }
    var gravador = null, fluxo = null, pedacos = [], relogio = null, inicio = 0, mandarAoParar = false;
    var faixaGrav = document.getElementById('zap-gravando'), tempoGrav = document.getElementById('zap-gravando-tempo');
    function pintarGravacao(ligada) {
      if (faixaGrav) { faixaGrav.hidden = !ligada; }
      if (btGravar) { btGravar.classList.toggle('ativo', !!ligada); btGravar.setAttribute('aria-pressed', ligada ? 'true' : 'false'); }
      if (btAnexar) { btAnexar.disabled = !!ligada; }
      txt.disabled = !!ligada || !liberado;
    }
    function soltarMicrofone() {
      if (relogio) { clearInterval(relogio); relogio = null; }
      if (fluxo) { fluxo.getTracks().forEach(function (t) { t.stop(); }); fluxo = null; }
      gravador = null; pintarGravacao(false);
    }
    // auditoria D3 (D-66): aos 10 min a gravação para e ESPERA o Enviar; nada sai sem o clique (R-9)
    var retido = null, dicaGrav = faixaGrav ? faixaGrav.querySelector('.zap-gravando-dica') : null;
    var DICA_GRAV = dicaGrav ? dicaGrav.textContent : '';
    function largarRetido() { retido = null; if (dicaGrav) { dicaGrav.textContent = DICA_GRAV; } pintarGravacao(false); }
    function mandarAudio(partes, tipo, gravado) {
      if (barradoPorIncerto()) { return; }
      // pedido 84: a bolha e a linha da lista aparecem quando a gravação para; o preparo do arquivo corre atrás
      var prov = bolhaNaHora('Áudio de voz'); subirNaLista(f.dataset.para, 'Áudio de voz'); dizer('');
      function nada(t) { if (prov) { estadoBolha(prov, 'falhou', t); } else { dizer(t, true); } atualizar(true, true, true); }
      paraWav(new Blob(partes, {type: tipo})).then(function (r) {
        if (r.seg < gravado - Math.max(2, gravado * 0.1)) {
          nada('O áudio saiu incompleto (' + mmss(r.seg) + ' de ' + mmss(gravado) + ' gravados). Nada foi enviado; grave de novo.');
          return;
        }
        enviarMidia(r.wav, 'voz', '', function () { citar(null); atualizar(true); }, prov);
      }).catch(function () { nada('Não consegui preparar o áudio gravado. Nada foi enviado; grave de novo.'); });
    }
    function pararGravacao(mandar) {
      mandarAoParar = mandar === 'segurar' ? 'segurar' : !!mandar;
      if (gravador && gravador.state !== 'inactive') { gravador.stop(); } else { soltarMicrofone(); }
    }
    function comecarGravacao() {
      if (gravador || !podeGravar) { return; }
      if (retido) { largarRetido(); }
      pendurar(null);
      navigator.mediaDevices.getUserMedia({audio: true}).then(function (s) {
        fluxo = s; pedacos = []; mandarAoParar = false;
        try { gravador = new MediaRecorder(s); } catch (e) { soltarMicrofone(); dizer('Este navegador não grava áudio.', true); return; }
        gravador.addEventListener('dataavailable', function (ev) { if (ev.data && ev.data.size) { pedacos.push(ev.data); } });
        gravador.addEventListener('stop', function () {
          var tipo = (gravador && gravador.mimeType) || 'audio/mp4', mandar = mandarAoParar, partes = pedacos;
          var gravado = (Date.now() - inicio) / 1000;
          pedacos = []; soltarMicrofone();
          if (!mandar) { dizer(''); return; }
          if (mandar === 'segurar') {
            retido = {partes: partes, tipo: tipo, gravado: gravado};
            if (dicaGrav) { dicaGrav.textContent = 'A gravação parou no limite de 10 min. Enviar manda o áudio; Cancelar descarta.'; }
            pintarGravacao(true); dizer('');
            return;
          }
          mandarAudio(partes, tipo, gravado);
        });
        inicio = Date.now(); if (tempoGrav) { tempoGrav.textContent = '0:00'; }
        relogio = setInterval(function () {
          var seg = Math.floor((Date.now() - inicio) / 1000);
          if (tempoGrav) { tempoGrav.textContent = Math.floor(seg / 60) + ':' + ('0' + (seg % 60)).slice(-2); }
          if (seg >= 600) { pararGravacao('segurar'); }   // teto de 10 min por áudio: para e espera o Enviar
        }, 500);
        gravador.start(); pintarGravacao(true); dizer('');
      }).catch(function () { soltarMicrofone(); dizer('Microfone não liberado. Autorize o Greg em Ajustes do Sistema, Privacidade, Microfone.', true); });
    }
    if (btGravar) { btGravar.addEventListener('click', function () { if (retido) { largarRetido(); } else if (gravador) { pararGravacao(false); } else { comecarGravacao(); } }); }
    var cancelarGrav = document.getElementById('zap-gravando-cancelar');
    if (cancelarGrav) { cancelarGrav.addEventListener('click', function () { if (retido) { largarRetido(); dizer(''); } else { pararGravacao(false); } }); }
    window.addEventListener('pagehide', function () { pararGravacao(false); });

    f.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var t = txt.value;
      if (gravador) { pararGravacao(true); return; }
      if (retido) { var rt = retido; largarRetido(); mandarAudio(rt.partes, rt.tipo, rt.gravado); return; }
      if (pendentes.length) {
        // a legenda e a citação vão com o primeiro; os outros seguem na mesma fila, na ordem das miniaturas
        var lote = pendentes.slice();
        if (!enviarAnexo(lote[0], tipoDe(lote[0]), t)) { return; }
        citar(null);
        for (var k = 1; k < lote.length; k++) { enviarAnexo(lote[k], tipoDe(lote[k]), ''); }
        pendurar(null); txt.value = ''; crescer(); txt.focus();
        return;
      }
      if (!t.trim()) { return; }
      if (editando) {
        enviar({para: f.dataset.para, id: editando, texto: t}, function () { citar(null); txt.value = ''; crescer(); setTimeout(function () { atualizar(true, true); }, 700); }, '/conversas/editar');
        return;
      }
      var corpo = {para: f.dataset.para, texto: t};
      if (citando) { corpo.citar = citando; }
      if (enviarTexto(corpo)) { citar(null); txt.value = ''; crescer(); txt.focus(); }
    });
    // ---- pedido 72: agendar o envio. O relógio abre um seletor de dia e hora; o que está no campo (e os anexos
    // pendentes) fica guardado no Greg e sai sozinho na hora. A faixa em cima do campo mostra e cancela.
    var faixaAg = document.getElementById('zap-agendados'), quandoAg = document.getElementById('zap-ag-quando');
    var atalhosAg = document.getElementById('zap-ag-atalhos'), okAg = document.getElementById('zap-ag-ok'), agendando = false;
    function doisDig(n) { return (n < 10 ? '0' : '') + n; }
    function paraCampo(d) { return d.getFullYear() + '-' + doisDig(d.getMonth() + 1) + '-' + doisDig(d.getDate()) + 'T' + doisDig(d.getHours()) + ':' + doisDig(d.getMinutes()); }
    function atalhosDeAgora() {
      var a = new Date(), l = [], h1 = new Date(a.getTime() + 3600000), h18 = new Date(a), am = new Date(a), sg = new Date(a);
      l.push(['Em 1 hora', h1]);
      h18.setHours(18, 0, 0, 0); if (h18.getTime() - a.getTime() > 600000) { l.push(['Hoje 18:00', h18]); }
      am.setDate(am.getDate() + 1); am.setHours(9, 0, 0, 0); l.push(['Amanhã 09:00', am]);
      sg.setDate(sg.getDate() + ((8 - sg.getDay()) % 7 || 7)); sg.setHours(9, 0, 0, 0);
      if (sg.getTime() - am.getTime() > 3600000) { l.push(['Segunda 09:00', sg]); }
      return l;
    }
    function abrirAgenda() {
      atalhosAg.textContent = '';
      atalhosDeAgora().forEach(function (p) {
        var b = document.createElement('button'); b.type = 'button'; b.className = 'btn'; b.textContent = p[0];
        b.addEventListener('click', function () { quandoAg.value = paraCampo(p[1]); });
        atalhosAg.appendChild(b);
      });
      var pad = atalhosDeAgora().filter(function (p) { return p[0] === 'Amanhã 09:00'; })[0][1];
      quandoAg.min = paraCampo(new Date(Date.now() + 120000)); quandoAg.value = paraCampo(pad);
      popAg.hidden = false; quandoAg.focus();
    }
    function pintarAgendados(html) { if (faixaAg && typeof html === 'string') { faixaAg.innerHTML = html; } }
    function agendarTudo(quando) {
      // um pedido por item, em ordem; para no primeiro que falhar e deixa o resto no campo
      var t = txt.value, lote = pendentes.slice(), feitos = 0;
      function fim(x) {
        agendando = false; okAg.disabled = false;
        if (x && x.s === 200) {
          pintarAgendados(x.d.html); pendurar(null); citar(null); txt.value = ''; crescer(); popAg.hidden = true;
          dizer((feitos > 1 ? feitos + ' envios agendados' : 'Envio agendado') + ' para ' + x.d.quando + '.'); txt.focus();
          return;
        }
        if (x && x.s === 423) { pintarTrava(false); dizer('Envio travado: libere e agende de novo.', true); return; }
        if (x && x.d && x.d.html) { pintarAgendados(x.d.html); }
        dizer(((x && x.d && x.d.mensagem) || 'O painel não respondeu; nada foi agendado.') + (feitos ? ' ' + feitos + ' já ' + (feitos > 1 ? 'ficaram agendados' : 'ficou agendado') + '.' : ''), true);
        if (feitos) { for (var k = 0; k < feitos && pendentes.length; k++) { pendentes.shift(); } sel = 0; pintarPendentes(); if (lote.length) { txt.value = ''; crescer(); } }
      }
      function um(i, ultimo) {
        if (!lote.length) {
          postJson('/conversas/agendar', {para: f.dataset.para, texto: t, quando: quando}).then(function (x) { if (x.s === 200) { feitos = 1; } fim(x); }).catch(function () { fim(null); });
          return;
        }
        if (i >= lote.length) { fim(ultimo); return; }
        var a = lote[i], q = 'para=' + encodeURIComponent(f.dataset.para) + '&tipo=' + tipoDe(a) + '&nome=' + encodeURIComponent(a.name || 'arquivo') +
                '&legenda=' + encodeURIComponent(i === 0 ? t : '') + '&quando=' + quando;
        pedirJson('/conversas/agendar-midia?' + q, {method: 'POST', headers: {'Content-Type': a.type || 'application/octet-stream'}, body: a})
          .then(function (x) { if (x.s !== 200) { fim(x); return; } feitos++; um(i + 1, x); }).catch(function () { fim(null); });
      }
      agendando = true; okAg.disabled = true; dizer('Agendando…'); um(0, null);
    }
    if (btAgendar && popAg) {
      btAgendar.addEventListener('click', function () {
        if (!popAg.hidden) { popAg.hidden = true; return; }
        if (gravador) { dizer('Áudio de voz não se agenda: envie agora ou cancele a gravação.', true); return; }
        if (editando) { dizer('Termine a edição antes de agendar.', true); return; }
        if (!txt.value.trim() && !pendentes.length) { dizer('Escreva a mensagem ou anexe o arquivo e depois agende.', true); txt.focus(); return; }
        dizer(''); abrirAgenda();
      });
      okAg.addEventListener('click', function () {
        if (agendando) { return; }
        var ms = new Date(quandoAg.value).getTime();
        if (!quandoAg.value || isNaN(ms)) { dizer('Escolha o dia e a hora.', true); return; }
        if (ms < Date.now() + 60000) { dizer('Escolha um horário pelo menos 1 minuto à frente.', true); return; }
        agendarTudo(Math.floor(ms / 1000));
      });
      popAg.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') { ev.stopPropagation(); popAg.hidden = true; btAgendar.focus(); } });
      document.addEventListener('click', function (ev) { if (!popAg.hidden && !popAg.contains(ev.target) && !btAgendar.contains(ev.target)) { popAg.hidden = true; } });
    }
    if (faixaAg) {
      faixaAg.addEventListener('click', function (ev) {
        var b = ev.target.closest('[data-ag-cancelar]'); if (!b) { return; }
        b.disabled = true;
        postJson('/conversas/agendados/cancelar', {id: parseInt(b.getAttribute('data-ag-cancelar'), 10)}).then(function (x) {
          if (x.d && typeof x.d.html === 'string') { pintarAgendados(x.d.html); }
          if (x.s === 200) { dizer('Envio cancelado.'); return; }
          b.disabled = false; dizer((x.d && x.d.mensagem) || 'Não consegui cancelar.', true);
          if (x.s === 409) { atualizarAgendados(); }
        }).catch(function () { b.disabled = false; dizer('O painel não respondeu; o envio continua agendado.', true); });
      });
      // enquanto houver algo na faixa, confere a cada 30 s: o item some quando sai e aparece o aviso se não sair
      var atualizarAgendados = function () {
        pedirJson('/conversas/agendados?para=' + encodeURIComponent(f.dataset.para)).then(function (x) { if (x.s === 200) { pintarAgendados(x.d.html); } }).catch(function () {});
      };
      setInterval(function () { if (faixaAg.firstChild && !document.hidden) { atualizarAgendados(); } }, 30000);
    }
    if (btLiberar) { btLiberar.addEventListener('click', function () { liberar(function () { txt.focus(); }); }); }
    if (liberado) { txt.focus(); }
  }
  var busca = document.getElementById('zap-busca');
  function semAcento(s) { return s.toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g, ''); }
  function filtrarLista() {
    if (!busca) { return; }
    var t = semAcento(busca.value.trim()), n = t.replace(/\\D/g, '');
    document.querySelectorAll('#zap-itens .zap-item').forEach(function (a) {
      var b = a.getAttribute('data-busca') || '';
      a.hidden = !!t && b.indexOf(t) < 0 && !(n.length >= 3 && b.indexOf(n) >= 0);
    });
  }
  if (busca) { busca.addEventListener('input', filtrarLista); }
  // pedido 52: o fio é trocado por diferença. Bolha igual fica como está (foto não pisca, áudio não para); só entra
  // a nova e só é refeita a que mudou (tique, reação, edição). As provisórias saem conforme as de verdade chegam.
  var ultimaCheia = Date.now();
  // pedido 64: a foto não pisca. (1) Bolha que só mudou o tique (enviada, entregue, lida) é refeita, mas a imagem que
  // já estava carregada é reaproveitada: o nó antigo vai para dentro da bolha nova. (2) Quando a bolha de verdade
  // toma o lugar da provisória, a miniatura fica na tela até a foto do servidor terminar de carregar.
  function srcReal(im) { return im.getAttribute('data-src-real') || im.getAttribute('src'); }
  function manterImagens(o, n) {
    var velhas = Array.prototype.slice.call(o.querySelectorAll('img[src]'));
    Array.prototype.forEach.call(n.querySelectorAll('img[src]'), function (nova) {
      for (var i = 0; i < velhas.length; i++) {
        if (srcReal(velhas[i]) === nova.getAttribute('src') && nova.parentNode) {
          velhas[i].className = nova.className; nova.parentNode.replaceChild(velhas[i], nova); velhas.splice(i, 1); return;
        }
      }
    });
  }
  function herdarMiniatura(prov, real) {
    var mini = prov.querySelector('.zap-previa-foto'), im = real && real.querySelector('.zap-foto img[src]');
    if (!mini || !im) { return; }
    var src = im.getAttribute('src'), pre = new Image();
    function pronto() { if (im.getAttribute('data-src-real') === src) { im.removeAttribute('data-src-real'); im.src = src; } }
    im.setAttribute('data-src-real', src); im.src = mini.src;   // sem mexer nos outros atributos: depois de carregar, a bolha volta a ser igual à do servidor
    pre.onload = pronto; pre.onerror = pronto; pre.src = src;
  }
  function trocarFio(mn) {
    var lista = function (el) { return Array.prototype.slice.call(el.children); };
    var antes = {}, velhos = {}, provs = [];
    lista(msgs).forEach(function (o) { if (o.id && o.classList.contains('saida')) { antes[o.id] = 1; } });
    lista(msgs).forEach(function (o) { if (o.classList.contains('otimista')) { provs.push(o); } else if (o.id) { velhos[o.id] = o; } });
    var cur = msgs.firstElementChild;
    lista(mn).forEach(function (n) {
      n = document.importNode(n, true);
      while (cur && cur.classList.contains('otimista')) { cur = cur.nextElementSibling; }
      var o = n.id ? velhos[n.id] : null;
      if (o) {
        delete velhos[n.id];
        if (o === cur) { cur = cur.nextElementSibling; } else { msgs.insertBefore(o, cur); }
        if (o.outerHTML !== n.outerHTML) {
          var midia = o.querySelector('audio, video'), mo = o.querySelector('.zap-meta'), mnv = n.querySelector('.zap-meta');
          if (midia && !midia.paused && mo && mnv) { mo.innerHTML = mnv.innerHTML; } else { manterImagens(o, n); msgs.replaceChild(n, o); }
        }
      } else if (cur && !cur.id && cur.outerHTML === n.outerHTML) { cur = cur.nextElementSibling; }
      else { chegou(n); msgs.insertBefore(n, cur); }
    });
    while (cur) { var prox = cur.nextElementSibling; if (!cur.classList.contains('otimista')) { cur.remove(); } cur = prox; }
    // conta as próprias que não estavam antes (pelo id: o fio tem teto e a mais antiga pode ter saído na mesma leitura);
    // provisória já enviada há mais de 1 min sai de qualquer jeito, para nunca ficar duplicada
    var chegaram = 0, agora = Date.now(), novas = [];
    lista(msgs).forEach(function (o) { if (o.id && o.classList.contains('saida') && !antes[o.id]) { chegaram++; novas.push(o); } });
    provs.forEach(function (b) {
      var velha = b.classList.contains('saiu') && agora - (+b.getAttribute('data-em') || agora) > 60000;
      if (velha || (chegaram > 0 && b.classList.contains('saiu'))) {
        if (!velha) { chegaram--; herdarMiniatura(b, novas.filter(function (o) { return o.querySelector('.zap-foto img[src]') && !o.querySelector('img[data-src-real]'); })[0]); }
        b.remove();
      } else { msgs.appendChild(b); }
    });
  }
  // pedido 84: a bolha nova entra com a animação curta; a marca sai logo, para a bolha voltar a ser igual à do servidor
  // (senão a próxima leitura a acharia diferente e a refaria)
  function chegou(el) { el.classList.add('chegou'); setTimeout(function () { el.classList.remove('chegou'); }, 400); }
  // pedido 84: a lista era trocada inteira (innerHTML) a cada leitura e todas as fotos piscavam. Agora a troca é por
  // linha (data-jid): linha igual fica como está, a que mudou é refeita mantendo a foto, a ordem nova é feita movendo nós.
  function trocarLista(li, lt) {
    if (!li.querySelector(':scope > .zap-item') || !lt.querySelector(':scope > .zap-item')) { lt.innerHTML = li.innerHTML; return; }
    var velhos = {};
    Array.prototype.forEach.call(lt.children, function (o) { if (o.classList.contains('zap-item')) { velhos[o.getAttribute('data-jid')] = o; } });
    var cur = lt.firstElementChild;
    Array.prototype.slice.call(li.children).forEach(function (n) {
      var j = n.classList.contains('zap-item') ? n.getAttribute('data-jid') : null, o = j ? velhos[j] : null;
      n = document.importNode(n, true);
      if (o) {
        delete velhos[j];
        var oculto = o.hidden; o.hidden = false; var igual = o.outerHTML === n.outerHTML; o.hidden = oculto;
        if (!igual) { manterImagens(o, n); lt.replaceChild(n, o); if (cur === o) { cur = n; } o = n; }
        if (o === cur) { cur = cur.nextElementSibling; } else { lt.insertBefore(o, cur); }
      } else if (cur && !cur.classList.contains('zap-item') && cur.outerHTML === n.outerHTML) { cur = cur.nextElementSibling; }
      else { lt.insertBefore(n, cur); }
    });
    while (cur) { var prox = cur.nextElementSibling; cur.remove(); cur = prox; }
  }
  function atualizar(forcar, fica, eco) {
    // pedido 51: depois de um envio o tique muda em segundos (enviada, entregue); duas leituras extras, sem rolar
    if (forcar && !eco) { [2500, 7000].forEach(function (ms) { setTimeout(function () { atualizar(false, true, true); }, ms); }); }
    if (document.visibilityState !== 'visible' && !forcar) { return; }
    ultimaCheia = Date.now();
    var naFrente = document.visibilityState === 'visible' && (!document.hasFocus || document.hasFocus());
    fetch(location.href, {credentials: 'same-origin', headers: naFrente ? {} : {'X-Greg-Fundo': '1'}}).then(function (r) { return r.ok ? r.text() : ''; }).then(function (h) {
      window.vigiaOffline(false);
      if (!h) { return; }
      var doc = new DOMParser().parseFromString(h, 'text/html');
      var li = doc.getElementById('zap-itens'), lt = document.getElementById('zap-itens');
      // D-48: com o modo Selecionar ligado ou um menu de linha aberto, a lista não é trocada (perderia as marcas).
      var travada = document.querySelector('.zap-lista.selecionando, .zap-item.menu-aberto');
      if (li && lt && !travada && li.innerHTML !== lt.innerHTML) { trocarLista(li, lt); filtrarLista(); }
      var sn = doc.getElementById('zap-status-lista'), st = document.getElementById('zap-status-lista');
      if (sn && st && sn.innerHTML !== st.innerHTML) { st.innerHTML = sn.innerHTML; }
      var mn = doc.getElementById('zap-msgs');
      if (mn && msgs && mn.getAttribute('data-assinatura') !== msgs.getAttribute('data-assinatura')) {
        var noFim = msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 80;
        trocarFio(mn); msgs.setAttribute('data-assinatura', mn.getAttribute('data-assinatura'));
        if (!fica && (noFim || forcar)) { rolar(); }
      }
    }).catch(function () { window.vigiaOffline(true); });
  }

  // ---- D-48: menus ··· (cabeçalho e topo da lista) em passos, menu da linha (··· ou botão direito) e modo Selecionar.
  // Os <details class="menu"> são os da base (ela fecha ao clicar fora, com Esc e ao escolher um item direto);
  // aqui só os passos, o menu flutuante da linha e as marcas.
  var lista = document.querySelector('.zap-lista'), itens = document.getElementById('zap-itens');
  // pedido 84: abrir outra conversa recarrega a página e a lista voltava ao topo. A rolagem fica guardada na aba
  // (sessionStorage, 5 min) e volta ao abrir. Sem armazenamento a lista só abre no topo, como antes.
  if (itens) {
    try {
      var rol = JSON.parse(sessionStorage.getItem('greg.zap.lista') || 'null');
      if (rol && Date.now() - rol.em < 300000 && rol.y > 0) { itens.scrollTop = rol.y; }
    } catch (e) {}
    window.addEventListener('pagehide', function () {
      try { sessionStorage.setItem('greg.zap.lista', JSON.stringify({y: itens.scrollTop, em: Date.now()})); } catch (e) {}
    });
  }
  var menuLinha = document.getElementById('zap-menu-linha');
  function irPasso(raiz, nome) {
    if (!raiz) { return; }
    raiz.querySelectorAll('.zap-passo').forEach(function (p) { p.hidden = p.getAttribute('data-passo') !== nome; });
    var foco = raiz.querySelector('.zap-passo:not([hidden]) input:not([type=hidden]), .zap-passo:not([hidden]) [role=menuitem], .zap-passo:not([hidden]) button');
    if (foco) { foco.focus(); }
  }
  function fecharMenuLinha() {
    if (!menuLinha || menuLinha.hidden) { return; }
    menuLinha.hidden = true; irPasso(menuLinha, 'menu');
    document.querySelectorAll('.zap-item.menu-aberto').forEach(function (a) { a.classList.remove('menu-aberto'); });
  }
  function fecharMenus() {
    document.querySelectorAll('details.menu[open]').forEach(function (d) { d.removeAttribute('open'); });
    fecharMenuLinha();
  }
  function abrirMenuLinha(item) {
    fecharMenus();
    item.classList.add('menu-aberto');
    document.getElementById('zap-ml-abrir').setAttribute('href', item.getAttribute('href'));
    document.getElementById('zap-ml-titulo').textContent = 'Deixar “' + item.getAttribute('data-nome') + '” fora do Greg?';
    menuLinha.querySelector('input[name=c]').value = item.getAttribute('data-jid');
    menuLinha.hidden = false;
    // posição relativa ao painel da lista (o menu é filho dele): embaixo da linha, alinhado à direita; sobe se não couber
    var r = item.getBoundingClientRect(), L = lista.getBoundingClientRect(), w = menuLinha.offsetWidth, h = menuLinha.offsetHeight;
    var left = Math.max(4, r.right - w - 8 - L.left), top = r.bottom - 6 - L.top;
    if (top + h > L.height - 8) { top = Math.max(4, r.top - L.top - h + 6); }
    menuLinha.style.left = left + 'px'; menuLinha.style.top = top + 'px';
    irPasso(menuLinha, 'menu');
  }
  // pedido 75: "Ler agora" também daqui; a IA lê o que chegou e joga as demandas sem esperar a meia hora
  var lerAgora = document.getElementById('zap-ler-agora');
  if (lerAgora) {
    lerAgora.addEventListener('click', function () {
      var d = lerAgora.closest('details'); if (d) { d.open = false; }
      window.vigiaPost('/conexao/ler-agora', {volta: '/conversas'}).then(function (j) {
        window.vigiaToast(j.toast || j.erro || (j.ok ? 'Leitura disparada.' : 'Não deu para disparar a leitura.'));
      }).catch(function () { window.vigiaToast('O painel não respondeu; a leitura não foi disparada.'); });
    });
  }
  document.querySelectorAll('#zap-menu-topo, #zap-menu-cab').forEach(function (d) {
    d.addEventListener('toggle', function () { if (d.open) { fecharMenuLinha(); } else { irPasso(d, 'menu'); } });
  });
  document.addEventListener('click', function (ev) {
    var ir = ev.target.closest('[data-passo-ir]');
    if (ir) { ev.preventDefault(); irPasso(ir.closest('.zap-menu-lista'), ir.getAttribute('data-passo-ir')); return; }
    if (menuLinha && !menuLinha.hidden && !menuLinha.contains(ev.target) && !ev.target.closest('.zap-mais')) { fecharMenuLinha(); }
  });
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') { fecharMenuLinha(); } });
  // modo Selecionar: as caixas pertencem ao formulário da barra (atributo form); clicar na linha marca em vez de abrir
  var selBtn = document.getElementById('zap-selecionar'), selCancelar = document.getElementById('zap-sel-cancelar');
  var barra = document.getElementById('form-fora-varios'), selConta = document.getElementById('zap-sel-conta');
  var selFora = document.getElementById('zap-sel-fora'), tituloLista = document.getElementById('zap-titulo-lista');
  function selecionando() { return !!(lista && lista.classList.contains('selecionando')); }
  var armado = null;
  function desarmar() { clearTimeout(armado); armado = null; if (selFora) { selFora.textContent = selFora.getAttribute('data-rotulo'); } }
  function contar() {
    var n = itens ? itens.querySelectorAll('.zap-marca:checked').length : 0;
    if (selConta) { selConta.textContent = n === 0 ? 'Nenhuma marcada' : n + (n > 1 ? ' marcadas' : ' marcada'); }
    if (selFora) { selFora.disabled = n === 0; }
    if (tituloLista && selecionando()) { tituloLista.textContent = n === 0 ? 'Selecionar' : n + (n > 1 ? ' marcadas' : ' marcada'); }
    desarmar();
  }
  function marcar(item, valor) {
    var cb = item.querySelector('.zap-marca');
    if (!cb) { return; }
    cb.checked = valor === undefined ? !cb.checked : valor;
    item.classList.toggle('marcada', cb.checked);
    contar();
  }
  function selecionar(ligar) {
    if (!lista) { return; }
    fecharMenus();
    lista.classList.toggle('selecionando', ligar);
    if (barra) { barra.hidden = !ligar; }
    if (selCancelar) { selCancelar.hidden = !ligar; }
    if (!ligar) {
      itens.querySelectorAll('.zap-item').forEach(function (a) { marcar(a, false); });
      if (tituloLista) { tituloLista.textContent = 'Conversas'; }
    }
    contar();
  }
  if (selBtn) { selBtn.addEventListener('click', function () { selecionar(true); }); }
  if (selCancelar) { selCancelar.addEventListener('click', function () { selecionar(false); }); }
  if (barra && selFora) {
    /* dois cliques no mesmo botão em vez de modal: o primeiro troca o rótulo pela pergunta, o segundo (em 6 s) confirma */
    barra.addEventListener('submit', function (ev) {
      if (armado) { desarmar(); return; }
      ev.preventDefault();
      selFora.textContent = selFora.getAttribute('data-confirma');
      armado = setTimeout(desarmar, 6000);
    });
  }
  if (itens && menuLinha) {
    itens.addEventListener('click', function (ev) {
      var item = ev.target.closest('.zap-item');
      if (!item) { return; }
      if (selecionando()) { ev.preventDefault(); marcar(item); return; }   // inclusive no clique da própria caixa (a âncora navegaria)
      if (ev.target.closest('.zap-mais')) { ev.preventDefault(); ev.stopPropagation(); abrirMenuLinha(item); }
    });
    itens.addEventListener('keydown', function (ev) {
      var mais = ev.target.closest('.zap-mais');
      if (mais && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); ev.stopPropagation(); abrirMenuLinha(mais.closest('.zap-item')); }
    });
    itens.addEventListener('contextmenu', function (ev) {
      var item = ev.target.closest('.zap-item');
      if (!item || selecionando()) { return; }
      ev.preventDefault(); abrirMenuLinha(item);
    });
  }
  // pedido 52: em vez de buscar a tela inteira a cada 15 s, a página pergunta o pulso (uma consulta curta) a cada 5 s
  // e só busca a tela quando ele muda. A cada 60 s busca de qualquer jeito (transcrição e resumo da IA não mexem no
  // pulso). Sem pulso (banco fora), volta ao ritmo antigo.
  var ultimoPulso = null;
  function pulsar() {
    if (document.visibilityState !== 'visible') { return; }
    if (Date.now() - ultimaCheia > 60000) { atualizar(); return; }
    pedirJson('/conversas/pulso').then(function (x) {
      window.vigiaOffline(false);
      var p = x.s === 200 && x.d ? x.d.p : '';
      if (!p) { if (Date.now() - ultimaCheia > 15000) { atualizar(); } return; }
      if (ultimoPulso !== null && p !== ultimoPulso) { atualizar(); }
      ultimoPulso = p;
    }).catch(function () { window.vigiaOffline(true); });
  }
  setInterval(pulsar, 5000);
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') { atualizar(); } });
  estado(); setInterval(estado, 30000);

  var nova = document.getElementById('zap-nova');
  if (nova) {
    var num = document.getElementById('nova-num'), res = document.getElementById('nova-res');
    var salvar = document.getElementById('nova-salvar'), nomeL = document.getElementById('nova-nome-l');
    var conferido = null;
    salvar.addEventListener('change', function () { nomeL.hidden = !salvar.checked; });
    num.addEventListener('input', function () { conferido = null; res.textContent = ''; res.className = 'zap-res meta-linha'; });
    function conferir(depois) {
      if (!num.value.trim()) { dizer('Informe o número.', true); return; }
      res.textContent = 'Conferindo…'; res.className = 'zap-res meta-linha';
      postJson('/conversas/verificar', {numero: num.value}).then(function (x) {
        if (x.s !== 200) { res.textContent = (x.d && x.d.mensagem) || 'Não consegui conferir agora.'; res.className = 'zap-res meta-linha ruim'; return; }
        if (!x.d.existe) { res.textContent = 'Este número não tem WhatsApp: ' + x.d.numero; res.className = 'zap-res meta-linha ruim'; return; }
        conferido = x.d;
        res.textContent = 'Tem WhatsApp: ' + x.d.numero + (x.d.conversa ? '. Já tem conversa, a mensagem cai nela.' : '');
        res.className = 'zap-res meta-linha ok';
        if (depois) { depois(); }
      }).catch(function () { res.textContent = 'O painel não respondeu. Tente conferir de novo.'; res.className = 'zap-res meta-linha ruim'; });
    }
    document.getElementById('nova-conferir').addEventListener('click', function () { conferir(); });
    nova.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var t = document.getElementById('nova-texto').value;
      if (!t.trim()) { dizer('Escreva a mensagem.', true); return; }
      function mandar() {
        var corpo = {para: conferido.jid, texto: t};
        if (salvar.checked && document.getElementById('nova-nome').value.trim()) { corpo.nome = document.getElementById('nova-nome').value; }
        enviar(corpo, function (d) { location.href = '/conversas?c=' + encodeURIComponent(d.jid || conferido.jid); });
      }
      if (conferido) { mandar(); } else { conferir(mandar); }
    });
    num.focus();
  }
})();
</script>"""


def _f(req) -> str:
    f = c.query(req, "f", "todas")
    return f if f in dict(FILTROS) else "todas"


def _aviso_fora(req) -> tuple[str, str]:
    """Volta do POST /bloqueios com volta=/conversas: `fora=N` (quantas saíram) e `falhou=1` (o banco parou no meio).
    Devolve (aviso inline já escapado, texto do toast): sucesso vira toast; falha fica inline, vermelha, no topo da lista."""
    n = c.query(req, "fora")
    if not n.isdigit():
        return "", ""
    n = int(n)
    if c.query(req, "falhou") == "1":
        return (f'<p class="zap-aviso-lista ruim" role="alert">O banco falhou no meio: {n} saíram; o resto segue dentro. Tente de novo.</p>', "")
    if not n:
        return "", "Nenhuma conversa saiu."
    base = "1 conversa fora do Greg." if n == 1 else f"{n} conversas fora do Greg."
    return "", base + " Desfazer vale só para o que chegar depois."


def conversas_get(req):
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    f, q = _f(req), c.query(req, "q")
    ativo = c.query(req, "c")
    if ativo and not wa.JID_OK.match(ativo):
        ativo = ""
    # pedido 60: abrir a conversa apaga a bolinha dela. A atualização automática com a janela em segundo plano avisa
    # (X-Greg-Fundo) e não conta como "vi".
    fundo = str((req.cabecalhos or {}).get("x-greg-fundo") or (req.cabecalhos or {}).get("X-Greg-Fundo") or "") == "1"
    todas, problema, bloq = carregar(req.ctx, vista="" if fundo else ativo)
    inline, toast = _aviso_fora(req)
    lista = html_lista(filtrar(todas, f, q), problema, ativo, f, q, todas, aviso_html=inline)
    fio = html_fio(req.ctx, ativo, todas, bloqueados=bloq, limite=limite_fio(c.query(req, "n"))) if ativo else html_vazio()
    titulo = next((x["nome"] for x in todas if x["jid"] == ativo), "Conversas")
    return c.pagina(titulo, _CSS + lista + fio + _JS, "/conversas", ctx=req.ctx, toast=toast)


def pulso_get(req):
    """GET /conversas/pulso -> {"p": "..."}: muda quando há o que redesenhar (pedido 52). A página pergunta isto a
    cada poucos segundos e só busca a tela inteira quando o valor muda; sem banco ou sem pareamento devolve ""."""
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    par, psql = desde_leitura(req.ctx), getattr(req.ctx, "psql", None)
    if par is None or psql is None:
        return json_resposta({"p": ""})
    try:
        return json_resposta({"p": wa.pulso(psql, desde=par)})
    except (VigiaErro, ValueError, IndexError) as e:
        log.warning("pulso indisponível: %s", causa(e))
        return json_resposta({"p": ""})


def nova_get(req):
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    todas, problema = carregar_lista(req.ctx)
    corpo = _CSS + html_lista(todas, problema, "", "todas", "", todas) + html_nova(req.ctx) + _JS
    return c.pagina("Nova mensagem", corpo, "/conversas", ctx=req.ctx)


# ------------------------------------------------------------------ ações

def _json(status: int, mensagem: str, **extra):
    return json_resposta(dict(mensagem=mensagem, **extra), status)


def verificar_post(req):
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    d = wa.normalizar_numero(corpo.get("numero", ""))
    if not (8 <= len(d) <= 15):
        return _json(400, "Número fora do padrão: use DDD e número, ou +DDI para fora do Brasil.")
    try:
        r = req.ctx.evo.numero_no_whatsapp(d)
    except VigiaErro as e:
        log.warning("conferência de número falhou: %s", causa(e))
        return _json(502, "A Evolution não respondeu; confira a Conexão.")
    if not r:
        return _json(502, "A Evolution não respondeu; confira a Conexão.")
    jid = r["jid"] if wa.JID_OK.match(r.get("jid") or "") else f"{d}@s.whatsapp.net"
    existe_conversa = False
    par, psql = desde_leitura(req.ctx), getattr(req.ctx, "psql", None)
    if r["exists"] and par is not None and psql is not None:
        try:
            existe_conversa = bool(wa.mensagens(psql, jid, desde=par, limite=1))
        except (VigiaErro, ValueError):
            existe_conversa = False
    return json_resposta({"existe": r["exists"], "jid": jid, "numero": wa.numero_formatado(jid if r["exists"] else d),
                          "conversa": existe_conversa, "nome": req.ctx.banco.apelidos().get(jid, "")})


def _citada_do_pedido(req, para: str, msg_id):
    """(key, None) da mensagem a citar ou reagir, (None, None) sem pedido, (None, resposta 4xx) se o id não é desta
    conversa ou já saiu da janela de leitura."""
    if msg_id in (None, ""):
        return None, None
    par, psql = desde_leitura(req.ctx), getattr(req.ctx, "psql", None)
    if not isinstance(msg_id, str) or not wa.MSG_ID_OK.match(msg_id) or par is None or psql is None:
        return None, _json(400, "A mensagem escolhida não foi encontrada nesta conversa.")
    try:
        key = wa.chave_na_conversa(psql, msg_id, para, desde=par)
    except (VigiaErro, ValueError) as e:
        log.warning("mensagem de referência indisponível: %s", causa(e))
        return None, _json(502, "O banco da Evolution não respondeu; nada foi enviado.")
    if key is None:
        return None, _json(400, "A mensagem escolhida não foi encontrada nesta conversa.")
    return key, None


def reagir_post(req):
    """POST /conversas/reagir {para, id, emoji}: reage a uma mensagem (emoji vazio tira a reação). É um envio: mesma
    trava do texto (D-39) e um clique por reação (R-9). Guarda só o hash."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    if not getattr(getattr(req.ctx, "tokens", None), "envio", ""):
        return _json(503, "Envio desligado: o token de envio não está configurado.")
    if not servicos(req.ctx).liberacao.liberada():
        return _json(423, "Envio travado: confirme com o Touch ID.", liberar=True)
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    para, emoji = corpo.get("para"), corpo.get("emoji")
    if not isinstance(para, str) or not wa.JID_OK.match(para) or para.endswith("@lid"):
        return _json(400, "Destino inválido.")
    if not isinstance(emoji, str) or len(emoji) > 16 or any(ch.isascii() for ch in emoji):
        return _json(400, "Reação inválida: use um emoji.")
    key, recusa = _citada_do_pedido(req, para, corpo.get("id") or "?")
    if recusa:
        return recusa
    h = hashlib.sha256(f"{key.get('id')}:{emoji}".encode("utf-8")).hexdigest()
    try:
        mid = req.ctx.evo.reagir(key, emoji)
    except EnvioIncerto:
        _registrar(req, para=para, hash_texto=h, por="painel:conversa:reacao:incerto")
        log.warning("reação incerta")
        return _json(202, "A reação pode ter saído. Confira no celular antes de repetir.", incerto=True)
    except VigiaErro as e:
        log.warning("reação falhou: %s", causa(e))
        return _json(502, _motivo_da_falha(e))
    _registrar(req, para=para, hash_texto=h, por="painel:conversa:reacao")
    return json_resposta({"enviadas": 1, "id": mid, "jid": para})


def _motivo_da_falha(e: BaseException) -> str:
    """Auditoria T5 (lote 4 do pedido 63): "recusou ou está fora" juntava causas com saídas diferentes. O cliente da
    Evolution já separa: fora do ar ou sem resposta (EvolutionIndisponivel), chave recusada (NaoAutorizado) e recusa
    do pedido (4xx: número inválido, mensagem fora do prazo, formato)."""
    from app.erros import EvolutionIndisponivel, NaoAutorizado
    if isinstance(e, EvolutionIndisponivel):
        return "A ponte com o WhatsApp não respondeu; nada foi enviado. Confira a tela Conexão e tente de novo."
    if isinstance(e, NaoAutorizado):
        return "A ponte com o WhatsApp recusou a chave do Greg; nada foi enviado. Confira a tela Conexão."
    return "O WhatsApp recusou este envio; nada foi enviado. Confira o número ou o arquivo antes de repetir."


def _registrar(req, **campos) -> None:
    """Auditoria C3 (lote 3 do pedido 63): o registro do envio (só hash) roda DEPOIS que a mensagem saiu. Se o SQLite
    falhar aqui, a resposta continua sendo a do envio: responder 500 faria a tela dizer "Falhou", devolver o texto
    ao campo e convidar a mandar de novo o que já chegou. A falha vai para o log com código para achar depois."""
    try:
        req.ctx.banco.registrar_envio(**campos)
    except Exception as e:  # noqa: BLE001
        log.error("envio saiu mas o registro falhou (por=%s): %s", campos.get("por", "?"), causa(e))


def _pode_enviar(req):
    """None se o pedido pode seguir; senão a resposta de recusa (método, origem, token de envio, trava)."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    if not getattr(getattr(req.ctx, "tokens", None), "envio", ""):
        return _json(503, "Envio desligado: o token de envio não está configurado.")
    if not servicos(req.ctx).liberacao.liberada():
        return _json(423, "Envio travado: confirme com o Touch ID.", liberar=True)
    return None


def _destino_ok(jid) -> bool:
    return isinstance(jid, str) and bool(wa.JID_OK.match(jid)) and not jid.endswith("@lid")


def _mensagem_do_pedido(req, conversa: str, msg_id):
    """(mensagem, None) com key, tipo, hora e conteúdo, ou (None, resposta 4xx/502)."""
    par, psql = desde_leitura(req.ctx), getattr(req.ctx, "psql", None)
    if not isinstance(msg_id, str) or not wa.MSG_ID_OK.match(msg_id) or par is None or psql is None:
        return None, _json(400, "A mensagem escolhida não foi encontrada nesta conversa.")
    try:
        achada = wa.mensagem_na_conversa(psql, msg_id, conversa, desde=par)
    except (VigiaErro, ValueError) as e:
        log.warning("mensagem de referência indisponível: %s", causa(e))
        return None, _json(502, "O banco da Evolution não respondeu; nada foi enviado.")
    if achada is None:
        return None, _json(400, "A mensagem escolhida não foi encontrada nesta conversa.")
    return achada, None


def _concluir_envio(req, para: str, h: str, rotulo: str, acao):
    """Roda o envio e responde como os outros: 200, 202 (pode ter saído) ou 502. Guarda só o hash."""
    try:
        mid = acao()
    except EnvioIncerto:
        _registrar(req, para=para, hash_texto=h, por=f"painel:conversa:{rotulo}:incerto")
        log.warning("envio incerto (%s)", rotulo)
        return _json(202, "Pode ter saído. Confira no celular antes de repetir.", incerto=True)
    except MidiaInvalida:
        return _json(502, "Não consegui baixar o arquivo dessa mensagem; nada foi enviado.")
    except VigiaErro as e:
        log.warning("envio falhou (%s): %s", rotulo, causa(e))
        return _json(502, _motivo_da_falha(e))
    _registrar(req, para=para, hash_texto=h, por=f"painel:conversa:{rotulo}")
    return json_resposta({"enviadas": 1, "id": mid, "jid": para})


def editar_post(req):
    """POST /conversas/editar {para, id, texto}: troca o texto de uma mensagem própria, até 15 min depois do envio
    (regra do WhatsApp). É um envio: mesma trava (D-39), um clique (R-9), só o hash."""
    if (e := _pode_enviar(req)):
        return e
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    para, texto = corpo.get("para"), corpo.get("texto")
    if not _destino_ok(para):
        return _json(400, "Destino inválido.")
    if not isinstance(texto, str) or not texto.strip() or len(texto) > wa.LIMITE_TEXTO:
        return _json(400, f"Texto vazio ou acima de {wa.LIMITE_TEXTO} caracteres.")
    msg, recusa = _mensagem_do_pedido(req, para, corpo.get("id"))
    if recusa:
        return recusa
    if not wa._de_mim(msg["key"]) or msg["tipo_msg"] not in _MSG_TEXTO:
        return _json(400, "Só dá para editar mensagem de texto enviada por você.")
    if not (0 <= time.time() - msg["ts"] <= JANELA_EDICAO):
        return _json(400, "O WhatsApp só deixa editar até 15 minutos depois do envio.")
    numero = para if para.endswith("@g.us") else wa.digitos(para)
    h = hashlib.sha256(texto.encode("utf-8")).hexdigest()
    return _concluir_envio(req, para, h, "edicao", lambda: req.ctx.evo.editar_texto(numero, msg["key"], texto))


def apagar_post(req):
    """POST /conversas/apagar {para, id}: apaga para todos uma mensagem enviada pelo dono (D-63). Sem volta. Mesma
    trava (D-39); a tela pede dois cliques. O WhatsApp decide o prazo; a Evolution marca a linha como DELETED."""
    if (e := _pode_enviar(req)):
        return e
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    para = corpo.get("para")
    if not _destino_ok(para):
        return _json(400, "Destino inválido.")
    msg, recusa = _mensagem_do_pedido(req, para, corpo.get("id"))
    if recusa:
        return recusa
    if not wa._de_mim(msg["key"]):
        return _json(400, "Só dá para apagar para todos uma mensagem enviada por você.")
    h = hashlib.sha256(f"apagar:{msg['key'].get('id')}".encode("utf-8")).hexdigest()
    resp = _concluir_envio(req, para, h, "apagar", lambda: req.ctx.evo.apagar_para_todos(msg["key"]) or "apagada")
    if resp.status == 200:
        _esquecer_midia(req.ctx, corpo["id"])
    return resp


def encaminhar_post(req):
    """POST /conversas/encaminhar {de, id, para}: reenvia uma mensagem (texto, foto, vídeo, arquivo ou áudio) para outra
    conversa. A Evolution 2.3.7 não tem rota de encaminhar, então chega como mensagem nova, sem o selo. Mesma trava."""
    if (e := _pode_enviar(req)):
        return e
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    de, para = corpo.get("de"), corpo.get("para")
    if not _destino_ok(para) or not isinstance(de, str) or not wa.JID_OK.match(de):
        return _json(400, "Destino inválido.")
    msg, recusa = _mensagem_do_pedido(req, de, corpo.get("id"))
    if recusa:
        return recusa
    cont = wa.conteudo(msg["message"], msg["tipo_msg"])
    if cont["tipo"] not in _ENCAMINHAVEL:
        return _json(400, "Esse tipo de mensagem não dá para encaminhar pelo Greg.")
    numero = para if para.endswith("@g.us") else wa.digitos(para)
    evo = req.ctx.evo
    if cont["tipo"] == "texto":
        texto = cont["texto"]
        if not texto.strip():
            return _json(400, "Mensagem vazia.")
        return _concluir_envio(req, para, hashlib.sha256(texto.encode("utf-8")).hexdigest(), "encaminhar",
                               lambda: evo.enviar_texto(numero, texto))
    obtida = _obter_midia(req.ctx, corpo["id"], msg["key"], msg["tipo_msg"], msg.get("message"))
    if isinstance(obtida, Resposta):
        return _json(502, "Não consegui baixar o arquivo dessa mensagem; nada foi enviado.")
    dados, tipo = obtida
    if len(dados) > LIMITE_MIDIA:
        return _json(413, "Arquivo acima de 16 MB.")
    legenda = cont["texto"][:LIMITE_LEGENDA]
    if cont["tipo"] == "audio":
        acao = lambda: evo.enviar_audio(numero, dados)
    elif cont["tipo"] == "imagem":
        mime = _mime_foto(dados) or "image/jpeg"
        acao = lambda: evo.enviar_midia(numero, "image", dados, nome="foto." + _EXT.get(mime, "jpg"), mime=mime, legenda=legenda)
    elif cont["tipo"] == "video":
        acao = lambda: evo.enviar_midia(numero, "video", dados, nome="video.mp4", mime="video/mp4", legenda=legenda)
    else:
        nome = _nome_arquivo(cont["extra"], "arquivo")
        acao = lambda: evo.enviar_midia(numero, "document", dados, nome=nome,
                                        mime=mimetypes.guess_type(nome)[0] or "application/octet-stream", legenda=legenda)
    return _concluir_envio(req, para, hashlib.sha256(dados).hexdigest(), "encaminhar", acao)


def enviar_post(req):
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    if not getattr(getattr(req.ctx, "tokens", None), "envio", ""):
        return _json(503, "Envio desligado: o token de envio não está configurado.")
    if not servicos(req.ctx).liberacao.liberada():
        return _json(423, "Envio travado: confirme com o Touch ID.", liberar=True)
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    para, texto = corpo.get("para"), corpo.get("texto")
    if not isinstance(para, str) or not wa.JID_OK.match(para) or para.endswith("@lid"):
        return _json(400, "Destino inválido.")
    if not isinstance(texto, str) or not texto.strip() or len(texto) > wa.LIMITE_TEXTO:
        return _json(400, f"Texto vazio ou acima de {wa.LIMITE_TEXTO} caracteres.")
    numero = para if para.endswith("@g.us") else wa.digitos(para)
    nome = corpo.get("nome")
    salvar_nome = isinstance(nome, str) and bool(nome.strip()) and not para.endswith("@g.us")
    citar, recusa = _citada_do_pedido(req, para, corpo.get("citar"))
    if recusa:
        return recusa
    h = hashlib.sha256(texto.encode("utf-8")).hexdigest()
    try:
        mid = req.ctx.evo.enviar_texto(numero, texto, citar=citar)
    except EnvioIncerto:
        _registrar(req, para=para, hash_texto=h, por="painel:conversa:incerto")
        if salvar_nome:
            req.ctx.banco.definir_apelido(para, nome)      # pode ter saído: o contato fica salvo
        log.warning("envio incerto")
        return _json(202, "A mensagem pode ter saído. Confira no celular antes de repetir.", incerto=True)
    except VigiaErro as e:
        log.warning("envio falhou: %s", causa(e))
        return _json(502, _motivo_da_falha(e))   # nem o contato é salvo (pedido 9)
    _registrar(req, para=para, hash_texto=h, por="painel:conversa")
    contato_salvo = True
    if salvar_nome:
        try:
            req.ctx.banco.definir_apelido(para, nome)      # "criar contato e enviar": o contato nasce com a mensagem
        except Exception as e:  # noqa: BLE001  (C3: a mensagem já saiu; a resposta não pode virar "Falhou")
            log.error("mensagem saiu mas o contato não foi salvo: %s", causa(e))
            contato_salvo = False
    return json_resposta({"enviadas": 1, "id": mid, "jid": para, **({} if contato_salvo else {"contato_salvo": False})})


def apelido_post(req):
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    jid = str(corpo.get("c") or "")
    if not wa.JID_OK.match(jid) or jid.endswith("@g.us"):
        return c.texto_simples("conversa inválida", 400)
    req.ctx.banco.definir_apelido(jid, str(corpo.get("nome") or ""))
    return c.redirecionar("/conversas?c=" + _q(jid))


_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "audio/mp4": "m4a", "audio/ogg": "ogg",
        "audio/mpeg": "mp3", "video/mp4": "mp4", "application/octet-stream": "bin"}
_MSG_IMAGEM = ("imageMessage", "stickerMessage")
_MSG_AUDIO = ("audioMessage", "pttMessage")
_MSG_VIDEO = ("videoMessage", "ptvMessage")
_MSG_DOC = ("documentMessage", "documentWithCaptionMessage")
_NOME_RUIM = re.compile(r"[^\w .()\-]+")


def _nome_arquivo(bruto: str, padrao: str) -> str:
    """Nome para o Content-Disposition: só letras, números, espaço e . ( ) - _; nunca barra, aspas ou quebra de linha."""
    nome = _NOME_RUIM.sub("_", str(bruto or "")).strip(" .")[:120]
    return nome or padrao


def _anexo(resp: Resposta, nome: str) -> Resposta:
    resp.cabecalhos = list(resp.cabecalhos) + [
        ("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(nome, safe="")),
        ("X-Content-Type-Options", "nosniff")]
    return resp


def _servir(req, dados: bytes, tipo: str, mid: str) -> Resposta:
    """Foto, áudio e vídeo abrem na página; documento só sai como download (nunca é interpretado na origem do painel).
    Com ?baixar=1 qualquer um vira download, com nome seguro."""
    doc = tipo == "application/octet-stream"
    resp = _faixa(req, dados, tipo)
    if doc or c.query(req, "baixar") == "1":
        padrao = f"greg-{mid[-8:]}.{_EXT.get(tipo, 'bin')}"
        return _anexo(resp, _nome_arquivo(c.query(req, "n"), padrao) if doc else padrao)
    return resp
_TIPO_EXT = {v: k for k, v in _EXT.items()}


def _faixa(req, dados: bytes, tipo: str) -> Resposta:
    """Responde com suporte a Range (o WebKit pede bytes=0-1 antes de tocar áudio)."""
    rng = str((req.cabecalhos or {}).get("range") or "")
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
    total = len(dados)
    if m and total and (m.group(1) or m.group(2)):
        if m.group(1):
            ini = int(m.group(1))
            fim = min(int(m.group(2)) if m.group(2) else total - 1, total - 1)
        else:
            ini, fim = max(0, total - int(m.group(2))), total - 1
        if ini > fim or ini >= total:
            return Resposta(416, b"", tipo, [("Content-Range", f"bytes */{total}")])
        return Resposta(206, dados[ini:fim + 1], tipo, [("Content-Range", f"bytes {ini}-{fim}/{total}"),
                                                        ("Accept-Ranges", "bytes")])
    return Resposta(200, dados, tipo, [("Accept-Ranges", "bytes")])


def midia_get(req):
    """Imagem/figurinha/áudio de uma mensagem. Cache em dados/caixa/.midia/<id>.<ext> (a limpeza semanal apaga)."""
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    mid = c.query(req, "m")
    par = desde_leitura(req.ctx)
    psql = getattr(req.ctx, "psql", None)
    if not wa.MSG_ID_OK.match(mid) or par is None or psql is None:
        return c.texto_simples("não encontrado", 404)
    try:
        # segurança r01 (B3): o banco é consultado sempre, mesmo com o arquivo em cache. Mensagem apagada pelo
        # remetente (D-63) ou de conversa bloqueada (D-41) deixa de ser servida na hora e o arquivo guardado some.
        achou = wa.chave_da_mensagem(psql, mid, desde=par)
    except (VigiaErro, ValueError):
        return c.texto_simples("banco indisponível", 503)
    if not achou or achou[1] == "bloqueado":
        _esquecer_midia(req.ctx, mid)
        return c.texto_simples("não encontrado", 404)
    if c.query(req, "capa") == "1":          # pedido 59: capa do vídeo, direto do banco (não baixa o vídeo)
        try:
            capa = wa.capa_do_video(psql, mid)
        except (VigiaErro, ValueError):
            capa = None
        if capa is None:
            return c.texto_simples("não encontrado", 404)
        return Resposta(200, capa, "image/jpeg", [("Cache-Control", "private, max-age=3600")])
    obtida = _obter_midia(req.ctx, mid, achou[0], achou[1], achou[2] if len(achou) > 2 else None)
    if isinstance(obtida, Resposta):
        return obtida
    return _servir(req, obtida[0], obtida[1], mid)


def _esquecer_midia(ctx, mid: str) -> None:
    """Apaga do cache do painel o arquivo de uma mensagem que o remetente apagou (D-63)."""
    achado = _midia_em_cache(ctx, mid)
    if achado is not None:
        try:
            achado.unlink()
        except OSError:
            log.warning("cache de mídia apagada não removido")


def _midia_em_cache(ctx, mid: str):
    """O arquivo já baixado desta mensagem (dados/caixa/.midia/<id>.<ext>) ou None."""
    cache = c.dados_dir(ctx) / "caixa" / ".midia"
    seguro = lotes._nome_seguro(mid, "x")
    try:
        return next((p for p in sorted(cache.glob(f"{seguro}.*")) if p.is_file() and p.suffix[1:] in _TIPO_EXT), None)
    except OSError:
        return None


def _obter_midia(ctx, mid: str, key: dict, tipo_msg: str, message: dict | None = None):
    """(bytes, tipo MIME) da mídia de uma mensagem, do cache ou baixada da Evolution (e guardada no cache), ou a
    Resposta de erro. Quem chama já conferiu que a mensagem existe na janela de leitura."""
    achado = _midia_em_cache(ctx, mid)
    if achado is not None:
        return achado.read_bytes(), _TIPO_EXT[achado.suffix[1:]]
    if tipo_msg == "productMessage" and lotes.interna_de_album(message):
        tipo_msg = "imageMessage"   # pedido 57: a foto do produto do catálogo
    if tipo_msg not in _MSG_IMAGEM + _MSG_AUDIO + _MSG_VIDEO + _MSG_DOC:
        return c.texto_simples("não encontrado", 404)
    chave = {k: key.get(k) for k in ("id", "remoteJid", "fromMe", "participant") if key.get(k) is not None}
    # pedido 56: foto ou vídeo de álbum só baixa se a mensagem de dentro for junto no pedido
    album = {"interna": interna} if (interna := lotes.interna_de_album(message)) else {}
    try:
        if tipo_msg in _MSG_VIDEO:
            dados, tipo = ctx.evo.midia_video(chave, **album)
        elif tipo_msg in _MSG_DOC:
            dados, tipo = ctx.evo.midia_documento(chave), "application/octet-stream"
        else:
            dados, tipo = ctx.evo.midia_bytes(chave, audio=tipo_msg in _MSG_AUDIO, **album)
    except MidiaInvalida:
        return c.texto_simples("mídia indisponível", 404)
    except VigiaErro as e:
        log.warning("mídia não baixada: %s", causa(e))
        return c.texto_simples("Evolution indisponível", 502)
    cache = c.dados_dir(ctx) / "caixa" / ".midia"
    try:
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        destino = cache / f"{lotes._nome_seguro(mid, 'x')}.{_EXT.get(tipo, 'bin')}"
        destino.write_bytes(dados)
        destino.chmod(0o600)
    except OSError:
        log.warning("cache de mídia do painel não gravado")
    return dados, tipo



# ------------------------------------------------------------------ envio de mídia (pedido 50)
LIMITE_MIDIA = 16 * 1024 * 1024
LIMITE_LEGENDA = 1024
_TIPOS_ENVIO = ("foto", "video", "arquivo", "voz")


def _mime_foto(d: bytes) -> str:
    if d.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if d.startswith(b"\x89PNG"):
        return "image/png"
    if d.startswith(b"RIFF") and d[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _e_audio(d: bytes) -> bool:
    """Contêineres que o gravador do navegador ou um arquivo de áudio comum produzem (MP4/M4A, Ogg, WebM, MP3, WAV)."""
    return (d[4:8] == b"ftyp" or d.startswith(b"OggS") or d.startswith(b"\x1a\x45\xdf\xa3") or d.startswith(b"ID3")
            or d[:2] in (b"\xff\xfb", b"\xff\xf3") or (d.startswith(b"RIFF") and d[8:12] == b"WAVE"))


def _mp4_fragmentado(d: bytes) -> bool:
    """MP4 com caixa `moof` no nível de cima (é o que o MediaRecorder do WebKit grava). A Evolution 2.3.7 lê o áudio
    por um pipe com um ffmpeg de 2019 e só converte o primeiro segundo desse formato (pedido 51, medido)."""
    if d[4:8] != b"ftyp":
        return False
    i, n = 0, len(d)
    while i + 8 <= n:
        tam, caixa = int.from_bytes(d[i:i + 4], "big"), d[i + 4:i + 8]
        if caixa == b"moof":
            return True
        if tam == 1 and i + 16 <= n:
            tam = int.from_bytes(d[i + 8:i + 16], "big")
        if tam < 8:
            break
        i += tam
    return False


def enviar_midia_post(req):
    """POST /conversas/enviar-midia?para=&tipo=foto|video|arquivo|voz&nome=&legenda= com o arquivo cru no corpo.
    Mesma trava do texto (D-39): só parte com o envio liberado. Guarda só o hash do arquivo, nunca o conteúdo."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    if not getattr(getattr(req.ctx, "tokens", None), "envio", ""):
        return _json(503, "Envio desligado: o token de envio não está configurado.")
    if not servicos(req.ctx).liberacao.liberada():
        return _json(423, "Envio travado: confirme com o Touch ID.", liberar=True)
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    dados = corpo.get("_bruto")
    para, tipo = c.query(req, "para"), c.query(req, "tipo")
    legenda = str((req.query or {}).get("legenda") or "")
    if not wa.JID_OK.match(para) or para.endswith("@lid"):
        return _json(400, "Destino inválido.")
    if tipo not in _TIPOS_ENVIO:
        return _json(400, "Tipo de envio desconhecido.")
    if not isinstance(dados, (bytes, bytearray)) or not dados:
        return _json(400, "Arquivo vazio.")
    if len(dados) > LIMITE_MIDIA:
        return _json(413, "Arquivo acima de 16 MB.")
    if len(legenda) > LIMITE_LEGENDA:
        return _json(400, f"Legenda acima de {LIMITE_LEGENDA} caracteres.")
    dados = bytes(dados)
    numero = para if para.endswith("@g.us") else wa.digitos(para)
    evo = req.ctx.evo
    citar, recusa = _citada_do_pedido(req, para, c.query(req, "citar"))
    if recusa:
        return recusa
    if tipo == "foto":
        mime = _mime_foto(dados)
        if not mime:
            return _json(415, "Essa imagem não é JPEG, PNG nem WebP. Envie como arquivo.")
        acao = lambda: evo.enviar_midia(numero, "image", dados, nome="foto." + _EXT[mime], mime=mime, legenda=legenda, citar=citar)
    elif tipo == "video":
        if dados[4:8] != b"ftyp":
            return _json(415, "Esse vídeo não é MP4 nem MOV. Envie como arquivo.")
        acao = lambda: evo.enviar_midia(numero, "video", dados, nome="video.mp4", mime="video/mp4", legenda=legenda, citar=citar)
    elif tipo == "voz":
        if not _e_audio(dados):
            return _json(415, "Esse arquivo não é um áudio reconhecido.")
        if _mp4_fragmentado(dados):
            return _json(415, "Esse áudio está em MP4 fragmentado e sairia cortado. Envie em WAV ou Ogg.")
        acao = lambda: evo.enviar_audio(numero, dados, citar=citar)
    else:
        nome = _nome_arquivo(c.query(req, "nome"), "arquivo")
        mime = mimetypes.guess_type(nome)[0] or "application/octet-stream"
        acao = lambda: evo.enviar_midia(numero, "document", dados, nome=nome, mime=mime, legenda=legenda, citar=citar)
    h = hashlib.sha256(dados).hexdigest()
    try:
        mid = acao()
    except EnvioIncerto:
        _registrar(req, para=para, hash_texto=h, por=f"painel:conversa:{tipo}:incerto")
        log.warning("envio de mídia incerto")
        return _json(202, "O arquivo pode ter saído. Confira no celular antes de repetir.", incerto=True)
    except VigiaErro as e:
        log.warning("envio de mídia falhou: %s", causa(e))
        return _json(502, _motivo_da_falha(e))
    _registrar(req, para=para, hash_texto=h, por=f"painel:conversa:{tipo}")
    return json_resposta({"enviadas": 1, "id": mid, "jid": para})


def avatar_get(req):
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    jid = c.query(req, "j")
    if not wa.JID_OK.match(jid):
        return c.texto_simples("não encontrado", 404)
    arq = servicos(req.ctx).avatares.arquivo(jid)
    if arq is None:
        return c.texto_simples("não encontrado", 404)
    dados = Path(arq).read_bytes()
    # pedido 52: a lista tem dezenas de fotos e era pedida inteira a cada tela; 10 min de cache só no navegador do Greg
    return Resposta(200, dados, wa.tipo_imagem(dados), cabecalhos=[("Cache-Control", "private, max-age=600")])


def envio_liberar(req):
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    cab = req.cabecalhos or {}
    recebido = cab.get("x-vigia-envio") or cab.get("X-Vigia-Envio") or ""
    lib = servicos(req.ctx).liberacao
    if not lib.liberar(recebido, getattr(getattr(req.ctx, "tokens", None), "envio", "")):
        return _json(401, "Token de envio ausente ou errado.")
    return json_resposta({"liberado": True, "restante_s": lib.restante()})


def envio_estado(req):
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    lib = servicos(req.ctx).liberacao
    return json_resposta({"configurado": bool(getattr(getattr(req.ctx, "tokens", None), "envio", "")),
                          "liberado": lib.liberada(), "restante_s": lib.restante()})


def envio_travar(req):
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    servicos(req.ctx).liberacao.travar()
    return json_resposta({"liberado": False, "restante_s": 0})


def _quando_do_pedido(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _agendado_ok(req, para: str, it: dict):
    ag = getattr(req.ctx, "agendador", None)
    if ag is not None:
        ag.acordar()
    return json_resposta({"agendado": it["id"], "quando": quando_agendado(it["quando"]), "html": html_agendados(req.ctx, para)})


def agendar_post(req):
    """POST /conversas/agendar {para, texto, quando}: guarda o texto e manda na hora (pedido 72, D-70). `quando` é
    o instante em segundos (a página converte da hora do Mac). Só agenda com o envio liberado: a liberação vale como
    o aval de quem manda; na hora marcada o Greg manda mesmo com a trava fechada."""
    if (e := _pode_enviar(req)):
        return e
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    para, texto = corpo.get("para"), corpo.get("texto")
    if not _destino_ok(para):
        return _json(400, "Destino inválido.")
    if not isinstance(texto, str) or not texto.strip() or len(texto) > wa.LIMITE_TEXTO:
        return _json(400, f"Texto vazio ou acima de {wa.LIMITE_TEXTO} caracteres.")
    try:
        it = agendados.da(req.ctx).criar(para, _quando_do_pedido(corpo.get("quando")), texto=texto)
    except agendados.AgendaInvalida as e:
        return _json(e.status, str(e))
    except (VigiaErro, OSError) as e:
        log.warning("agendar falhou: %s", causa(e))
        return _json(500, "Não consegui guardar o agendamento; nada foi agendado.")
    return _agendado_ok(req, para, it)


def agendar_midia_post(req):
    """POST /conversas/agendar-midia?para=&tipo=foto|video|arquivo&nome=&legenda=&quando= com o arquivo cru no corpo.
    As mesmas conferências do envio na hora (tamanho, formato); áudio de voz não se agenda."""
    if (e := _pode_enviar(req)):
        return e
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    dados = corpo.get("_bruto")
    para, tipo = c.query(req, "para"), c.query(req, "tipo")
    legenda = str((req.query or {}).get("legenda") or "")
    if not _destino_ok(para):
        return _json(400, "Destino inválido.")
    if tipo not in ("foto", "video", "arquivo"):
        return _json(400, "Tipo de envio desconhecido.")
    if not isinstance(dados, (bytes, bytearray)) or not dados:
        return _json(400, "Arquivo vazio.")
    if len(dados) > LIMITE_MIDIA:
        return _json(413, "Arquivo acima de 16 MB.")
    if len(legenda) > LIMITE_LEGENDA:
        return _json(400, f"Legenda acima de {LIMITE_LEGENDA} caracteres.")
    dados = bytes(dados)
    if tipo == "foto":
        mime = _mime_foto(dados)
        if not mime:
            return _json(415, "Essa imagem não é JPEG, PNG nem WebP. Envie como arquivo.")
        nome = "foto." + _EXT[mime]
    elif tipo == "video":
        if dados[4:8] != b"ftyp":
            return _json(415, "Esse vídeo não é MP4 nem MOV. Envie como arquivo.")
        nome, mime = "video.mp4", "video/mp4"
    else:
        nome = _nome_arquivo(c.query(req, "nome"), "arquivo")
        mime = mimetypes.guess_type(nome)[0] or "application/octet-stream"
    try:
        it = agendados.da(req.ctx).criar(para, _quando_do_pedido(c.query(req, "quando")), texto=legenda, tipo=tipo,
                                         arquivo=dados, nome=nome, mime=mime)
    except agendados.AgendaInvalida as e:
        return _json(e.status, str(e))
    except (VigiaErro, OSError) as e:
        log.warning("agendar arquivo falhou: %s", causa(e))
        return _json(500, "Não consegui guardar o agendamento; nada foi agendado.")
    return _agendado_ok(req, para, it)


def agendados_cancelar_post(req):
    """POST /conversas/agendados/cancelar {id}: tira da agenda e apaga o texto e o arquivo guardados. Não pede a
    trava de envio: cancelar não manda nada."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    id_ = _quando_do_pedido(corpo.get("id"))
    try:
        it = agendados.da(req.ctx).cancelar(id_) if id_ is not None else None
    except (VigiaErro, OSError) as e:
        log.warning("cancelar agendado falhou: %s", causa(e))
        return _json(500, "Não consegui cancelar; confira em Envios.")
    if it is None:
        return _json(409, "Esse envio não está mais agendado (já saiu ou já foi cancelado).")
    return json_resposta({"cancelado": it["id"], "html": html_agendados(req.ctx, it["para"])})


def agendados_get(req):
    """GET /conversas/agendados?para=: a faixa da conversa, para a página atualizar sem recarregar."""
    para = c.query(req, "para")
    if not _destino_ok(para):
        return _json(400, "Destino inválido.")
    return json_resposta({"html": html_agendados(req.ctx, para)})


ROTAS = {"/conversas": conversas_get, "/conversas/pulso": pulso_get, "/conversas/nova": nova_get, "/conversas/verificar": verificar_post,
         "/conversas/enviar": enviar_post, "/conversas/enviar-midia": enviar_midia_post, "/conversas/reagir": reagir_post, "/conversas/editar": editar_post, "/conversas/apagar": apagar_post, "/conversas/encaminhar": encaminhar_post, "/conversas/apelido": apelido_post, "/conversas/midia": midia_get,
         "/conversas/avatar": avatar_get, "/conversas/perfil": perfil_get, "/envio/liberar": envio_liberar, "/envio/estado": envio_estado,
         "/envio/travar": envio_travar, "/conversas/agendar": agendar_post, "/conversas/agendar-midia": agendar_midia_post,
         "/conversas/agendados": agendados_get, "/conversas/agendados/cancelar": agendados_cancelar_post}
