from google import genai
import requests
import gspread
from google.oauth2.service_account import Credentials
import time

# ==========================================
# 1. CONFIGURAÇÕES GERAIS
# ==========================================
TELEGRAM_TOKEN = '(os.getenv('TELEGRAM_TOKEN') or '').strip()'
TELEGRAM_CHAT_ID = '@OBugdasPromos'
GEMINI_API_KEY = '(os.getenv('GEMINI_API_KEY') or '').strip()'
NOME_DA_PLANILHA = 'BugdasPromos'

# Nova forma de configurar o Gemini
cliente_gemini = genai.Client(api_key=GEMINI_API_KEY)

# ==========================================
# 2. CONEXÃO COM GOOGLE SHEETS
# ==========================================
escopos = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
credenciais = Credentials.from_service_account_file("credentials.json", scopes=escopos)
cliente_sheets = gspread.authorize(credenciais)
planilha = cliente_sheets.open(NOME_DA_PLANILHA).sheet1

# ==========================================
# 3. FUNÇÕES BASE
# ==========================================
def gerar_copy(produto, preco_antigo, preco_novo, link):
    prompt = f"""
    Aja como um copywriter focado em conversão para grupos de ofertas no Telegram. 
    Crie uma mensagem curta sobre esta promoção:
    
    Produto: {produto}
    Preço Original: R$ {preco_antigo}
    Preço Promoção: R$ {preco_novo}
    Link da Oferta: {link}
    
    Regras estritas:
    - Adapte o tom ao tipo de produto: item de casa (utilidade e economia), eletrônico (leve urgência).
    - Amigável e persuasivo. NUNCA pareça desesperado.
    - Mostre o desconto de forma clara.
    - Máximo de 3 emojis.
    - CTA final apontando pro link.
    - Sem saudações.
    """
    
    # Lista atualizada direto de 2026!
    modelos = ['gemini-3.8-flash', 'gemini-3.8-flash-8b']
    
    for modelo in modelos:
        for tentativa in range(2): 
            try:
                resposta = cliente_gemini.models.generate_content(
                    model=modelo,
                    contents=prompt
                )
                return resposta.text
                
            except Exception as e:
                if "503" in str(e) or "429" in str(e):
                    tempo_espera = 3 * (tentativa + 1)
                    print(f"⚠️ API ocupada no modelo {modelo}. Pausa de {tempo_espera}s...")
                    time.sleep(tempo_espera)
                else:
                    print(f"❌ Erro inesperado no {modelo}: {e}. Tentando o próximo modelo...")
                    break 
                
    print("🚨 Google caiu completamente. Usando texto de emergência.")
    texto_reserva = f"🚨 *PREÇO BAIXO DETECTADO*\n\n{produto}\n\n❌ De: ~R$ {preco_antigo}~\n✅ Por: *R$ {preco_novo}*\n\n🛒 Garanta o seu aqui:\n👉 {link}"
    return texto_reserva

def enviar_telegram(texto, url_imagem):
    url_photo = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'}
    
    # 1. Tenta baixar a imagem pelo Python e enviar o arquivo binário direto (Evita 100% o erro "failed to get HTTP URL content")
    try:
        if url_imagem and url_imagem.startswith("http"):
            img_resp = requests.get(url_imagem, headers=headers, timeout=10)
            if img_resp.status_code == 200 and len(img_resp.content) > 1000:
                files = {'photo': ('oferta.jpg', img_resp.content, 'image/jpeg')}
                payload = {
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": texto,
                    "parse_mode": "Markdown"
                }
                res = requests.post(url_photo, data=payload, files=files).json()
                
                # Se der erro de formatação Markdown (ex: um caractere _ ou * aberto), reenvia em texto limpo
                if not res.get("ok") and "can't parse entities" in res.get("description", ""):
                    payload.pop("parse_mode")
                    res = requests.post(url_photo, data=payload, files={'photo': ('oferta.jpg', img_resp.content, 'image/jpeg')}).json()
                    
                if res.get("ok"):
                    return res
    except Exception as e:
        print(f"⚠️ Aviso ao baixar imagem ({e}). Tentando envio alternativo...")

    # 2. Fallback: Se a imagem estiver corrompida ou inacessível, envia apenas o texto da promoção para não perder a venda!
    url_msg = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload_msg = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": texto,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    res_msg = requests.post(url_msg, data=payload_msg).json()
    if not res_msg.get("ok") and "can't parse entities" in res_msg.get("description", ""):
        payload_msg.pop("parse_mode")
        res_msg = requests.post(url_msg, data=payload_msg).json()
        
    return res_msg

# ==========================================
# 4. LOOP DE EXECUÇÃO (A MÁQUINA)
# ==========================================
def processar_planilha():
    print("🔄 Lendo planilha...")
    registros = planilha.get_all_records()
    
    for indice, linha in enumerate(registros, start=2): # start=2 pois a linha 1 é o cabeçalho
        status = linha.get('Status', '')
        
        # Só processa se a coluna Status estiver vazia
        if not status:
            print(f"\n⚙️ Processando: {linha['Produto']}")
            
            try:
                # 1. Gera a Copy
                copy = gerar_copy(
                    linha['Produto'], 
                    linha['Preco_Antigo'], 
                    linha['Preco_Novo'], 
                    linha['Link']
                )
                
                # 2. Envia para o Telegram
                resultado = enviar_telegram(copy, linha['Imagem_URL'])
                
                # 3. Atualiza a Planilha
                if resultado.get("ok"):
                    planilha.update_cell(indice, 6, "Postado") # Atualiza a coluna F (Status)
                    print("✅ Enviado e marcado como Postado!")
                else:
                    erro = resultado.get('description')
                    planilha.update_cell(indice, 6, f"Erro: {erro}")
                    print(f"❌ Erro no envio: {erro}")
                    
                # Pausa de 3 segundos para respeitar o limite de requisições da API do Telegram
                time.sleep(3) 
                
            except Exception as e:
                print(f"❌ Erro crítico na linha {indice}: {e}")

if __name__ == "__main__":
    processar_planilha()
