import io
import re
from typing import List, Any
from fastapi import FastAPI, File, UploadFile, HTTPException
import pdfplumber

app = FastAPI(title="API Topsol - Interpretador de Faturas COPEL")


# ==========================
# Helpers
# ==========================

def parse_currency(s: str) -> float:
    """Converte '1.234,56' em float."""
    if not s:
        return 0.0
    s = s.strip().replace("R$", "").replace(" ", "")
    s = s.replace("(", "-").replace(")", "")
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except:
        m = re.search(r"-?\d+(\.\d+)?", s)
        if m:
            return float(m.group(0))
    return 0.0


def find_all_currency(text: str) -> List[float]:
    matches = re.findall(r"-?\d{1,3}(?:\.\d{3})*(?:,\d{2})", text)
    return [parse_currency(m) for m in matches]


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).upper()


def extract_section_lines(text: str, keyword: str) -> List[str]:
    """Retorna todas as linhas que contêm o termo keyword."""
    pattern = rf"([^\n]*{re.escape(keyword)}[^\n]*)"
    return [l.strip() for l in re.findall(pattern, text, flags=re.IGNORECASE)]


def extract_table_like_values(segment: str):
    """Extrai kWh, tarifa unitária e valor monetário de um trecho."""
    res = {"qtd": 0.0, "tarifa_unit": None, "valor": 0.0}

    # QTD kWh
    m_kwh = re.findall(
        r"-?\d{1,3}(?:\.\d{3})?(?:[.,]\d+)?(?=\s*(?:kwh|UN))",
        segment,
        flags=re.IGNORECASE,
    )
    if m_kwh:
        res["qtd"] = parse_currency(m_kwh[0])

    # Tarifa unitária
    m_tar = re.search(r"\d+,\d{5,6}", segment)
    if m_tar:
        res["tarifa_unit"] = parse_currency(m_tar.group(0))

    # Valor R$
    m_val = re.findall(r"-?\d{1,3}(?:\.\d{3})*(?:,\d{2})", segment)
    if m_val:
        res["valor"] = parse_currency(m_val[-1])

    return res


def sum_sections_values(text: str, keyword_patterns, exclude_patterns=None):
    """Soma valores de várias seções."""
    if exclude_patterns is None:
        exclude_patterns = []

    entries = []

    for kp in keyword_patterns:
        segs = extract_section_lines(text, kp)
        for s in segs:
            if any(ep.upper() in s.upper() for ep in exclude_patterns):
                continue

            parsed = extract_table_like_values(s)
            parsed["raw"] = s
            parsed["keyword"] = kp
            entries.append(parsed)

    total_qtd = sum(e["qtd"] for e in entries)
    total_valor = sum(e["valor"] for e in entries)

    pis = cofins = icms = 0.0

    all_curr = find_all_currency(text)
    if len(all_curr) >= 3:
        icms, cofins, pis = all_curr[-3:]

    return {
        "qtd": round(total_qtd, 3),
        "valor": round(total_valor, 2),
        "pis": round(pis, 2),
        "cofins": round(cofins, 2),
        "icms": round(icms, 2),
        "entries": entries,
    }


def extract_metadata(text: str):
    """Extrai REF, vencimento, total a pagar, UC e saldo acumulado TP."""
    text_up = text.upper()
    meta = {}

    # REF
    m_ref = re.search(r"(\b(?:0?[1-9]|1[0-2])/[12]\d{3}\b)", text_up)
    if m_ref:
        meta["REF"] = m_ref.group(1)

    # Vencimento
    m_v = re.search(r"VENCIMENT[O|A]?\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})", text_up)
    if m_v:
        meta["VENCIMENTO"] = m_v.group(1)

    # Total a pagar
    m_total = re.search(
        r"TOTAL(?:\s*A\s*PAGAR)?\D{0,20}R?\$?\s*([-]?\d{1,3}(?:\.\d{3})*(?:,\d{2}))",
        text,
        flags=re.IGNORECASE,
    )
    if m_total:
        meta["TOTAL_A_PAGAR"] = parse_currency(m_total.group(1))

    # Unidade Consumidora (nome)
    m_uc = re.search(r"NOME:\s*(.+?)\s{2,}", text, flags=re.IGNORECASE)
    if m_uc:
        meta["UNIDADE_CONSUMIDORA"] = m_uc.group(1).strip()

    # Saldo acumulado TP
    m_saldo = re.search(r"SALDO ACUMULADO.*?([-\d\.,]+)", text_up)
    if m_saldo:
        meta["SALDO_ACUMULADO_TP"] = parse_currency(m_saldo.group(1))

    return meta


# ==========================
# Rota Principal
# ==========================

@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    """Recebe PDF da COPEL e retorna JSON com todos os dados solicitados."""

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Envie um arquivo PDF.")

    data = await file.read()

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            full_text = "\n".join([p.extract_text() or "" for p in pdf.pages])
    except Exception as e:
        raise HTTPException(500, f"Erro ao abrir PDF: {e}")

    text = full_text

    # ---- 1) Energia Injetada ----
    inj = sum_sections_values(
        text,
        ["ENERGIA INJ."],
        exclude_patterns=["ENERGIA INJETADA"],
    )

    # ---- 2) Consumo Energia ----
    cons = sum_sections_values(
        text,
        ["ENERGIA ELET"],
    )

    # ---- 3) Bandeiras ----
    bande_consumida = sum_sections_values(text, ["ENERGIA CONS."])
    bande_compensada = sum_sections_values(text, ["ENERGIA INJ. BAND."])

    # ---- 4) Outros valores ----
    all_curr = find_all_currency(text)
    sum_all_currency = round(sum(all_curr), 2)
    classified_total = round(
        inj["valor"] + cons["valor"] + bande_consumida["valor"] + bande_compensada["valor"],
        2,
    )
    outros = round(sum_all_currency - classified_total, 2)

    # ---- 5) Metadados da Fatura ----
    meta = extract_metadata(text)

    # ---- Resultado Final ----
    return {
        "energia_injetada": inj,
        "consumo_kwh": cons,
        "bandeira": {
            "consumida": bande_consumida,
            "compensada": bande_compensada,
        },
        "outros_valores": {
            "soma_total_documento": sum_all_currency,
            "classificados": classified_total,
            "outros": outros,
        },
        "dados_fatura": meta,
    }
