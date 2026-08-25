# -*- coding: utf-8 -*-

#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program. If not, see <http://www.gnu.org/licenses/>.

# Server di sincronizzazione compatibile con il protocollo kosync di KOReader,
# usato anche dagli e-reader Xteink con firmware CrossPoint. Sincronizza il
# punto di lettura tra dispositivi della famiglia KOReader.
#
# Note:
#  - Gli utenti kosync sono un archivio a se' (tabella kosync_user), distinto
#    dagli account calibre-web: KOReader autentica inviando md5(password) come
#    chiave (header x-auth-key), incompatibile con l'hashing di calibre-web.
#    E' anche il modello dei server kosync di riferimento (es. crosspoint-sync).
#  - Il "document" e' un hash calcolato dal dispositivo; il "progress" e' un
#    locator opaco (xpointer/pagina) che memorizziamo cosi' com'e'.
#
# Endpoint (montati sotto /kosync):
#   POST /users/create           {username, password}      -> crea utente
#   GET  /users/auth             headers x-auth-user/key    -> valida credenziali
#   PUT  /syncs/progress         {document, progress, ...}  -> salva progresso
#   GET  /syncs/progress/<doc>   headers x-auth-user/key    -> legge progresso

from functools import wraps
from time import time

from flask import Blueprint, request, jsonify, make_response

from . import ub, logger, csrf

log = logger.create()

kosync = Blueprint("kosync", __name__, url_prefix="/kosync")


def _error(status, message):
    return make_response(jsonify({"message": message}), status)


def _authenticated_user():
    username = request.headers.get("x-auth-user")
    userkey = request.headers.get("x-auth-key")
    if not username or not userkey:
        return None
    user = (
        ub.session.query(ub.KoSyncUser)
        .filter(ub.KoSyncUser.username == username)
        .first()
    )
    if user and user.userkey == userkey:
        return user
    return None


def requires_kosync_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _authenticated_user()
        if not user:
            return _error(401, "Unauthorized")
        return f(user, *args, **kwargs)

    return decorated


@csrf.exempt
@kosync.route("/users/create", methods=["POST"])
def create_user():
    data = request.get_json(force=True, silent=True) or {}
    username = data.get("username")
    password = data.get("password")  # md5 inviato dal client
    if not username or not password:
        return _error(400, "Invalid request")
    existing = (
        ub.session.query(ub.KoSyncUser)
        .filter(ub.KoSyncUser.username == username)
        .first()
    )
    if existing:
        return _error(409, "Username is already registered.")
    user = ub.KoSyncUser()
    user.username = username
    user.userkey = password
    ub.session.add(user)
    ub.session_commit()
    return make_response(jsonify({"username": username}), 201)


@csrf.exempt
@kosync.route("/users/auth", methods=["GET"])
@requires_kosync_auth
def authorize(user):
    return make_response(jsonify({"authorized": "OK"}), 200)


@csrf.exempt
@kosync.route("/syncs/progress", methods=["PUT"])
@requires_kosync_auth
def update_progress(user):
    data = request.get_json(force=True, silent=True) or {}
    document = data.get("document")
    if not document:
        return _error(400, "Invalid request")
    record = (
        ub.session.query(ub.KoSyncProgress)
        .filter(
            ub.KoSyncProgress.user_id == user.id,
            ub.KoSyncProgress.document == document,
        )
        .first()
    )
    if not record:
        record = ub.KoSyncProgress()
        record.user_id = user.id
        record.document = document
        ub.session.add(record)
    record.progress = str(data.get("progress", ""))
    try:
        record.percentage = float(data.get("percentage", 0) or 0)
    except (TypeError, ValueError):
        record.percentage = 0.0
    record.device = str(data.get("device", ""))
    record.device_id = str(data.get("device_id", ""))
    record.timestamp = int(time())
    ub.session_commit()
    return make_response(
        jsonify({"document": document, "timestamp": record.timestamp}), 200
    )


@csrf.exempt
@kosync.route("/syncs/progress/<document>", methods=["GET"])
@requires_kosync_auth
def get_progress(user, document):
    record = (
        ub.session.query(ub.KoSyncProgress)
        .filter(
            ub.KoSyncProgress.user_id == user.id,
            ub.KoSyncProgress.document == document,
        )
        .first()
    )
    if not record:
        return make_response(jsonify({}), 200)
    return make_response(
        jsonify(
            {
                "document": record.document,
                "progress": record.progress,
                "percentage": record.percentage,
                "device": record.device,
                "device_id": record.device_id,
                "timestamp": record.timestamp,
            }
        ),
        200,
    )
