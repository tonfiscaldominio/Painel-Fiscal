from __future__ import annotations

from io import BytesIO
from datetime import date
import hashlib
import json
import os
import re
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

try:
    from cryptography.fernet import Fernet
except Exception:
    Fernet = None

try:
    from supabase import create_client, Client
except Exception:
    create_client = None
    Client = Any

st.set_page_config(page_title="Painel Fiscal Integrado", page_icon="📊", layout="wide")

SEFAZ_LOGIN_URL = "https://nfe.sefaz.ba.gov.br/servicos/NFENC/SSL/ASLibrary/Login?ReturnUrl=%2fservicos%2fnfenc%2fModulos%2fAutenticado%2fRestrito%2fNFENC_consulta_destinatario.aspx"
CERTIDAO_URL = "https://servicos.sefaz.ba.gov.br/sistemas/DSCRE/Modulos/Publico/EmissaoCertidao.aspx"
SIMPLES_URL = "https://www8.receita.fazenda.gov.br/simplesnacional/aplicacoes.aspx?id=21"

SEFAZ_COLUMNS = {
    "Numero NF-e": "numero_nfe",
    "CNPJ/CPF Emitente": "cnpj_emitente",
    "Razao Social Emitente": "razao_social_emitente",
    "Data de Emissao": "data_emissao",
    "Data de Autorizacao": "data_autorizacao",
    "Valor": "valor",
    "Chave de Acesso": "chave_acesso",
    "UF Emit.": "uf_emitente",
    "Situacao": "situacao",
    "Tipo Operacao": "tipo_operacao",
}


def get_secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, os.getenv(name, default)))
    except Exception:
        return os.getenv(name, default)


def get_supabase() -> Client | None:
    if create_client is None:
        return None
    url = get_secret("SUPABASE_URL")
    key = get_secret("SUPABASE_SERVICE_ROLE_KEY") or get_secret("SUPABASE_ANON_KEY")
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception as exc:
        st.session_state["supabase_error"] = str(exc)
        return None


APP_PASSWORD = get_secret("APP_PASSWORD")
if APP_PASSWORD and not st.session_state.get("authenticated"):
    st.title("Acesso ao Painel Fiscal Integrado")
    st.caption("Informe a senha de acesso compartilhada definida pelo administrador.")
    with st.form("shared_login"):
        access_password = st.text_input("Senha de acesso", type="password")
        submitted = st.form_submit_button("Entrar")
    if submitted and access_password == APP_PASSWORD:
        st.session_state["authenticated"] = True
        st.rerun()
    if submitted:
        st.error("Senha incorreta.")
    st.stop()


def normalise_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def clean_cnpj(value: Any) -> str:
    return re.sub(r"\D", "", normalise_text(value))


def parse_brl(value: Any) -> float:
    text = normalise_text(value).replace("'", "")
    if not text:
        return 0.0
    text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_sefaz_csv(uploaded_file) -> pd.DataFrame:
    raw = uploaded_file.getvalue()
    for encoding in ("utf-8-sig", "latin1"):
        try:
            df = pd.read_csv(BytesIO(raw), sep=";", dtype=str, encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    df = df.rename(columns={col: SEFAZ_COLUMNS.get(str(col).strip(), str(col).strip()) for col in df.columns})
    df = df[[col for col in SEFAZ_COLUMNS.values() if col in df.columns]].copy()
    for col in df.columns:
        df[col] = df[col].astype("string").str.strip()
    for col in ["data_emissao", "data_autorizacao"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], dayfirst=True, errors="coerce")
    if "valor" in df.columns:
        df["valor"] = df["valor"].map(parse_brl)
    if "chave_acesso" in df.columns:
        df["chave_acesso"] = df["chave_acesso"].str.replace("'", "", regex=False).str.strip()
    if "cnpj_emitente" in df.columns:
        df["cnpj_emitente"] = df["cnpj_emitente"].map(clean_cnpj)
    df["origem_arquivo_hash"] = hashlib.sha256(raw).hexdigest()
    return df


def format_brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def detect_anomalies(df: pd.DataFrame) -> pd.DataFrame:
    findings: list[dict[str, Any]] = []
    if df.empty:
        return pd.DataFrame(columns=["severidade", "regra", "chave_acesso", "descricao", "valor"])
    duplicate = df["chave_acesso"].duplicated(keep=False) if "chave_acesso" in df.columns else pd.Series(False, index=df.index)
    for idx, row in df.iterrows():
        key = normalise_text(row.get("chave_acesso"))
        value = float(row.get("valor", 0) or 0)
        if duplicate.loc[idx]:
            findings.append({"severidade": "Alta", "regra": "NF-e duplicada", "chave_acesso": key, "descricao": "Chave de acesso repetida no relatório.", "valor": value})
        if len(key) != 44 or not key.isdigit():
            findings.append({"severidade": "Média", "regra": "Chave inválida", "chave_acesso": key, "descricao": "Chave de acesso diferente de 44 dígitos.", "valor": value})
        if value <= 0:
            findings.append({"severidade": "Alta", "regra": "Valor não positivo", "chave_acesso": key, "descricao": "Documento com valor menor ou igual a zero.", "valor": value})
        emission = row.get("data_emissao")
        authorization = row.get("data_autorizacao")
        if pd.notna(emission) and pd.notna(authorization) and authorization < emission:
            findings.append({"severidade": "Alta", "regra": "Data inconsistente", "chave_acesso": key, "descricao": "Autorização anterior à emissão.", "valor": value})
    return pd.DataFrame(findings, columns=["severidade", "regra", "chave_acesso", "descricao", "valor"])


def compare_internal(df_sefaz: pd.DataFrame, internal_file) -> pd.DataFrame:
    internal = pd.read_excel(internal_file, dtype=str)
    aliases = {str(c).strip().lower(): c for c in internal.columns}
    key_col = next((aliases[k] for k in ["chave_acesso", "chave de acesso", "chave_nfe", "chave nf-e"] if k in aliases), None)
    value_col = next((aliases[k] for k in ["valor", "valor_total", "valor total"] if k in aliases), None)
    if not key_col:
        raise ValueError("O Excel interno precisa conter uma coluna de chave de acesso.")
    internal = internal.rename(columns={key_col: "chave_acesso"})
    internal["chave_acesso"] = internal["chave_acesso"].astype(str).str.replace("'", "", regex=False).str.strip()
    if value_col:
        internal = internal.rename(columns={value_col: "valor_interno"})
        internal["valor_interno"] = internal["valor_interno"].map(parse_brl)
    else:
        internal["valor_interno"] = pd.NA
    base = df_sefaz[["chave_acesso", "valor", "cnpj_emitente", "razao_social_emitente", "tipo_operacao"]].copy()
    base = base.rename(columns={"valor": "valor_sefaz"})
    result = base.merge(internal[["chave_acesso", "valor_interno"]], on="chave_acesso", how="outer", indicator=True)
    result["status_confronto"] = result["_merge"].map({"both": "Encontrada nas duas bases", "left_only": "Somente SEFAZ", "right_only": "Somente base interna"})
    result["diferenca_valor"] = result["valor_sefaz"].fillna(0) - result["valor_interno"].fillna(0)
    result.loc[result["_merge"] != "both", "diferenca_valor"] = pd.NA
    return result.drop(columns=["_merge"])


def excel_bytes(df: pd.DataFrame, sheet: str = "relatorio") -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet[:31])
    return output.getvalue()


def supabase_insert(table: str, rows: list[dict[str, Any]]) -> tuple[bool, str]:
    client = get_supabase()
    if not client:
        return False, "Supabase não configurado."
    try:
        client.table(table).insert(rows).execute()
        return True, "Dados salvos no Supabase."
    except Exception as exc:
        return False, str(exc)


st.title("Painel Fiscal Integrado")
st.caption("Cadastro de empresas, consultas autorizadas, relatórios SEFAZ-BA e auditorias")

with st.sidebar:
    st.header("Empresa e período")
    mode_demo = st.toggle("Modo demonstrativo", value=True, help="Use para testar sem configurar o Supabase.")
    regime = st.selectbox("Regime tributário", ["Simples Nacional", "Lucro Presumido", "Lucro Real", "Não informado"])
    reference_date = st.date_input("Data de referência", value=date.today())
    st.divider()
    st.write("**Consultas externas**")
    st.link_button("Abrir portal NF-e / SEFAZ-BA", SEFAZ_LOGIN_URL)
    st.link_button("Emitir certidão SEFAZ-BA", CERTIDAO_URL)
    st.link_button("Consultar Simples Nacional", SIMPLES_URL)
    if get_supabase() is None and not mode_demo:
        st.warning("Configure SUPABASE_URL e SUPABASE_ANON_KEY em Secrets antes de usar o modo compartilhado.")

supabase = get_supabase()

# Estado das bases
if "sefaz_df" not in st.session_state:
    st.session_state["sefaz_df"] = pd.DataFrame()
if "company" not in st.session_state:
    st.session_state["company"] = {}

company_tab, sefaz_tab, audit_tab, cert_tab, reports_tab = st.tabs(["Cadastro da empresa", "Relatório SEFAZ-BA", "Auditorias", "Certidões e consultas", "Relatórios"])

with company_tab:
    st.subheader("Cadastro compartilhado da empresa")
    st.write("Cadastre os dados operacionais da empresa. Senhas e certificados não devem ser colocados neste formulário nem em arquivos do GitHub.")
    with st.form("company_form"):
        c1, c2 = st.columns(2)
        company_name = c1.text_input("Razão social")
        cnpj = c2.text_input("CNPJ")
        c3, c4 = st.columns(2)
        responsible_cpf = c3.text_input("CPF do responsável", type="password", help="Usado apenas como campo protegido; não será exibido no painel.")
        state_registration = c4.text_input("Inscrição estadual")
        city = st.text_input("Município / unidade")
        save_company = st.form_submit_button("Salvar cadastro")
    if save_company:
        company = {"razao_social": company_name, "cnpj": clean_cnpj(cnpj), "cpf_responsavel_hash": hashlib.sha256(clean_cnpj(responsible_cpf).encode()).hexdigest() if responsible_cpf else "", "inscricao_estadual": state_registration, "municipio": city, "regime": regime}
        st.session_state["company"] = company
        if mode_demo:
            st.success("Cadastro mantido em memória no modo demonstrativo.")
        else:
            ok, msg = supabase_insert("companies", [company])
            (st.success if ok else st.error)(msg)
    if st.session_state["company"]:
        st.info(f"Empresa ativa: {st.session_state['company'].get('razao_social') or 'não informada'} — CNPJ {st.session_state['company'].get('cnpj') or 'não informado'}")

with sefaz_tab:
    st.subheader("Importação do relatório de destinatário da SEFAZ-BA")
    st.warning("O portal informado exige login e pode bloquear o IP após tentativas inválidas. Esta versão abre o portal para a sessão autorizada e importa o arquivo baixado; não tenta contornar CAPTCHA, certificado, bloqueio ou controles de acesso.")
    uploaded = st.file_uploader("Envie o CSV baixado do portal", type=["csv", "txt"], key="sefaz_upload")
    if uploaded is not None:
        try:
            parsed = parse_sefaz_csv(uploaded)
            st.session_state["sefaz_df"] = parsed
            if mode_demo:
                st.success(f"{len(parsed):,} registros carregados no modo demonstrativo.".replace(",", "."))
            else:
                rows = parsed.drop(columns=["data_emissao", "data_autorizacao"], errors="ignore").copy()
                rows = rows.where(pd.notna(rows), None).to_dict(orient="records")
                company_id = st.session_state.get("company", {}).get("id")
                if company_id:
                    for row in rows:
                        row["company_id"] = company_id
                ok, msg = supabase_insert("sefaz_documents", rows)
                (st.success if ok else st.error)(msg)
        except Exception as exc:
            st.error(f"Falha ao interpretar o relatório: {exc}")
    df = st.session_state["sefaz_df"]
    if not df.empty:
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Documentos", f"{len(df):,}".replace(",", "."))
        k2.metric("Valor total", format_brl(df["valor"].sum()))
        k3.metric("Entradas", format_brl(df.loc[df["tipo_operacao"].str.lower().eq("entrada"), "valor"].sum()))
        k4.metric("Saídas", format_brl(df.loc[df["tipo_operacao"].str.lower().eq("saida"), "valor"].sum()))
        st.dataframe(df.drop(columns=["origem_arquivo_hash"], errors="ignore"), use_container_width=True, hide_index=True)
        monthly = df.assign(mes=df["data_emissao"].dt.to_period("M").astype(str)).groupby(["mes", "tipo_operacao"], as_index=False)["valor"].sum()
        st.plotly_chart(px.bar(monthly, x="mes", y="valor", color="tipo_operacao", barmode="group", title="Movimentação por operação"), use_container_width=True)

with audit_tab:
    st.subheader("Auditorias automáticas")
    df = st.session_state["sefaz_df"]
    if df.empty:
        st.info("Importe um relatório SEFAZ-BA para executar as auditorias.")
    else:
        anomalies = detect_anomalies(df)
        a, b, c = st.columns(3)
        a.metric("Achados", len(anomalies))
        b.metric("Alta severidade", int((anomalies["severidade"] == "Alta").sum()))
        c.metric("Regras avaliadas", 5)
        if anomalies.empty:
            st.success("Nenhuma anomalia foi encontrada nas regras básicas.")
        else:
            st.dataframe(anomalies, use_container_width=True, hide_index=True)
            st.download_button("Baixar achados", excel_bytes(anomalies, "achados"), "achados_sefaz.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.divider()
        st.subheader("Confronto com base interna")
        internal_file = st.file_uploader("Excel interno com chave de acesso e, opcionalmente, valor", type=["xlsx", "xls"], key="internal_upload")
        if internal_file is not None:
            try:
                comparison = compare_internal(df, internal_file)
                st.dataframe(comparison, use_container_width=True, hide_index=True)
                st.download_button("Baixar confronto", excel_bytes(comparison, "confronto"), "confronto_sefaz.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            except Exception as exc:
                st.error(str(exc))

with cert_tab:
    st.subheader("Certidões e consultas externas")
    st.write("Use os links oficiais para executar a consulta. Depois registre o resultado e anexe a evidência no sistema.")
    q1, q2 = st.columns(2)
    with q1:
        cert_type = st.selectbox("Tipo de certidão", ["Débitos tributários SEFAZ-BA", "Autenticidade de certidão", "Baixa de inscrição", "Outra"])
        cert_status = st.selectbox("Resultado", ["Não consultada", "Negativa", "Positiva", "Positiva com efeito de negativa", "Não se aplica"])
    with q2:
        cert_number = st.text_input("Número da certidão")
        cert_expiry = st.date_input("Validade, se houver", value=date.today())
    cert_file = st.file_uploader("Evidência da certidão", type=["pdf", "png", "jpg"], key="cert_file")
    if st.button("Registrar consulta de certidão"):
        record = {"tipo": cert_type, "resultado": cert_status, "numero": cert_number, "validade": str(cert_expiry), "empresa_cnpj": st.session_state.get("company", {}).get("cnpj", "")}
        if mode_demo:
            st.success("Consulta registrada no modo demonstrativo.")
        else:
            ok, msg = supabase_insert("certificates", [record])
            (st.success if ok else st.error)(msg)

with reports_tab:
    st.subheader("Relatórios e exportação")
    df = st.session_state["sefaz_df"]
    if df.empty:
        st.info("Importe o relatório SEFAZ-BA para habilitar os downloads.")
    else:
        st.download_button("Baixar relatório normalizado em Excel", excel_bytes(df, "sefaz_ba"), "relatorio_sefaz_normalizado.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        anomalies = detect_anomalies(df)
        st.download_button("Baixar pacote de auditoria em Excel", excel_bytes(anomalies, "auditoria"), "pacote_auditoria.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.write("O pacote completo deverá evoluir para incluir cadastro da empresa, certidões, histórico de consultas, confrontos e decisão sobre cada achado.")

st.divider()
st.caption("Sistema de apoio à auditoria. Verifique regras, prazos e resultados com o profissional contábil responsável antes de qualquer entrega fiscal.")
