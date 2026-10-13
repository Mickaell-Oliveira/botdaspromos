import google.generativeai as genai
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
    Aja como um copywriter focado em conversão. Crie uma mensagem curta para Telegram sobre esta promoção:
    Produto: {produto}
    Preço Original: R$ {preco_antigo}
    Preço Promoção: R$ {preco_novo}
    Link da Oferta: {link}
    
    Regras:
    - Gatilho de urgência forte.
    - Mostre o desconto de forma clara.
    - Emojis essenciais (🚨, 🔥, 🛒, 🏃‍♂️).
    - CTA final apontando pro link.
    - Sem saudações (fá direto ao ponto).
    """
    
    # Nova chamada da API usando o modelo mais atualizado
    resposta = cliente_gemini.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt
    )
    return resposta.text

def enviar_telegram(texto, url_imagem):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": url_imagem,
        "caption": texto,
        "parse_mode": "Markdown"
    }
    return requests.post(url, data=payload).json()

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
