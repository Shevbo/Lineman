"""Shared HTTP daemon: loads the Searchers once, serves rag_search over loopback HTTP.

Usage:  python -m ragkit.httpd configs/stl.toml [configs/federation.toml ...] [port]
Only ever binds 127.0.0.1 — never exposed off-box. ragkit/server.py is the stdio client,
run locally or over `ssh smain '...'`, both talking to this same loopback port.

Profiles (added 2026-09-12). Every config on the command line becomes a named profile
(its `project_name`), and a request may pick one with {"profile": "fed"}. The first
config stays the default, so existing callers are unaffected.

Why profiles and not a second daemon: Searcher applies `secondary_penalty` to every index
after the first, so under configs/stl.toml the federation canon is deliberately demoted in
favour of STL's own code. That is right for a dev window and exactly wrong for a federation
agent asking about the federation — it got STL chunks back. A second process would cost
another resident copy of the models; a second Searcher costs only its vectors
(federation.db is ~2k chunks), because embed._MODELS caches the weights.
"""
import json
import os
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .search import from_config

DEFAULT_PORT = 8791
_lock = threading.Lock()


def _stamp(searcher):
    """(size, mtime) of every index file this searcher has open."""
    out = {}
    for idx in searcher.indexes:
        try:
            st = os.stat(idx.path)
            out[idx.path] = (st.st_size, st.st_mtime)
        except OSError:
            out[idx.path] = None
    return out


def _profile_name(config_path):
    """project_name from the config, falling back to the file's basename."""
    try:
        with open(config_path, "rb") as fh:
            return tomllib.load(fh).get("project_name") or ""
    except (OSError, ValueError):
        return ""


class Reloader:
    """Holds the Searcher and rebuilds it after the reindex timer rewrites an index.

    The searcher keeps every chunk vector in memory but reads chunk text from sqlite on
    demand, so once the incremental indexer deletes and reinserts the chunks of a changed
    file the two halves disagree: the in-memory ids point at rows that no longer exist.
    That is not staleness, it is a broken searcher — the reindex runs every 15 minutes,
    so a daemon that never reloads is dead within the hour. Reloading costs a few seconds
    (the models stay cached in embed._MODELS; only the vectors are re-read).
    """

    def __init__(self, config_path):
        self.config_path = config_path
        self.searcher = from_config(config_path)
        self.stamp = _stamp(self.searcher)

    def current(self):
        fresh = _stamp(self.searcher)
        if fresh != self.stamp:
            print("ragkit httpd: index changed on disk, reloading", file=sys.stderr)
            self.searcher = from_config(self.config_path)
            self.stamp = _stamp(self.searcher)
        return self.searcher


def make_handler(reloaders, default_profile):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # stdlib default logs every request to stderr; the journal doesn't need it

        def _send(self, status, obj):
            payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path != "/profiles":
                self.send_response(404)
                self.end_headers()
                return
            self._send(200, {"profiles": sorted(reloaders), "default": default_profile})

        def do_POST(self):
            if self.path != "/search":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            query = (body.get("query") or "").strip()
            depth = body.get("depth") or "snippet"
            profile = body.get("profile") or default_profile

            if profile not in reloaders:
                self._send(400, {"text": "unknown profile %r; have %s"
                                         % (profile, sorted(reloaders))})
                return

            # ponytail: serialize — concurrent-call safety of the ONNX embed/rerank sessions
            # under real parallel load is unmeasured; cheap to queue at STL's human-paced
            # query rate. Upgrade to a session pool if a queue shows up under load.
            with _lock:
                if query:
                    text = reloaders[profile].current().search(query, depth)
                else:
                    text = "rag_search: 'query' is required."
            self._send(200, {"text": text, "profile": profile})
    return Handler


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)

    args = sys.argv[1:]
    port = DEFAULT_PORT
    if args and args[-1].isdigit():
        port = int(args[-1])
        args = args[:-1]
    if not args:
        sys.exit(__doc__)

    print("ragkit httpd: loading models...", file=sys.stderr)
    reloaders = {}
    default_profile = None
    for cfg_path in args:
        name = _profile_name(cfg_path) or os.path.basename(cfg_path).split(".")[0]
        reloaders[name] = Reloader(cfg_path)
        if default_profile is None:
            default_profile = name
        print("ragkit httpd: profile %s <- %s" % (name, cfg_path), file=sys.stderr)

    print("ragkit httpd: ready on 127.0.0.1:%d (default profile %s)"
          % (port, default_profile), file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", port),
                        make_handler(reloaders, default_profile)).serve_forever()


if __name__ == "__main__":
    main()
