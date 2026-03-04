#!/usr/bin/env python3
"""Extrai exames de um PDF e gera exams-data.js para o painel HTML."""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader


def clean(text: str) -> str:
    normalized = (text or "").replace("\x00", " ")
    normalized = re.sub(r"[\x01-\x1f\x7f]", " ", normalized)
    return " ".join(normalized.split())


def parse_num(text: str):
    m = re.search(r"(-?\d{1,3}(?:\.\d{3})*(?:,\d+)?|-?\d+(?:,\d+)?)", text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def parse_val_unit(result: str):
    result = clean(result)
    value = parse_num(result)
    unit = ""
    mm = re.search(r"(?:-?\d{1,3}(?:\.\d{3})*(?:,\d+)?|-?\d+(?:,\d+)?)[ ]*([%A-Za-zµ/]+(?:/[A-Za-zµ]+)?)", result)
    if mm:
        unit = mm.group(1)
    return value, unit


def parse_ref(ref_text: str, exam_name: str):
    t = clean(ref_text).upper()
    if not t:
        return None, None, None

    patterns = [
        (r"HOMENS?\s*:\s*DE\s*([\d\.,]+)\s*A\s*([\d\.,]+)", "range"),
        (r"HOMEM\s*:\s*DE\s*([\d\.,]+)\s*A\s*([\d\.,]+)", "range"),
        (r"HOMENS?\s*:\s*INFERIOR(?: OU IGUAL)?\s*A\s*([\d\.,]+)", "max"),
        (r"HOMEM\s*:\s*INFERIOR(?: OU IGUAL)?\s*A\s*([\d\.,]+)", "max"),
        (r"HOMENS?\s*:\s*SUPERIOR(?: OU IGUAL)?\s*A\s*([\d\.,]+)", "min"),
    ]
    for patt, kind in patterns:
        m = re.search(patt, t)
        if not m:
            continue
        if kind == "range":
            mn = float(m.group(1).replace(".", "").replace(",", "."))
            mx = float(m.group(2).replace(".", "").replace(",", "."))
            return mn, mx, f"Homem: {m.group(1)} a {m.group(2)}"
        if kind == "max":
            mx = float(m.group(1).replace(".", "").replace(",", "."))
            return None, mx, f"Homem: <={m.group(1)}"
        mn = float(m.group(1).replace(".", "").replace(",", "."))
        return mn, None, f"Homem: >={m.group(1)}"

    if "FERRITINA" in exam_name.upper():
        m = re.search(r"ADULTO[^\n]*?DE\s*([\d\.,]+)\s*A\s*([\d\.,]+)[^\n]*?DE\s*([\d\.,]+)\s*A\s*([\d\.,]+)", t)
        if m:
            mn = float(m.group(3).replace(".", "").replace(",", "."))
            mx = float(m.group(4).replace(".", "").replace(",", "."))
            return mn, mx, f"Homem adulto: {m.group(3)} a {m.group(4)}"

    m = re.search(r"DE\s*([\d\.,]+)\s*A\s*([\d\.,]+)", t)
    if m:
        return float(m.group(1).replace(".", "").replace(",", ".")), float(m.group(2).replace(".", "").replace(",", ".")), f"{m.group(1)} a {m.group(2)}"

    m = re.search(r"INFERIOR(?: OU IGUAL)?\s*A\s*([\d\.,]+)", t) or re.search(r"MENOR QUE\s*([\d\.,]+)", t)
    if m:
        return None, float(m.group(1).replace(".", "").replace(",", ".")), f"<={m.group(1)}"

    m = re.search(r"SUPERIOR(?: OU IGUAL)?\s*A\s*([\d\.,]+)", t)
    if m:
        return float(m.group(1).replace(".", "").replace(",", ".")), None, f">={m.group(1)}"

    return None, None, None


def normalize_name(s: str) -> str:
    return clean(s).upper()


def parse_dt(raw: str) -> datetime:
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%y %H:%M", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    return datetime.min


def build_payload(pdf_path: Path):
    full = "\n".join((p.extract_text() or "") for p in PdfReader(str(pdf_path)).pages)

    m_name = re.search(r"\n([A-ZÁÉÍÓÚÂÊÔÃÕÇ ]{8,})\s+(\d{2}/\d{2}/\d{4}\s*\(\d+ anos\))", full)
    name, birth_age = (m_name.group(1).strip(), m_name.group(2).strip()) if m_name else ("Paciente", "")

    m_meta = re.search(r"\n([A-ZÇÁÉÍÓÚÂÊÔÃÕ ]+ - CRM-[A-Z]{2} \d+)\s+(\d{2}/\d{2}/\d{4})\s+([\w-]+)", full)
    requester, entry_date, order_id = (m_meta.group(1).strip(), m_meta.group(2), m_meta.group(3)) if m_meta else ("", "", "")

    labs = sorted(set(x.strip() for x in re.findall(r"Respons[áa]vel T[ée]cnico:\s*([^\n]+)", full, re.I)))
    addresses = sorted(set(x.strip() for x in re.findall(r"(Rua [^\n]+|End\.: [^\n]+)", full)))
    phones = sorted(set(x.strip() for x in re.findall(r"(Telefone[^\n]+)", full)))

    starts = list(re.finditer(r"\[DATA DA COLETA\s*:\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})\]", full))
    alias = {
        "TRANSAMINASE PIRÚVICA": "TGP",
        "TRANSAMINASE OXALACÉTICA": "TGO",
        "TSH ULTRA SENSÍVEL": "TSH BASAL",
        "CREATINOFOSFOQUINASE": "CPK",
        "PSA LIVRE / TOTAL": "PSA TOTAL",
    }

    exams = []
    for i, match in enumerate(starts):
        dt = match.group(1)
        prev = full[: match.start()]
        lines = [ln.strip() for ln in prev.split("\n") if ln.strip()]
        title = clean(lines[-1]) if lines else "EXAME"

        seg_end = starts[i + 1].start() if i + 1 < len(starts) else len(full)
        seg = full[match.end() : seg_end]

        if normalize_name(title) == "HEMOGRAMA":
            keep = []
            for line in seg.splitlines():
                c = clean(line)
                if any(k in c for k in ["Hemacias :", "Hemoglobina:", "Hematocrito:", "Leucocitos - Global:", "Plaquetas:"]):
                    keep.append(c)
            result = " | ".join(keep)
        else:
            rm = re.search(r"\nRESULTADOS?:\s*([^\n]+)", "\n" + seg)
            result = clean(rm.group(1)) if rm else ""
            if normalize_name(title) == "HEMOGLOBINA GLICADA (A1C)":
                gm = re.search(r"GLICEMIA M[ÉE]DIA ESTIMADA[\s\S]*?RESULTADO:\s*([^\n]+)", seg)
                if gm:
                    result = f"{result}; Glicemia média estimada: {clean(gm.group(1))}"

        rfm = re.search(r"VALOR(?:ES)? DE REFER[ÊE]NCIA:?([\s\S]*?)(?=\n(?:NOTA|NOTAS|Atenção|Este laudo|RESULTADOS ANTERIORES|CNES:|GLICEMIA M[ÉE]DIA ESTIMADA|$))", seg, re.I)
        ref = clean(rfm.group(1)) if rfm else ""
        if normalize_name(title) == "HEMOGRAMA":
            ref = "Faixas de referência por componente (hemácias, hemoglobina, hematócrito, leucócitos e plaquetas)."

        val, unit = parse_val_unit(result)
        ref_min, ref_max, ref_label = parse_ref(ref, title)

        records = [{"date": dt, "valueText": result, "value": val, "unit": unit, "reference": ref_label or ref[:120]}]

        pm = re.search(r"RESULTADOS ANTERIORES\s+([^\n]+)", seg)
        if pm:
            date_tokens = re.findall(r"(\d{2}/\d{2}/\d{2,4}\s+\d{2}:\d{2})", pm.group(1))
            tail = seg[pm.end() :]
            rows = [clean(x) for x in tail.splitlines()[:8] if clean(x)]
            candidates = []
            for row in rows:
                if row.startswith("CNES:") or row.startswith("Resultado impresso") or row.startswith("____________________________________"):
                    break
                candidates.append(row)

            target = None
            exam_key = alias.get(title, "")
            for row in candidates:
                nrow = normalize_name(row)
                if nrow.startswith(normalize_name(title)) or (exam_key and nrow.startswith(exam_key)):
                    target = row
                    break

            if target:
                after = target.split(" ", 1)[1] if " " in target else ""
                vals = re.findall(r"(-?\d{1,3}(?:\.\d{3})*(?:,\d+)?|-?\d+(?:,\d+)?)", after)
                for d, v in zip(date_tokens, vals):
                    records.append({"date": d, "valueText": v, "value": parse_num(v), "unit": unit, "reference": ref_label or ""})

        exams.append(
            {
                "name": title,
                "collectedAt": dt,
                "resultText": result,
                "referenceText": ref,
                "refMin": ref_min,
                "refMax": ref_max,
                "history": sorted(records, key=lambda x: parse_dt(x["date"]), reverse=True),
            }
        )

    unique = []
    seen = set()
    for exam in exams:
        if exam["name"] in seen:
            continue
        seen.add(exam["name"])
        unique.append(exam)

    return {
        "patient": {
            "name": name,
            "birthAndAge": birth_age,
            "sex": "Masculino",
            "age": 41,
            "weightKg": 92,
            "heightCm": 170,
            "requester": requester,
            "entryDate": entry_date,
            "orderId": order_id,
            "labs": labs,
            "addresses": addresses,
            "phones": phones,
            "generatedAt": datetime.now().strftime("%d/%m/%Y %H:%M"),
        },
        "exams": unique,
    }


def main():
    if len(sys.argv) < 2:
        print("Uso: python3 extract_exams.py <arquivo.pdf> [saida.js]")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("exams-data.js")

    payload = build_payload(pdf_path)
    out_path.write_text("window.EXAMS_DATA = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n", encoding="utf-8")

    print(f"Gerado: {out_path} ({len(payload['exams'])} exames)")


if __name__ == "__main__":
    main()
