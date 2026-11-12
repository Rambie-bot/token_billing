#!/usr/bin/env python3
"""
Мониторинг предоплаты клиентов по расходам OpenAI + алерты в Slack.

Как считается:
  предоплата (например 26 000 ₽)
  − наша комиссия (commission_pct, 10% = 2 600 ₽)
  = бюджет клиента на токены (23 400 ₽)
  потрачено = расходы проекта OpenAI в $ с даты оплаты × курс (ЦБ × cbr_multiplier ≈ курс Райфа)
  остаток = бюджет − потрачено

Когда остаток опускается до порога (по умолчанию 5 000 ₽ и 0 ₽), в Slack уходит сообщение.
Каждый порог срабатывает один раз за период оплаты (состояние — в state.json).

Переменные окружения:
  OPENAI_ADMIN_KEY   — Admin-ключ OpenAI (sk-admin-...), только он умеет читать расходы
  SLACK_WEBHOOK_URL  — Incoming Webhook канала Slack
  DRY_RUN=1          — ничего не слать и не сохранять, только напечатать отчёт и сообщения
  FORCE_ALERT=1      — отправить в Slack текущий статус всех клиентов (проверка связи)

Запуск:  python3 billing_monitor.py [путь_к_clients.json]
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))


# ---------- HTTP ----------

def http_get_json(url, headers=None, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:500]
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt * 5)
                continue
            raise RuntimeError(f"GET {url.split('?')[0]} -> HTTP {e.code}: {body}") from e
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(5)
                continue
            raise


def http_post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


# ---------- OpenAI Costs ----------

def fetch_costs_by_project(admin_key, project_ids, start_ts):
    """Возвращает {project_id: сумма в USD} с start_ts по сейчас."""
    headers = {"Authorization": f"Bearer {admin_key}"}
    totals = {pid: 0.0 for pid in project_ids}
    page = None
    while True:
        params = [
            ("start_time", str(int(start_ts))),
            ("bucket_width", "1d"),
            ("group_by", "project_id"),
            ("limit", "180"),
        ]
        params += [("project_ids", pid) for pid in project_ids]
        if page:
            params.append(("page", page))
        resp = http_get_json(OPENAI_COSTS_URL + "?" + urllib.parse.urlencode(params), headers)

        for bucket in resp.get("data", []):
            for res in bucket.get("results", []):
                pid = res.get("project_id")
                amount = (res.get("amount") or {}).get("value") or 0
                if pid in totals:
                    totals[pid] += float(amount)

        if resp.get("has_more") and resp.get("next_page"):
            page = resp["next_page"]
        else:
            break
    return totals


# ---------- Курс ----------

_rate_cache = {}


def cbr_usd_rate():
    if "cbr" not in _rate_cache:
        _rate_cache["cbr"] = float(http_get_json(CBR_URL)["Valute"]["USD"]["Value"])
    return _rate_cache["cbr"]


def get_rate(client, defaults):
    """(курс, пояснение). Фиксированный rub_per_usd важнее; иначе ЦБ × cbr_multiplier.
    03.11.2026: Райф онлайн продавал USD по 87,38 при ЦБ 80,97 → коэффициент ≈ 1,08."""
    if client.get("rub_per_usd"):
        return float(client["rub_per_usd"]), "фикс."
    mult = float(client.get("cbr_multiplier", defaults.get("cbr_multiplier", 1.0)))
    cbr = cbr_usd_rate()
    if mult != 1:
        return cbr * mult, f"ЦБ {cbr:.2f} × {mult:g}"
    return cbr, "ЦБ"


# ---------- State ----------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text("utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", "utf-8")


# ---------- Сообщения ----------
# Плейсхолдеры: {mention} {name} {paid} {commission} {budget} {spent} {spent_usd}
#               {rate} {remaining} {percent} {threshold} {period_start}
# Свои шаблоны: "message_low" / "message_exhausted" в clients.json (в defaults или у клиента).

DEFAULT_TEMPLATES = {
    "message_low": (
        "{mention} :warning: *{name}*: бюджет на токены подходит к концу\n"
        "Оплачено {paid} ({period_start}), из них на токены {budget}\n"
        "Потрачено: {spent} ({spent_usd}, курс {rate}) — {percent}\n"
        "Осталось: *{remaining}* → пора выставить новый счёт :receipt:"
    ),
    "message_exhausted": (
        "{mention} :red_circle: *{name}*: бюджет на токены закончился\n"
        "Оплачено {paid} ({period_start}), из них на токены {budget}\n"
        "Потрачено: {spent} ({spent_usd}, курс {rate})\n"
        "Остаток: *{remaining}* → срочно выставить счёт"
    ),
    "message_status": (
        ":information_source: *{name}*: потрачено {spent} из {budget} ({percent}), "
        "осталось *{remaining}* (курс {rate})"
    ),
}


def fmt_rub(x):
    return f"{x:,.0f} ₽".replace(",", " ")


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def mention_str(client, defaults):
    ids = client.get("slack_mention", defaults.get("slack_mention", []))
    if isinstance(ids, str):
        ids = [ids]
    return " ".join(f"<@{u}>" for u in ids if u)


def compute(client, defaults, usd):
    paid = float(client["prepaid_rub"])
    pct_fee = float(client.get("commission_pct", defaults.get("commission_pct", 0)))
    commission = paid * pct_fee / 100
    budget = paid - commission
    rate, rate_src = get_rate(client, defaults)
    spent = usd * rate
    remaining = budget - spent
    return {
        "paid": paid, "commission": commission, "budget": budget, "rate": rate,
        "rate_src": rate_src, "usd": usd, "spent": spent, "remaining": remaining,
        "percent": spent / budget * 100 if budget else 0,
    }


def render(tpl, client, defaults, r, threshold=0):
    return tpl.format(
        mention=mention_str(client, defaults),
        name=client["name"],
        paid=fmt_rub(r["paid"]),
        commission=fmt_rub(r["commission"]),
        budget=fmt_rub(r["budget"]),
        spent=fmt_rub(r["spent"]),
        spent_usd=f"${r['usd']:.2f}",
        rate=f"{r['rate']:.2f}" + ("" if r["rate_src"] == "фикс." else f" = {r['rate_src']}"),
        remaining=fmt_rub(r["remaining"]),
        percent=f"{r['percent']:.0f}%",
        threshold=fmt_rub(threshold),
        period_start=datetime.strptime(client["period_start"], "%Y-%m-%d").strftime("%d.%m.%Y"),
    ).strip()


# ---------- Main ----------

def main():
    cfg_path = Path(sys.argv[1] if len(sys.argv) > 1 else "clients.json")
    cfg = json.loads(cfg_path.read_text("utf-8"))
    admin_key = os.environ["OPENAI_ADMIN_KEY"]
    webhook = os.getenv("SLACK_WEBHOOK_URL")
    dry_run = os.getenv("DRY_RUN") == "1"
    force = os.getenv("FORCE_ALERT") == "1"

    defaults = cfg.get("defaults", {})
    clients = [c for c in cfg["clients"] if c.get("enabled", True)]
    state = load_state()

    # один запрос к OpenAI на каждую дату начала периода
    by_start = {}
    for c in clients:
        by_start.setdefault(c["period_start"], []).append(c["project_id"])
    spent_usd = {}
    for start, pids in by_start.items():
        spent_usd.update(fetch_costs_by_project(admin_key, pids, parse_date(start).timestamp()))

    alerts, report = [], []
    for c in clients:
        r = compute(c, defaults, spent_usd.get(c["project_id"], 0.0))
        report.append(
            f"{c['name']}: ${r['usd']:.2f} × {r['rate']:.2f} ({r['rate_src']}) = {fmt_rub(r['spent'])} "
            f"из бюджета {fmt_rub(r['budget'])} (оплачено {fmt_rub(r['paid'])}, комиссия {fmt_rub(r['commission'])}) "
            f"→ осталось {fmt_rub(r['remaining'])}, {r['percent']:.0f}%"
        )

        thresholds = c.get("alert_thresholds_rub", defaults.get("alert_thresholds_rub", [5000, 0]))
        key = f"{c['project_id']}:{c['period_start']}:{c['prepaid_rub']}"
        fired = set(state.get(key, []))
        crossed = [t for t in thresholds if r["remaining"] <= t]

        if force:
            tpl = c.get("message_status") or defaults.get("message_status") or DEFAULT_TEMPLATES["message_status"]
            alerts.append(render(tpl, c, defaults, r))
        elif crossed:
            worst = min(crossed)  # если за сутки проскочили несколько порогов — пишем про самый серьёзный
            if str(worst) not in fired:
                tpl_key = "message_exhausted" if worst <= 0 else "message_low"
                tpl = c.get(tpl_key) or defaults.get(tpl_key) or DEFAULT_TEMPLATES[tpl_key]
                alerts.append(render(tpl, c, defaults, r, worst))
        fired.update(str(t) for t in crossed)
        state[key] = sorted(fired)

    print("\n".join(report))

    if alerts:
        text = "\n\n".join(alerts)
        if dry_run or not webhook:
            print("\n--- Сообщение в Slack (не отправлено) ---\n" + text)
        else:
            http_post_json(webhook, {"text": text})
            print(f"\nОтправлено в Slack: {len(alerts)}")

    if not dry_run and not force:
        save_state(state)


if __name__ == "__main__":
    main()
