import os
import sys
import json
import time
import requests
import gspread
from google.oauth2.service_account import Credentials
from insta_manager import desenhar_arte, gerar_reels_mp4, formatar_preco, preco_para_float, criar_textos_insta, upload_midia_publica_temporaria, descobrir_ig_user_id

NOME_DA_PLANILHA = (os.getenv('NOME_DA_PLANILHA') or 'BugdasPromos').strip()
IG_ACCESS_TOKEN = (os.getenv('IG_ACCESS_TOKEN') or '').strip()
IG_USER_ID = (os.getenv('IG_USER_ID') or '').strip()
TELEGRAM_TOKEN = (os.getenv('TELEGRAM_TOKEN') or '').strip()
ADMIN_CHAT_ID = (os.getenv('ADMIN_CHAT_ID') or '5096433671').strip()

escopos = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

google_creds = (os.getenv("GOOGLE_CREDENTIALS") or "").strip()
if google_creds:
    credenciais = Credentials.from_service_account_info(json.loads(google_creds), scopes=escopos)
elif os.path.exists("credentials.json"):
    credenciais = Credentials.from_service_account_file("credentials.json", scopes=escopos)
else:
    raise FileNotFoundError("Credenciais do Google não encontradas!")

cliente_sheets = gspread.authorize(credenciais)
planilha = cliente_sheets.open(NOME_DA_PLANILHA).sheet1

def publicar_ultima_linha(forcar_formato=None):
    print("📋 Lendo planilha para pegar a última oferta cadastrada...")
    registros = planilha.get_all_records(numericise_ignore=['all'])
    if not registros:
        print("❌ Nenhum registro encontrado na planilha.")
        return

    indice_linha = len(registros) + 1  # 1-based index (cabeçalho é linha 1)
    linha = registros[-1]

    produto = str(linha.get('Produto', '')).strip()
    preco_antigo = formatar_preco(str(linha.get('Preco_Antigo', '')))
    preco_novo = formatar_preco(str(linha.get('Preco_Novo', '')))
    url_img = str(linha.get('Imagem_URL', ''))
    cupom = str(linha.get('Cupom', '')).strip()
    condicao = str(linha.get('Condicao', '')).strip()
    formato_original = str(linha.get('Formato_Insta', 'FOTO')).upper()

    formato = (forcar_formato or formato_original or "FOTO").upper()

    print(f"\n🎯 Linha selecionada: Linha {indice_linha}")
    print(f"📦 Produto: {produto}")
    print(f"💰 De: {preco_antigo or '--'} | Por: {preco_novo}")
    print(f"🖼️ Formato: {formato}")

    if not IG_ACCESS_TOKEN:
        print("❌ ERRO: IG_ACCESS_TOKEN não está definido nas variáveis de ambiente.")
        return

    ig_id = descobrir_ig_user_id(IG_ACCESS_TOKEN) or IG_USER_ID
    if not ig_id:
        print("❌ ERRO: Não foi possível obter o IG_USER_ID da conta.")
        return
    print(f"👤 Instagram Account ID: {ig_id}")

    # 1. Gerar Textos e Legenda
    print("\n📝 Gerando legenda...")
    textos = criar_textos_insta(produto, preco_antigo, preco_novo, cupom, condicao)
    legenda = textos['legenda']

    # 2. Gerar Mídia Local
    print(f"🎨 Gerando mídia ({formato})...")
    if formato == "VIDEO":
        caminho_midia = gerar_reels_mp4(produto, preco_antigo, preco_novo, cupom, url_img, textos['narracao'])
    else:
        caminho_midia = desenhar_arte(produto, preco_antigo, preco_novo, cupom, url_img, formato="FOTO")

    print(f"✅ Mídia gerada em: {caminho_midia}")

    # 3. Gerar URL pública temporária para a Meta baixar
    print("☁️ Fazendo upload temporário da mídia...")
    media_url = upload_midia_publica_temporaria(caminho_midia)
    if not media_url:
        print("❌ Falha ao obter URL pública da mídia.")
        return
    print(f"🔗 Mídia disponível em: {media_url}")

    # 4. Enviar para a Graph API da Meta
    base_api = "https://graph.instagram.com/v21.0" if IG_ACCESS_TOKEN.startswith("IG") else "https://graph.facebook.com/v21.0"
    payload = {
        "caption": legenda,
        "access_token": IG_ACCESS_TOKEN
    }
    if formato == "VIDEO":
        payload["media_type"] = "REELS"
        payload["video_url"] = media_url
        payload["share_to_feed"] = "true"
    else:
        payload["image_url"] = media_url

    print("🚀 Criando container de publicação na Meta...")
    r_cont = requests.post(f"{base_api}/{ig_id}/media", data=payload, timeout=30).json()
    if "id" not in r_cont:
        print(f"❌ Erro da Meta ao criar container: {r_cont}")
        return

    creation_id = r_cont["id"]
    print(f"⏳ Container criado ({creation_id}). Aguardando processamento...")

    # Aguardar processamento
    for tentativa in range(15):
        time.sleep(5)
        r_status = requests.get(
            f"{base_api}/{creation_id}",
            params={"fields": "status_code,status", "access_token": IG_ACCESS_TOKEN},
            timeout=15
        ).json()
        status_code = r_status.get("status_code", "")
        print(f"   Status ({tentativa+1}/15): {status_code or 'Processando...'}")
        if status_code == "FINISHED" or (formato == "FOTO" and not status_code):
            break
        if status_code == "ERROR":
            print(f"❌ A Meta recusou a mídia: {r_status}")
            return

    # 5. Publicar!
    print("📢 Publicando container no Instagram...")
    r_pub = requests.post(
        f"{base_api}/{ig_id}/media_publish",
        data={"creation_id": creation_id, "access_token": IG_ACCESS_TOKEN},
        timeout=30
    ).json()

    if "id" in r_pub:
        post_id = r_pub["id"]
        print(f"\n🎉 SUCESSO TOTAL! Publicação realizada no Instagram!")
        print(f"🆔 Post ID: {post_id}")
        planilha.update_cell(indice_linha, 11, "Publicado")  # Coluna K: Status_Insta
        if TELEGRAM_TOKEN and ADMIN_CHAT_ID:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={
                    "chat_id": ADMIN_CHAT_ID,
                    "text": f"🎉 Publicação de Teste Realizada no Instagram!\n\n📦 {produto}\n💰 {preco_novo}\n🖼️ Formato: {formato}\n🆔 ID: {post_id}"
                }
            )
    else:
        print(f"❌ Falha ao publicar: {r_pub}")

if __name__ == "__main__":
    formato_arg = sys.argv[1].upper() if len(sys.argv) > 1 else None
    publicar_ultima_linha(forcar_formato=formato_arg)
