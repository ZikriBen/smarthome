#!/usr/bin/env bash
# READ-ONLY subtitle audit for the Telegram Streams library (tg-stream PoC).
# Run from any host that can ssh to the NUC:
#   ssh ben@<TAILSCALE_IP> 'bash -s' < 08-subs-audit.sh
# Optional:  ssh ben@<nuc> 'JF_USER=ben LOG_SINCE=72h bash -s' < 08-subs-audit.sh
#
# What it does (GET requests and file listings only; changes nothing):
#   1. user prefs of JF_USER (default "ben"): SubtitleLanguagePreference,
#      SubtitleMode, AudioLanguagePreference, PlayDefaultAudioTrack,
#      RememberSubtitleSelections, RememberAudioSelections
#   2. the Telegram Streams library options that affect subtitles, then every
#      item: title, Hebrew text sub yes/no (embedded/external), each subtitle
#      stream (index/lang/codec/external/default/forced), and the per-user
#      DefaultSubtitleStreamIndex Jellyfin computes for that user
#   3. sidecars (*.srt etc.) under /home/ben/media/streams with cue counts and
#      last cue time, plus the per-folder .tgstream.json "subs" bookkeeping
#   4. Jellyfin's own extracted-subtitle cache (cue count / last cue: spots the
#      truncated-extraction bug), and the [subs]/[watch]/StreamError lines of
#      telegram-browser's log (signed /s/<id>/<sig> paths redacted)
# The Jellyfin key is read INSIDE the telegram-browser container from
# JELLYFIN_API_KEY_FILE / JELLYFIN_API_KEY (by variable name), is only sent as
# "Authorization: MediaBrowser Token=...", and is never printed. Item media
# source paths (which, for .strm items, are signed stream URLs) are not printed;
# every output line is also passed through a redaction filter.
set -uo pipefail
JF_USER="${JF_USER:-ben}"
STREAMS_HOST="${STREAMS_HOST:-/home/ben/media/streams}"
LOG_SINCE="${LOG_SINCE:-48h}"
TB_CONTAINER="${TB_CONTAINER:-telegram-browser}"
JF_CONTAINER="${JF_CONTAINER:-jellyfin}"

redact() {
  sed -E 's#(/s/[0-9]+/)[A-Za-z0-9_=-]{8,}#\1<sig>#g;
          s#(\b(api_key|ApiKey|X-Emby-Token|token|sig)=)[^&" ]+#\1<redacted>#gI;
          s#(MediaBrowser Token=")[^"]*#\1<redacted>#g'
}
hr() { printf '\n===== %s =====\n' "$*"; }
# scratch file for the item-id -> name/runtime map (local temp, removed on exit)
APIOUT="$(mktemp)"
trap 'rm -f "$APIOUT"' EXIT

hr "host / containers"
date -u '+now: %Y-%m-%dT%H:%M:%SZ'
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' </dev/null 2>&1 | grep -E "^($TB_CONTAINER|$JF_CONTAINER)\b" || echo "(docker ps: containers not found)"

if docker inspect -f '{{.State.Running}}' "$TB_CONTAINER" </dev/null 2>/dev/null | grep -q true; then
hr "Jellyfin API (via $TB_CONTAINER, GET only)"
docker exec -i -e JF_USER="$JF_USER" "$TB_CONTAINER" python - <<'PY' 2>&1 | redact | tee "$APIOUT" | grep -v '^CACHEMAP '
import json, os, urllib.error, urllib.parse, urllib.request
from pathlib import Path

def secret():
    p = os.environ.get("JELLYFIN_API_KEY_FILE", "")
    if p:
        try:
            v = Path(p).read_text().strip()
            if v:
                return v
        except OSError:
            pass
    return os.environ.get("JELLYFIN_API_KEY", "").strip() or None

KEY = secret()
BASE = os.environ.get("JELLYFIN_URL", "http://jellyfin:8096").rstrip("/")
LIBPATH = os.environ.get("JELLYFIN_STREAMS_PATH", "/media/telegram-streams").rstrip("/")
LIBID_ENV = os.environ.get("JELLYFIN_STREAMS_LIBRARY_ID", "").strip()
if not KEY:
    raise SystemExit("no Jellyfin key in the container (JELLYFIN_API_KEY_FILE / JELLYFIN_API_KEY unset)")

def get(path, **q):
    url = BASE + path + ("?" + urllib.parse.urlencode(q) if q else "")
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": 'MediaBrowser Token="%s"' % KEY, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise SystemExit("GET %s -> HTTP %s" % (path, e.code))
    except OSError as e:
        raise SystemExit("GET %s -> %s" % (path, type(e).__name__))

def clean(s):  # never echo anything key-shaped back
    s = "" if s is None else str(s)
    return s.replace(KEY, "<redacted>")

info = get("/System/Info/Public") or {}
print("server: %s version=%s" % (clean(info.get("ServerName")), clean(info.get("Version"))))

# ---- 1. user prefs
users = get("/Users") or []
want = os.environ.get("JF_USER", "ben").lower()
user = next((u for u in users if str(u.get("Name", "")).lower() == want), None)
print("\n-- 1. user prefs")
if not user:
    print("user %r not found; users: %s" % (want, ", ".join(clean(u.get("Name")) for u in users)))
    uid = None
else:
    uid = user["Id"]
    c = user.get("Configuration") or {}
    for k in ("SubtitleLanguagePreference", "SubtitleMode", "AudioLanguagePreference",
              "PlayDefaultAudioTrack", "RememberSubtitleSelections", "RememberAudioSelections"):
        print("  %-28s %r" % (k, c.get(k)))
    others = [(clean(u.get("Name")), (u.get("Configuration") or {}).get("SubtitleLanguagePreference"),
               (u.get("Configuration") or {}).get("SubtitleMode")) for u in users if u is not user]
    if others:
        print("  other users (name, SubtitleLanguagePreference, SubtitleMode): %s" % others)

# ---- 2. library + items
folders = get("/Library/VirtualFolders") or []
lib = None
for f in folders:
    locs = [str(x).rstrip("/") for x in (f.get("Locations") or [])]
    if (LIBID_ENV and f.get("ItemId") == LIBID_ENV) or LIBPATH in locs or f.get("Name") == "Telegram Streams":
        lib = f
        break
print("\n-- 2. library")
if not lib:
    raise SystemExit("Telegram Streams library not found (path %s)" % LIBPATH)
lo = lib.get("LibraryOptions") or {}
print("  name=%s ItemId=%s type=%s locations=%s" % (clean(lib.get("Name")), lib.get("ItemId"),
      lib.get("CollectionType"), lib.get("Locations")))
for k in ("AllowEmbeddedSubtitles", "EnableRealtimeMonitor", "SubtitleDownloadLanguages",
          "SaveSubtitlesWithMedia", "RequirePerfectSubtitleMatch", "SkipSubtitlesIfEmbeddedSubtitlesPresent",
          "SkipSubtitlesIfAudioTrackMatches", "PreferredMetadataLanguage"):
    if k in lo:
        print("  %-40s %r" % (k, lo.get(k)))

HEB = {"heb", "he", "hebrew", "iw"}
TEXT = {"subrip", "srt", "ass", "ssa", "webvtt", "vtt", "mov_text", "text", "ttml", "smi", "sami"}
q = dict(ParentId=lib["ItemId"], Recursive="true", IncludeItemTypes="Movie,Episode,Video",
         Fields="MediaSources,MediaStreams,Path,DateCreated,RunTimeTicks", SortBy="DateCreated", SortOrder="Descending", Limit="1000")
if uid:
    q["userId"] = uid
items = (get("/Items", **q) or {}).get("Items") or []
print("\n-- items in library: %d (newest first; DefaultSubtitleStreamIndex is computed for user %r)" % (len(items), want))
summary = {"hebrew_text": 0, "hebrew_default": 0, "no_hebrew": 0}
for it in items:
    srcs = it.get("MediaSources") or [{}]
    streams = it.get("MediaStreams") or srcs[0].get("MediaStreams") or []
    subs = [s for s in streams if s.get("Type") == "Subtitle"]
    def is_heb(s):
        t = " ".join(str(s.get(k) or "") for k in ("Language", "Title", "DisplayTitle")).lower()
        return str(s.get("Language") or "").lower() in HEB or "hebrew" in t or "עברית" in t
    heb = [s for s in subs if is_heb(s) and str(s.get("Codec") or "").lower() in TEXT]
    where = sorted({"external" if s.get("IsExternal") else "embedded" for s in heb})
    path = str(it.get("Path") or "")
    print("\n* %s (%s)  id=%s  file=%s" % (clean(it.get("Name")), it.get("ProductionYear"), it.get("Id"),
          clean(path.rsplit("/", 2)[-2] + "/" + path.rsplit("/", 1)[-1]) if "/" in path else clean(path)))
    print("  hebrew text sub: %s" % (("yes: " + "+".join(where)) if heb else "NO"))
    for si, src in enumerate(srcs):
        print("  source[%d] DefaultSubtitleStreamIndex=%r DefaultAudioStreamIndex=%r container=%r" % (
              si, src.get("DefaultSubtitleStreamIndex"), src.get("DefaultAudioStreamIndex"), src.get("Container")))
    if not subs:
        print("  (no subtitle streams probed%s)" % ("" if streams else "; no MediaStreams at all: item not probed yet"))
    for s in subs:
        p = str(s.get("Path") or "")
        print("    #%-3s lang=%-5s codec=%-8s external=%-5s default=%-5s forced=%-5s title=%r%s" % (
              s.get("Index"), s.get("Language"), s.get("Codec"), s.get("IsExternal"), s.get("IsDefault"),
              s.get("IsForced"), clean(s.get("DisplayTitle") or s.get("Title")),
              ("  file=" + clean(p.rsplit("/", 1)[-1])) if s.get("IsExternal") and p else ""))
    iid = str(it.get("Id") or "")
    if len(iid) == 32:  # Jellyfin caches extractions under <id[:2]>/<id as GUID>/<stream index>.<ext>
        g = "%s-%s-%s-%s-%s" % (iid[:8], iid[8:12], iid[12:16], iid[16:20], iid[20:])
        print("CACHEMAP %s %d %s" % (g, int((it.get("RunTimeTicks") or 0) / 10**7), clean(it.get("Name")).replace(" ", "_")))
    if heb:
        summary["hebrew_text"] += 1
        d = srcs[0].get("DefaultSubtitleStreamIndex")
        if any(s.get("Index") == d for s in heb):
            summary["hebrew_default"] += 1
        else:
            print("  !! Hebrew text sub exists but the per-user default subtitle index is %r" % (d,))
            if d == -1:
                ud = it.get("UserData") or {}
                print("     -1 = a REMEMBERED 'Off' for this user (RememberSubtitleSelections; Jellyfin 12.1 never computes -1"
                      " when an external sub exists). LastPlayedDate=%s. Cleared by playing once with"
                      " RememberSubtitleSelections off, or by picking Hebrew in the player once." % ud.get("LastPlayedDate"))
            elif any(s.get("Index") == d and not s.get("IsExternal") for s in subs) and any(s.get("IsExternal") for s in heb):
                print("     embedded track chosen over the external sidecar: a remembered selection, or Jellyfin's directory"
                      " cache hid the sidecar (restart Jellyfin / wait 5 min idle)")
    else:
        summary["no_hebrew"] += 1
print("\n-- summary: %s" % summary)
PY
else
  echo "($TB_CONTAINER is not running: skipping the Jellyfin API part)"
fi

hr "3. sidecars under $STREAMS_HOST"
if [[ -d "$STREAMS_HOST" ]]; then
  find "$STREAMS_HOST" -type f \( -iname '*.srt' -o -iname '*.ass' -o -iname '*.ssa' -o -iname '*.vtt' -o -iname '*.sub' \) \
       -printf '%TY-%Tm-%Td %TH:%TM  %8s B  %P\n' </dev/null 2>/dev/null | sort -r | while IFS= read -r line; do
    f="$STREAMS_HOST/${line#*B  }"
    cues="$(grep -c -- '-->' "$f" 2>/dev/null || echo 0)"
    last="$(grep -- '-->' "$f" 2>/dev/null | tail -1 | awk '{print $3}')"
    note=""
    if [[ "$f" == *.heb.forced.srt && -n "$last" ]]; then
      secs=$(( 10#${last:0:2} * 3600 + 10#${last:3:2} * 60 + 10#${last:6:2} ))
      if (( secs > 0 && cues * 600 >= 15 * secs )); then
        note="  !! a FULL track named .forced (round 1 trusted the MKV flag): round 2 renames it to .heb.default.srt at startup"
      fi
    fi
    printf '%s  cues=%s last=%s%s\n' "$line" "$cues" "${last:-?}" "$note"
  done
  n_strm="$(find "$STREAMS_HOST" -type f -name '*.strm' </dev/null 2>/dev/null | wc -l)"
  n_srt="$(find "$STREAMS_HOST" -type f -iname '*.srt' </dev/null 2>/dev/null | wc -l)"
  echo "totals: .strm=$n_strm  .srt=$n_srt"
  echo "-- .tgstream.json subs bookkeeping (folder: variant -> sidecar names)"
  find "$STREAMS_HOST" -mindepth 2 -maxdepth 2 -name .tgstream.json </dev/null 2>/dev/null | sort | while IFS= read -r m; do
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("  %s: variants=%s subs=%s" % (sys.argv[1].split("/")[-2], sorted(d.get("variants",{})), d.get("subs", "-")))' "$m" 2>/dev/null \
      || echo "  $(basename "$(dirname "$m")"): (unreadable marker)"
  done
else
  echo "($STREAMS_HOST not found)"
fi

hr "4a. Jellyfin extracted-subtitle cache (Telegram Streams items flagged)"
# Jellyfin 12.1 never invalidates this cache for .strm items (the source is a
# URL, so IsCachedSubtitleFresh is always true) and accepts a partial ffmpeg
# output: a truncated extraction is served forever. Embedded stream indexes
# also shift when sidecars appear (externals are inserted at index 0), so an
# old <index>.srt can belong to another track. Nothing is deleted here.
docker exec "$JF_CONTAINER" sh -c '
  for d in /config/data/subtitles /cache/subtitles /config/cache/subtitles; do
    [ -d "$d" ] || continue
    echo "DIR $d $(find "$d" -type f | wc -l)"
    find "$d" -type f -printf "%T@ %p\n" 2>/dev/null | sort -rn | head -400 | while read -r t f; do
      c=$(grep -c -- "-->" "$f" 2>/dev/null || echo 0)
      l=$(grep -- "-->" "$f" 2>/dev/null | tail -1 | cut -d" " -f3)
      echo "F ${t%.*} $c ${l:-?} ${f#"$d"/}"
    done
  done' </dev/null 2>/dev/null | redact | python3 -c '
import sys, time
cmap = {}
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    if line.startswith("CACHEMAP "):
        _, g, rt, name = line.rstrip("\n").split(" ", 3)
        cmap[g.lower()] = (int(rt), name.replace("_", " "))
def secs(ts):
    try:
        h, m, s = ts.replace(",", ".").split(":"); return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return None
base, shown, flagged, ours = None, 0, [], set()
for line in sys.stdin:
    p = line.rstrip("\n").split(" ", 4)
    if p[0] == "DIR":
        base = p[1]; print("dir: %s (%s files)" % (base, p[2])); continue
    if p[0] != "F" or len(p) < 5:
        continue
    t, cues, last, rel = int(p[1]), p[2], p[3], p[4]
    parts = rel.split("/")
    g = parts[1].lower() if len(parts) >= 3 else ""
    item = cmap.get(g)
    if item is None and shown >= 40:
        continue
    rt, name = item if item else (0, "")
    tag = ""
    if item:
        ours.add((base, "/".join(parts[:2])))
        ls = secs(last)
        if rt and (ls is None or ls < 0.8 * rt):
            tag = "  !! last cue at %s of %s: TRUNCATED? (or subs simply end early)" % (
                last, time.strftime("%H:%M:%S", time.gmtime(rt)))
            flagged.append((base, "/".join(parts[:2]), name))
    shown += 1
    print("  %s  cues=%-5s last=%-12s %s%s%s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)), cues, last, rel,
          ("  [Telegram Streams: %s]" % name) if item else "", tag))
print("Telegram Streams items with cached extractions: %d; flagged: %d" % (len(ours), len(flagged)))
if ours:
    print()
    print("CLEANUP (NOT run; needs approval). Safe: Jellyfin re-extracts on demand, and with")
    print("the external <stem>.heb.default.srt the Hebrew track is not extracted at all.")
    print("Flagged only:")
    for b, d, name in flagged or [("", "", "")]:
        if d:
            print("  docker exec %s rm -rf %s/%s   # %s" % (sys.argv[2], b, d, name))
        else:
            print("  (none)")
    print("All Telegram Streams items (recommended once after the deploy, since old indexes may be shifted):")
    for b, d in sorted(ours):
        print("  docker exec %s rm -rf %s/%s" % (sys.argv[2], b, d))
' "$APIOUT" "$JF_CONTAINER" || echo "(could not read the cache)"

hr "4b. $TB_CONTAINER log since $LOG_SINCE: [subs] [watch] tg-stream errors"
docker logs --since "$LOG_SINCE" "$TB_CONTAINER" </dev/null 2>&1 \
  | grep -E '\[subs\]|\[watch\]|tg-stream|StreamError|Traceback|cancelled' | tail -200 | redact
echo
echo "audit done (read-only; nothing was changed)"
