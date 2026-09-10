#!/usr/bin/env python3
"""
Corrida The Line - gerador do placar gamificado.

Le os cadastros de cliente The Line (fonte: API de consulta do Zeev, ou
alternativamente um CSV exportado manualmente), aplica as regras de
normalizacao/negocio, e gera a pagina HTML final (output.html) pronta para
ser publicada como Artifact.

Uso (fluxo atual - dados ja buscados da API do Zeev, ver rotina diaria):
    python3 build_scoreboard.py --zeev-json <linhas.json> --config <config.json> \
        --template <template.html> --out <saida.html> \
        [--provocacao "texto atual da provocacao do dia"] \
        [--today AAAA-MM-DD]  (para testes; default = hoje em America/Sao_Paulo)

Uso (fluxo antigo - CSV exportado manualmente do Zeev, mantido como fallback):
    python3 build_scoreboard.py --csv <caminho_csv> --config <config.json> \
        --template <template.html> --out <saida.html> [...]

--zeev-json espera uma lista de objetos {"imobiliaria", "corretor", "date"
(AAAA-MM-DD ou ISO datetime), "resultado"} - o formato que a rotina diaria
produz ao consultar https://paysagecorpal.zeev.it/api/2/instances/report
(flowId 339, "Cadastro de Cliente The Line") pelo navegador do computador
do Yuiti (a chamada precisa sair da rede do computador dele porque o
sandbox de nuvem nao tem saida liberada para o dominio do Zeev).

Regras de negocio (definidas pelo Yuiti em 2026-09-10, semana redefinida em
2026-09-10):
  - So conta como cadastro valido linhas com coluna H "Resultado da solicitacao"
    igual a "Aprovado" ou "Concluido".
  - Data de referencia = coluna E "Data de solicitacao" (formato DD/MM/AAAA).
  - Imobiliaria = coluna S ("imobiliaria"). Corretor = coluna V ("corretor").
  - Semana: Sexta-feira a quinta-feira seguinte.
  - Cada cadastro valido rende R$20 para a imobiliaria (config valor_por_cadastro).
  - Premio semanal: R$1.000 para a imobiliaria com mais cadastros validos na semana
    (config premio_semanal).
  - Linhas cujo nome de imobiliaria/corretor contenha um dos termos em
    *_excluir_contendo (ex.: "TESTE") sao descartadas por completo (nao contam
    em nenhum lugar) - normalmente cadastros de teste feitos durante a
    configuracao da campanha.
  - Imobiliarias listadas em agencias_excluidas_ranking (config) - hoje so
    "INTERNO", que e' o alias canonico para o que chegava do Zeev como
    "Monaco"/"Imobiliaria Monaco" - sao tratadas como imobiliaria comum em
    tudo (grafico diario, filtro), MAS: (1) nunca aparecem no pódio/placar da
    semana (leaderboard) nem no ranking final de uma semana encerrada - se
    ficariam entre as 3 primeiras, a 4a colocada sobe de posicao naturalmente
    porque essa imobiliaria e' removida da lista antes do ranking ser
    recalculado; (2) corretores associados a ela sao removidos por completo
    do ranking de corretores.
"""
import argparse
import csv
import json
import os
import re
import unicodedata
from collections import defaultdict
from datetime import date, timedelta, datetime

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Sao_Paulo")
except Exception:
    TZ = None

MESES_PT = ["janeiro","fevereiro","março","abril","maio","junho","julho",
            "agosto","setembro","outubro","novembro","dezembro"]

STATUS_VALID = {"APROVADO", "CONCLUIDO"}  # comparado ja sem acento/maiusculo
STATUS_LABELS = {
    "APROVADO": "Aprovado",
    "CONCLUIDO": "Concluído",
    "REJEITADO": "Rejeitado",
    "PENDENTE": "Pendente",
}
STATUS_ORDER = ["APROVADO", "CONCLUIDO", "REJEITADO", "PENDENTE"]


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_key(s):
    s = strip_accents(s or "").upper().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def contains_any(norm_value, terms):
    return any(t in norm_value for t in terms)


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("valor_por_cadastro", 20)
    cfg.setdefault("premio_semanal", 1000)
    cfg.setdefault("premio_corretor_semanal", 100)
    cfg.setdefault("imobiliaria_aliases", {})
    cfg.setdefault("corretor_aliases", {})
    cfg.setdefault("imobiliaria_excluir_contendo", ["TESTE"])
    cfg.setdefault("corretor_excluir_contendo", ["TESTE"])
    cfg.setdefault("agencias_excluidas_ranking", [])
    # normaliza chaves dos mapas de alias
    cfg["_imob_map"] = {norm_key(k): v for k, v in cfg["imobiliaria_aliases"].items()}
    cfg["_corr_map"] = {norm_key(k): v for k, v in cfg["corretor_aliases"].items()}
    cfg["_imob_excl"] = [norm_key(t) for t in cfg["imobiliaria_excluir_contendo"]]
    cfg["_corr_excl"] = [norm_key(t) for t in cfg["corretor_excluir_contendo"]]
    # agencias (ja no nome canonico, pos-alias) que ficam de fora do
    # pódio/leaderboard e do ranking de corretores - ver docstring do modulo
    cfg["_rank_excl"] = {norm_key(a) for a in cfg["agencias_excluidas_ranking"]}
    return cfg


def canonical_agency(raw, cfg):
    key = norm_key(raw)
    if not key:
        return None
    if contains_any(key, cfg["_imob_excl"]):
        return None
    return cfg["_imob_map"].get(key, raw.strip())


def canonical_broker(raw, cfg):
    key = norm_key(raw)
    if not key:
        return None
    if contains_any(key, cfg["_corr_excl"]):
        return None
    return cfg["_corr_map"].get(key, raw.strip())


def parse_date_br(s):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def week_bounds(d):
    """Sexta-feira a quinta-feira seguinte contendo a data d."""
    dow_fri0 = (d.weekday() - 4) % 7  # segunda=0..domingo=6 -> sexta=0..quinta=6
    start = d - timedelta(days=dow_fri0)
    end = start + timedelta(days=6)
    return start, end


def fmt_week_label(start, end):
    if start.month == end.month:
        return f"{start.day:02d} a {end.day:02d} de {MESES_PT[end.month-1]}"
    return f"{start.day:02d} de {MESES_PT[start.month-1]} a {end.day:02d} de {MESES_PT[end.month-1]}"


def assign_ranks(rows, key):
    """Ranking por competição (empate mantém o mesmo lugar, ex.: 1,2,3,3,5)."""
    prev_val = None
    prev_rank = 0
    for i, row in enumerate(rows):
        if row[key] != prev_val:
            rank = i + 1
        else:
            rank = prev_rank
        row["posicao"] = rank
        prev_rank = rank
        prev_val = row[key]


def load_history_store(path):
    """Le o arquivo onde as semanas ja fechadas ficam congeladas (persistido
    entre execucoes da rotina diaria). Se nao existir ainda ou estiver
    corrompido, comeca vazio (equivale a nenhuma semana congelada ainda -
    todas as semanas fechadas encontradas nesta execucao serao congeladas
    agora, com os numeros de hoje)."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_history_store(path, store):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)


def status_key(raw):
    k = norm_key(raw)
    if k in ("APROVADO",):
        return "APROVADO"
    if k in ("CONCLUIDO", "CONCLUÍDO".upper()):
        return "CONCLUIDO"
    if k in ("REJEITADO",):
        return "REJEITADO"
    return "PENDENTE"  # branco ou qualquer outro status intermediario do Zeev


def rows_from_csv(csv_path):
    """Le o CSV exportado manualmente do Zeev e devolve uma lista de dicts
    {"imobiliaria", "corretor", "date" (objeto date ou None), "resultado"} -
    o formato de entrada comum que build_data() consome, independente da
    fonte dos dados (CSV manual ou API do Zeev)."""
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.reader(f, delimiter=";")
        raw_rows = list(reader)
    data_rows = raw_rows[1:] if len(raw_rows) > 1 else []

    COL_DATA, COL_RESULTADO, COL_IMOB, COL_CORRETOR = 4, 7, 18, 21

    out = []
    for r in data_rows:
        if len(r) <= max(COL_DATA, COL_RESULTADO, COL_IMOB, COL_CORRETOR):
            continue
        out.append({
            "imobiliaria": r[COL_IMOB],
            "corretor": r[COL_CORRETOR],
            "date": parse_date_br(r[COL_DATA]),
            "resultado": r[COL_RESULTADO],
        })
    return out


def rows_from_zeev_json(json_path):
    """Le um JSON ja no formato de linhas (produzido a partir da API de
    consulta do Zeev - ver rotina diaria) - uma lista de objetos
    {"imobiliaria", "corretor", "date" (string ISO AAAA-MM-DD ou
    AAAA-MM-DDTHH:MM:SS), "resultado"} - e devolve no mesmo formato interno
    usado por build_data(), convertendo a data de string para objeto date."""
    with open(json_path, encoding="utf-8") as f:
        raw_rows = json.load(f)
    out = []
    for r in raw_rows:
        date_str = (r.get("date") or "").strip()
        d = None
        if date_str:
            try:
                d = datetime.fromisoformat(date_str).date()
            except ValueError:
                d = parse_date_br(date_str)
        out.append({
            "imobiliaria": r.get("imobiliaria", "") or "",
            "corretor": r.get("corretor", "") or "",
            "date": d,
            "resultado": r.get("resultado", "") or "",
        })
    return out


def build_data(rows, cfg, today=None, history_store=None):
    """rows: lista de dicts {"imobiliaria", "corretor", "date", "resultado"} -
    ver rows_from_csv() / rows_from_zeev_json() para como produzir essa lista
    a partir de cada fonte de dados suportada.

    history_store: dict mutavel {semana_start_iso: entry} com o placar FINAL
    de cada semana ja fechada (persistido em disco entre execucoes - ver
    load_history_store/save_history_store). Uma semana e' "fechada" (e some
    da view atual, virando historico) assim que a semana corrente vira uma
    semana mais nova - na pratica isso so acontece na primeira execucao da
    rotina diaria em que "hoje" cai num novo domingo. Essa mesma execucao e'
    a unica em que essa semana ainda nao esta em history_store: por isso o
    fechamento OFICIAL (numeros finais congelados, nunca mais recalculados
    mesmo que o status de algum cadastro daquela semana mude depois no Zeev)
    acontece exatamente na atualizacao de domingo, sem precisar checar o dia
    da semana explicitamente. Se history_store for None, nada e' congelado
    (todo fechamento e' recalculado ao vivo a cada chamada - usado so em
    testes que nao se importam com persistencia)."""
    if history_store is None:
        history_store = {}
    if today is None:
        today = datetime.now(TZ).date() if TZ else date.today()

    week_start, week_end = week_bounds(today)

    rows_total = len(rows)
    rows_excluded_test = 0
    rows_no_date = 0

    # estruturas de agregacao
    # leaderboard_week: agencia -> contagem por status (na semana atual)
    week_agency_status = defaultdict(lambda: defaultdict(int))
    # daily[agencia][ISO date] = {status: count}
    daily = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    # brokers na semana atual: corretor -> {agencia: nome_mais_frequente, validos: n}
    week_broker_valid = defaultdict(int)
    week_broker_agency_count = defaultdict(lambda: defaultdict(int))
    # historico: semana(start_iso) -> agencia -> status -> contagem (usado so
    # para CONGELAR semanas recem-fechadas - ver history_store abaixo)
    history_week_agency_status = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    # historico do corretor campeao: semana(start_iso) -> corretor -> validos,
    # e semana -> corretor -> agencia -> contagem (pra achar a imobiliaria mais
    # frequente daquele corretor naquela semana) - usado so' para CONGELAR o
    # "Rei/Rainha dos Cadastros" de semanas recem-fechadas junto com o resto
    history_week_broker_valid = defaultdict(lambda: defaultdict(int))
    history_week_broker_agency_count = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    # todas as agencias vistas (para popular filtro), com pelo menos 1 registro valido em qualquer semana
    all_agencies_ever = set()
    # funil geral da campanha inteira (todas as semanas, todas as imobiliarias
    # inclusive as excluidas do ranking) - ver funil_geral no retorno
    funil_validos = 0
    funil_rejeitados = 0
    funil_pendentes = 0

    for r in rows:
        raw_imob = r["imobiliaria"]
        raw_corretor = r["corretor"]
        agencia = canonical_agency(raw_imob, cfg)
        corretor_is_test = contains_any(norm_key(raw_corretor), cfg["_corr_excl"])
        if agencia is None or corretor_is_test:
            if norm_key(raw_imob) or norm_key(raw_corretor):
                rows_excluded_test += 1
            continue

        d = r["date"]
        if d is None:
            rows_no_date += 1
            continue

        st = status_key(r["resultado"])
        is_valid = st in STATUS_VALID

        all_agencies_ever.add(agencia)

        if is_valid:
            funil_validos += 1
        elif st == "REJEITADO":
            funil_rejeitados += 1
        else:
            funil_pendentes += 1

        w_start, _ = week_bounds(d)
        history_week_agency_status[w_start.isoformat()][agencia][st] += 1

        if is_valid and norm_key(agencia) not in cfg["_rank_excl"]:
            corretor_hist = canonical_broker(raw_corretor, cfg)
            if corretor_hist:
                w_start_iso = w_start.isoformat()
                history_week_broker_valid[w_start_iso][corretor_hist] += 1
                history_week_broker_agency_count[w_start_iso][corretor_hist][agencia] += 1

        if w_start == week_start:
            week_agency_status[agencia][st] += 1
            daily[agencia][d.isoformat()][st] += 1
            daily["__ALL__"][d.isoformat()][st] += 1

            if is_valid:
                corretor = canonical_broker(raw_corretor, cfg)
                if corretor:
                    week_broker_valid[corretor] += 1
                    week_broker_agency_count[corretor][agencia] += 1

    valor_unit = cfg["valor_por_cadastro"]
    premio = cfg["premio_semanal"]

    # leaderboard da semana atual (imobiliarias em agencias_excluidas_ranking
    # ficam de fora do podio - se estivesse entre as 3 primeiras, a 4a
    # colocada assume a posicao dela automaticamente, ja que o ranking e'
    # recalculado so' com quem sobrou aqui)
    leaderboard = []
    for agencia, statuses in week_agency_status.items():
        if norm_key(agencia) in cfg["_rank_excl"]:
            continue
        aprovados = statuses.get("APROVADO", 0)
        concluidos = statuses.get("CONCLUIDO", 0)
        rejeitados = statuses.get("REJEITADO", 0)
        pendentes = statuses.get("PENDENTE", 0)
        validos = aprovados + concluidos
        leaderboard.append({
            "agencia": agencia,
            "validos": validos,
            "aprovados": aprovados,
            "concluidos": concluidos,
            "rejeitados": rejeitados,
            "pendentes": pendentes,
            "valor": validos * valor_unit,
        })
    leaderboard.sort(key=lambda x: (-x["validos"], x["agencia"]))
    assign_ranks(leaderboard, "validos")
    max_validos = max([row["validos"] for row in leaderboard], default=0)

    # dias da semana (dom..sab) ate hoje (dias futuros ficam com zero/omitidos no front)
    week_days = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]

    # normaliza "daily" para incluir todos os status em todos os dias/agencias presentes
    def normalize_daily(d_map):
        out = {}
        for day in week_days:
            counts = d_map.get(day, {})
            out[day] = {s: counts.get(s, 0) for s in STATUS_ORDER}
        return out

    daily_out = {}
    agencies_with_week_data = sorted(set(list(week_agency_status.keys())))
    for agencia in agencies_with_week_data:
        daily_out[agencia] = normalize_daily(daily.get(agencia, {}))
    daily_out["__ALL__"] = normalize_daily(daily.get("__ALL__", {}))

    # ranking de corretores da semana atual (top 15) - corretores cuja
    # imobiliaria mais frequente esta em agencias_excluidas_ranking sao
    # descartados por completo desta lista (nao so' do topo)
    brokers = []
    for corretor, validos in week_broker_valid.items():
        agencia_top = max(week_broker_agency_count[corretor].items(), key=lambda kv: kv[1])[0]
        if norm_key(agencia_top) in cfg["_rank_excl"]:
            continue
        brokers.append({"corretor": corretor, "agencia": agencia_top, "validos": validos})
    brokers.sort(key=lambda x: (-x["validos"], x["corretor"]))
    assign_ranks(brokers, "validos")
    brokers = brokers[:15]

    # corretor com mais cadastros validos na semana atual ("Rei/Rainha dos
    # Cadastros") - premio individual separado do premio da imobiliaria.
    # brokers[0] ja' esta' ordenado/filtrado (sem agencias de
    # agencias_excluidas_ranking) e com posicao atribuida, entao e' so' usar
    # ele direto quando houver pelo menos 1 cadastro valido na semana.
    corretor_campeao = None
    if brokers and brokers[0]["validos"] > 0:
        corretor_campeao = {
            "corretor": brokers[0]["corretor"],
            "agencia": brokers[0]["agencia"],
            "validos": brokers[0]["validos"],
        }

    # historico de semanas anteriores (fechadas): a primeira vez que uma
    # semana aparece aqui com w_start_d < week_start ela acabou de "fechar"
    # nesta execucao. Se ela ainda nao estiver em history_store, seu placar
    # final e' calculado agora (com o mesmo formato do leaderboard da semana
    # atual, para poder alimentar a imagem de compartilhamento) e gravado la
    # PARA SEMPRE - nunca mais recalculado, mesmo que o status de algum
    # cadastro daquela semana mude depois no Zeev. Semanas ja presentes em
    # history_store (fechadas em execucoes anteriores) sao deixadas intocadas.
    for w_start_iso, agency_statuses in history_week_agency_status.items():
        w_start_d = date.fromisoformat(w_start_iso)
        if w_start_d >= week_start:
            continue  # semana atual ou futura nao entra no historico
        if w_start_iso in history_store:
            continue  # ja congelada antes - nao mexe mais
        w_end_d = w_start_d + timedelta(days=6)
        ranking = []
        for agencia, statuses in agency_statuses.items():
            if norm_key(agencia) in cfg["_rank_excl"]:
                continue
            aprovados = statuses.get("APROVADO", 0)
            concluidos = statuses.get("CONCLUIDO", 0)
            rejeitados = statuses.get("REJEITADO", 0)
            pendentes = statuses.get("PENDENTE", 0)
            validos = aprovados + concluidos
            ranking.append({
                "agencia": agencia,
                "validos": validos,
                "aprovados": aprovados,
                "concluidos": concluidos,
                "rejeitados": rejeitados,
                "pendentes": pendentes,
                "valor": validos * valor_unit,
            })
        if not ranking:
            continue
        ranking.sort(key=lambda x: (-x["validos"], x["agencia"]))
        assign_ranks(ranking, "validos")
        top_count = ranking[0]["validos"]
        winners = [r["agencia"] for r in ranking if r["validos"] == top_count] if top_count > 0 else []

        # congela junto o "Rei/Rainha dos Cadastros" daquela semana (corretor
        # com mais cadastros validos, ja excluindo agencias_excluidas_ranking)
        week_brokers = history_week_broker_valid.get(w_start_iso, {})
        corretor_campeao_hist = None
        if week_brokers:
            top_corretor, top_validos = sorted(
                week_brokers.items(), key=lambda kv: (-kv[1], kv[0])
            )[0]
            if top_validos > 0:
                agencia_top = max(
                    history_week_broker_agency_count[w_start_iso][top_corretor].items(),
                    key=lambda kv: kv[1],
                )[0]
                corretor_campeao_hist = {
                    "corretor": top_corretor,
                    "agencia": agencia_top,
                    "validos": top_validos,
                }

        history_store[w_start_iso] = {
            "semana_label": fmt_week_label(w_start_d, w_end_d),
            "semana_start": w_start_iso,
            "vencedoras": winners,
            "validos": top_count,
            "premio": premio,
            "ranking": ranking,
            "corretor_campeao": corretor_campeao_hist,
        }

    history = sorted(history_store.values(), key=lambda x: x["semana_start"], reverse=True)

    insight = None
    if leaderboard and leaderboard[0]["validos"] > 0:
        lider = leaderboard[0]
        segundo_validos = leaderboard[1]["validos"] if len(leaderboard) > 1 else 0
        gap = lider["validos"] - segundo_validos
        insight = {"lider": lider["agencia"], "gap": gap}

    generated_at = datetime.now(TZ) if TZ else datetime.now()

    funil_total = funil_validos + funil_rejeitados + funil_pendentes
    funil_geral = {
        "total": funil_total,
        "validos": funil_validos,
        "rejeitados": funil_rejeitados,
        "pendentes": funil_pendentes,
    }

    return {
        "generated_at": generated_at.strftime("%d/%m/%Y %H:%M"),
        "generated_at_iso": generated_at.isoformat(),
        "week": {
            "start": week_start.isoformat(),
            "end": week_end.isoformat(),
            "label": fmt_week_label(week_start, week_end),
            "days": week_days,
        },
        "today": today.isoformat(),
        "config": {
            "valor_por_cadastro": valor_unit,
            "premio_semanal": premio,
            "premio_corretor_semanal": cfg["premio_corretor_semanal"],
        },
        "totals": {
            "rows_total": rows_total,
            "rows_excluded_test": rows_excluded_test,
            "rows_no_date": rows_no_date,
        },
        "leaderboard": leaderboard,
        "max_validos": max_validos,
        "insight": insight,
        "brokers": brokers,
        "corretor_campeao": corretor_campeao,
        "daily": daily_out,
        "agencies": ["Todas"] + sorted(all_agencies_ever),
        "history": history,
        "funil_geral": funil_geral,
        "status_labels": STATUS_LABELS,
        "status_order": STATUS_ORDER,
    }


def render_html(template_path, data, provocacao_do_dia):
    with open(template_path, encoding="utf-8") as f:
        html = f.read()
    data_json = json.dumps(data, ensure_ascii=False, indent=None)
    html = html.replace("__SCORE_DATA_JSON__", data_json.replace("</", "<\\/"))
    html = html.replace("__PROVOCACAO_DO_DIA_JSON__", json.dumps(provocacao_do_dia, ensure_ascii=False))
    html = html.replace("__GENERATED_AT__", data["generated_at"])
    html = html.replace("__WEEK_LABEL__", data["week"]["label"])
    return html


DEFAULT_PROVOCACAO = "Quem vai liderar a Corrida The Line essa semana? 🏁"


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="CSV exportado manualmente do Zeev (fluxo antigo)")
    src.add_argument("--zeev-json", help="JSON de linhas ja buscado da API do Zeev (fluxo atual da rotina diaria)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--provocacao", default=None)
    ap.add_argument("--today", default=None, help="AAAA-MM-DD, usar so para testes")
    ap.add_argument("--history-store", default=None,
                     help="Caminho do JSON onde semanas ja fechadas ficam congeladas "
                          "permanentemente (history_frozen.json). Se omitido, nenhuma "
                          "semana e' congelada entre execucoes (fechamento recalculado "
                          "ao vivo a cada chamada - usar so em testes).")
    args = ap.parse_args()

    cfg = load_config(args.config)
    today = date.fromisoformat(args.today) if args.today else None
    rows = rows_from_csv(args.csv) if args.csv else rows_from_zeev_json(args.zeev_json)
    history_store = load_history_store(args.history_store) if args.history_store else {}
    weeks_before = set(history_store.keys())
    data = build_data(rows, cfg, today=today, history_store=history_store)
    weeks_frozen_now = sorted(set(history_store.keys()) - weeks_before)
    if args.history_store and weeks_frozen_now:
        save_history_store(args.history_store, history_store)
    provocacao = args.provocacao if args.provocacao is not None else DEFAULT_PROVOCACAO
    html = render_html(args.template, data, provocacao)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)

    print(json.dumps({
        "ok": True,
        "rows_total": data["totals"]["rows_total"],
        "rows_excluded_test": data["totals"]["rows_excluded_test"],
        "week_label": data["week"]["label"],
        "leaderboard_top3": data["leaderboard"][:3],
        "brokers_top3": data["brokers"][:3],
        "history_weeks": len(data["history"]),
        "weeks_frozen_now": weeks_frozen_now,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
