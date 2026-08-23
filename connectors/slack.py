# -*- coding: utf-8 -*-
"""
Slack — рабочие обсуждения команды как знание для ИИ.

Владелец создаёт приложение у себя (api.slack.com/apps → From scratch), даёт
права `channels:read` и `channels:history`, ставит его в рабочее пространство
и копирует Bot User OAuth Token (xoxb-…). Затем приглашает бота в нужный
канал: `/invite @VELOR`.

Что делает коннектор: складывает свежие сообщения выбранных каналов в
документы компании — тем же путём, что и загруженные файлы. Дальше их
находит обычный поиск по документам и подхватывает Context Engine, поэтому
ассистент отвечает с учётом того, что команда обсуждала.
"""
import database
from connectors.base import (ConnectorError, field, need, request, since, stamp,
                             to_stamp)

ID = "slack"
NAME = "Slack"
GROUP = "Работа и команда"
GIVES = ["обсуждения команды в контексте бизнеса", "решения из переписки"]
HOWTO = ("api.slack.com/apps → Create New App → From scratch → OAuth & Permissions: "
         "добавьте channels:read и channels:history → Install to Workspace → "
         "скопируйте Bot User OAuth Token (xoxb-…). Затем в нужном канале "
         "напишите /invite @имя_бота.")
FIELDS = [
    field("token", "Bot User OAuth Token", "Начинается на xoxb-", secret=True),
    field("channels", "Каналы", "Через запятую, без решётки. Пусто — все, куда добавлен бот",
          required=False, placeholder="general, sales"),
]

API = "https://slack.com/api"
MAX_MESSAGES = 300


def _headers(creds):
    return {"Authorization": "Bearer " + need(creds, "token")}


def _call(creds, method, params=None):
    data = request("GET", f"{API}/{method}", headers=_headers(creds), params=params or {})
    if not data.get("ok"):
        err = data.get("error") or "unknown"
        human = {
            "invalid_auth": "Токен не подошёл. Скопируйте Bot User OAuth Token заново.",
            "missing_scope": "У приложения не хватает прав. Добавьте channels:read "
                             "и channels:history и переустановите приложение.",
            "not_in_channel": "Бот не добавлен в канал. Напишите в канале /invite @имя_бота.",
        }.get(err, f"Slack отклонил запрос: {err}")
        raise ConnectorError(human)
    return data


def check(creds, meta):
    data = _call(creds, "auth.test")
    return dict(meta or {}, team=data.get("team"), bot=data.get("user"), verified=True)


def _wanted(creds):
    raw = (creds.get("channels") or "").strip()
    return {c.strip().lstrip("#").lower() for c in raw.split(",") if c.strip()} if raw else None


def sync(bid, creds, meta, cursor):
    wanted = _wanted(creds)
    oldest = since(cursor, days=14).timestamp()      # переписка живёт коротко
    data = _call(creds, "conversations.list",
                 {"types": "public_channel", "limit": 200, "exclude_archived": "true"})

    added = 0
    for ch in (data.get("channels") or []):
        name = (ch.get("name") or "").lower()
        if wanted is not None and name not in wanted:
            continue
        if wanted is None and not ch.get("is_member"):
            continue                                  # бота туда не звали
        try:
            hist = _call(creds, "conversations.history",
                         {"channel": ch.get("id"), "oldest": int(oldest),
                          "limit": MAX_MESSAGES})
        except ConnectorError:
            continue                                  # один недоступный канал не ломает остальные

        lines = []
        for m in reversed(hist.get("messages") or []):
            text = (m.get("text") or "").strip()
            if not text or m.get("subtype"):           # служебные события пропускаем
                continue
            lines.append(text[:400])
        if not lines:
            continue

        # Кладём как обычный документ компании: его увидят и поиск, и Context Engine.
        body = "\n".join(lines)
        filename = f"Slack #{ch.get('name')} ({stamp()[:10]})"
        database.add_document(bid, filename, body)
        added += 1
    return added, stamp()
