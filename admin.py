#!/usr/bin/env python3
"""
Админка: правит clients.json по формам из GitHub Actions и пишет подтверждение в Slack.

  python3 admin.py invoice  --client "Займер" --amount 80000 [--date 2026-11-12]
  python3 admin.py add-client --name "Ромашка" --project-id proj_... --amount 26000 [--date 2026-11-12]
                              [--commission 10]

Бюджет клиента = сумма всех выставленных счетов (оплата на расчёт не влияет).
  python3 admin.py pause|resume --client "Займер"

Переменные окружения: SLACK_WEBHOOK_URL (необязательно), CLIENTS_FILE (по умолчанию clients.json).
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from billing_monitor import fmt_rub, http_post_json, invoiced_total, ddmm, compute, fetch_costs_by_project, parse_date

CLIENTS_FILE = Path(os.getenv("CLIENTS_FILE", "clients.json"))
MSK = timezone(timedelta(hours=3))


class AdminError(Exception):
    pass


def today():
    return datetime.now(MSK).strftime("%Y-%m-%d")


def norm_date(s):
    s = (s or "").strip()
    if not s:
        return today()
    if re.fullmatch(r"\d{1,2}\.\d{1,2}", s):
        s = f"{s}.{datetime.now(MSK).year}"
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise AdminError(f"Не понимаю дату «{s}». Пишите как 12.11.2026 или 2026-11-12.")


def norm_amount(s):
    s = re.sub(r"(руб\.?|р\.?|₽|\s)", "", str(s), flags=re.I).replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        raise AdminError(f"Не понимаю сумму «{s}». Пишите числом, например 80000.")
    if v <= 0:
        raise AdminError("Сумма должна быть больше нуля.")
    return int(v) if v == int(v) else v


def find_client(cfg, name):
    name_l = name.strip().lower()
    for c in cfg["clients"]:
        if c["name"].strip().lower() == name_l:
            return c
    names = ", ".join(c["name"] for c in cfg["clients"])
    raise AdminError(f"Клиент «{name}» не найден. Есть: {names}.")


def migrate(client):
    """Старый формат (prepaid_rub) → список счетов."""
    if "invoices" not in client:
        amt = client.pop("prepaid_rub", 0)
        client["invoices"] = [{"amount_rub": amt, "issued": client["period_start"]}] if amt else []
    return client


def summary(c, fee):
    total = invoiced_total(c)
    return (f"Всего счетов с {ddmm(c['period_start'])}: {fmt_rub(total)}, "
            f"бюджет на токены {fmt_rub(total * (1 - fee / 100))}")


def fee_of(cfg, c):
    return float(c.get("commission_pct", cfg.get("defaults", {}).get("commission_pct", 0)))


def cmd_invoice(cfg, a):
    c = migrate(find_client(cfg, a.client))
    amount, date = norm_amount(a.amount), norm_date(a.date)
    if any(float(i["amount_rub"]) == float(amount) and i["issued"] == date for i in c["invoices"]):
        raise AdminError(f"Счёт на {fmt_rub(amount)} от {ddmm(date)} у {c['name']} уже внесён. "
                         "Если это правда второй такой счёт, поставьте другую дату.")
    c["invoices"].append({"amount_rub": amount, "issued": date})
    fee = fee_of(cfg, c)
    return (f":receipt: *{c['name']}*: выставлен счёт на {fmt_rub(amount)} от {ddmm(date)}, "
            f"на токены из него {fmt_rub(amount * (1 - fee / 100))}.\n{summary(c, fee)}")


def cmd_add_client(cfg, a):
    name = a.name.strip()
    if not name:
        raise AdminError("Укажите название клиента.")
    if any(c["name"].strip().lower() == name.lower() for c in cfg["clients"]):
        raise AdminError(f"Клиент «{name}» уже есть.")
    pid = a.project_id.strip()
    if not re.fullmatch(r"proj_[A-Za-z0-9]+", pid):
        raise AdminError(f"Project ID должен выглядеть как proj_XXXX, а пришло «{pid}».")
    if any(c["project_id"] == pid for c in cfg["clients"]):
        raise AdminError(f"Проект {pid} уже привязан к другому клиенту.")
    amount, date = norm_amount(a.amount), norm_date(a.date)
    client = {
        "name": name,
        "project_id": pid,
        "period_start": date,
        "invoices": [{"amount_rub": amount, "issued": date}],
    }
    if a.commission not in (None, ""):
        client["commission_pct"] = float(a.commission)
    cfg["clients"].append(client)
    return f":new: Добавлен клиент *{name}* ({pid}), счёт на {fmt_rub(amount)} от {ddmm(date)}.\n{summary(client, fee_of(cfg, client))}"


def balance_line(cfg, name):
    """Строка с текущим остатком клиента. Если OpenAI недоступен — пусто, подтверждение всё равно уйдёт."""
    key = os.getenv("OPENAI_ADMIN_KEY")
    if not key:
        return ""
    try:
        c = find_client(cfg, name)
        usd = fetch_costs_by_project(key, [c["project_id"]], parse_date(c["period_start"]).timestamp())[c["project_id"]]
        r = compute(c, cfg.get("defaults", {}), usd)
        return (f"\nУже потрачено: {fmt_rub(r['spent'])} (${usd:.2f}, курс {r['rate']:.2f})\n"
                f"*Осталось на токены: {fmt_rub(r['remaining'])}*")
    except Exception as e:
        print(f"[warn] не удалось посчитать остаток: {e}", file=sys.stderr)
        return ""


def cmd_pause(cfg, a, enabled):
    c = find_client(cfg, a.client)
    c["enabled"] = enabled
    return f":{'arrow_forward' if enabled else 'double_vertical_bar'}: *{c['name']}*: мониторинг {'включён' if enabled else 'на паузе'}."


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("invoice")
    i.add_argument("--client", required=True)
    i.add_argument("--amount", required=True)
    i.add_argument("--date", default="")
    n = sub.add_parser("add-client")
    n.add_argument("--name", required=True)
    n.add_argument("--project-id", required=True)
    n.add_argument("--amount", required=True)
    n.add_argument("--date", default="")
    n.add_argument("--commission", default="")
    for name in ("pause", "resume"):
        x = sub.add_parser(name)
        x.add_argument("--client", required=True)
    a = p.parse_args()

    cfg = json.loads(CLIENTS_FILE.read_text("utf-8"))
    webhook = os.getenv("SLACK_WEBHOOK_URL")
    try:
        if a.cmd == "invoice":
            msg = cmd_invoice(cfg, a) + balance_line(cfg, a.client)
        elif a.cmd == "add-client":
            msg = cmd_add_client(cfg, a) + balance_line(cfg, a.name)
        else:
            msg = cmd_pause(cfg, a, a.cmd == "resume")
    except AdminError as e:
        print(f"Ошибка: {e}")
        if webhook:
            http_post_json(webhook, {"text": f":x: Админка: {e}"})
        sys.exit(1)

    CLIENTS_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(msg)
    if webhook:
        http_post_json(webhook, {"text": msg})


if __name__ == "__main__":
    main()
