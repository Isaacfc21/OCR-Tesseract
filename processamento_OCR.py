import sys, os, subprocess, tempfile, glob, requests, re, time
import psycopg2, psycopg2.extras
import cv2
import fitz 
import numpy as np
import pytesseract
from PIL import Image
import re   
import unicodedata
from spellchecker import SpellChecker
import language_tool_python
from difflib import get_close_matches
from unidecode import unidecode
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------- Configurações ---------------- #
POPPLER_PATH = r"C:\Poppler\Release-23.10.0-0\poppler-23.10.0\Library\bin\pdftoppm.exe"
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
# spell = SpellChecker(language='pt')
tool = language_tool_python.LanguageTool('pt-BR')
TMPDIR = tempfile.gettempdir()



# DB_HOST = "172.16.5.205"
DB_HOST = "172.16.4.178"
DB_PORT = 5432
DB_NAME = "sistema_pmg"
DB_USER = "postgres"
DB_PASS = "postgres"

# ---------------- Funções de Banco ---------------- #
def conectar_bd():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME,
                            user=DB_USER, password=DB_PASS)

def buscar_lote(inicio_id, tamanho_lote):
    conn = conectar_bd()
    cur = conn.cursor()
    cur.execute("SELECT id_lei FROM leis.lei WHERE id_lei >= %s ORDER BY id_lei ASC LIMIT %s",
                (inicio_id, tamanho_lote))
    ids = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return ids

def buscar_leis_por_ids(ids):
    if not ids:
        return []
    conn = conectar_bd()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT id_lei, arquivo_lei, tipo FROM leis.lei WHERE id_lei IN %s", (tuple(ids),))
    leis = cur.fetchall()
    cur.close()
    conn.close()
    return leis

# ---------------- Funções de Download e OCR ---------------- #
def montar_url(lei):
    conn = conectar_bd()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT tipo_caminho, caminho FROM leis.lei_caminho")
    caminhos = cur.fetchall()
    urls_por_tipo = {row['tipo_caminho']: row['caminho'].rstrip('/') for row in caminhos}
    raizes = urls_por_tipo.get(6, "https://www.guarulhos.sp.gov.br/06_prefeitura/leis/")
    
    arquivo_raw = lei['arquivo_lei'] or ''
    arquivo_limpo = arquivo_raw.strip()
    tipo = lei['tipo']

    # ---------------- Casos especiais ---------------- #
    # Decretos
    match_decreto = re.match(r'^\.decretos_(\d{4})/', arquivo_limpo)
    if match_decreto:
        ano = match_decreto.group(1)
        base_path = f"https://www.guarulhos.sp.gov.br/06_prefeitura/leis/decretos_{ano}/"
        arquivo_limpo = re.sub(r'^\.decretos_\d{4}/', '', arquivo_limpo)
    elif arquivo_limpo.startswith("/normativas_"):
        match_portaria = re.match(r'^/normativas_(\d{4})/(.+)$', arquivo_limpo)
        if match_portaria:
            ano = match_portaria.group(1)
            arquivo_limpo = match_portaria.group(2)
            base_path = f"https://www.guarulhos.sp.gov.br/06_prefeitura/leis/normativas_{ano}/"
    # Autógrafos
    elif tipo == 4:
        base_path = urls_por_tipo.get(4, raizes)
        m = re.match(r'^autografos(\d{3}_\d{4}_autografo\.pdf)$', arquivo_limpo)
        if m:
            arquivo_limpo = m[1]
    # Outros tipos
    else:
        base_path = urls_por_tipo.get(tipo, raizes)

    cur.close()
    conn.close()

    if not arquivo_limpo or not base_path:
        return None  # sem caminho válido

    return f"{base_path.rstrip('/')}/{requests.utils.requote_uri(arquivo_limpo)}"


def baixar_pdf(url, destino):
    try:
        r = requests.get(url, timeout=60, headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code == 200:
            with open(destino, "wb") as f: 
                f.write(r.content)
            return True
    except Exception as e:
        print(f"[ERRO] Baixando PDF {url}: {e}")
    return False

# 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

# ---------------- PRÉ-PROCESSAMENTO ---------------- #

CACHE = {}

# DICIONARIO_URL = "https://www.ime.usp.br/~pf/dicios/br-utf8.txt"
# print("🔹 Baixando dicionário...")
# r = requests.get(DICIONARIO_URL)
# DICIONARIO_BASE = set(line.strip().lower() for line in r.text.splitlines() if line.strip())

DICIONARIO_ARQUIVO = r"C:\xampp\htdocs\leis_externo\br-utf8.txt"

# Verifica se o arquivo existe
if not os.path.exists(DICIONARIO_ARQUIVO):
    raise FileNotFoundError(f"❌ Arquivo de dicionário não encontrado: {DICIONARIO_ARQUIVO}")

print("🔹 Carregando dicionário local...")
with open(DICIONARIO_ARQUIVO, "r", encoding="utf-8") as f:
    DICIONARIO_BASE = set(line.strip().lower() for line in f if line.strip())

# Palavras técnicas da sua área
DICIONARIO_TECNICO = {
    "siggeo", "sig", "erptech", "geotiff", "cad/gis", "qgis",
    "geoprocessamento", "geodésia", "geotecnologias", "georreferenciamento",
    "cartografia", "geoespacial", "teledetecção", "altimetria",
    "hidrografia", "topografia", "geocodificação",
    "georreferenciadas", "avanços", "divisão", "instalações",
    "unidade", "Seção", "empresas", "aculturamento"
}

DICIONARIO = DICIONARIO_BASE.union(DICIONARIO_TECNICO)
print(f"[DICIONÁRIO] {len(DICIONARIO):,} palavras carregadas (incluindo técnicas).")

TERMOS_ESPECIAIS = {"SIGeo", "ERPTech", "GeoTIFF", "CAD/GIS", "QGIS"}
TERMOS_ESPECIAIS_SEM_ACENTO = set(unidecode(t.lower()) for t in TERMOS_ESPECIAIS)
TERMOS_APRENDIDOS = set()

# -------------------- Correções fixas (se houver) -------------------- #
CORRECOES_FIXAS = {
    "Divisadão": "Divisão",
    "Sintema": "Sistema",
    "Municípios": "Município",
    "à": "a",
    "Sagrou-Guarulhos": "Sigeo-Guarulhos",
    "anos": "aos",
    "asas": "as",
    "Projetos": "Projeto",
    "socãmo": "Seção",
    "Socão": "Seção",
    "arca": "área",
    "laorafo": "geógrafo",
    "vVAass": "administrativas",
    "Socãmo": "Seção",
    "AA": "a",
    "Carlos-": "cartográfico",
    "toronja-": "topografia",
    "Torna-r": "formatos",
    "alunização": "alimentação",
    "alunização": "alimentação",
    "suplementavas": "suplementadas",
    "BEL.": "Bacharel",
    "cão": "ção",
    "especificas": "específicas",
    "experiência na arca": "experiência na área",
    "Geo-": "Geo",
    "grafico": "gráfico",
    "grã-r": "gráficos",
    "da'": "da",
    "profissão eximias": "profissão exigida",
    "650": "Geógrafo",
    "Gerir à": "Gerir a",
    "às": "as",
    "Signo": "SIGeo",
    "SRH/SUL": "SCM/GUL",  # manter
    "projétos": "projetos",
    "Geociências": "Geociências",  # manter
    "informação": "Informação",
    "Georreferenciadas": "Georreferenciadas",
    "Referência".lower(): "referência",
    "Divisadã Técnica": "Divisão Técnica",
    "Socãmo Técnica": "Seção Técnica",
    "Geoprocessamento-": "Geoprocessamento",
    "Carlos-gráfico": "cartográfico",
    "marcos": "marcos",
    "posterior migração": "posterior migração",
    "suplementavas": "suplementadas",
    "Georáficas": "Geográficas",
    "de-": "de ",
    "Grã-r": "Gráficos",
    "Quadra-da": "Qualidade",
    "qualidade, e |": "qualidade, e",
    "topo -": "topográficos",
    "geo-r": "geodésicos",
    "Geotecnologias": "Geotecnologias"
}

CORRECOES_FRASES = {
    "Denominação da' |": "Denominação da",
    "Nível Universitário.com especializa-": "Nível Universitário com especialização",
    "Engenheiro, Geólogo. Geo- grafo ou Arquiteto": "Engenheiro, Geólogo, Geógrafo ou Arquiteto",
    "Georreferenciadas de Guarulhos (SIGNO-Guarulhos)": "Georreferenciadas de Guarulhos (SIGeo-Guarulhos)",
    "adequada e atrelada.aos": "adequada e atrelada aos",
    "geoprocessamento;s": "geoprocessamento;",
    "empresas privadas ou publicas prestaras de serviços": "empresas privadas ou públicas prestadoras de serviços",
    "questões fronteiriços": "questões fronteiriças",
    "administrai-|administrativas": "administrativas",
    "Bases doe dados gráficos": "bases de dados gráficos",
    "ficou e alfanuméricos": "gráficos e alfanuméricos",
    "cruzamento de da-r dos existentes": "cruzamento dos dados existentes",
    "a fim doe carregá-las": "a fim de carregá-las",
    "Profissão eximias": "Profissão exigida",
    "continuamente atualizadas": "continuamente atualizada",
    "Carlos- gráfico": "cartográfico",
    "rede básica de apoio geodésico;s": "rede básica de apoio geodésico;",
    "toronja- fia": "topografia",
    "posterior migração": "posterior migração",
    "formatos tos' específicos": "formatos específicos",
    "suplementavas se necessário": "suplementadas se necessário"
}



def normalizar_hifen_linhas(texto):
    """
    Junta palavras quebradas por hífen no final da linha, preservando indentação e bullets.
    """
    linhas = texto.splitlines()
    linhas_corrigidas = []
    i = 0
    while i < len(linhas):
        linha = linhas[i]
        if linha.rstrip().endswith('-') and i + 1 < len(linhas):
            prox = linhas[i + 1].lstrip()
            linha = linha.rstrip()[:-1] + prox
            i += 1
        linhas_corrigidas.append(linha)
        i += 1
    return '\n'.join(linhas_corrigidas)

CORRECOES_FIXAS_EXPANDIDO = {}
for k, v in CORRECOES_FIXAS.items():
    CORRECOES_FIXAS_EXPANDIDO[k] = v
    sem_acento = unidecode(k)
    if sem_acento != k:
        CORRECOES_FIXAS_EXPANDIDO[sem_acento] = v

CORRECOES_FIXAS = CORRECOES_FIXAS_EXPANDIDO

# -------------------- Funções auxiliares -------------------- #
def distancia_levenshtein(a, b):
    if a == b: return 0
    if len(a) == 0: return len(b)
    if len(b) == 0: return len(a)
    matrix = [[0]*(len(b)+1) for _ in range(len(a)+1)]
    for i in range(len(a)+1): matrix[i][0] = i
    for j in range(len(b)+1): matrix[0][j] = j
    for i in range(1, len(a)+1):
        for j in range(1, len(b)+1):
            custo = 0 if a[i-1] == b[j-1] else 1
            matrix[i][j] = min(matrix[i-1][j]+1, matrix[i][j-1]+1, matrix[i-1][j-1]+custo)
    return matrix[len(a)][len(b)]

def aplicar_acentos(original, corrigida):
    resultado = ""
    for o, c in zip(original, corrigida):
        if re.match(r'[áéíóúãõâêôàèìòùç]', o.lower()):
            resultado += o
        else:
            resultado += c
    resultado += corrigida[len(resultado):]
    return resultado

def corrigir_frases(texto):
    for errado, certo in CORRECOES_FRASES.items():
        if errado in texto:
            texto = texto.replace(errado, certo)
            print(f"🔹 Frase corrigida: '{errado}' -> '{certo}'")
    return texto

# -------------------- Função de correção -------------------- #
def corrigir_texto_ocr(texto):
    """
    Corrige texto OCR mantendo:
      - Indentação e bullets
      - Quebra de linhas
      - Hífens no final de linha
      - Siglas e termos técnicos
    """

    texto = normalizar_hifen_linhas(texto)

    matches = tool.check(texto)
    texto = language_tool_python.utils.correct(texto, matches)

    for errado, certo in CORRECOES_FIXAS.items():
        texto = re.sub(rf'\b{re.escape(errado)}\b', certo, texto)

    for errado, certo in CORRECOES_FRASES.items():
        texto = texto.replace(errado, certo)

    linhas = texto.splitlines()
    linhas_corrigidas = []

    for linha in linhas:
        palavras = re.findall(r'\b[\w\'-]+\b', linha)
        palavras_corrigidas = []

        for p in palavras:
            palavra_lower = p.lower()
            palavra_sem_acento = unidecode(palavra_lower)
        
            if len(p) <= 2 or p.isupper() or p.isdigit() or '-' in p:
                palavras_corrigidas.append(p)
                continue
    
            if p in TERMOS_ESPECIAIS or palavra_sem_acento in TERMOS_ESPECIAIS_SEM_ACENTO:
                palavras_corrigidas.append(p)
                continue
            
            if palavra_lower in DICIONARIO:
                palavras_corrigidas.append(p)
                continue
            match_sem_acento = next((w for w in DICIONARIO if unidecode(w) == palavra_sem_acento), None)
            if match_sem_acento:
                correcao = match_sem_acento
                if p[0].isupper():
                    correcao = correcao.capitalize()
                palavras_corrigidas.append(correcao)
                continue

            sugestoes = get_close_matches(palavra_sem_acento, DICIONARIO, n=1, cutoff=0.9)
            if sugestoes:
                correcao = sugestoes[0]
                if p[0].isupper():
                    correcao = correcao.capitalize()
                palavras_corrigidas.append(correcao)
            else:
                palavras_corrigidas.append(p)

        espacos = re.match(r'^\s*', linha).group(0)
        linha_corrigida = espacos + ' '.join(palavras_corrigidas)
        linhas_corrigidas.append(linha_corrigida)

    return '\n'.join(linhas_corrigidas)



# ---------------- Pré-processamento ---------------- #
def preprocess_image(img_path):
    """Pré-processa imagem para OCR mais confiável"""
    img = cv2.imread(img_path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.bilateralFilter(gray, 9, 75, 75)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 15, 8
    )
    
    processed_path = img_path.replace(".png", "_proc.png")
    cv2.imwrite(processed_path, thresh)
    return processed_path

def extrair_texto_tesseract(img_path):
    """Extrai texto da imagem usando Tesseract OCR"""
    img_proc = preprocess_image(img_path)
    pil_img = Image.open(img_proc)
    texto_final = pytesseract.image_to_string(pil_img, lang='por', config='--oem 3 --psm 4')
    os.remove(img_proc)
    return texto_final

# ---------------- OCR do PDF ---------------- #
def ocr_pdf_para_texto(pdf_path):
    """
    Extrai texto de todas as páginas do PDF, aplica OCR quando necessário
    e corrige erros ortográficos preservando parágrafos.
    """
    pdf = fitz.open(pdf_path)
    texto_completo = ""

    for i, pagina in enumerate(pdf):
        numero = i + 1
        print(f"Processando página {numero}...")

        # Extrai texto direto do PDF
        texto = pagina.get_text("text")

        # Se a página tiver pouco texto, aplica OCR
        if len(texto.strip()) < 600:
            pix = pagina.get_pixmap(dpi=250)
            img_path = os.path.join(TMPDIR, f"pagina_{numero}.png")
            pix.save(img_path)
            texto = extrair_texto_tesseract(img_path)
            os.remove(img_path)

        texto_completo += f"\n\n--- Página {numero} ---\n\n{texto}"

    pdf.close()

    # Corrige automaticamente erros de OCR e ortografia
    # texto_corrigido = corrigir_texto_ocr(texto_completo)
    texto_corrigido = corrigir_texto_ocr(texto_completo)
    texto_corrigido = corrigir_frases(texto_corrigido)
    return texto_corrigido.strip()

def processar_ocr(pdf_path, lei_id):
    base = os.path.join(TMPDIR, f"lei_{lei_id}")
    # Converter PDF para PNG
    subprocess.run([POPPLER_PATH, "-png", "-r", "150", pdf_path, base],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    paginas = glob.glob(f"{base}-*.png")
    if not paginas:
        return ""

    texto_total = ""
    for pg in paginas:
        saida = pg.replace(".png", "")
        subprocess.run([TESSERACT_PATH, pg, saida, "-l", "por"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        txt_file = saida + ".txt"
        if os.path.exists(txt_file):
            with open(txt_file, "r", encoding="utf-8", errors="ignore") as f:
                texto_total += f.read() + "\n\n"
            try:
                os.remove(txt_file)
            except Exception:
                pass
        try:
            os.remove(pg)
        except Exception:
            pass

    # ✅ Evita crash ao remover PDF
    if os.path.exists(pdf_path):
        try:
            os.remove(pdf_path)
        except PermissionError:
            print(f"⚠️ Não consegui apagar {pdf_path}, provavelmente ainda em uso. Continuando...")
        except Exception as e:
            print(f"⚠️ Erro ao tentar remover {pdf_path}: {e}")

    return texto_total.strip()

def processar_uma_lei(lei):
    lei_id = lei['id_lei']
    url = montar_url(lei)
    if not url:
        print(f"⚠️ Lei {lei_id} sem URL")
        return lei_id, ""

    # Cria o caminho, mas fecha imediatamente o arquivo temporário
    destino = os.path.join(TMPDIR, f"lei_{lei_id}.pdf")

    if baixar_pdf(url, destino):
        try:
            # Escolha de OCR
            if lei['tipo'] == 2:  # exemplo: decretos
                texto = processar_ocr(destino, lei_id)
            else:
                texto = ocr_pdf_para_texto(destino)
        finally:
            if os.path.exists(destino):
                os.remove(destino)
        return lei_id, texto

    print(f"⚠️ Falha ao baixar PDF da lei {lei_id}")
    return lei_id, ""


def atualizar_banco(resultado):
    conn = conectar_bd()
    cur = conn.cursor()
    for lei_id, texto in resultado.items():
        if texto.strip():
            cur.execute("UPDATE leis.lei SET conteudo_pdf = %s WHERE id_lei = %s", (texto, lei_id))
            print(f"✅ Lei {lei_id} atualizada")
        else:
            print(f"⚠️ Lei {lei_id} sem texto OCR")
    conn.commit()
    cur.close()
    conn.close()

# ---------------- Main ---------------- #
if __name__ == "__main__":
    INICIO = 21312
    LIMITE_POR_LOTE = 1
    MAX_ID = 21313

    while INICIO < MAX_ID:
        print(f"\n🔹 Processando lote a partir do ID {INICIO}...")
        ids = buscar_lote(INICIO, LIMITE_POR_LOTE)
        if not ids:
            print("⚠️ Nenhum registro encontrado")
            break

        leis = buscar_leis_por_ids(ids)
        resultado = {}

        # Processamento paralelo (10 threads)
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(processar_uma_lei, lei): lei['id_lei'] for lei in leis}
            for future in as_completed(futures):
                try:
                    lei_id, texto = future.result()
                    resultado[str(lei_id)] = texto
                except Exception as e:
                    print(f"❌ Erro processando lei: {e}")

        atualizar_banco(resultado)
        print(f"🏁 Lote ID {INICIO} finalizado!")

        INICIO += LIMITE_POR_LOTE
        time.sleep(1)  # pequeno intervalo entre lotes

    print("\n🏆 Todos os lotes processados!")
