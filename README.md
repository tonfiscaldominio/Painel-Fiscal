# Painel Fiscal Integrado — SEFAZ-BA e Supabase

Aplicativo Streamlit compartilhado para cadastro de empresas, registro de certidões, importação do relatório de destinatário da NF-e/SEFAZ-BA, confrontos com base interna e auditorias fiscais.

## Layout real identificado

O arquivo de referência enviado possui separador `;`, valores monetários no formato brasileiro e as colunas abaixo:

| Coluna no portal | Uso no sistema |
|---|---|
| `Numero NF-e` | Número do documento |
| `CNPJ/CPF Emitente` | Emitente normalizado sem pontuação |
| `Razao Social Emitente` | Nome do emitente |
| `Data de Emissao` | Data e hora de emissão |
| `Data de Autorizacao` | Data e hora de autorização |
| `Valor` | Valor convertido para número |
| `Chave de Acesso` | Chave NF-e de 44 dígitos |
| `UF Emit.` | UF do emitente |
| `Situacao` | Situação do documento |
| `Tipo Operacao` | `Entrada` ou `Saida` |

O arquivo de referência contém 166 registros, todos autorizados, com 14 entradas e 152 saídas, abrangendo abril de 2025. Esses números são específicos do arquivo enviado e não representam qualquer apuração fiscal geral.

## Configuração do Supabase

1. Crie uma conta em [supabase.com](https://supabase.com/) e crie um projeto gratuito.
2. Abra **SQL Editor** no projeto.
3. Cole o conteúdo de `supabase_schema.sql`.
4. Execute o script.
5. Abra **Project Settings > API**.
6. Copie a URL do projeto e a chave pública `anon`.
7. No Streamlit Community Cloud, abra **App settings > Secrets** e adicione:

```toml
SUPABASE_URL = "https://seu-projeto.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "sua-chave-de-servidor"
APP_PASSWORD = "defina-uma-senha-forte-para-a-equipe"
```

`APP_PASSWORD` é uma senha única compartilhada para abrir o aplicativo, sem criação de perfis diferenciados. Ela deve ser definida somente em **Secrets** e nunca no GitHub. Para dados fiscais reais, a senha deve ser trocada periodicamente e compartilhada por canal seguro.

A chave `service_role` será usada somente pelo servidor Streamlit e deve ser cadastrada exclusivamente em **Secrets**. Nunca coloque essa chave no código, no GitHub ou em uma mensagem. Como ela ignora as políticas RLS, proteja o aplicativo com a senha compartilhada e não publique a URL sem controle de acesso.

## Execução local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Sem as variáveis do Supabase, o aplicativo funciona em modo demonstrativo, mantendo os dados somente em memória.

## Fluxo SEFAZ-BA

O portal informado pelo usuário é o módulo NFENC da SEFAZ-BA e exige autenticação. A aplicação abre o portal por um link e importa o CSV baixado. A automação direta do login e do download não deve ser presumida, pois o portal informa bloqueio de IP após tentativas inválidas e pode exigir certificado, sessão, controles adicionais ou configuração de segurança do navegador.

A evolução do download automático deve ser feita somente com serviço oficial, autorização da empresa e validação do layout. O sistema não contorna CAPTCHA, bloqueio de IP, certificado, MFA ou qualquer controle de acesso.

## Segurança

O aplicativo não grava senha da SEFAZ. O CPF do responsável é usado apenas para gerar um hash no cadastro demonstrativo; o valor original não é exibido. Para uso empresarial, recomenda-se autenticação do próprio Supabase, política de senhas fortes, rotação de segredos, backups, controle de acesso por organização e trilha de auditoria.

O projeto não deve conter arquivos fiscais reais, certificados digitais, senhas, tokens ou chaves privadas. O CSV enviado pelo usuário foi mantido apenas como `exemplo_sefaz_ba.csv` para teste local e não deve ser publicado se contiver dados reais.

## Auditorias implementadas

A primeira versão avalia duplicidade de chave de acesso, chave inválida, valores não positivos e inconsistência de datas. Também confronta a base SEFAZ com um Excel interno usando a chave de acesso e, quando existente, o valor do documento, classificando registros como encontrados em ambas as bases, somente SEFAZ ou somente base interna.

## Próximas etapas

Antes de uso produtivo, deve-se acrescentar autenticação obrigatória, armazenamento de evidências no Supabase Storage, persistência dos achados na tabela `audit_findings`, histórico de consultas, parametrização por empresa e revisão contábil das regras. Certidões e consultas de optantes devem manter a evidência da consulta e a data de verificação.
