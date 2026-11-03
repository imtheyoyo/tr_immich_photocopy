#!/usr/bin/env python3
"""
PhotoCopy (tr-photocopy) - web app: point it at a folder on a NAS, see which photos/videos
are not in your Immich account (with previews), then copy them to a folder, upload them to
Immich, or move the ones already in Immich to the share's recycle bin.

Matching is SHA-1 vs the checksum of every live asset owned by the API key's account (the same
hash Immich uses for its own duplicate check), read straight from Postgres.

Never overwrites. "Delete" moves to the share's #recycle (recoverable in UGOS), only for files
whose bytes are confirmed in Immich at that moment, and never inside a folder Immich reads.

Env (normally from .env next to the compose file):
  PGHOST / PGUSER / PGPASSWORD / PGDATABASE   Immich Postgres
  IMMICH_URL       default http://immich_server:2283
  IMMICH_KEY_<NAME>  one per immich account, e.g. IMMICH_KEY_MAIN, IMMICH_KEY_MEMORIES - picked in the page
                   (asset.upload, asset.read, user.read, tag.create, tag.asset; album.create +
                   albumAsset.create for folder -> album)
  IMMICH_DEFAULT   name selected at startup, default the first account that can upload
  IMMICH_NO_UPLOAD comma list of NAMEs that can match but never upload / upgrade (external-library accounts)
  IMMICH_API_KEY / IMMICH_API_KEY_FILE   single-key fallback, shown as MAIN
  ALLOWED_ROOTS    comma list - scans, browsing, copies, moves, deletes must sit under one, default /volume1
                   (ALLOWED_ROOT still read)
  BLOCKED_DEST     comma list of destination prefixes refused, default /volume1/appdata
  LIBRARY_PATHS    paths Immich reads - never moved or deleted from
                   default /volume1/appdata/immich,/volume1/photos/exports_lr,/volume1/memories
  CACHE_DIR        thumbnail cache, default /cache
  PORT             default 8791
  CHECKSUMS_FILE   hex SHA-1 list used instead of Postgres (testing)
"""
import errno, csv, datetime, re, hashlib, io, json, os, shutil, subprocess, threading, time, traceback, uuid
import urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageOps
Image.MAX_IMAGE_PIXELS = None  # local trusted files; panoramas trip the decompression-bomb guard
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

IMAGE = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp"}
RAW = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".srw", ".pef"}
VIDEO = {".mp4", ".mov", ".m4v", ".avi", ".mts", ".m2ts", ".3gp", ".mkv", ".webm"}
MEDIA = IMAGE | RAW | VIDEO
SKIP_DIRS = {"#recycle", "@eaDir", ".sync", ".stversions", ".stfolder", "__MACOSX", ".Spotlight-V100", ".fseventsd"}

def _paths(v):
    return [os.path.realpath(p.strip()) for p in v.split(",") if p.strip()]


ROOTS = _paths(os.environ.get("ALLOWED_ROOTS") or os.environ.get("ALLOWED_ROOT") or "/volume1")
BLOCKED_DEST = _paths(os.environ.get("BLOCKED_DEST", "/volume1/appdata"))
LIBRARY_PATHS = _paths(os.environ.get("LIBRARY_PATHS", "/volume1/appdata/immich,/volume1/photos/exports_lr,/volume1/memories"))
IMMICH_URL = os.environ.get("IMMICH_URL", "http://immich_server:2283").rstrip("/")


def _keys():
    """{NAME: key} in .env order; IMMICH_API_KEY(_FILE) is the single-key fallback."""
    keys = {k[len("IMMICH_KEY_"):]: v.strip() for k, v in os.environ.items()
            if k.startswith("IMMICH_KEY_") and k != "IMMICH_KEY_FILE" and v.strip()}
    if keys:
        return keys
    k = os.environ.get("IMMICH_API_KEY", "").strip()
    f = os.environ.get("IMMICH_API_KEY_FILE", "")
    if not k and f and os.path.isfile(f):
        for line in open(f):
            line = line.strip()
            if line and not line.startswith("#"):
                k = line.split("=", 1)[1].strip() if line.startswith("IMMICH_API_KEY=") else line
                break
    return {"MAIN": k} if k else {}


KEYS = _keys()
def _name(n):
    """Account name as written in .env, matched case-insensitively."""
    return next((k for k in KEYS if k.lower() == (n or "").strip().lower()), "")


NO_UPLOAD = {_name(n) for n in os.environ.get("IMMICH_NO_UPLOAD", "").split(",") if _name(n)}
_active_file = Path(os.environ.get("CACHE_DIR", "/cache")) / "account.txt"
try:
    _saved = _active_file.read_text().strip()
except OSError:
    _saved = ""
# Dropdown order: accounts that can upload first, then the match-only ones, each A-Z
KEYS = dict(sorted(KEYS.items(), key=lambda kv: (kv[0] in NO_UPLOAD, kv[0].lower())))
# Docker hands env vars over alphabetically, so "first" means nothing - fall back to the first account that can upload
ACTIVE = (_name(_saved) or _name(os.environ.get("IMMICH_DEFAULT"))
          or next((k for k in KEYS if k not in NO_UPLOAD), "") or next(iter(KEYS), ""))
if os.environ.get("IMMICH_DEFAULT") and not _name(os.environ.get("IMMICH_DEFAULT")):
    print(f"IMMICH_DEFAULT={os.environ['IMMICH_DEFAULT']} matches no IMMICH_KEY_<name> ({', '.join(KEYS)})", flush=True)


def root_of(p):
    """The allowed root a path sits under, else None."""
    for root in ROOTS:
        if under(p, root):
            return root
    return None


def can_upload():
    return bool(ACTIVE) and ACTIVE not in NO_UPLOAD
CACHE = Path(os.environ.get("CACHE_DIR", "/cache"))
PORT = int(os.environ.get("PORT", "8791"))
THUMB = 320

state_lock = threading.Lock()
state = {"running": False, "phase": "idle", "done": 0, "total": 0, "root": "", "error": "",
         "items": [], "account": "", "started": 0, "finished": 0}
known = {}  # path -> sha1 for the current scan


def under(path, root):
    path = os.path.realpath(path)
    return path == root or path.startswith(root.rstrip("/") + "/")


def set_progress(**kw):
    with state_lock:
        state.update(kw)


# ---------- Immich ----------

def api(method, path, body=None, headers=None, key=None):
    key = key or KEYS.get(ACTIVE, "")
    if not key:
        raise RuntimeError("No immich API key set")
    h = {"x-api-key": key, "Accept": "application/json"}
    h.update(headers or {})
    r = urllib.request.Request(IMMICH_URL + path, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=600) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def me(key=None):
    code, j = api("GET", "/api/users/me", key=key)
    if code != 200:
        raise RuntimeError(f"immich /users/me failed ({code}): {j.get('message', j)}")
    return j["id"], (j.get("name") or "").strip() or j.get("email", "")


def immich_names(owner_id):
    """{lower original filename: [(asset id, size bytes or 0)]} for the owner's live assets."""
    f = os.environ.get("NAMES_FILE")  # testing: lines "name<TAB>id<TAB>size"
    out = {}
    if f:
        for l in open(f):
            n, i, s = l.rstrip("\n").split("\t")
            out.setdefault(n.lower(), []).append((i, int(s or 0)))
        return out
    last = ""
    for asset, exif in (("asset", "asset_exif"), ("assets", "exif")):
        sql = (f"select a.id, a.\"originalFileName\", coalesce(e.\"fileSizeInByte\", 0) from {asset} a "
               f"left join {exif} e on e.\"assetId\" = a.id "
               f"where a.\"deletedAt\" is null and a.\"ownerId\" = '{owner_id}'")
        r = subprocess.run(["psql", "-At", "-F", "\t", "-c", sql], capture_output=True, text=True)
        if r.returncode == 0:
            for l in r.stdout.splitlines():
                parts = l.split("\t")
                if len(parts) == 3:
                    out.setdefault(parts[1].lower(), []).append((parts[0], int(parts[2] or 0)))
            return out
        last = r.stderr.strip()
    raise RuntimeError(f"immich Postgres name query failed: {last}")


def immich_checksums(owner_id):
    f = os.environ.get("CHECKSUMS_FILE")
    if f:
        return {l.strip().lower() for l in open(f) if l.strip()}
    last = ""
    for table in ("asset", "assets"):  # older Immich used the plural
        sql = (f"select encode(checksum, 'hex') from {table} "
               f"where \"deletedAt\" is null and \"ownerId\" = '{owner_id}'")
        r = subprocess.run(["psql", "-At", "-c", sql], capture_output=True, text=True)
        if r.returncode == 0:
            return {l.strip() for l in r.stdout.splitlines() if l.strip()}
        last = r.stderr.strip()
    raise RuntimeError(f"immich Postgres query failed: {last}")


key_state = {}  # NAME -> {"ok", "msg", "at"}


def check_key(name=None):
    """Validate keys against Immich (all at startup), so the page can say so before any scan."""
    for n in ([name] if name else KEYS):
        st = key_state.setdefault(n, {"ok": False, "msg": "", "at": 0})
        st["at"] = time.time()
        try:
            _, who = me(KEYS[n])
            st.update(ok=True, msg=who)
        except Exception as e:
            st.update(ok=False, msg=f"key rejected: {e}")
    if ACTIVE in key_state and key_state[ACTIVE]["ok"]:
        set_progress(account=key_state[ACTIVE]["msg"])


def account():
    if os.environ.get("CHECKSUMS_FILE"):
        return "test", "test"
    return me()


def upload(p):
    st = os.stat(p)
    when = datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc).isoformat()
    boundary = uuid.uuid4().hex
    fields = {"deviceAssetId": f"photocopy-{os.path.basename(p)}-{st.st_size}-{int(st.st_mtime)}",
              "deviceId": "photocopy", "fileCreatedAt": when, "fileModifiedAt": when,
              "isFavorite": "false"}
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    name = os.path.basename(p).replace('"', "_")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"assetData\"; filename=\"{name}\"\r\n"
                 f"Content-Type: application/octet-stream\r\n\r\n".encode())
    parts.append(Path(p).read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    code, j = api("POST", "/api/assets", b"".join(parts),
                  {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    if code in (200, 201):
        return j.get("status", "created"), j.get("id")
    raise RuntimeError(f"{code}: {j.get('message', j)}")


def tag_assets(value, ids):
    """Upsert a hierarchical tag (PUT /api/tags) and apply it (PUT /api/tags/assets). Returns count."""
    code, j = api("PUT", "/api/tags", json.dumps({"tags": [value]}).encode(), {"Content-Type": "application/json"})
    if code != 200 or not isinstance(j, list) or not j:
        raise RuntimeError(f"tag upsert {code}: {j}")
    tag_id, n = j[0]["id"], 0
    for i in range(0, len(ids), 500):
        code, r = api("PUT", "/api/tags/assets", json.dumps({"tagIds": [tag_id], "assetIds": ids[i:i + 500]}).encode(),
                      {"Content-Type": "application/json"})
        if code != 200:
            raise RuntimeError(f"tag apply {code}: {r}")
        n += r.get("count", 0) if isinstance(r, dict) else sum(1 for x in r if x.get("success"))
    return n


# ---------- scanning ----------

def sha1(p):
    h = hashlib.sha1()
    try:
        with open(p, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest()
    except OSError:
        return None


# App / non-camera sources, matched on folder + file name (and EXIF Software). First hit wins.
SOURCES = [
    ("Screenshot", re.compile(r"screenshot|screen ?shot|screen_?recording|scrnli", re.I), None),
    ("WhatsApp", re.compile(r"whatsapp|(^|/)(img|vid)-\d{8}-wa\d+", re.I), None),
    ("Facebook", re.compile(r"facebook|(^|/)fb_img_|messenger|(^|/)received_\d+", re.I), None),
    ("Instagram", re.compile(r"instagram", re.I), re.compile(r"instagram", re.I)),
    ("Snapchat", re.compile(r"snapchat", re.I), None),
    ("Twitter / X", re.compile(r"twitter|(^|/)twimg", re.I), None),
    ("Telegram", re.compile(r"telegram", re.I), None),
    ("Bixby Vision", re.compile(r"bixby", re.I), None),
    ("Download", re.compile(r"(^|/)downloads?(/|$)", re.I), None),
]


def source_of(rel, camera, software):
    for name, path_re, sw_re in SOURCES:
        if path_re.search(rel) or (sw_re and software and sw_re.search(software)):
            return name
    return camera or ""


# Friendly names for phone model codes (extend as needed)
MODEL_NAMES = {
    "SM-G930F": "Galaxy S7", "SM-G935F": "Galaxy S7 edge", "SM-G950F": "Galaxy S8", "SM-G955F": "Galaxy S8+",
    "SM-G960F": "Galaxy S9", "SM-G965F": "Galaxy S9+", "SM-G970F": "Galaxy S10e", "SM-G973F": "Galaxy S10",
    "SM-G975F": "Galaxy S10+", "SM-N950F": "Galaxy Note8", "SM-N960F": "Galaxy Note9",
    "SM-G991B": "Galaxy S21", "SM-G996B": "Galaxy S21+", "SM-G998B": "Galaxy S21 Ultra",
    "SM-S901B": "Galaxy S22", "SM-S906B": "Galaxy S22+", "SM-S908B": "Galaxy S22 Ultra",
    "SM-S911B": "Galaxy S23", "SM-S916B": "Galaxy S23+", "SM-S918B": "Galaxy S23 Ultra",
    "SM-S921B": "Galaxy S24", "SM-S926B": "Galaxy S24+", "SM-S928B": "Galaxy S24 Ultra",
}


def _camera_name(make, model):
    make, model = (make or "").strip(), (model or "").strip()
    if not model:
        return make
    nice = MODEL_NAMES.get(model.upper())
    if nice:
        return f"{nice} ({model})"
    if make and model.lower().startswith(make.split()[0].lower()):
        make = ""
    return " ".join(x for x in (make, model) if x)


def _first(row, *keys):
    """First non-empty value among exiftool CSV columns whose name ends with any of keys."""
    for k in keys:  # exact column first (EXIF Make/Model), then vendor variants (AndroidModel etc.)
        v = (row.get(k) or "").strip()
        if v and v != "-":
            return v
    for k in keys:
        for col, v in row.items():
            c = (col or "").lower()
            if c.endswith(k.lower()) and not c.startswith("lens") and v and v.strip() and v.strip() != "-":
                return v.strip()
    return ""


def cameras(paths):
    """exiftool Make/Model/Software -> {path: ("camera name", "Software")}; empty name = no camera info.
    Photos: EXIF Make/Model. Videos: any *Make*/*Model* tag (phones write them as QuickTime Keys /
    Android / vendor tags, often at the end of the file, so videos are read without -fast2)."""
    out = {}
    vids = [p for p in paths if Path(p).suffix.lower() in VIDEO]
    imgs = [p for p in paths if Path(p).suffix.lower() not in VIDEO]
    done = 0
    for group, args in ((imgs, ["-fast2", "-Make", "-Model", "-Software"]),
                        (vids, ["*Make*", "*Model*", "-Software", "-Encoder"])):
        args = [a if a.startswith("-") else "-" + a for a in args]
        for i in range(0, len(group), 200):
            check_stop()
            chunk = group[i:i + 200]
            r = subprocess.run(["exiftool", "-q", "-m", "-csv", *args, "-charset", "filename=utf8", "-@", "-"],
                               input="\n".join(chunk) + "\n", capture_output=True, text=True)
            for row in csv.DictReader(r.stdout.splitlines()):
                make = _first(row, "Make", "Manufacturer")
                model = _first(row, "Model")
                out[row["SourceFile"]] = (_camera_name(make, model), _first(row, "Software", "Encoder"))
            done += len(chunk)
            set_progress(done=done)
    return out


def decodes(p):
    """True if the image decodes fully. Damaged files must never replace a good immich copy."""
    if Path(p).suffix.lower() not in IMAGE:
        return True
    try:
        with Image.open(p) as im:
            im.draft("RGB", (512, 512))  # JPEG: still walks the whole entropy stream, at a fraction of the cost
            im.load()
        return True
    except Exception:
        return False


def walk(root, excludes):
    """excludes: folder names (skipped at any depth) or paths relative to root (contain '/' or match a top-level dir)."""
    names = {e for e in excludes if "/" not in e}
    rels = {e.strip("/") for e in excludes}
    skip = SKIP_DIRS | names
    for dp, dns, fns in os.walk(root):
        reld = os.path.relpath(dp, root)
        dns[:] = sorted(d for d in dns if d not in skip and not d.startswith(".Trash")
                        and (d if reld == "." else f"{reld}/{d}") not in rels)
        for n in sorted(fns):
            if n.startswith("._"):
                continue
            if Path(n).suffix.lower() in MEDIA:
                yield os.path.join(dp, n)


class Stopped(Exception):
    pass


def check_stop():
    if state.get("cancel"):
        raise Stopped()


def scan(root, excludes):
    try:
        set_progress(running=True, phase="Reading Immich checksums", done=0, total=0, root=root,
                     error="", items=[], started=time.time(), finished=0, cancel=False)
        owner, email = account()
        have = immich_checksums(owner)
        try:
            names = immich_names(owner)
        except Exception as e:  # never let the same-name check break a scan
            print(f"same-name lookup failed: {e}", flush=True)
            names = {}
        set_progress(account=email)
        set_progress(phase="Listing files")
        files = []
        for f in walk(root, excludes):
            files.append(f)
            if len(files) % 500 == 0:
                check_stop()
                set_progress(phase=f"Listing files ({len(files):,})")
        set_progress(phase=f"SHA-1 ({len(have):,} assets in {email})", total=len(files), done=0)
        hashes = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            for i, (p, h) in enumerate(zip(files, ex.map(lambda f: None if state.get("cancel") else sha1(f), files)), 1):
                hashes[p] = h
                if i % 25 == 0 or i == len(files):
                    set_progress(done=i)
                    check_stop()
        set_progress(phase="Reading camera EXIF", total=len(files), done=0)
        cams = cameras(files)
        todo = [p for p in files if hashes[p] and hashes[p] not in have]  # only files we might upload / upgrade
        set_progress(phase="Checking files decode", total=len(todo), done=0)
        broken = set()
        with ThreadPoolExecutor(max_workers=4) as ex:
            for i, (p, ok) in enumerate(zip(todo, ex.map(lambda f: True if state.get("cancel") else decodes(f), todo)), 1):
                if not ok:
                    broken.add(p)
                if i % 10 == 0 or i == len(todo):
                    set_progress(done=i)
                    check_stop()
        lib = any(under(root, l) or under(l, root) for l in LIBRARY_PATHS)
        items = []
        for p in files:
            h = hashes[p]
            try:
                st = os.stat(p)
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = 0, 0
            status = "unreadable" if h is None else ("sha1" if h in have else "missing")
            twins = names.get(os.path.basename(p).lower(), []) if status == "missing" else []
            if twins:
                status = "name"
            items.append({"path": p, "rel": os.path.relpath(p, root), "name": os.path.basename(p),
                          "size": size, "mtime": mtime, "status": status,
                          "twins": [{"id": i, "size": s} for i, s in twins],
                          "kind": "video" if Path(p).suffix.lower() in VIDEO else "image",
                          "camera": cams.get(p, ("", ""))[0],
                          "source": source_of(os.path.relpath(p, root), *cams.get(p, ("", ""))),
                          "damaged": p in broken,
                          "protected": any(under(p, l) for l in LIBRARY_PATHS)})
        with state_lock:
            known.clear()
            known.update({p: h for p, h in hashes.items() if h})
        set_progress(phase="Done" + (" - inside an immich library folder, delete disabled" if lib else ""),
                     items=items, finished=time.time())
    except Stopped:
        set_progress(phase="Stopped", error="Scan stopped", items=[])
    except Exception as e:
        traceback.print_exc()
        set_progress(phase="Failed", error=str(e))
    finally:
        set_progress(running=False)


# ---------- background jobs (copy / upload / upgrade / recycle / delete) ----------

job = {"kind": "", "total": 0, "done": 0, "running": False, "cancel": False, "log": [], "result": None}
LIVE = {"upload": ("status", "sha1"), "upgrade": ("status", "sha1"), "recycle": ("remove", None), "delete": ("remove", None)}


def job_iter(paths, res, kind):
    """Yield paths one by one; after each, log its outcome (from res) and apply it to the live item list.
    Stops early when the job is cancelled."""
    prev = None
    snap = (0, 0, 0)

    def settle(p):
        d, s, f = res["done"], len(res["skipped"]), len(res["failed"])
        if f > snap[2]:
            out = "failed: " + res["failed"][-1][1]
        elif s > snap[1]:
            out = "skipped: " + res["skipped"][-1][1]
        elif d > snap[0]:
            out = "done"
            how, st = LIVE.get(kind, (None, None))
            if how == "status":
                set_status({p}, st)
            elif how == "remove":
                remove_items({p})
        else:
            out = "done"
        with state_lock:
            job["log"].append([p, out])
            job["done"] += 1
        return d, s, f

    for p in paths:
        if prev is not None:
            snap = settle(prev)
        if job["cancel"]:
            prev = None
            res.setdefault("stopped", True)
            break
        prev = p
        yield p
    if prev is not None:
        settle(prev)


def run_job(kind, fn, *args):
    try:
        res = fn(*args)
    except Exception as e:
        traceback.print_exc()
        res = {"error": str(e)}
    with state_lock:
        job["result"] = res
        job["running"] = False


def set_status(paths, status):
    with state_lock:
        for it in state["items"]:
            if it["path"] in paths:
                it["status"] = status


def remove_items(paths):
    with state_lock:
        state["items"] = [it for it in state["items"] if it["path"] not in paths]
        for p in paths:
            known.pop(p, None)


# ---------- thumbnails ----------

VIEW_MAX = 8192  # browsers choke on very wide panoramas - /file serves a downscaled copy past this


def view_bytes(p):
    """Original bytes if a browser can show it, else a VIEW_MAX JPEG rendition (cached)."""
    try:
        with Image.open(p) as im:
            w, h = im.size
    except Exception:
        return None
    if max(w, h) <= VIEW_MAX:
        return None
    st = os.stat(p)
    key = hashlib.sha1(f"view|{p}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()
    cf = CACHE / key[:2] / (key + ".jpg")
    if cf.exists():
        return cf.read_bytes()
    im = Image.open(p)
    im.draft("RGB", (VIEW_MAX, VIEW_MAX))
    im = ImageOps.exif_transpose(im)
    im.thumbnail((VIEW_MAX, VIEW_MAX))
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    data = buf.getvalue()
    try:
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_bytes(data)
    except OSError:
        pass
    return data


PLACEHOLDER = ('<svg xmlns="http://www.w3.org/2000/svg" width="320" height="240" viewBox="0 0 320 240">'
               '<rect width="320" height="240" fill="#333"/><text x="160" y="128" font-family="sans-serif" '
               'font-size="22" fill="#aaa" text-anchor="middle">{}</text></svg>')


def thumb_bytes(p):
    st = os.stat(p)
    key = hashlib.sha1(f"{p}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()
    cf = CACHE / key[:2] / (key + ".jpg")
    if cf.exists():
        return cf.read_bytes(), "image/jpeg"
    ext = Path(p).suffix.lower()
    src = None
    if ext in VIDEO:
        for ss in ("1", "0"):
            r = subprocess.run(["ffmpeg", "-v", "error", "-ss", ss, "-i", p, "-frames:v", "1",
                                "-vf", f"scale={THUMB}:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                               capture_output=True, timeout=60)
            if r.returncode == 0 and len(r.stdout) > 500:
                src = io.BytesIO(r.stdout)
                break
    if src is None and (ext in RAW or ext in VIDEO):
        for tag in ("-PreviewImage", "-JpgFromRaw", "-ThumbnailImage"):
            r = subprocess.run(["exiftool", "-b", tag, p], capture_output=True)
            if r.returncode == 0 and len(r.stdout) > 1000:
                src = io.BytesIO(r.stdout)
                break
        if src is None:
            return PLACEHOLDER.format(ext.lstrip(".").upper()).encode(), "image/svg+xml"
    try:
        im = Image.open(src or p)
        im.draft("RGB", (THUMB * 2, THUMB * 2))  # JPEG: decode at reduced scale - fast for huge panos
        im = ImageOps.exif_transpose(im)
        im.thumbnail((THUMB, THUMB))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        data = buf.getvalue()
    except Exception as e:
        print(f"thumb failed: {p}: {type(e).__name__}: {e}", flush=True)
        return PLACEHOLDER.format("No preview").encode(), "image/svg+xml"
    try:
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_bytes(data)
    except OSError:
        pass
    return data, "image/jpeg"


# ---------- actions ----------

def copy_files(paths, dest, keep_tree, root, move=False):
    """Copy (or move) selected files under dest. Move never takes files out of an immich library folder."""
    dest = os.path.realpath(dest)
    if not root_of(dest):
        return {"error": f"Destination must be under {', '.join(ROOTS)}"}
    for b in BLOCKED_DEST:
        if under(dest, b):
            return {"error": f"Destination under {b} is blocked"}
    if under(dest, root):
        return {"error": "Destination is inside the scanned folder"}
    os.makedirs(dest, exist_ok=True)
    owner = os.stat(dest)
    res = {"done": 0, "skipped": [], "failed": []}
    gone = set()
    for p in job_iter(paths, res, "move" if move else "copy"):
        if p not in known:
            res["failed"].append([p, "not in current scan"])
            continue
        if move and any(under(p, l) for l in LIBRARY_PATHS):
            res["skipped"].append([p, "inside an immich library folder"])
            continue
        rel = os.path.relpath(p, root) if keep_tree else os.path.basename(p)
        target = os.path.join(dest, rel)
        if os.path.lexists(target):
            res["skipped"].append([p, "already exists at destination"])
            continue
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if move:
                try:
                    os.rename(p, target)  # same filesystem - instant, bytes untouched
                except OSError as e:
                    if e.errno != errno.EXDEV:
                        raise
                    shutil.copy2(p, target)
                    if os.path.getsize(target) != os.path.getsize(p):
                        os.remove(target)
                        raise OSError("size mismatch after copy - source kept")
                    os.remove(p)
                gone.add(p)
            else:
                shutil.copy2(p, target)
            os.chown(target, owner.st_uid, owner.st_gid)
            d = os.path.dirname(target)
            while under(d, dest) and d != dest:
                os.chown(d, owner.st_uid, owner.st_gid)
                d = os.path.dirname(d)
            res["done"] += 1
        except OSError as e:
            res["failed"].append([p, str(e)])
    remove_items(gone)
    return res


def album_assets(groups):
    """{album name: [asset ids]} -> add to existing albums of that name (own account) or create them."""
    code, existing = api("GET", "/api/albums")
    if code != 200:
        raise RuntimeError(f"list albums {code}: {existing}")
    names = {x["albumName"]: x["id"] for x in existing}
    hdr = {"Content-Type": "application/json"}
    made, added = 0, 0
    for name, ids in groups.items():
        if name in names:
            for i in range(0, len(ids), 500):
                code, r = api("PUT", f"/api/albums/{names[name]}/assets", json.dumps({"ids": ids[i:i + 500]}).encode(), hdr)
                if code != 200:
                    raise RuntimeError(f"album add {code}: {r}")
                added += sum(1 for x in r if x.get("success"))
        else:
            code, r = api("POST", "/api/albums", json.dumps({"albumName": name, "assetIds": ids}).encode(), hdr)
            if code not in (200, 201):
                raise RuntimeError(f"album create {code}: {r}")
            made += 1
            added += len(ids)
    return made, added


def upload_files(paths, descriptor, albums, root, delete_after=False):
    res = {"done": 0, "skipped": [], "failed": []}
    ok, created = set(), []
    groups = {}
    for p in job_iter(paths, res, "upload"):
        if p not in known:
            res["failed"].append([p, "not in current scan"])
            continue
        if not decodes(p):
            res["skipped"].append([p, "damaged - doesn't decode"])
            continue
        try:
            s, aid = upload(p)
            if albums and aid:
                rel = os.path.dirname(os.path.relpath(p, root))
                name = os.path.basename(root.rstrip("/")) + (f" / {rel.replace('/', ' / ')}" if rel else "")
                groups.setdefault(name, []).append(aid)
            if s == "duplicate":
                res["skipped"].append([p, "already in immich - not tagged"])
            else:
                res["done"] += 1
                if aid:
                    created.append(aid)
            ok.add(p)
            if delete_after:  # bytes are in immich now (created or duplicate)
                why = move_to_recycle(p)
                if why:
                    res.setdefault("kept", []).append([p, why])
                else:
                    res["deleted"] = res.get("deleted", 0) + 1
                    remove_items({p})
        except Exception as e:
            res["failed"].append([p, str(e)])
    set_status(ok, "sha1")
    if created:
        d = (descriptor or "").replace("/", "-").strip()
        res["tag"] = "src_photocopy/" + time.strftime("%Y-%m-%d") + (f" - {d}" if d else "")
        try:
            res["tagged"] = tag_assets(res["tag"], created)
        except Exception as e:
            res["failed"].append(["(tagging)", str(e)])
    if groups:
        try:
            made, added = album_assets(groups)
            res["albums"] = f"{added} added to {len(groups)} album(s), {made} new"
        except Exception as e:
            res["failed"].append(["(albums)", str(e)])
    return res


def upgrade_files(paths, descriptor, delete_after=False):
    """Same-name, different-bytes: upload this file, carry the old asset's links/tags/description
    across, then trash the old asset. Logged to CACHE/upgrades.jsonl (old id -> new id)."""
    res = {"done": 0, "skipped": [], "failed": []}
    with state_lock:
        by = {it["path"]: it for it in state["items"]}
    hdr = {"Content-Type": "application/json"}
    new_ids, ok = [], set()
    for p in job_iter(paths, res, "upgrade"):
        it = by.get(p)
        if not it or p not in known:
            res["failed"].append([p, "not in current scan"])
            continue
        if it.get("damaged") or not decodes(p):
            res["skipped"].append([p, "damaged - doesn't decode, would replace a good immich copy"])
            continue
        if it["status"] != "name" or len(it.get("twins", [])) != 1:
            res["skipped"].append([p, "needs exactly one same-name asset in immich"])
            continue
        old = it["twins"][0]["id"]
        try:
            code, oldj = api("GET", f"/api/assets/{old}")
            if code != 200:
                raise RuntimeError(f"read old asset {code}: {oldj.get('message', oldj)}")
            s, new = upload(p)
            if s == "duplicate" or not new:
                res["skipped"].append([p, "immich says these bytes are already there"])
                continue
            code, r = api("PUT", "/api/assets/copy", json.dumps({"sourceId": old, "targetId": new}).encode(), hdr)
            if code not in (200, 204):
                raise RuntimeError(f"copy links {code}: {r.get('message', r)}")
            tag_ids = [t["id"] for t in (oldj.get("tags") or [])]
            if tag_ids:
                code, r = api("PUT", "/api/tags/assets", json.dumps({"tagIds": tag_ids, "assetIds": [new]}).encode(), hdr)
                if code != 200:
                    raise RuntimeError(f"copy tags {code}: {r}")
            desc = ((oldj.get("exifInfo") or {}).get("description") or "").strip()
            if desc:
                code, r = api("PUT", f"/api/assets/{new}", json.dumps({"description": desc}).encode(), hdr)
                if code != 200:
                    raise RuntimeError(f"copy description {code}: {r}")
            code, r = api("DELETE", "/api/assets", json.dumps({"ids": [old], "force": False}).encode(), hdr)
            if code not in (200, 204):
                raise RuntimeError(f"trash old {code}: {r.get('message', r) if isinstance(r, dict) else r}")
            with open(CACHE / "upgrades.jsonl", "a") as f:
                f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "path": p, "old": old, "new": new,
                                    "tags": len(tag_ids), "description": bool(desc)}) + "\n")
            new_ids.append(new)
            ok.add(p)
            res["done"] += 1
            if delete_after:
                why = move_to_recycle(p)
                if why:
                    res.setdefault("kept", []).append([p, why])
                else:
                    res["deleted"] = res.get("deleted", 0) + 1
                    remove_items({p})
        except Exception as e:
            res["failed"].append([p, str(e)])
    set_status(ok, "sha1")
    if new_ids:
        d = (descriptor or "").replace("/", "-").strip()
        res["tag"] = "src_photocopy/" + time.strftime("%Y-%m-%d") + " - upgrade" + (f" - {d}" if d else "")
        try:
            res["tagged"] = tag_assets(res["tag"], new_ids)
            tag_assets("src_photocopy/upgraded", new_ids)  # one standing tag for every upgrade, all batches
            res["tag"] += '" + src_photocopy/upgraded'
        except Exception as e:
            res["failed"].append(["(tagging)", str(e)])
    return res


def move_to_recycle(p):
    """Move one source file to its share's #recycle. Returns None on success, else a reason."""
    if any(under(p, l) for l in LIBRARY_PATHS):
        return "inside an immich library folder"
    if not root_of(p) or os.path.relpath(p, root_of(p)).count("/") < 1:
        return "not inside a share"
    target = recycle_path(p)
    if os.path.lexists(target):
        base, ext = os.path.splitext(target)
        target = f"{base}.{int(time.time())}{ext}"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.rename(p, target)
        return None
    except OSError as e:
        return str(e)


def recycle_path(p):
    """/volume1/<share>/a/b.jpg -> /volume1/<share>/#recycle/a/b.jpg (UGOS share recycle bin)."""
    root = root_of(p)
    share, rest = os.path.relpath(p, root).split("/", 1)
    return os.path.join(root, share, "#recycle", rest)


def recycle_files(paths, verify=True, kind="recycle"):
    """Move files to their share's #recycle. verify=True: only if the exact bytes are in immich.
    verify=False ("Delete"): any selected file - still never inside an immich library folder."""
    res = {"done": 0, "skipped": [], "failed": []}
    have = set()
    if verify:
        try:
            owner, _ = account()
            have = immich_checksums(owner)
        except Exception as e:
            return {"error": str(e)}
    gone = set()
    for p in job_iter(paths, res, kind):
        if p not in known:
            res["failed"].append([p, "not in current scan"])
            continue
        if any(under(p, l) for l in LIBRARY_PATHS):
            res["skipped"].append([p, "inside an immich library folder"])
            continue
        if not root_of(p) or os.path.relpath(p, root_of(p)).count("/") < 1:
            res["skipped"].append([p, "not inside a share"])
            continue
        if verify:
            h = sha1(p)
            if h is None or h != known[p] or h not in have:
                res["skipped"].append([p, "bytes not confirmed in immich"])
                continue
        target = recycle_path(p)
        if os.path.lexists(target):
            base, ext = os.path.splitext(target)
            target = f"{base}.{int(time.time())}{ext}"
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.rename(p, target)
            res["done"] += 1
            gone.add(p)
        except OSError as e:
            res["failed"].append([p, str(e)])
    remove_items(gone)
    return res


# ---------- http ----------

PAGE_FILE = Path(__file__).parent / "index.html"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def handle(self):
        # the browser cancels thumbnail/file loads as you scroll - not an error worth logging
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if ctype.startswith(("application/json", "text/html", "image/svg")) else "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, p, ctype):
        """Stream a file in chunks (videos can be GBs) instead of reading it into memory."""
        size = os.path.getsize(p)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        with open(p, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            return self.send(200, PAGE_FILE.read_text(), "text/html; charset=utf-8")
        if u.path == "/api/job":
            since = int(q.get("since", ["0"])[0] or 0)
            with state_lock:
                j = {k: job[k] for k in ("kind", "total", "done", "running", "result")}
                j["log"] = job["log"][since:]
                j["next"] = len(job["log"])
            return self.send(200, j)
        if u.path == "/api/status":
            with state_lock:
                s = dict(state)
            if s["running"]:
                s["items"] = []
            ks = key_state.get(ACTIVE)
            if ACTIVE and (not ks or (not ks["ok"] and time.time() - ks["at"] > 30)):
                check_key(ACTIVE)  # e.g. Immich was still starting when PhotoCopy came up
                ks = key_state.get(ACTIVE)
            s["upload"] = can_upload()
            s["key"] = {"ok": bool(ks and ks["ok"]), "msg": ks["msg"] if ks else "no API key"}
            s["accounts"] = [{"name": n, "who": key_state.get(n, {}).get("msg", ""), "ok": key_state.get(n, {}).get("ok", False),
                              "upload": n not in NO_UPLOAD, "active": n == ACTIVE} for n in KEYS]
            return self.send(200, s)
        if u.path in ("/thumb", "/file"):
            p = q.get("p", [""])[0]
            with state_lock:
                ok = p in known
            if not ok or not os.path.isfile(p):
                return self.send(404, {"error": "unknown file"})
            if u.path == "/thumb":
                data, ct = thumb_bytes(p)
                return self.send(200, data, ct)
            ext = Path(p).suffix.lower()
            if ext in RAW:
                data, ct = thumb_bytes(p)
                return self.send(200, data, ct)
            if ext in IMAGE:
                v = view_bytes(p)
                if v is not None:
                    return self.send(200, v, "image/jpeg")
            ct = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
                  ".webp": "image/webp", ".mp4": "video/mp4", ".mov": "video/quicktime",
                  ".m4v": "video/mp4"}.get(ext, "application/octet-stream")
            return self.send_file(p, ct)
        if u.path == "/api/ls":
            p = q.get("p", [""])[0]
            p = os.path.realpath(p) if p else ""
            if not p or not root_of(p) or not os.path.isdir(p):
                if len(ROOTS) == 1:
                    p = ROOTS[0]
                else:  # top level: list the roots themselves
                    return self.send(200, {"path": "/", "parent": "", "top": True,
                                           "dirs": [r.lstrip("/") for r in ROOTS if os.path.isdir(r)]})
            try:
                dirs = sorted(d for d in os.listdir(p) if os.path.isdir(os.path.join(p, d))
                              and not d.startswith(".") and d not in SKIP_DIRS)
            except OSError:
                dirs = []
            parent = os.path.dirname(p) if p not in ROOTS else ("/" if len(ROOTS) > 1 else "")
            return self.send(200, {"path": p, "parent": parent,
                                   "dirs": dirs})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        u = urlparse(self.path)
        if u.path == "/api/account":
            global ACTIVE
            n = _name(body.get("name"))
            if not n:
                return self.send(400, {"error": "Unknown account"})
            with state_lock:
                if state["running"] or job["running"]:
                    return self.send(409, {"error": "Wait for the scan / job to finish"})
                ACTIVE = n
                state.update(items=[], account=key_state.get(n, {}).get("msg", ""), phase="idle", done=0, total=0)
            try:
                _active_file.write_text(n)
            except OSError:
                pass
            check_key(n)
            return self.send(200, {"ok": True})
        if u.path == "/api/mkdir":
            parent = os.path.realpath(body.get("parent", ""))
            name = (body.get("name") or "").strip()
            if not name or name in (".", "..") or "/" in name or name.startswith(".") or name == "#recycle":
                return self.send(400, {"error": "Not a valid folder name"})
            if not root_of(parent) or not os.path.isdir(parent):
                return self.send(400, {"error": f"Not a folder under {', '.join(ROOTS)}"})
            if parent in ROOTS:
                return self.send(400, {"error": "Make new folders inside a share, not at the top level"})
            for b in BLOCKED_DEST:
                if under(parent, b):
                    return self.send(400, {"error": f"Folders can't be created under {b}"})
            target = os.path.join(parent, name)
            if os.path.lexists(target):
                return self.send(409, {"error": "Already exists"})
            try:
                os.mkdir(target)
                st = os.stat(parent)
                os.chown(target, st.st_uid, st.st_gid)
            except OSError as e:
                return self.send(500, {"error": str(e)})
            return self.send(200, {"path": target})
        if u.path == "/api/scan":
            root = os.path.realpath(body.get("root", ""))
            if not root_of(root) or not os.path.isdir(root):
                return self.send(400, {"error": f"Not a folder under {', '.join(ROOTS)}: {root}"})
            with state_lock:
                if state["running"]:
                    return self.send(409, {"error": "A scan is already running"})
                if job["running"]:
                    return self.send(409, {"error": f"Wait for the {job['kind']} job to finish"})
                state["running"] = True
            ex = [e.strip() for e in body.get("excludes", []) if e.strip()]
            threading.Thread(target=scan, args=(root, ex), daemon=True).start()
            return self.send(200, {"ok": True})
        if u.path == "/api/stop":
            set_progress(cancel=True)
            with state_lock:
                job["cancel"] = True
            return self.send(200, {"ok": True})
        if u.path in ("/api/copy", "/api/move", "/api/upload", "/api/recycle", "/api/upgrade", "/api/delete"):
            with state_lock:
                root = state["root"]
                busy = state["running"]
            if busy or not root:
                return self.send(409, {"error": "No finished scan"})
            paths = body.get("paths", [])
            kind = u.path.rsplit("/", 1)[1]
            if kind in ("upload", "upgrade") and not can_upload():
                return self.send(400, {"error": f"Upload is off for {ACTIVE or 'this account'}"})
            if kind in ("copy", "move"):
                fn, args = copy_files, (paths, body.get("dest", ""), bool(body.get("keep_tree")), root, kind == "move")
            elif kind == "upgrade":
                fn, args = upgrade_files, (paths, body.get("descriptor", ""), bool(body.get("delete_after")))
            elif kind == "upload":
                fn, args = upload_files, (paths, body.get("descriptor", ""), bool(body.get("albums")), root, bool(body.get("delete_after")))
            elif kind == "delete":
                fn, args = recycle_files, (paths, False, "delete")
            else:
                fn, args = recycle_files, (paths, True, "recycle")
            with state_lock:
                if job["running"]:
                    return self.send(409, {"error": f"A {job['kind']} job is already running"})
                job.update(kind=kind, total=len(paths), done=0, running=True, cancel=False, log=[], result=None)
            threading.Thread(target=run_job, args=(kind, fn, *args), daemon=True).start()
            return self.send(200, {"started": True, "total": len(paths)})
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))  # PID 1 ignores SIGTERM by default -> exit 137 on stop
    CACHE.mkdir(parents=True, exist_ok=True)
    check_key()
    for n, st in key_state.items():
        print(f"Immich key {n}: {'OK - ' if st['ok'] else ''}{st['msg']}{' (no upload)' if n in NO_UPLOAD else ''}", flush=True)
    print(f"PhotoCopy on :{PORT}, roots {', '.join(ROOTS)}, active account {ACTIVE or 'none'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
