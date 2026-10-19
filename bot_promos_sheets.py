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
DESCONTO_MINIMO_PCT = 30.0

COL_STATUS_TG = 6
COL_STATUS_INSTA = 11

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

def validar_oferta_para_publicacao(preco_antigo_raw, preco_novo_raw, cupom_raw):
    """Retorna (valida: bool, resumo_validacao: str, desconto_pct: float, cupom_limpo: str)."""
    p_antigo = preco_para_float(preco_antigo_raw)
    p_novo = preco_para_float(preco_novo_raw)
    cupom = limpar_cupom(cupom_raw)

    if p_novo <= 0:
        return False, "Preço promocional inválido", 0.0, ""

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
    tag_desconto = f" ({desconto_pct:.0f}% OFF)" if desconto_pct >= DESCONTO_MINIMO_PCT else ""
    bloco_de = f"❌ De: ~{preco_antigo}~\n" if preco_antigo else ""
    bloco_cupom = f"🎟️ Cupom: `{cupom}`\n" if cupom else ""

    prompt = f"""
    Crie UMA mensagem curta, persuasiva e chamativa para canal de ofertas no Telegram:
    Produto: {produto}
    Preço Antigo: {preco_antigo}
    Preço Novo: {preco_novo}{tag_desconto}
    Link: {link}
    Cupom: {cupom}
    Condição: {condicao}

    Siga EXATAMENTE esta estrutura (não adicione comentários extras):
    🚨 PREÇO BAIXO DETECTADO{tag_desconto}

    📦 {produto}

    {bloco_de}✅ Por: {preco_novo} {condicao}
    {bloco_cupom}
    🛒 Garanta o seu na Amazon:
    👉 {link}
    """
    for modelo in ['gemini-2.5-flash', 'gemini-2.0-flash']:
        try:
            resp = cliente_gemini.models.generate_content(model=modelo, contents=prompt)
            return resp.text.strip().replace('**', '')
        except Exception as e:
            print(f"   ⚠️ Aviso Gemini ({modelo}): {e}")
            time.sleep(1)

    # Fallback limpo caso a IA falhe
    texto = f"🚨 PREÇO BAIXO DETECTADO{tag_desconto}\n\n📦 {produto}\n\n"
    if preco_antigo:
        texto += f"❌ De: ~{preco_antigo}~\n"
    texto += f"✅ Por: {preco_novo} {condicao}\n"
    if cupom:
        texto += f"🎟️ Cupom: {cupom}\n"
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
# 4. LEITURA DE APROVAÇÕES DO ADMIN
# ==========================================
def verificar_respostas_do_admin():
    """
    Processa cliques de aprovação tanto do Canal do Telegram (aprovar_tg_ / rejeitar_tg_)
    quanto do Instagram (aprovar_ / rejeitar_ / replies) para nunca perder updates na fila.
    """
    try:
        resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", timeout=10).json()
        updates = resp.get("result", [])
        maior_id = 0
        for up in updates:
            maior_id = max(maior_id, up["update_id"])
            if "callback_query" in up:
                cb = up["callback_query"]
                data = cb.get("data", "")

                # Cliques de aprovação do Canal do Telegram
                if data.startswith("aprovar_tg_"):
                    linha = int(data.split("_")[2])
                    planilha.update_cell(linha, COL_STATUS_TG, "Aprovado")
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": ADMIN_CHAT_ID, "text": f"✅ Linha {linha} APROVADA para o Canal! Publicando..."})
                elif data.startswith("rejeitar_tg_"):
                    linha = int(data.split("_")[2])
                    planilha.update_cell(linha, COL_STATUS_TG, "Descartado")
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": ADMIN_CHAT_ID, "text": f"❌ Linha {linha} DESCARTADA do Canal!"})

                # Cliques de aprovação do Instagram (preserva sincronia com insta_manager.py)
                elif data.startswith("aprovar_"):
                    linha = int(data.split("_")[1])
                    planilha.update_cell(linha, COL_STATUS_INSTA, "Aprovado")
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": ADMIN_CHAT_ID, "text": f"✅ Linha {linha} (Instagram) APROVADA!"})
                elif data.startswith("rejeitar_"):
                    linha = int(data.split("_")[1])
                    planilha.update_cell(linha, COL_STATUS_INSTA, "Descartado")
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": ADMIN_CHAT_ID, "text": f"❌ Linha {linha} (Instagram) DESCARTADA!"})

            elif "message" in up and "reply_to_message" in up["message"]:
                msg = up["message"]
                caption_original = msg["reply_to_message"].get("caption", "") or msg["reply_to_message"].get("text", "")
                match = re.search(r"Linha (\d+)", caption_original)
                if match and msg.get("text"):
                    linha = int(match.group(1))
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data={"chat_id": ADMIN_CHAT_ID, "text": f"🔄 Pedido recebido para refazer a Linha {linha}..."})
                    planilha.update_cell(linha, COL_STATUS_INSTA, f"Regerar|{msg['text']}")

        if maior_id > 0:
            requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={maior_id + 1}", timeout=5)
    except Exception as e:
        print(f"⚠️ Aviso ao checar respostas do Admin: {e}")

# ==========================================
# 5. EXECUÇÃO PRINCIPAL
# ==========================================
def processar_planilha():
    print("🔄 Verificando aprovações do Admin e lendo planilha do Telegram...")
    verificar_respostas_do_admin()
    registros = planilha.get_all_records()
    previas_enviadas = 0

    for i, linha in enumerate(registros):
        indice_planilha = i + 2
        status_atual = str(linha.get('Status', '')).strip()
        produto = str(linha.get('Produto', '')).strip()
        if not produto:
            continue

        # 1. Validar preços e regra de desconto (>= 30% ou Cupom válido)
        valida, resumo_val, desconto_pct, cupom_limpo = validar_oferta_para_publicacao(
            linha.get('Preco_Antigo', ''),
            linha.get('Preco_Novo', ''),
            linha.get('Cupom', '')
        )

        preco_antigo_fmt = formatar_preco(linha.get('Preco_Antigo', ''))
        preco_novo_fmt = formatar_preco(linha.get('Preco_Novo', ''))

        # CASO A: Oferta já foi APROVADA pelo Admin -> Publicar no Canal @OBugdasPromos!
        if status_atual.lower() == "aprovado":
            if not valida:
                planilha.update_cell(indice_planilha, COL_STATUS_TG, f"Descartado: {resumo_val}")
                print(f"⏭️ Linha {indice_planilha} bloqueada na validação final ({resumo_val}).")
                continue

            print(f"🚀 Publicando oferta aprovada no canal {TELEGRAM_CHAT_ID}: {produto}")
            texto = gerar_copy_telegram(
                produto, preco_antigo_fmt, preco_novo_fmt,
                linha.get('Link', ''), cupom_limpo, linha.get('Condicao', ''),
                desconto_pct=desconto_pct
            )
            res = enviar_mensagem_telegram(TELEGRAM_CHAT_ID, texto, linha.get('Imagem_URL', ''))
            if res and res.get('ok'):
                planilha.update_cell(indice_planilha, COL_STATUS_TG, "Postado")
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    data={"chat_id": ADMIN_CHAT_ID, "text": f"🚀 Linha {indice_planilha} ({produto}) publicada com sucesso em {TELEGRAM_CHAT_ID}!"}
                )
                print("✅ Enviado para o Canal e marcado como Postado!")
            else:
                erro = res.get('description', 'Erro desconhecido') if res else 'Falha na requisição'
                planilha.update_cell(indice_planilha, COL_STATUS_TG, f"Erro: {erro}")
                print(f"❌ Erro no envio para o canal: {erro}")
            time.sleep(2)

        # CASO B: Oferta nova (Status vazio ou erro antigo de chat_id) -> Validar e enviar prévia para o Admin aprovar
        elif status_atual == "" or "chat_id is empty" in status_atual:
            if not valida:
                print(f"⏭️ Descartando linha {indice_planilha} ({produto}): {resumo_val}")
                planilha.update_cell(indice_planilha, COL_STATUS_TG, f"Descartado: {resumo_val}")
                continue

            if previas_enviadas >= 3:
                continue

            print(f"📩 Enviando prévia da linha {indice_planilha} para aprovação do Admin: {produto} ({resumo_val})")
            texto = gerar_copy_telegram(
                produto, preco_antigo_fmt, preco_novo_fmt,
                linha.get('Link', ''), cupom_limpo, linha.get('Condicao', ''),
                desconto_pct=desconto_pct
            )
            res = enviar_previa_canal_para_admin(indice_planilha, texto, linha.get('Imagem_URL', ''), resumo_val)
            if res and res.get('ok'):
                planilha.update_cell(indice_planilha, COL_STATUS_TG, "Em Aprovacao")
                previas_enviadas += 1
                print("   ✅ Prévia enviada ao Admin e marcada como 'Em Aprovacao'!")
            else:
                erro = res.get('description', 'Erro desconhecido') if res else 'Falha na requisição'
                planilha.update_cell(indice_planilha, COL_STATUS_TG, f"Erro: {erro}")
                print(f"   ❌ Erro ao enviar prévia ao Admin: {erro}")
            time.sleep(2)

if __name__ == "__main__":
    processar_planilha()
