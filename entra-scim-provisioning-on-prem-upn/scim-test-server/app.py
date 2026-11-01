"""A small SCIM 2.0 test server for the Rwanda Corporation lab.

It stands in for Netskope's SCIM endpoint, so Microsoft Entra provisioning has a
real place to search, create, match and update users and groups. Data is kept in
a JSON file (DATA_DIR/scim.json), so it survives restarts. For lab use only.

Settings (App Service > Environment variables):
  SCIM_TOKEN     bearer token Entra sends (Secret Token in the provisioning page)
  CLIENT_ID      optional, for OAuth2 client credentials
  CLIENT_SECRET  optional, for OAuth2 client credentials
  DATA_DIR       where scim.json is kept (default /home/data)

Endpoints:
  /scim/v2/Users, /scim/v2/Groups   SCIM 2.0 (GET with filter, POST, PUT, PATCH, DELETE)
  /oauth/token                      OAuth2 client credentials token endpoint
  /ui                               users, groups and recent requests (sign in with
                                    any user name and SCIM_TOKEN as the password)
"""
import datetime as dt
import hmac
import json
import os
import re
import secrets
import threading
import uuid
from html import escape
from urllib.parse import unquote

from flask import Flask, Response, request
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

SCIM_TOKEN = os.environ.get("SCIM_TOKEN", "")
CLIENT_ID = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
DATA_DIR = os.environ.get("DATA_DIR", "/home/data")
DATA_FILE = os.path.join(DATA_DIR, "scim.json")
BASE = "/scim/v2"
LOCK = threading.RLock()
LOG_KEEP = 300

USER = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
ENTERPRISE = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
KNOWN_SCHEMAS = [ENTERPRISE, USER, GROUP]
KEY_ATTR = {"Users": "userName", "Groups": "displayName"}

FILTER_RE = re.compile(r'^\s*([\w.]+)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.I)
VALUE_PATH_RE = re.compile(r'^(\w+)\[\s*(\w+)\s+eq\s+"([^"]*)"\s*\](?:\.(\w+))?$', re.I)


class ScimError(Exception):
    def __init__(self, status, detail, scim_type=None):
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type


# ---------- storage ----------

def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    for key in ("Users", "Groups", "tokens"):
        data.setdefault(key, {})
    data.setdefault("log", [])
    return data


def save(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, DATA_FILE)


def log(data, status, summary):
    query = unquote(request.query_string.decode("utf-8", "replace"))
    data["log"].append({
        "time": now(),
        "request": f"{request.method} {request.path}" + (f"?{query}" if query else ""),
        "status": status,
        "summary": summary,
    })
    del data["log"][:-LOG_KEEP]


# ---------- helpers ----------

def scim(body, status=200, headers=None):
    return Response(json.dumps(body), status=status, mimetype="application/scim+json", headers=headers)


def error(status, detail, scim_type=None):
    body = {"schemas": [ERROR], "status": str(status), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    return scim(body, status)


def same(a, b):
    return hmac.compare_digest(str(a).encode(), str(b).encode())


def ci_key(d, name):
    for k in d:
        if k.lower() == name.lower():
            return k
    return None


def ci_get(d, name, default=None):
    k = ci_key(d, name) if isinstance(d, dict) else None
    return d[k] if k is not None else default


def ci_set(d, name, value):
    k = ci_key(d, name)
    d[k if k is not None else name] = value


def ci_del(d, name):
    k = ci_key(d, name)
    if k is not None:
        del d[k]


def to_bool(v):
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return bool(v)


def item_key(v):
    return str(v.get("value")).lower() if isinstance(v, dict) and "value" in v else json.dumps(v, sort_keys=True)


def resource_url(kind, rid):
    return f"{request.host_url.rstrip('/')}{BASE}/{kind}/{rid}"


def shape(res, excluded=()):
    out = {k: v for k, v in res.items() if k.lower() not in excluded}
    return out


def excluded_attrs():
    raw = request.args.get("excludedAttributes", "")
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def name_of(res, kind):
    return ci_get(res, KEY_ATTR[kind]) or res.get("id")


# ---------- auth ----------

def bearer_ok():
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    token = auth[7:].strip()
    if SCIM_TOKEN and same(token, SCIM_TOKEN):
        return True
    if CLIENT_ID and CLIENT_SECRET:
        with LOCK:
            expiry = load()["tokens"].get(token)
        return bool(expiry and expiry > now())
    return False


def basic_ok():
    auth = request.authorization
    return bool(SCIM_TOKEN and auth and auth.type == "basic" and auth.password and same(auth.password, SCIM_TOKEN))


@app.before_request
def check_auth():
    path = request.path
    if path.startswith(BASE):
        if not (SCIM_TOKEN or (CLIENT_ID and CLIENT_SECRET)):
            return error(503, "The server has no SCIM_TOKEN or client credentials set.")
        if not bearer_ok():
            return error(401, "Missing or invalid bearer token.")
    elif path.startswith("/ui"):
        if not basic_ok():
            return Response("Sign in with any user name and the SCIM token as the password.", 401,
                            {"WWW-Authenticate": 'Basic realm="SCIM test server"'})
    return None


@app.errorhandler(ScimError)
def scim_error(e):
    return error(e.status, e.detail, e.scim_type)


# ---------- filter ----------

def parse_filter(text):
    if not text:
        return None
    m = FILTER_RE.match(text)
    if not m:
        raise ScimError(400, f"Unsupported filter: {text}. Only 'attribute eq \"value\"' is supported.", "invalidFilter")
    return m.group(1), m.group(2).replace('\\"', '"')


def values_at(res, path):
    current = [res]
    for part in path.split("."):
        nxt = []
        for c in current:
            v = ci_get(c, part) if isinstance(c, dict) else None
            if isinstance(v, list):
                nxt.extend(v)
            elif v is not None:
                nxt.append(v)
        current = nxt
    return [v for v in current if not isinstance(v, (dict, list))]


def matches(res, attr, value):
    if attr.lower() == "id":
        return res.get("id") == value
    return any(str(v).lower() == value.lower() for v in values_at(res, attr))


# ---------- patch ----------

def split_urn(path):
    """'urn:...:enterprise:2.0:User:department' -> (schema, 'department')."""
    if not path.lower().startswith("urn:"):
        return None, path
    for schema in KNOWN_SCHEMAS:
        if path.lower() == schema.lower():
            return schema, ""
        if path.lower().startswith(schema.lower() + ":"):
            return schema, path[len(schema) + 1:]
    i = path.rfind(":")
    return path[:i], path[i + 1:]


def container_for(res, path, create):
    schema, rest = split_urn(path)
    if schema and schema not in (USER, GROUP):
        cont = ci_get(res, schema)
        if cont is None and create:
            cont = {}
            res[schema] = cont
            if schema not in res.setdefault("schemas", []):
                res["schemas"].append(schema)
        return cont, rest
    return res, rest


def merge_list(current, value):
    seen = {item_key(v) for v in current}
    for v in value:
        if item_key(v) not in seen:
            current.append(v)
            seen.add(item_key(v))


def set_path(res, path, value, op):
    cont, rest = container_for(res, path, create=True)
    if rest == "":
        if isinstance(value, dict):
            for k, v in value.items():
                set_path(cont, k, v, op)
        return
    m = VALUE_PATH_RE.match(rest)
    if m:
        attr, fattr, fval, sub = m.groups()
        lst = ci_get(cont, attr)
        if not isinstance(lst, list):
            lst = []
            ci_set(cont, attr, lst)
        el = next((e for e in lst if isinstance(e, dict) and str(ci_get(e, fattr, "")).lower() == fval.lower()), None)
        if el is None:
            el = {fattr: fval}
            lst.append(el)
        if sub:
            ci_set(el, sub, value)
        elif isinstance(value, dict):
            el.update(value)
        return
    parts = rest.split(".")
    for p in parts[:-1]:
        nxt = ci_get(cont, p)
        if not isinstance(nxt, dict):
            nxt = {}
            ci_set(cont, p, nxt)
        cont = nxt
    last = parts[-1]
    current = ci_get(cont, last)
    if op == "add" and isinstance(current, list):
        merge_list(current, value if isinstance(value, list) else [value])
    elif op == "add" and isinstance(current, dict) and isinstance(value, dict):
        current.update(value)
    else:
        ci_set(cont, last, value)


def remove_path(res, path, value):
    cont, rest = container_for(res, path, create=False)
    if cont is None:
        return
    if rest == "":
        ci_del(res, path)
        return
    m = VALUE_PATH_RE.match(rest)
    if m:
        attr, fattr, fval, sub = m.groups()
        lst = ci_get(cont, attr)
        if not isinstance(lst, list):
            return
        for el in list(lst):
            if isinstance(el, dict) and str(ci_get(el, fattr, "")).lower() == fval.lower():
                if sub:
                    ci_del(el, sub)
                    if not {k.lower() for k in el} - {fattr.lower(), "primary", "type"}:
                        lst.remove(el)
                else:
                    lst.remove(el)
        return
    parts = rest.split(".")
    for p in parts[:-1]:
        cont = ci_get(cont, p)
        if not isinstance(cont, dict):
            return
    last = parts[-1]
    current = ci_get(cont, last)
    if isinstance(current, list) and isinstance(value, list) and value:
        drop = {item_key(v) for v in value}
        ci_set(cont, last, [v for v in current if item_key(v) not in drop])
    else:
        ci_del(cont, last)


def apply_patch(res, body):
    ops = ci_get(body, "Operations")
    if not isinstance(ops, list) or not ops:
        raise ScimError(400, "PATCH needs an Operations list.", "invalidSyntax")
    changed = []
    for op in ops:
        kind = str(ci_get(op, "op", "")).lower()
        path = ci_get(op, "path")
        value = ci_get(op, "value")
        if kind not in ("add", "replace", "remove"):
            raise ScimError(400, f"Unsupported op: {kind}", "invalidSyntax")
        if kind == "remove":
            if not path:
                raise ScimError(400, "remove needs a path.", "noTarget")
            remove_path(res, path, value)
            changed.append(f"removed {path}")
        elif path:
            set_path(res, path, value, kind)
            changed.append(f"{path} = {short(value)}")
        elif isinstance(value, dict):
            for k, v in value.items():
                set_path(res, k, v, kind)
                changed.append(f"{k} = {short(v)}")
        else:
            raise ScimError(400, "An op without a path needs an object value.", "invalidValue")
    return changed


def short(v):
    if isinstance(v, list) and all(isinstance(x, dict) and "value" in x for x in v):
        return f"{len(v)} item(s)"
    text = v if isinstance(v, str) else json.dumps(v)
    return text if len(text) <= 60 else text[:57] + "..."


def tidy(res, kind):
    if kind == "Users" and ci_key(res, "active") is not None:
        ci_set(res, "active", to_bool(ci_get(res, "active")))
    if kind == "Groups":
        members = ci_get(res, "members") or []
        ci_set(res, "members", [m for m in members if isinstance(m, dict) and m.get("value")])


def check_unique(data, kind, res, rid=None):
    key = KEY_ATTR[kind]
    name = ci_get(res, key)
    if not name:
        raise ScimError(400, f"{key} is required.", "invalidValue")
    for other_id, other in data[kind].items():
        if other_id != rid and str(ci_get(other, key, "")).lower() == str(name).lower():
            raise ScimError(409, f"A resource with {key} '{name}' already exists.", "uniqueness")


# ---------- SCIM routes ----------

def body_json():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        raise ScimError(400, "The request body must be a JSON object.", "invalidSyntax")
    return body


def list_resources(kind):
    with LOCK:
        data = load()
        try:
            flt = parse_filter(request.args.get("filter"))
            start = max(int(request.args.get("startIndex") or 1), 1)
            count = request.args.get("count")
            count = None if count in (None, "") else max(int(count), 0)
        except ValueError:
            raise ScimError(400, "startIndex and count must be numbers.", "invalidValue")
        except ScimError as e:
            log(data, e.status, e.detail)
            save(data)
            raise
        items = sorted(data[kind].values(), key=lambda r: r["meta"]["created"])
        if flt:
            items = [r for r in items if matches(r, *flt)]
        total = len(items)
        page = items[start - 1:] if count is None else items[start - 1:start - 1 + count]
        if flt:
            found = ", ".join(str(name_of(r, kind)) for r in items[:3])
            summary = f'search {flt[0]} eq "{flt[1]}": ' + (f"found {found}" if total else "no match")
        else:
            summary = f"listed {total} {kind.lower()}"
        log(data, 200, summary)
        save(data)
    ex = excluded_attrs()
    return scim({"schemas": [LIST], "totalResults": total, "startIndex": start,
                 "itemsPerPage": len(page), "Resources": [shape(r, ex) for r in page]})


def create_resource(kind):
    body = body_json()
    with LOCK:
        data = load()
        res = {k: v for k, v in body.items() if k.lower() not in ("id", "meta")}
        res.setdefault("schemas", [USER if kind == "Users" else GROUP])
        tidy(res, kind)
        try:
            check_unique(data, kind, res)
        except ScimError as e:
            log(data, e.status, f"create refused: {e.detail}")
            save(data)
            raise
        rid = str(uuid.uuid4())
        ts = now()
        res["id"] = rid
        res["meta"] = {"resourceType": kind[:-1], "created": ts, "lastModified": ts,
                       "location": resource_url(kind, rid)}
        data[kind][rid] = res
        extra = f" with {len(ci_get(res, 'members') or [])} member(s)" if kind == "Groups" else ""
        log(data, 201, f"created {kind[:-1].lower()} {name_of(res, kind)}{extra}")
        save(data)
    return scim(res, 201, {"Location": res["meta"]["location"]})


def get_resource(kind, rid):
    with LOCK:
        data = load()
        res = data[kind].get(rid)
        if res is None:
            log(data, 404, f"{kind[:-1].lower()} {rid} not found")
            save(data)
            raise ScimError(404, f"{kind[:-1]} {rid} not found.")
        log(data, 200, f"read {kind[:-1].lower()} {name_of(res, kind)}")
        save(data)
    return scim(shape(res, excluded_attrs()))


def replace_resource(kind, rid):
    body = body_json()
    with LOCK:
        data = load()
        old = data[kind].get(rid)
        if old is None:
            raise ScimError(404, f"{kind[:-1]} {rid} not found.")
        res = {k: v for k, v in body.items() if k.lower() not in ("id", "meta")}
        res.setdefault("schemas", old.get("schemas", []))
        tidy(res, kind)
        check_unique(data, kind, res, rid)
        res["id"] = rid
        res["meta"] = dict(old["meta"], lastModified=now())
        data[kind][rid] = res
        log(data, 200, f"replaced {kind[:-1].lower()} {name_of(res, kind)}")
        save(data)
    return scim(res)


def patch_resource(kind, rid):
    body = body_json()
    with LOCK:
        data = load()
        old = data[kind].get(rid)
        if old is None:
            log(data, 404, f"patch: {kind[:-1].lower()} {rid} not found")
            save(data)
            raise ScimError(404, f"{kind[:-1]} {rid} not found.")
        res = json.loads(json.dumps(old))
        try:
            changed = apply_patch(res, body)
            tidy(res, kind)
            check_unique(data, kind, res, rid)
        except ScimError as e:
            log(data, e.status, f"patch refused for {name_of(old, kind)}: {e.detail}")
            save(data)
            raise
        res["id"] = rid
        res["meta"] = dict(old["meta"], lastModified=now())
        data[kind][rid] = res
        log(data, 200, f"updated {kind[:-1].lower()} {name_of(old, kind)}: " + "; ".join(changed))
        save(data)
    return scim(res)


def delete_resource(kind, rid):
    with LOCK:
        data = load()
        res = data[kind].pop(rid, None)
        if res is None:
            log(data, 404, f"delete: {kind[:-1].lower()} {rid} not found")
            save(data)
            raise ScimError(404, f"{kind[:-1]} {rid} not found.")
        if kind == "Users":
            for g in data["Groups"].values():
                ci_set(g, "members", [m for m in ci_get(g, "members") or [] if m.get("value") != rid])
        log(data, 204, f"deleted {kind[:-1].lower()} {name_of(res, kind)}")
        save(data)
    return Response(status=204)


for _kind in ("Users", "Groups"):
    def _collection(kind=_kind):
        return list_resources(kind) if request.method == "GET" else create_resource(kind)

    def _item(rid, kind=_kind):
        return {"GET": get_resource, "PUT": replace_resource, "PATCH": patch_resource,
                "DELETE": delete_resource}[request.method](kind, rid)

    app.add_url_rule(f"{BASE}/{_kind}", f"{_kind}_collection", _collection, methods=["GET", "POST"])
    app.add_url_rule(f"{BASE}/{_kind}/<rid>", f"{_kind}_item", _item, methods=["GET", "PUT", "PATCH", "DELETE"])


@app.get(f"{BASE}/ServiceProviderConfig")
def service_provider_config():
    return scim({
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{"type": "oauthbearertoken", "name": "OAuth Bearer Token",
                                   "description": "Bearer token in the Authorization header", "primary": True}],
    })


@app.get(f"{BASE}/ResourceTypes")
def resource_types():
    types = [{"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"], "id": "User", "name": "User",
              "endpoint": "/Users", "schema": USER,
              "schemaExtensions": [{"schema": ENTERPRISE, "required": False}]},
             {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"], "id": "Group", "name": "Group",
              "endpoint": "/Groups", "schema": GROUP}]
    return scim({"schemas": [LIST], "totalResults": 2, "Resources": types})


@app.route(f"{BASE}/<path:rest>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def scim_not_found(rest):
    return error(404, f"Unknown SCIM endpoint: /{rest}")


# ---------- OAuth2 client credentials ----------

@app.post("/oauth/token")
def oauth_token():
    if not (CLIENT_ID and CLIENT_SECRET):
        return {"error": "unsupported_grant_type", "error_description": "Client credentials are not set up."}, 400
    cid, secret = request.form.get("client_id"), request.form.get("client_secret")
    auth = request.authorization
    if auth and auth.type == "basic":
        cid, secret = auth.username, auth.password
    if request.form.get("grant_type") != "client_credentials":
        return {"error": "unsupported_grant_type"}, 400
    if not (cid and secret and same(cid, CLIENT_ID) and same(secret, CLIENT_SECRET)):
        with LOCK:
            data = load()
            log(data, 401, "token refused: wrong client ID or secret")
            save(data)
        return {"error": "invalid_client"}, 401
    token = secrets.token_urlsafe(32)
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with LOCK:
        data = load()
        current = now()
        data["tokens"] = {t: e for t, e in data["tokens"].items() if e > current}
        data["tokens"][token] = expires
        log(data, 200, "issued an access token (client credentials)")
        save(data)
    return {"access_token": token, "token_type": "Bearer", "expires_in": 3600}


# ---------- pages ----------

@app.get("/")
def home():
    return Response("SCIM test server for the Rwanda Corporation lab. SCIM endpoint: /scim/v2. "
                    "Users and requests: /ui\n", mimetype="text/plain")


def cell(v):
    return f"<td>{escape(str(v)) if v not in (None, '') else '<span class=muted>(empty)</span>'}</td>"


@app.get("/ui")
def ui():
    with LOCK:
        data = load()
    users = sorted(data["Users"].values(), key=lambda r: r["meta"]["created"])
    groups = sorted(data["Groups"].values(), key=lambda r: r["meta"]["created"])
    names = {u["id"]: ci_get(u, "userName") for u in users}

    def work_email(u):
        emails = ci_get(u, "emails") or []
        e = next((x for x in emails if isinstance(x, dict) and str(x.get("type", "")).lower() == "work"), None)
        e = e or next((x for x in emails if isinstance(x, dict)), None)
        return e.get("value") if e else None

    user_rows = "".join(
        "<tr>" + cell(ci_get(u, "userName")) + cell(ci_get(u, "displayName"))
        + cell("Yes" if ci_get(u, "active", True) else "No") + cell(work_email(u))
        + cell(ci_get(u, "externalId")) + cell(u["meta"]["created"]) + cell(u["meta"]["lastModified"]) + "</tr>"
        for u in users) or "<tr><td colspan=7 class=muted>No users yet.</td></tr>"
    group_rows = "".join(
        "<tr>" + cell(ci_get(g, "displayName"))
        + cell(", ".join(str(names.get(m.get("value"), m.get("value"))) for m in ci_get(g, "members") or []))
        + cell(g["meta"]["lastModified"]) + "</tr>"
        for g in groups) or "<tr><td colspan=3 class=muted>No groups yet.</td></tr>"
    log_rows = "".join(
        f"<tr class='{'bad' if int(e['status']) >= 400 else ''}'>"
        + f"<td class=nowrap>{escape(e['time'].replace('T', ' ').rstrip('Z'))}</td>" + cell(e["request"])
        + cell(e["status"]) + cell(e["summary"]) + "</tr>"
        for e in reversed(data["log"][-60:])) or "<tr><td colspan=4 class=muted>No requests yet.</td></tr>"
    page = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>SCIM test server</title>
<style>
:root {{ --bg:#fff; --fg:#1d2433; --muted:#6b7280; --line:#e5e7eb; --head:#f3f4f6; --bad:#fef2f2; --accent:#1f5fbf; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#14171c; --fg:#e6e8eb; --muted:#9aa1ab; --line:#2a2f37; --head:#1c2027; --bad:#3a1d1d; --accent:#7fb0ff; }} }}
body {{ margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1180px; margin:0 auto; }}
h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:28px 0 8px; }}
p {{ margin:0; color:var(--muted); }} a {{ color:var(--accent); }}
.wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:8px; }}
table {{ border-collapse:collapse; width:100%; }}
th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ background:var(--head); font-weight:600; white-space:nowrap; }}
tr:last-child td {{ border-bottom:0; }} tr.bad td {{ background:var(--bad); }}
td {{ font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:12.5px; }}
.muted {{ color:var(--muted); font-family:inherit; }} .nowrap {{ white-space:nowrap; }}
</style></head><body><main>
<h1>SCIM test server &middot; Rwanda Corporation lab</h1>
<p>Stands in for Netskope. {len(users)} user(s), {len(groups)} group(s). Times are UTC. <a href="/ui">Refresh</a></p>
<h2>Users</h2><div class=wrap><table><tr><th>userName</th><th>displayName</th><th>Active</th><th>Work email</th>
<th>externalId</th><th>Created</th><th>Last modified</th></tr>{user_rows}</table></div>
<h2>Groups</h2><div class=wrap><table><tr><th>displayName</th><th>Members</th><th>Last modified</th></tr>{group_rows}</table></div>
<h2>Recent requests</h2><div class=wrap><table><tr><th>Time</th><th>Request</th><th>Status</th><th>What happened</th></tr>{log_rows}</table></div>
</main></body></html>"""
    return Response(page, mimetype="text/html", headers={"Cache-Control": "no-store"})
