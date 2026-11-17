
# Harness de teste: aviso.py não tem número default (I6); o contrato de aviso espera um destinatário. Número fictício.
import os as _os
_os.environ.setdefault("DONO_WHATSAPP", "5592900000000")
# Pedido 37: a instância do bot do aviso também não mora no código; nome fictício para o contrato.
_os.environ.setdefault("AVISO_INSTANCIA", "bot-avisos")
# Fase A (D-45): a suíte NUNCA toca no Keychain real do Mac; app/segredos.py só é exercitado com executor falso.
_os.environ.setdefault("VIGIA_KEYCHAIN", "off")
# Pedido 77: a suíte NUNCA dispara notificação de verdade no Mac; quem testa notificação troca saude.NOTIFICAR.
_os.environ.setdefault("VIGIA_SEM_NOTIFICACAO", "1")
