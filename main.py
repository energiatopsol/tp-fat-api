import io
import re
from typing import List, Dict, Any
from fastapi import FastAPI, File, UploadFile, HTTPException
import pdfplumber

app = FastAPI(title="API Topsol - Interpretador COPEL (final)")

# ----------------------
# Helpers de conversão
# ----------------------
def parse_currency(s: str) -> float:
    """Converte '1.234,56' ou '-3.714,00' em float."""
    if not s:
        return 0.0
    s = s.strip().replace("R$", "").replace(" ", "")
    s = s.replace("(", "-").replace(")", "")
    # remove thousands separators, keep decimal
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except:
        m = re.search(r"-?\d+(\.\d+)?", s)
        return float(m.group(0)) if m else 0.0

def parse_qty_kwh_token(tok: str) -> float:
    """Tenta interpretar um token numérico como quantidade kWh (mantém sinal)."""
    if not tok:
        return 0.0
    tok = tok.strip()
    # aceitar -3.714,00 ou -3714 ou 5183
    tok = tok.replace(".", "").replace(",", ".")
    try:
        return float(tok)
    except:
        return 0.0

def find_monetary_in_line(line: str) -> List[float]:
    """Encontra todos valores monetários do tipo x.xxx,yy na linha."""
    matches = re.findall(r"-?\d{1,3}(?:\.\d{3})*(?:,\d{2})", line)
    return [parse_currency(m) for m in matches]

def find_qty_candidates(line: str) -> List[float]:
    """Encontra números que podem ser qtd (kWh) na linha; retorna floats."""
    # pega padrões de número com possível sinal e milhares
    matches = re.findall(r"-?\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?", line)
    return [parse_qty_kwh_token(m) for m in matches]

# ----------------------
# Funções específicas para o quadro "Itens de Fatura"
# ----------------------
def extract_invoice_items_block(text: str) -> List[str]:
    """
    Retorna linhas entre 'ENERGIA ELET CONSUMO' (inclusive) e
    'CONT ILUMIN PUBLICA MUNICIPIO' (inclusive). Se não achar, tenta heurística.
    """
    up = text.upper()
    start_idx = up.find("ENERGIA ELET CONSUMO")
    end_idx = up.find("CONT ILUMIN PUBLICA MUNICIPIO")
    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        # fallback: tentar entre 'ENERGIA ELET' e 'ILUMIN'
        start_idx = up.find("ENERGIA ELET")
        end_idx = up.find("ILUMIN")
        if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
            # retorno todo o texto dividido se não detectar as delimitações
            return [line for line in text.splitlines() if line.strip()]
    block = text[start_idx:end_idx + len("CONT ILUMIN PUBLICA MUNICIPIO")]
    # dividir por linhas e filtrar vazias
    lines = [l.strip() for l in block.splitlines() if l.strip()]
    return lines

def classify_and_sum_lines(lines: List[str], ref: str = None) -> Dict[str, Any]:
    """
    Recebe as linhas do quadro e separa em:
      - consumo (ENERGIA ELET*)
      - injetada (ENERGIA INJ. OUC MPT TE/TUS/TUSD)  -> todos os meses (conforme ajuste)
      - bandeira consumida (ENERGIA CONS. B.)
      - bandeira compensada (ENERGIA INJ. BAND.)
      - outros (tudo no quadro que não for os acima; inclui ilum, multas, juros)
    Retorna somatórios com qtd, valor e impostos (pis/cofins/icms por linha quando presente).
    Observação: as colunas no bloco seguem layout: col1 desc | col2 ignore | col3 qtd | col4 ignore | col5 valor | col6 pis/cofins | col7 icms
    """
    consumo = {"qtd": 0.0, "valor": 0.0, "pis": 0.0, "cofins": 0.0, "icms": 0.0}
    injetada = {"qtd": 0.0, "valor": 0.0, "pis": 0.0, "cofins": 0.0, "icms": 0.0}
    bande_consumida = {"qtd": 0.0, "valor": 0.0, "pis": 0.0, "cofins": 0.0, "icms": 0.0}
    bande_comp = {"qtd": 0.0, "valor": 0.0, "pis": 0.0, "cofins": 0.0, "icms": 0.0}
    outros = {"valor": 0.0}

    for line in lines:
        up = line.upper()
        # ignore header lines that merely repeat labels
        if re.match(r'^(ENERGIA|KWH|UN|ICMS|PIS|COFINS|HISTÓRICO|CONSUMO)', up) and len(line.split()) < 3:
            continue

        # Extract monetary columns heuristically:
        # Strategy: find all monetary values (x.xxx,yy). Column 5 is usually the last monetary before pis/cofins/icms.
        monetary = re.findall(r"-?\d{1,3}(?:\.\d{3})*(?:,\d{2})", line)
        # find qty candidates
        qtys = find_qty_candidates(line)

        # prepare parsed fields
        qtd = 0.0
        valor = 0.0
        pis = 0.0
        cofins = 0.0
        icms = 0.0

        # heurística: se houver 3 ou mais valores monetários na linha, considerar:
        #   ... [valor] [pis/cofins] [icms]  -> então último é icms, penúlt pis/cofins, antepenúlt valor
        if len(monetary) >= 3:
            icms = parse_currency(monetary[-1])
            pis_or_cofins = parse_currency(monetary[-2])
            valor = parse_currency(monetary[-3])
            # distribuimos pis/cofins por metade (não sabemos qual é qual quando só há um campo),
            # mas na maior parte das linhas da Copel há dois valores iguais para PIS e COFINS em bloco separado.
            # Para segurança, deixamos pis=0 e cofins=0 aqui; impostos finais serão lidos do bloco de impostos.
        elif len(monetary) == 2:
            # pode ser [valor] [icms] ou [valor] [pis/cofins]
            valor = parse_currency(monetary[-2])
            # decidir se último é ICMS (muito provável se valor pequeno) — confiamos menos aqui
            icms_candidate = parse_currency(monetary[-1])
            # se icms_candidate <= valor, provavelmente é ICMS; senão, pode ser PIS/COFINS
            icms = icms_candidate if icms_candidate <= valor else 0.0
            if icms == 0.0:
                # assume pertence a PIS/COFINS (soma em cofins)
                cofins = icms_candidate
        elif len(monetary) == 1:
            valor = parse_currency(monetary[0])

        # qty selection: prefer numbers that are plausibly kWh (>=1)
        if qtys:
            # pick largest absolute number as qtd
            qtd = max(qtys, key=lambda x: abs(x))

        # Classificação pela descrição
        if "ENERGIA ELET" in up:
            # consumo line
            consumo["qtd"] += qtd
            consumo["valor"] += valor
            consumo["pis"] += pis
            consumo["cofins"] += cofins
            consumo["icms"] += icms
        elif re.search(r"ENERGIA INJ\..*OUC MPT.*TE|ENERGIA INJ\..*OUC MPT.*TUS|ENERGIA INJ\..*TUSD", up):
            # injetada TE/TUS/TUSD (qualquer mês)
            injetada["qtd"] += abs(qtd)  # tornar positivo
            injetada["valor"] += abs(valor)
            injetada["pis"] += pis
            injetada["cofins"] += cofins
            injetada["icms"] += icms
        elif "ENERGIA CONS. B." in up or up.startswith("ENERGIA CONS. B"):
            # bandeira consumida (B.)
            bande_consumida["qtd"] += qtd
            bande_consumida["valor"] += valor
            bande_consumida["pis"] += pis
            bande_consumida["cofins"] += cofins
            bande_consumida["icms"] += icms
        elif "ENERGIA INJ. BAND" in up or "ENERGIA INJ. BAND." in up:
            # bandeira compensada (injetada para bandeira)
            bande_comp["qtd"] += abs(qtd)
            bande_comp["valor"] += abs(valor)
            bande_comp["pis"] += pis
            bande_comp["cofins"] += cofins
            bande_comp["icms"] += icms
        else:
            # outros (inclui CONT ILUMIN PUBLICA MUNICIPIO, multas, juros, taxas)
            outros["valor"] += valor

    # garantir formatos corretos e arredondamento
    for d in (consumo, injetada, bande_consumida, bande_comp):
        for k in d:
            d[k] = round(d[k], 2) if isinstance(d[k], float) else d[k]
    outros["valor"] = round(outros["valor"], 2)

    return {
        "consumo": consumo,
        "injetada": injetada,
        "bande_consumida": bande_consumida,
        "bande_compensada": bande_comp,
        "outros": outros,
    }

def extract_tax_block_values(text: str) -> Dict[str, float]:
    """
    Extrai os valores cobrados de ICMS, COFINS e PIS a partir do bloco onde essas palavras aparecem.
    """
    text_up = text.upper()
    # procurar o trecho que contenha "ICMS" seguido de valores próximos
    m = re.search(r"(ICMS[\s\S]{0,200}PIS)", text_up)
    if m:
        snippet = text_up[m.start(): m.start() + 400]
        vals = re.findall(r"-?\d{1,3}(?:\.\d{3})*(?:,\d{2})", snippet)
        # esperar que apareçam as bases e depois os valores; pegar os últimos 3 valores monetários
        if len(vals) >= 3:
            last3 = vals[-3:]
            return {
                "icms": round(parse_currency(last3[0]), 2),
                "cofins": round(parse_currency(last3[1]), 2),
                "pis": round(parse_currency(last3[2]), 2),
            }
    # fallback: procurar linhas individuais contendo as palavras
    icms = cofins = pis = 0.0
    m_icms = re.search(r"ICMS[^\d\-]*(-?\d{1,3}(?:\.\d{3})*(?:,\d{2}))", text, flags=re.IGNORECASE)
    if m_icms:
        icms = parse_currency(m_icms.group(1))
    m_cof = re.search(r"COFINS[^\d\-]*(-?\d{1,3}(?:\.\d{3})*(?:,\d{2}))", text, flags=re.IGNORECASE)
    if m_cof:
        cofins = parse_currency(m_cof.group(1))
    m_pis = re.search(r"PIS[^\d\-]*(-?\d{1,3}(?:\.\d{3})*(?:,\d{2}))", text, flags=re.IGNORECASE)
    if m_pis:
        pis = parse_currency(m_pis.group(1))
    return {"icms": round(icms, 2), "cofins": round(cofins, 2), "pis": round(pis, 2)}

# ----------------------
# Rota principal
# ----------------------
@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    # validação
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Envie um arquivo PDF.")

    data = await file.read()
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao abrir PDF: {e}")

    full_text = "\n".join(pages)
    if not full_text.strip():
        raise HTTPException(status_code=400, detail="PDF sem texto extraível (use versão textual ou OCR).")

    # extrai bloco "itens de fatura"
    lines_block = extract_invoice_items_block(full_text)

    # classificar e somar
    sums = classify_and_sum_lines(lines_block)

    # extrair impostos (valores cobrados)
    taxes = extract_tax_block_values(full_text)

    # total a pagar (buscar "TOTAL" próximo)
    m_total = re.search(r"TOTAL(?:\s*A\s*PAGAR)?\D{0,30}R?\$?\s*(-?\d{1,3}(?:\.\d{3})*(?:,\d{2}))", full_text, flags=re.IGNORECASE)
    total_pagar = parse_currency(m_total.group(1)) if m_total else None

    # REF e vencimento e UC
    m_ref = re.search(r"(\b(?:0?[1-9]|1[0-2])/[12]\d{3}\b)", full_text)
    ref = m_ref.group(1) if m_ref else None
    m_v = re.search(r"VENCIMENT[O|A]?\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})", full_text, flags=re.IGNORECASE)
    venc = m_v.group(1) if m_v else None
    m_uc = re.search(r"Nome:\s*(.+?)\s{2,}", full_text, flags=re.IGNORECASE)
    uc = m_uc.group(1).strip() if m_uc else None

    # preparar JSON resumido (formato A)
    result = {
        "energia_injetada": {
            "qtd_kwh": round(sums["injetada"]["qtd"], 3),
            "valor": round(sums["injetada"]["valor"], 2),
            "pis": taxes.get("pis", 0.0),
            "cofins": taxes.get("cofins", 0.0),
            "icms": taxes.get("icms", 0.0)
        },
        "consumo_kwh": {
            "qtd_kwh": round(sums["consumo"]["qtd"], 3),
            "valor": round(sums["consumo"]["valor"], 2),
            "pis": taxes.get("pis", 0.0),
            "cofins": taxes.get("cofins", 0.0),
            "icms": taxes.get("icms", 0.0),
            "tarifa_unit": (round(sums["consumo"]["valor"] / sums["consumo"]["qtd"], 6) if sums["consumo"]["qtd"] else None)
        },
        "bandeira": {
            "consumida": {
                "qtd_kwh": round(sums["bande_consumida"]["qtd"], 3),
                "valor": round(sums["bande_consumida"]["valor"], 2),
                "pis": taxes.get("pis", 0.0),
                "cofins": taxes.get("cofins", 0.0),
                "icms": taxes.get("icms", 0.0)
            },
            "compensada": {
                "qtd_kwh": round(sums["bande_compensada"]["qtd"], 3),
                "valor": round(sums["bande_compensada"]["valor"], 2),
                "pis": taxes.get("pis", 0.0),
                "cofins": taxes.get("cofins", 0.0),
                "icms": taxes.get("icms", 0.0)
            }
        },
        "outros_valores": {
            "valor": round(sums["outros"]["valor"], 2)
        },
        "dados_fatura": {
            "REF": ref,
            "VENCIMENTO": venc,
            "TOTAL_A_PAGAR": round(total_pagar, 2) if total_pagar is not None else None,
            "UNIDADE_CONSUMIDORA": uc
        }
    }

    return result

# rota simples de status
@app.get("/")
def home():
    return {"status": "API Topsol COPEL funcionando. Use /docs para testar."}
