from __future__ import annotations

from io import BytesIO
from datetime import date
import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import urljoin
import base64
import requests
from bs4 import BeautifulSoup

import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components

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
FEDERAL_CERTIDAO_URL = "https://servicos.receitafederal.gov.br/servico/certidoes/"
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


def encrypt_sefaz_password(password: str) -> str:
    key = get_secret("SEFAZ_ENCRYPTION_KEY")
    if not password:
        return ""
    if not key or Fernet is None:
        raise ValueError("Configure SEFAZ_ENCRYPTION_KEY nos Secrets antes de salvar a senha SEFAZ.")
    try:
        return Fernet(key.encode()).encrypt(password.encode()).decode()
    except Exception as exc:
        raise ValueError(f"Chave de criptografia inválida: {exc}") from exc


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


def format_cnpj(cnpj: str) -> str:
    digits = clean_cnpj(cnpj)
    if len(digits) != 14:
        return digits
    return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"


def emit_certidao_sefaz(cnpj: str) -> tuple[bytes | None, str, str]:
    """Submete CNPJ ao formulário público oficial e retorna PDF ou mensagem."""
    digits = clean_cnpj(cnpj)
    formatted_cnpj = format_cnpj(digits)
    if len(digits) != 14:
        return None, "", "O CNPJ da empresa ativa precisa conter 14 dígitos."
    try:
        session = requests.Session()
        response = session.get(CERTIDAO_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        form = soup.find("form", {"id": "Form1"}) or soup.find("form")
        if not form:
            return None, "", "O formulário oficial da SEFAZ-BA não foi localizado."
        payload = {}
        for field in form.find_all("input"):
            name = field.get("name")
            if name and field.get("type", "text").lower() in {"hidden", "text"}:
                payload[name] = field.get("value", "")
        payload["ctl00$PHConteudo$TxtNumCNPJ"] = formatted_cnpj
        payload["ctl00$PHConteudo$TxtNumInscricaoEstadual"] = ""
        payload["ctl00$PHConteudo$TxtNumCPF"] = ""
        payload["__EVENTTARGET"] = "ctl00$PHConteudo$btnImprimir"
        payload["__EVENTARGUMENT"] = ""
        result = session.post(CERTIDAO_URL, data=payload, headers={"Referer": CERTIDAO_URL, "User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}, timeout=45, allow_redirects=True)
        content_type = result.headers.get("content-type", "").lower()
        if "pdf" in content_type or result.content.startswith(b"%PDF"):
            return result.content, f"certidao_sefaz_{digits}.pdf", ""
        if "text/plain" in content_type or "window.open" in result.text:
            report_match = re.search(r"window\.open\('([^']+)'", result.text)
            if report_match:
                report_url = urljoin(CERTIDAO_URL, report_match.group(1))
                report_response = session.get(report_url, headers={"Referer": CERTIDAO_URL, "User-Agent": "Mozilla/5.0", "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8"}, timeout=45)
                report_type = report_response.headers.get("content-type", "").lower()
                if "pdf" in report_type or report_response.content.startswith(b"%PDF"):
                    return report_response.content, f"certidao_sefaz_{digits}.pdf", ""
                result = report_response
                content_type = report_type
        result_soup = BeautifulSoup(result.text, "html.parser")
        pdf_link = None
        for link in result_soup.find_all("a", href=True):
            href = link.get("href", "")
            if ".pdf" in href.lower() or "certid" in href.lower():
                pdf_link = urljoin(CERTIDAO_URL, href)
                break
        if pdf_link:
            pdf_response = session.get(pdf_link, headers={"Referer": CERTIDAO_URL}, timeout=45)
            if pdf_response.content.startswith(b"%PDF") or "pdf" in pdf_response.headers.get("content-type", "").lower():
                return pdf_response.content, f"certidao_sefaz_{digits}.pdf", ""
        error_box = result_soup.find(id="ASModal_Erro_ASLoadPageModal") or result_soup.find(id="PHConteudo_div_mensagem")
        visible_text = " ".join(error_box.stripped_strings) if error_box else " ".join(result_soup.stripped_strings)
        if visible_text:
            return None, "", f"A SEFAZ-BA não retornou o PDF. Mensagem do portal: {visible_text[-700:]}"
        return None, "", "A SEFAZ-BA não retornou um PDF diretamente. Verifique o resultado no portal oficial."
    except requests.RequestException as exc:
        return None, "", f"Falha de conexão com a SEFAZ-BA: {exc}"
    except Exception as exc:
        return None, "", f"Não foi possível processar a certidão: {exc}"


def auto_download_pdf(pdf_bytes: bytes, filename: str) -> None:
    """Tenta download e abertura do PDF; o bloqueio pode depender do navegador."""
    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
    components.html(
        f"""<script>
        (() => {{
          const data = atob('{encoded}');
          const bytes = new Uint8Array(data.length);
          for (let i = 0; i < data.length; i++) bytes[i] = data.charCodeAt(i);
          const blob = new Blob([bytes], {{type: 'application/pdf'}});
          const url = URL.createObjectURL(blob);
          const link = document.createElement('a');
          link.href = url;
          link.download = '{safe_name}';
          link.target = '_blank';
          link.rel = 'noopener';
          link.textContent = 'Abrir ou baixar certidão';
          link.style.cssText = 'display:block;padding:8px;font-family:sans-serif';
          document.body.appendChild(link);
          try {{ link.click(); }} catch (e) {{ window.open(url, '_blank', 'noopener'); }}
          setTimeout(() => {{ window.open(url, '_blank', 'noopener'); }}, 350);
          setTimeout(() => {{ URL.revokeObjectURL(url); }}, 60000);
        }})();
        </script>""",
        height=45,
    )


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


def supabase_select(table: str, columns: str = "*") -> tuple[list[dict[str, Any]], str]:
    client = get_supabase()
    if not client:
        return [], "Supabase não configurado."
    try:
        response = client.table(table).select(columns).order("created_at", desc=True).execute()
        return response.data or [], ""
    except Exception as exc:
        return [], str(exc)


def supabase_update(table: str, record_id: str, values: dict[str, Any]) -> tuple[bool, str]:
    client = get_supabase()
    if not client:
        return False, "Supabase não configurado."
    try:
        client.table(table).update(values).eq("id", record_id).execute()
        return True, "Dados atualizados no Supabase."
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
    st.link_button("Consultar Certidão Federal", FEDERAL_CERTIDAO_URL)
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
    st.subheader("Cadastro e edição de empresas")
    st.write("A senha SEFAZ será tratada como um campo informativo comum do cadastro.")

    saved_companies: list[dict[str, Any]] = []
    if mode_demo:
        if st.session_state.get("company"):
            saved_companies = [st.session_state["company"]]
    else:
        saved_companies, company_error = supabase_select("companies")
        if company_error:
            st.error(f"Não foi possível carregar as empresas salvas: {company_error}")

    selected_company: dict[str, Any] = {}
    if saved_companies:
        labels = [f"{item.get('razao_social', 'Sem razão social')} — CNPJ {item.get('cnpj', 'não informado')}" for item in saved_companies]
        selected_label = st.selectbox("Selecione a empresa ativa", labels, key="active_company_selector")
        selected_company = saved_companies[labels.index(selected_label)]
        st.session_state["company"] = selected_company
        display_companies = pd.DataFrame(saved_companies).drop(columns=["cpf_responsavel_hash", "sefaz_password_encrypted"], errors="ignore")
        if "sefaz_password" not in display_companies.columns:
            display_companies["sefaz_password"] = ""
        display_companies = display_companies.rename(columns={"sefaz_password": "Senha SEFAZ"})
        st.dataframe(display_companies, use_container_width=True, hide_index=True)
    else:
        st.info("Nenhuma empresa cadastrada ainda. Preencha o formulário abaixo para criar a primeira.")

    with st.expander("Editar empresa selecionada", expanded=bool(selected_company)):
        with st.form("edit_company_form"):
            e1, e2 = st.columns(2)
            edit_name = e1.text_input("Razão social", value=selected_company.get("razao_social", ""), key="edit_company_name")
            edit_cnpj = e2.text_input("CNPJ", value=selected_company.get("cnpj", ""), key="edit_company_cnpj")
            e3, e4 = st.columns(2)
            edit_cpf = e3.text_input("Novo CPF do responsável (opcional)", type="password", key="edit_responsible_cpf")
            edit_ie = e4.text_input("Inscrição estadual", value=selected_company.get("inscricao_estadual", ""), key="edit_state_registration")
            e5, e6 = st.columns(2)
            edit_city = e5.text_input("Município / unidade", value=selected_company.get("municipio", ""), key="edit_city")
            edit_regime = e6.selectbox("Regime tributário", ["Simples Nacional", "Lucro Presumido", "Lucro Real", "Não informado"], index=["Simples Nacional", "Lucro Presumido", "Lucro Real", "Não informado"].index(selected_company.get("regime", "Não informado")) if selected_company.get("regime", "Não informado") in ["Simples Nacional", "Lucro Presumido", "Lucro Real", "Não informado"] else 3, key="edit_regime")
            edit_sefaz_password = st.text_input("Senha SEFAZ", value=selected_company.get("sefaz_password", ""), key="edit_sefaz_password")
            update_company = st.form_submit_button("Atualizar empresa")
        if update_company:
            if not edit_name.strip() or not clean_cnpj(edit_cnpj):
                st.error("Informe pelo menos a razão social e o CNPJ.")
            elif not selected_company.get("id") and not mode_demo:
                st.error("A empresa selecionada não possui ID no Supabase. Recarregue o aplicativo.")
            else:
                updated = {"razao_social": edit_name.strip(), "cnpj": clean_cnpj(edit_cnpj), "inscricao_estadual": edit_ie.strip(), "municipio": edit_city.strip(), "regime": edit_regime}
                if edit_cpf:
                    updated["cpf_responsavel_hash"] = hashlib.sha256(clean_cnpj(edit_cpf).encode()).hexdigest()
                updated["sefaz_password"] = edit_sefaz_password
                if updated is not None:
                    if mode_demo:
                        st.session_state["company"] = {**selected_company, **updated}
                        st.success("Empresa atualizada no modo demonstrativo.")
                    else:
                        ok, msg = supabase_update("companies", selected_company["id"], updated)
                        if ok:
                            st.success("Empresa atualizada com sucesso.")
                            st.rerun()
                        else:
                            if "sefaz_password" in msg and "schema cache" in msg:
                                st.error("O Supabase ainda não possui a coluna sefaz_password. Execute no SQL Editor: alter table public.companies add column if not exists sefaz_password text; Depois reinicie o aplicativo.")
                            else:
                                st.error(msg)

    with st.expander("Cadastrar nova empresa"):
        with st.form("company_form"):
            c1, c2 = st.columns(2)
            company_name = c1.text_input("Razão social", key="new_company_name")
            cnpj = c2.text_input("CNPJ", key="new_company_cnpj")
            c3, c4 = st.columns(2)
            responsible_cpf = c3.text_input("CPF do responsável", type="password", key="new_responsible_cpf")
            state_registration = c4.text_input("Inscrição estadual", key="new_state_registration")
            c5, c6 = st.columns(2)
            city = c5.text_input("Município / unidade", key="new_city")
            new_regime = c6.selectbox("Regime tributário", ["Simples Nacional", "Lucro Presumido", "Lucro Real", "Não informado"], key="new_regime")
            sefaz_password = st.text_input("Senha SEFAZ", key="new_sefaz_password")
            save_company = st.form_submit_button("Salvar nova empresa")
        if save_company:
            company = {"razao_social": company_name.strip(), "cnpj": clean_cnpj(cnpj), "cpf_responsavel_hash": hashlib.sha256(clean_cnpj(responsible_cpf).encode()).hexdigest() if responsible_cpf else "", "inscricao_estadual": state_registration.strip(), "municipio": city.strip(), "regime": new_regime}
            if not company["razao_social"] or not company["cnpj"]:
                st.error("Informe pelo menos a razão social e o CNPJ.")
            else:
                try:
                    company["sefaz_password"] = sefaz_password
                    if mode_demo:
                        st.session_state["company"] = company
                        st.success("Empresa criada no modo demonstrativo.")
                    else:
                        ok, msg = supabase_insert("companies", [company])
                        if ok:
                            st.success("Empresa salva no Supabase.")
                            st.rerun()
                        else:
                            st.error(msg)
                except ValueError as exc:
                    st.error(str(exc))

    if st.session_state["company"]:
        active_for_extension = st.session_state["company"]
        st.info(f"Empresa ativa: {active_for_extension.get('razao_social') or 'não informada'} — CNPJ {active_for_extension.get('cnpj') or 'não informado'}")
        extension_payload = {
            "razao_social": active_for_extension.get("razao_social", ""),
            "cnpj": active_for_extension.get("cnpj", ""),
            "inscricao_estadual": active_for_extension.get("inscricao_estadual", ""),
            "sefaz_password": active_for_extension.get("sefaz_password", "")
        }
        if extension_payload["inscricao_estadual"] and extension_payload["sefaz_password"]:
            st.download_button("Exportar empresa ativa para extensão", json.dumps(extension_payload, ensure_ascii=False, indent=2), "empresa_sefaz_extensao.json", "application/json", help="Importe este arquivo no popup da extensão Chrome/Edge. O arquivo contém a senha SEFAZ.")
        else:
            st.warning("Preencha Inscrição estadual e Senha SEFAZ para exportar os dados para a extensão.")

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
    active_company = st.session_state.get("company", {})
    active_cnpj = active_company.get("cnpj", "")
    active_name = active_company.get("razao_social", "")
    st.markdown("### Certidão de Regularidade Fiscal Federal")
    st.write("A consulta federal conjunta Receita Federal/PGFN informa a regularidade fiscal perante a Fazenda Nacional. O CNPJ da empresa ativa será usado como referência; a emissão ocorre no portal oficial.")
    federal_cols = st.columns([2, 1])
    federal_cols[0].text_input("CNPJ para consulta federal", value=format_cnpj(active_company.get("cnpj", "")) if active_company.get("cnpj") else "", disabled=True, key="federal_cnpj_display")
    federal_cols[1].link_button("Abrir consulta federal", FEDERAL_CERTIDAO_URL, use_container_width=True)
    st.caption("No portal oficial, selecione Pessoa Jurídica e informe o CNPJ exibido acima. Depois registre o resultado e anexe a certidão no formulário abaixo.")
    st.divider()
    if active_cnpj:
        st.info(f"Empresa selecionada: {active_name} — CNPJ preenchido automaticamente: {active_cnpj}")
    else:
        st.warning("Selecione uma empresa na aba Cadastro e edição de empresas antes de emitir a certidão.")

    if st.button("Emitir certidão SEFAZ-BA", type="primary", disabled=not bool(active_cnpj)):
        with st.spinner("Consultando a emissão pública da SEFAZ-BA..."):
            pdf_bytes, pdf_name, cert_error = emit_certidao_sefaz(active_cnpj)
        if pdf_bytes:
            st.session_state["last_cert_pdf"] = pdf_bytes
            st.session_state["last_cert_filename"] = pdf_name
            st.success("Certidão recebida. O aplicativo tentou iniciar o download e abriu uma alternativa compatível com o navegador.")
            auto_download_pdf(pdf_bytes, pdf_name)
            st.download_button("Baixar certidão", pdf_bytes, pdf_name, "application/pdf")
        else:
            st.error(cert_error)
            st.link_button("Abrir emissão oficial da SEFAZ-BA", CERTIDAO_URL)

    st.divider()
    st.write("O CNPJ da empresa ativa é usado no formulário público oficial. Se o navegador bloquear o download automático, use o botão de fallback exibido após o retorno do PDF.")
    q1, q2 = st.columns(2)
    with q1:
        cert_type = st.selectbox("Tipo de certidão", ["Certidão de Regularidade Fiscal Federal — Receita Federal/PGFN", "Débitos tributários SEFAZ-BA", "Autenticidade de certidão", "Baixa de inscrição", "Outra"])
        cert_status = st.selectbox("Resultado", ["Não consultada", "Negativa", "Positiva", "Positiva com efeito de negativa", "Não se aplica"])
    with q2:
        cert_number = st.text_input("Número da certidão")
        cert_expiry = st.date_input("Validade, se houver", value=date.today())
    cert_file = st.file_uploader("Evidência complementar da certidão", type=["pdf", "png", "jpg"], key="cert_file")
    if st.button("Registrar consulta de certidão"):
        if not mode_demo and not active_company.get("id"):
            st.error("Selecione uma empresa cadastrada no Supabase antes de registrar a certidão.")
        else:
            record = {"tipo": cert_type, "resultado": cert_status, "numero": cert_number, "validade": str(cert_expiry), "company_id": active_company.get("id")}
        if mode_demo:
            st.success("Consulta registrada no modo demonstrativo.")
        elif active_company.get("id"):
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
