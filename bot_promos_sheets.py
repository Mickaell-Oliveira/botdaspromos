import os
import re
import json
import time
import requests
import gspread
from io import BytesIO
from google.oauth2.service_account import Credentials
from google import genai
from PIL import Image

# ==========================================
# 1. CONFIGURAÇÕES
# ==========================================
GEMINI_API_KEY = (os.getenv('GEMINI_API_KEY') or '').strip()
NOME_DA_PLANILHA = (os.getenv('NOME_DA_PLANILHA') or 'BugdasPromos').strip()
TELEGRAM_TOKEN = (os.getenv('TELEGRAM_TOKEN') or '').strip()
TELEGRAM_CHAT_ID = (os.getenv('TELEGRAM_CHAT_ID') or '@OBugdasPromos').strip()
ADMIN_CHAT_ID = (os.getenv('ADMIN_CHAT_ID') or '5096433671').strip()
DESCONTO_MINIMO_PCT = 20.0

COL_PRODUTO = 1
COL_PRECO_ANTIGO = 2
COL_PRECO_NOVO = 3
COL_STATUS_TG = 6
COL_CUPOM = 7
COL_STATUS_INSTA = 11

MODELOS_GEMINI = ['gemini-3.8-flash-lite', 'gemini-3.8-flash']

CUPONS_INVALIDOS = {
    'NENHUM', 'VAZIO', 'SEM CUPOM', 'NAO', 'NÃO', 'NONE', 'NULL',
    'PRIME', 'NO APP', 'APP', 'PIX', 'NO PIX'
}

cliente_gemini = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()

escopos = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

google_creds = (os.getenv("GOOGLE_CREDENTIALS") or "").strip()
if google_creds:
    credenciais = Credentials.from_service_account_info(json.loads(google_creds), scopes=escopos)
elif os.path.exists("credentials.json"):
    credenciais = Credentials.from_service_account_file("credentials.json", scopes=escopos)
else:
    raise FileNotFoundError("Credenciais do Google não encontradas! Defina a variável GOOGLE_CREDENTIALS ou mantenha o arquivo credentials.json.")

cliente_sheets = gspread.authorize(credenciais)
planilha = cliente_sheets.open(NOME_DA_PLANILHA).sheet1

# ==========================================
# 2. FUNÇÕES DE FORMATAÇÃO E VALIDAÇÃO
# ==========================================
def preco_para_float(valor_str):
    if not valor_str:
        return 0.0
    s = str(valor_str).replace("R$", "").replace(" ", "").strip()
    s = re.sub(r'[^\d.,]', '', s)
    if not s:
        return 0.0
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    elif '.' in s:
        partes = s.split('.')
        if len(partes[-1]) == 3 and len(partes) > 1:
            s = s.replace('.', '')
    elif s.isdigit() and len(s) >= 3:
        # Corrige valores em que o Google Sheets/gspread removeu a vírgula dos centavos (ex: "689,00" -> "68900")
        s = s[:-2] + "." + s[-2:]
    try:
        return float(s)
    except ValueError:
        return 0.0

def limpar_cupom(cupom_raw):
    if not cupom_raw:
        return ""
    c = str(cupom_raw).strip().upper()
    if c in CUPONS_INVALIDOS or len(c) < 3 or len(c) > 25:
        return ""
    for concorrente in ['ML', 'MELI', 'SHOPEE', 'MAGALU', 'ALI', 'KABUM']:
        if concorrente in c:
            return ""
    return c

def validar_oferta_para_publicacao(preco_antigo_raw, preco_novo_raw, cupom_raw, produto_titulo=""):
    """Retorna (valida: bool, resumo_validacao: str, desconto_pct: float, cupom_limpo: str)."""
    p_antigo = preco_para_float(preco_antigo_raw)
    p_novo = preco_para_float(preco_novo_raw)
    cupom = limpar_cupom(cupom_raw)

    if p_novo <= 0:
        return False, "Preço promocional inválido", 0.0, ""

    if produto_titulo:
        t_lower = str(produto_titulo).lower()
        eh_acessorio = any(t in t_lower for t in ["controle", "capa", "suporte", "filtro", "defletor", "peca", "peça", "cabo", "mangueira", "sensor", "adesivo", "pelicula", "película", "protetor", "bolsa", "estojo", "case"])
        if not eh_acessorio:
            pisos = [
                (["ar condicionado", "ar-condicionado", "split inverter", "geladeira", "refrigerador", "freezer", "lava e seca", "notebook", "playstation 5", "ps5"], 1100.0),
                (["lavadora de roupas", "maquina de lavar", "máquina de lavar", "smart tv", "televisao", "televisão", "iphone", "smartphone"], 550.0),
            ]
            for palavras_cat, piso in pisos:
                if any(p in t_lower for p in palavras_cat) and p_novo < piso:
                    return False, f"Preço inverossímil para o produto (R$ {p_novo:.2f})", 0.0, ""

    desconto_pct = 0.0
    if p_antigo > p_novo:
        desconto_pct = ((p_antigo - p_novo) / p_antigo) * 100.0
        if desconto_pct >= 95.0:
            return False, f"Desconto irreal ({desconto_pct:.0f}%)", desconto_pct, cupom

    if desconto_pct >= DESCONTO_MINIMO_PCT:
        return True, f"🔥 {desconto_pct:.0f}% OFF", desconto_pct, cupom
    if cupom:
        return True, f"🎟️ Cupom {cupom}", desconto_pct, cupom

    return False, f"Desconto abaixo de 30% ({desconto_pct:.1f}%) e sem cupom válido", desconto_pct, ""

def formatar_preco(valor):
    """Garante R$, separador de milhar e centavos corretos."""
    val_float = preco_para_float(valor)
    if val_float <= 0:
        return ""
    inteiro = int(val_float)
    centavos = int(round((val_float - inteiro) * 100))
    inteiro_fmt = f"{inteiro:,}".replace(",", ".")
    return f"R$ {inteiro_fmt},{centavos:02d}"

def gerar_copy_telegram(produto, preco_antigo, preco_novo, link, cupom, condicao, desconto_pct=0.0):
    """Gera a copy padronizada do Telegram instantaneamente (poupa 100% da cota da IA)."""
    tag_desconto = f" ({desconto_pct:.0f}% OFF)" if desconto_pct >= DESCONTO_MINIMO_PCT else ""
    texto = f"🚨 PREÇO BAIXO DETECTADO{tag_desconto}\n\n📦 {produto}\n\n"
    if preco_antigo:
        texto += f"❌ De: ~{preco_antigo}~\n"
    texto += f"✅ Por: {preco_novo} {condicao}\n".rstrip() + "\n"
    if cupom:
        texto += f"🎟️ Cupom: `{cupom}`\n"
    texto += f"\n🛒 Garanta o seu na Amazon:\n👉 {link}"
    return texto

# ==========================================
# 3. PREPARO DE IMAGEM E ENVIO TELEGRAM
# ==========================================
def obter_bytes_imagem_tratada(url_imagem):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0'}
    if not url_imagem or not str(url_imagem).startswith("http"):
        return None
    try:
        img_resp = requests.get(url_imagem, headers=headers, timeout=10)
        if img_resp.status_code == 200 and len(img_resp.content) > 1000:
            img = Image.open(BytesIO(img_resp.content)).convert("RGB")
            larg_img, alt_img = img.size
            # Só corta 15% do topo se não for imagem oficial direta da Amazon
            if "media-amazon.com" not in url_imagem and "images-amazon.com" not in url_imagem:
                img = img.crop((0, int(alt_img * 0.15), larg_img, alt_img))

            img_byte_arr = BytesIO()
            img.save(img_byte_arr, format='JPEG', quality=90)
            return img_byte_arr.getvalue()
    except Exception as e:
        print(f"⚠️ Aviso ao baixar/cortar imagem ({e}).")
    return None

def enviar_mensagem_telegram(chat_id, texto, url_imagem, reply_markup=None):
    url_photo = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    img_bytes = obter_bytes_imagem_tratada(url_imagem)

    if img_bytes:
        payload = {"chat_id": chat_id, "caption": texto[:1024], "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)

        res = requests.post(url_photo, data=payload, files={'photo': ('oferta.jpg', img_bytes, 'image/jpeg')}).json()
        if not res.get("ok") and "can't parse entities" in res.get("description", ""):
            payload.pop("parse_mode", None)
            res = requests.post(url_photo, data=payload, files={'photo': ('oferta.jpg', img_bytes, 'image/jpeg')}).json()
        if res.get("ok"):
            return res

    url_msg = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload_msg = {"chat_id": chat_id, "text": texto, "parse_mode": "Markdown", "disable_web_page_preview": False}
    if reply_markup:
        payload_msg["reply_markup"] = json.dumps(reply_markup)

    res_msg = requests.post(url_msg, data=payload_msg).json()
    if not res_msg.get("ok") and "can't parse entities" in res_msg.get("description", ""):
        payload_msg.pop("parse_mode", None)
        res_msg = requests.post(url_msg, data=payload_msg).json()

    return res_msg

def enviar_previa_canal_para_admin(indice_linha, texto_oferta, url_imagem, resumo_validacao):
    teclado = {
        "inline_keyboard": [
            [
                {"text": "✅ Publicar no Canal", "callback_data": f"aprovar_tg_{indice_linha}"},
                {"text": "❌ Descartar", "callback_data": f"rejeitar_tg_{indice_linha}"}
            ]
        ]
    }
    cabecalho = f"📢 PRÉVIA CANAL TELEGRAM (Linha {indice_linha}) | {resumo_validacao}\n{'─'*28}\n"
    return enviar_mensagem_telegram(ADMIN_CHAT_ID, cabecalho + texto_oferta, url_imagem, reply_markup=teclado)

# ==========================================
# 4. LEITURA DE APROVAÇÕES E SUGESTÕES DO ADMIN
# ==========================================
def responder_callback(cb_id, texto="Comando registrado!"):
    """Tira o relógio/spinner do botão do Telegram imediatamente e exibe pop-up."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": texto},
            timeout=5
        )
    except Exception:
        pass

def atualizar_botoes_mensagem(cb, texto_status):
    """Atualiza a mensagem no Telegram para desativar o botão e mostrar status final."""
    try:
        if "message" in cb:
            chat_id = cb["message"]["chat"]["id"]
            msg_id = cb["message"]["message_id"]
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageReplyMarkup",
                json={
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "reply_markup": {
                        "inline_keyboard": [[
                            {"text": texto_status, "callback_data": "noop"}
                        ]]
                    }
                },
                timeout=5
            )
    except Exception:
        pass

def interpretar_e_aplicar_sugestao_admin(linha_idx, texto_sugestao):
    """
    Usa o Gemini 3.8 Flash para interpretar o pedido do Admin (ex: 'muda o preço para 49,90',
    'coloca cupom PROMO', 'tira essa oferta', etc.) e aplica as mudanças diretamente na planilha.
    """
    try:
        linha_atual = planilha.row_values(linha_idx)
        prod_atual = linha_atual[0] if len(linha_atual) > 0 else ""
        antigo_atual = linha_atual[1] if len(linha_atual) > 1 else ""
        novo_atual = linha_atual[2] if len(linha_atual) > 2 else ""
        cupom_atual = linha_atual[6] if len(linha_atual) > 6 else ""

        prompt = f"""
        O administrador do canal de ofertas pediu uma alteração nesta oferta (Linha {linha_idx}):
        Mensagem do Admin: "{texto_sugestao}"

        Dados atuais da oferta na planilha:
        - Produto: {prod_atual}
        - Preço Antigo: {antigo_atual}
        - Preço Novo: {novo_atual}
        - Cupom: {cupom_atual}

        Retorne APENAS um JSON válido com os campos que devem ser alterados (ou null se não for para alterar):
        {{
            "novo_produto": "Novo nome limpo do produto ou null",
            "novo_preco_antigo": "Novo preco antigo formatado ou null (apenas números e vírgula, ex: 149,90)",
            "novo_preco_novo": "Novo preco promocional formatado ou null (apenas números e vírgula, ex: 99,90)",
            "novo_cupom": "Novo cupom ou null",
            "descartar": false,
            "resumo_mudancas": "Uma frase curta e objetiva em português confirmando o que foi alterado"
        }}
        """
        dados = None
        for modelo in MODELOS_GEMINI:
            try:
                resp = cliente_gemini.models.generate_content(
                    model=modelo,
                    contents=prompt,
                    config={'response_mime_type': 'application/json'}
                )
                dados = json.loads(resp.text.strip().replace('```json', '').replace('```', '').strip())
                break
            except Exception:
                continue

        # Fallback em Python caso a cota da IA esteja esgotada
        if not dados:
            txt_l = texto_sugestao.lower()
            dados = {
                "novo_produto": None,
                "novo_preco_antigo": None,
                "novo_preco_novo": None,
                "novo_cupom": None,
                "descartar": any(p in txt_l for p in ["descart", "rejeit", "apaga", "exclui", "remove"]),
                "resumo_mudancas": f"Ajuste aplicado via comando: {texto_sugestao}"
            }
            m_preco = re.search(r'(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})', texto_sugestao)
            if m_preco:
                if "antigo" in txt_l:
                    dados["novo_preco_antigo"] = m_preco.group(1)
                else:
                    dados["novo_preco_novo"] = m_preco.group(1)
            m_cupom = re.search(r'cupom\s+([A-Z0-9]{3,20})', texto_sugestao, re.IGNORECASE)
            if m_cupom:
                dados["novo_cupom"] = m_cupom.group(1).upper()

        if dados.get("descartar"):
            planilha.update_cell(linha_idx, COL_STATUS_TG, "Descartado")
            planilha.update_cell(linha_idx, COL_STATUS_INSTA, "Descartado")
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": ADMIN_CHAT_ID, "text": f"❌ Linha {linha_idx} descartada conforme seu pedido!"}
            )
            return

        mudou = False
        if dados.get("novo_produto"):
            planilha.update_cell(linha_idx, COL_PRODUTO, str(dados["novo_produto"]).strip())
            mudou = True
        if dados.get("novo_preco_antigo"):
            planilha.update_cell(linha_idx, COL_PRECO_ANTIGO, str(dados["novo_preco_antigo"]).strip())
            mudou = True
        if dados.get("novo_preco_novo"):
            planilha.update_cell(linha_idx, COL_PRECO_NOVO, str(dados["novo_preco_novo"]).strip())
            mudou = True
        if dados.get("novo_cupom") is not None:
            planilha.update_cell(linha_idx, COL_CUPOM, str(dados["novo_cupom"]).strip().upper())
            mudou = True

        # Reseta o status para que Telegram e Instagram gerem as novas prévias corrigidas
        planilha.update_cell(linha_idx, COL_STATUS_TG, "")
        planilha.update_cell(linha_idx, COL_STATUS_INSTA, "Aguardando Geracao")

        msg_feedback = dados.get("resumo_mudancas") or "Valores atualizados na planilha."
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": ADMIN_CHAT_ID, "text": f"✏️ Linha {linha_idx} atualizada!\n{msg_feedback}\n\n🔄 As prévias serão recriadas automaticamente com os novos dados."}
        )
    except Exception as e:
        print(f"⚠️ Erro ao interpretar sugestão: {e}")
        planilha.update_cell(linha_idx, COL_STATUS_INSTA, f"Regerar|{texto_sugestao}")
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": ADMIN_CHAT_ID, "text": f"🔄 Pedido registrado para refazer a Linha {linha_idx}..."}
        )

def verificar_respostas_do_admin():
    """
    Processa cliques de aprovação tanto do Canal do Telegram quanto do Instagram,
    responde imediatamente ao callback do Telegram para parar o spinner e atualiza botões.
    """
    try:
        resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", timeout=10).json()
        updates = resp.get("result", [])
        maior_id = 0
        for up in updates:
            maior_id = max(maior_id, up["update_id"])
            if "callback_query" in up:
                cb = up["callback_query"]
                data = str(cb.get("data", "")).strip()
                cb_id = cb.get("id")

                match_linha = re.search(r'(\d+)$', data)
                if not match_linha:
                    responder_callback(cb_id, "Comando concluído")
                    continue
                linha = int(match_linha.group(1))

                # 1. Cliques do Canal do Telegram
                if "aprovar_tg" in data:
                    responder_callback(cb_id, "🚀 Aprovado! Publicando no canal...")
                    atualizar_botoes_mensagem(cb, "✅ PUBLICADO NO CANAL")
                    planilha.update_cell(linha, COL_STATUS_TG, "Aprovado")
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        data={"chat_id": ADMIN_CHAT_ID, "text": f"🚀 Linha {linha} APROVADA para o Canal! Iniciando publicação imediata..."}
                    )
                elif "rejeitar_tg" in data:
                    responder_callback(cb_id, "❌ Descartado do canal!")
                    atualizar_botoes_mensagem(cb, "❌ DESCARTADO DO CANAL")
                    planilha.update_cell(linha, COL_STATUS_TG, "Descartado")
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        data={"chat_id": ADMIN_CHAT_ID, "text": f"❌ Linha {linha} DESCARTADA do Canal!"}
                    )

                # 2. Cliques do Instagram
                elif "aprovar" in data:
                    responder_callback(cb_id, "✅ Aprovado para o Instagram!")
                    atualizar_botoes_mensagem(cb, "✅ APROVADO PARA INSTAGRAM")
                    planilha.update_cell(linha, COL_STATUS_INSTA, "Aprovado")
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        data={"chat_id": ADMIN_CHAT_ID, "text": f"✅ Linha {linha} (Instagram) APROVADA!"}
                    )
                elif "rejeitar" in data:
                    responder_callback(cb_id, "❌ Descartado do Instagram!")
                    atualizar_botoes_mensagem(cb, "❌ DESCARTADO DO INSTAGRAM")
                    planilha.update_cell(linha, COL_STATUS_INSTA, "Descartado")
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        data={"chat_id": ADMIN_CHAT_ID, "text": f"❌ Linha {linha} (Instagram) DESCARTADA!"}
                    )

            elif "message" in up and "reply_to_message" in up["message"]:
                msg = up["message"]
                caption_original = msg["reply_to_message"].get("caption", "") or msg["reply_to_message"].get("text", "")
                match = re.search(r"Linha\s*(\d+)", caption_original)
                if match and msg.get("text"):
                    linha = int(match.group(1))
                    print(f"📩 Sugestão do Admin recebida para Linha {linha}: {msg['text']}")
                    interpretar_e_aplicar_sugestao_admin(linha, msg['text'])

        if maior_id > 0:
            requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={maior_id + 1}", timeout=5)
    except Exception as e:
        print(f"⚠️ Erro ao checar respostas do Admin: {e}")

# ==========================================
# 5. EXECUÇÃO PRINCIPAL
# ==========================================
def processar_planilha():
    print("🔄 Verificando aprovações do Admin e lendo planilha do Telegram...")
    verificar_respostas_do_admin()
    registros = planilha.get_all_records(numericise_ignore=['all'])

    for i, linha in enumerate(registros):
        indice_planilha = i + 2
        status_atual = str(linha.get('Status', '')).strip()
        produto = str(linha.get('Produto', '')).strip()
        if not produto:
            continue

        # Publica no Canal @OBugdasPromos ofertas novas ("") ou que estavam aguardando ("Em Aprovacao" / "Aprovado")
        if status_atual == "" or status_atual.lower() in ["aprovado", "em aprovacao"] or "chat_id is empty" in status_atual:
            valida, resumo_val, desconto_pct, cupom_limpo = validar_oferta_para_publicacao(
                linha.get('Preco_Antigo', ''),
                linha.get('Preco_Novo', ''),
                linha.get('Cupom', ''),
                produto_titulo=produto
            )

            if not valida:
                print(f"⏭️ Descartando linha {indice_planilha} ({produto}): {resumo_val}")
                planilha.update_cell(indice_planilha, COL_STATUS_TG, f"Descartado: {resumo_val}")
                continue

            preco_antigo_fmt = formatar_preco(linha.get('Preco_Antigo', ''))
            preco_novo_fmt = formatar_preco(linha.get('Preco_Novo', ''))

            print(f"🚀 Publicando oferta no canal {TELEGRAM_CHAT_ID}: {produto} ({ preco_antigo_fmt } -> { preco_novo_fmt })")
            texto = gerar_copy_telegram(
                produto, preco_antigo_fmt, preco_novo_fmt,
                linha.get('Link', ''), cupom_limpo, linha.get('Condicao', ''),
                desconto_pct=desconto_pct
            )
            res = enviar_mensagem_telegram(TELEGRAM_CHAT_ID, texto, linha.get('Imagem_URL', ''))
            if res and res.get('ok'):
                planilha.update_cell(indice_planilha, COL_STATUS_TG, "Postado")
                print("✅ Enviado para o Canal e marcado como Postado!")
            else:
                erro = res.get('description', 'Erro desconhecido') if res else 'Falha na requisição'
                planilha.update_cell(indice_planilha, COL_STATUS_TG, f"Erro: {erro}")
                print(f"❌ Erro no envio para o canal: {erro}")
            time.sleep(2)

if __name__ == "__main__":
    processar_planilha()
