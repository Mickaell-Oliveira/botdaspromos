import os
import requests
import gspread
import json
import time
from io import BytesIO
from google.oauth2.service_account import Credentials
from google import genai
from PIL import Image

# ==========================================
# 1. CONFIGURAÇÕES
# ==========================================
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
NOME_DA_PLANILHA = os.getenv('NOME_DA_PLANILHA', 'BugdasPromos')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '@OBugdasPromos')

cliente_gemini = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()

escopos = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

google_creds = os.getenv("GOOGLE_CREDENTIALS")
if google_creds:
    credenciais = Credentials.from_service_account_info(json.loads(google_creds), scopes=escopos)
elif os.path.exists("credentials.json"):
    credenciais = Credentials.from_service_account_file("credentials.json", scopes=escopos)
else:
    raise FileNotFoundError("Credenciais do Google não encontradas! Defina a variável GOOGLE_CREDENTIALS ou mantenha o arquivo credentials.json.")

cliente_sheets = gspread.authorize(credenciais)
planilha = cliente_sheets.open(NOME_DA_PLANILHA).sheet1

# ==========================================
# 2. FUNÇÕES DE FORMATAÇÃO
# ==========================================
def formatar_preco(valor):
    """Garante R$, separador de milhar e centavos corretos."""
    if not valor or str(valor).strip() == "": return ""
    v = str(valor).replace("R$", "").replace(" ", "").strip()
    v = v.replace(".", "")
    if "," not in v and len(v) >= 3 and v.isdigit():
        v = v[:-2] + "," + v[-2:]
    elif "," not in v:
        v = v + ",00"
    try:
        partes = v.split(",")
        inteiro = int(partes[0])
        decimal = partes[1][:2].ljust(2, '0')
        inteiro_fmt = f"{inteiro:,}".replace(",", ".")
        return f"R$ {inteiro_fmt},{decimal}"
    except:
        return f"R$ {valor}"

def gerar_copy_telegram(produto, preco_antigo, preco_novo, link, cupom, condicao):
    prompt = f"""
    Crie UMA mensagem curta e chamativa para Telegram de ofertas:
    Produto: {produto}
    Preço Antigo: {preco_antigo}
    Preço Novo: {preco_novo}
    Link: {link}
    Cupom: {cupom}
    Condição: {condicao}
    
    Siga EXATAMENTE este formato:
    🚨 PREÇO BAIXO DETECTADO
    
    {produto}
    
    ❌ De: ~{preco_antigo}~
    ✅ Por: {preco_novo} {condicao}
    
    🛒 Garanta o seu aqui:
    👉 {link}
    """
    for modelo in ['gemini-3.8-flash', 'gemini-3.8-flash-8b']:
        try:
            resp = cliente_gemini.models.generate_content(model=modelo, contents=prompt)
            # Remove blocos markdown e asteriscos exagerados para manter limpo
            return resp.text.strip().replace('**', '')
        except Exception:
            time.sleep(2)
            
    # Fallback caso a IA falhe
    texto = f"🚨 PREÇO BAIXO DETECTADO\n\n{produto}\n\n❌ De: ~{preco_antigo}~\n✅ Por: {preco_novo} {condicao}\n"
    if cupom: texto += f"🎟️ Cupom: {cupom}\n"
    texto += f"\n🛒 Garanta o seu aqui:\n👉 {link}"
    return texto

# ==========================================
# 3. ENVIO COM CORTE DE MARCA D'ÁGUA
# ==========================================
def enviar_telegram(texto, url_imagem):
    url_photo = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0'}
    
    try:
        if url_imagem and url_imagem.startswith("http"):
            img_resp = requests.get(url_imagem, headers=headers, timeout=10)
            if img_resp.status_code == 200 and len(img_resp.content) > 1000:
                
                # CORTE INTELIGENTE: Arranca o topo da imagem para tirar o perfil de outros canais
                img = Image.open(BytesIO(img_resp.content)).convert("RGB")
                larg_img, alt_img = img.size
                img_cortada = img.crop((0, int(alt_img * 0.15), larg_img, alt_img))
                
                img_byte_arr = BytesIO()
                img_cortada.save(img_byte_arr, format='JPEG', quality=90)
                img_bytes = img_byte_arr.getvalue()
                
                files = {'photo': ('oferta.jpg', img_bytes, 'image/jpeg')}
                payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": texto, "parse_mode": "Markdown"}
                
                res = requests.post(url_photo, data=payload, files=files).json()
                
                # Tratamento de erro caso o markdown da copy quebre a API
                if not res.get("ok") and "can't parse entities" in res.get("description", ""):
                    payload.pop("parse_mode")
                    res = requests.post(url_photo, data=payload, files={'photo': ('oferta.jpg', img_bytes, 'image/jpeg')}).json()
                    
                if res.get("ok"):
                    return res
    except Exception as e:
        print(f"⚠️ Aviso ao baixar/cortar imagem ({e}). Enviando sem foto...")

    # Se a foto der erro total, envia só o texto
    url_msg = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload_msg = {"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "Markdown", "disable_web_page_preview": False}
    res_msg = requests.post(url_msg, data=payload_msg).json()
    if not res_msg.get("ok") and "can't parse entities" in res_msg.get("description", ""):
        payload_msg.pop("parse_mode")
        res_msg = requests.post(url_msg, data=payload_msg).json()
        
    return res_msg

# ==========================================
# 4. EXECUÇÃO
# ==========================================
def processar_planilha():
    print("🔄 Lendo planilha para postar no Telegram...")
    registros = planilha.get_all_records()
    
    for i, linha in enumerate(registros):
        # O bot só posta no Telegram as linhas que a Coluna F (Status) estiver vazia
        if not str(linha.get('Status', '')).strip():
            produto = linha['Produto']
            print(f"⚙️ Processando: {produto}")
            
            # Corrige a formatação dos preços antes de enviar para a Copy
            preco_antigo = formatar_preco(linha.get('Preco_Antigo', ''))
            preco_novo = formatar_preco(linha.get('Preco_Novo', ''))
            
            texto = gerar_copy_telegram(
                produto, preco_antigo, preco_novo, 
                linha['Link'], linha.get('Cupom', ''), linha.get('Condicao', '')
            )
            
            res = enviar_telegram(texto, linha['Imagem_URL'])
            
            if res and res.get('ok'):
                planilha.update_cell(i + 2, 6, "Postado")
                print("✅ Enviado para o Canal e marcado como Postado!")
            else:
                erro = res.get('description', 'Erro desconhecido') if res else 'Falha na requisição'
                planilha.update_cell(i + 2, 6, f"Erro: {erro}")
                print(f"❌ Erro no envio: {erro}")
                
            time.sleep(3)

if __name__ == "__main__":
    processar_planilha()
