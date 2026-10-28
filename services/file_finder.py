import json
import re
import shutil
from collections import defaultdict, deque
from pathlib import Path

from tree_sitter_language_pack import get_parser

# ───────────────────────── config ─────────────────────────
SKIP = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", "vendor", "target"}
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "e2e"}

LANGS = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
         ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx", ".java": "java",
         ".go": "go", ".rb": "ruby", ".php": "php"}
JS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}

# Named groups: method, path, handler (optional), handler2 (optional), recv (optional)
ROUTE_PATTERNS = [
    # Express / Koa / Fastify / Hono: app.get('/x', ...). Handlers are read from the call's arguments.
    (JS, r"\b(?P<recv>\w+)\.(?P<method>get|post|put|delete|patch|all)\(\s*['\"`](?P<path>/[^'\"`]*)['\"`]"),
    # NestJS
    ({".ts"}, r"@(?P<method>Get|Post|Put|Delete|Patch)\(\s*['\"`]?(?P<path>[^'\"`)]*)['\"`]?\s*\)\s*"
              r"(?:async\s+)?(?P<handler>\w+)\s*\("),
    # Flask / FastAPI decorators (handles stacked decorators)
    ({".py"}, r"@\w+(?:\.\w+)*\.(?P<method>get|post|put|delete|patch|route|websocket)\(\s*['\"](?P<path>[^'\"]*)['\"]"
              r"[^)]*\)\s*(?:@[^\n]*\n\s*)*(?:async\s+)?def\s+(?P<handler>\w+)"),
    # Django urls
    ({".py"}, r"\b(?P<method>path|re_path|url)\(\s*r?['\"](?P<path>[^'\"]*)['\"]\s*,\s*(?P<handler>[\w.]+)"),
    # Spring
    ({".java"}, r"@(?P<method>Get|Post|Put|Delete|Patch|Request)Mapping(?:\(\s*(?:value\s*=\s*)?[\"'](?P<path>[^\"']*)[\"'][^)]*\))?"
                r"\s*(?:@\w+(?:\([^)]*\))?\s*)*public\s+[\w<>\[\], ?]+\s+(?P<handler>\w+)\s*\("),
    # Go: gin / echo / chi / net/http
    ({".go"}, r"\b\w+\.(?P<method>GET|POST|PUT|DELETE|PATCH|Get|Post|Put|Delete|Patch|HandleFunc|Handle)\(\s*\"(?P<path>[^\"]+)\"\s*,\s*(?P<handler>[\w.]+)?"),
    # Rails
    ({".rb"}, r"^\s*(?P<method>get|post|put|patch|delete)\s+['\"](?P<path>[^'\"]+)['\"].*?to:\s*['\"](?P<handler>[\w/#]+)['\"]"),
    # Laravel
    ({".php"}, r"Route::(?P<method>get|post|put|delete|patch)\(\s*['\"](?P<path>[^'\"]+)['\"]\s*,\s*"
               r"(?:\[[\w:\\]+,\s*['\"](?P<handler>\w+)['\"]\]|['\"][\w\\]+@(?P<handler2>\w+)['\"])"),
]
ROUTE_PATTERNS = [(e, re.compile(p, re.M)) for e, p in ROUTE_PATTERNS]

# receivers that are HTTP clients / maps, not route registrars
HTTP_CLIENTS = {"axios", "http", "https", "request", "superagent", "fetch", "client", "api",
                "map", "headers", "params", "cache", "redis"}

DEF_TYPES = {"function_definition", "function_declaration", "method_definition",
             "method_declaration", "method"}
CALL_TYPES = {"call", "call_expression", "method_invocation", "function_call_expression",
              "member_call_expression", "scoped_call_expression"}
FUNC_VALUES = {"arrow_function", "function_expression", "function"}
HANDLER_ARG_TYPES = {"identifier", "member_expression", "attribute", "selector_expression"}


# ───────────────────────── helpers ─────────────────────────
def is_test(f: Path) -> bool:
    n = f.name.lower()
    return (any(p.lower() in TEST_DIRS for p in f.parts)
            or ".test." in n or ".spec." in n or n.startswith("test_") or n.endswith("_test.go"))


def last_ident(text):
    ids = re.findall(r"[A-Za-z_]\w*", text)
    return ids[-1] if ids else None


def walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def def_name(n):
    if n.type in DEF_TYPES:
        nm = n.child_by_field_name("name")
        return nm.text.decode() if nm else None
    if n.type == "variable_declarator":  # const handler = () => {}
        v = n.child_by_field_name("value")
        nm = n.child_by_field_name("name")
        if v is not None and v.type in FUNC_VALUES and nm is not None:
            return nm.text.decode()


def callees(node):
    out = set()
    for n in walk(node):
        if n.type in CALL_TYPES:
            for field in ("name", "method", "function"):
                t = n.child_by_field_name(field)
                if t is not None:
                    name = last_ident(t.text.decode(errors="ignore"))
                    if name:
                        out.add(name)
                    break
    return out


# ───────────────────────── indexing ─────────────────────────
def build_index(root):
    funcs = defaultdict(list)  # name -> [(file, node)]
    trees, sources = {}, {}
    for f in Path(root).rglob("*"):
        if f.suffix not in LANGS or any(p in SKIP for p in f.parts) or is_test(f):
            continue
        try:
            src = f.read_bytes()
            tree = get_parser(LANGS[f.suffix]).parse(src)
        except Exception:
            continue
        trees[f], sources[f] = tree, src
        for n in walk(tree.root_node):
            name = def_name(n)
            if name:
                funcs[name].append((f, n))
    return funcs, trees, sources


def find_routes(trees, sources):
    """Yield (file, line, method, path, handler_names, seed_node).

    handler_names: named handlers/middleware found (regex group + identifier arguments)
    seed_node: the call's arguments node, scanned for inline handlers (or None)
    """
    for f, tree in trees.items():
        text = sources[f].decode(errors="ignore")
        for exts, rx in ROUTE_PATTERNS:
            if f.suffix not in exts:
                continue
            for m in rx.finditer(text):
                gd = m.groupdict()
                if gd.get("recv") and gd["recv"].lower() in HTTP_CLIENTS:
                    continue

                handlers = set()
                raw = gd.get("handler") or gd.get("handler2")
                if raw:
                    h = last_ident(re.split(r"[#@]", raw)[-1])
                    if h:
                        handlers.add(h)

                byte = len(text[:m.start()].encode())
                n = tree.root_node.descendant_for_byte_range(byte, byte)
                while n is not None and n.type not in CALL_TYPES:
                    n = n.parent

                seed = None
                if n is not None:
                    seed = n.child_by_field_name("arguments")
                    if seed is not None:
                        for a in seed.named_children:
                            if a.type in HANDLER_ARG_TYPES:
                                h = last_ident(a.text.decode(errors="ignore"))
                                if h:
                                    handlers.add(h)

                yield (f, text.count("\n", 0, m.start()) + 1,
                       (gd["method"] or "?").upper(), gd.get("path") or "?",
                       handlers, seed)


# ───────────────────────── analysis ─────────────────────────
def find_api_files(root="codebase", max_depth=6):
    funcs, trees, sources = build_index(root)
    direct = defaultdict(set)    # file -> {"GET /x", "handler foo"}
    indirect = defaultdict(set)  # file -> {function names reached from handlers}

    for f, line, method, path, handlers, seed in find_routes(trees, sources):
        direct[f].add(f"{method} {path}")

        seeds = []
        for h in handlers:
            for hf, hn in funcs.get(h, []):
                direct[hf].add(f"handler {h}")
                seeds.append((hf, hn))
        if seed is not None:  # inline handlers and middleware calls
            seeds.append((f, seed))

        seen = {(sf, sn.start_byte) for sf, sn in seeds}
        queue = deque((sf, sn, 0) for sf, sn in seeds)
        while queue:
            cur_file, node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for name in callees(node):
                defs = funcs.get(name)
                if not defs:
                    continue
                # prefer a definition in the same file; otherwise accept all candidates
                for cf, cn in ([d for d in defs if d[0] == cur_file] or defs):
                    key = (cf, cn.start_byte)
                    if key in seen:
                        continue
                    seen.add(key)
                    indirect[cf].add(name)
                    queue.append((cf, cn, depth + 1))

    return {
        "direct": {str(f): sorted(r) for f, r in sorted(direct.items())},
        "indirect": {str(f): sorted(fn) for f, fn in sorted(indirect.items())
                     if f not in direct},
    }


# ───────────────────────── export ─────────────────────────
def export_target_files(root="codebase", out="target_files", max_depth=6, clean=True):
    root_p, out_p = Path(root), Path(out)
    res = find_api_files(root, max_depth)

    if clean and out_p.exists():
        shutil.rmtree(out_p)  # avoid stale files from a previous run
    out_p.mkdir(parents=True, exist_ok=True)

    manifest = {}
    for kind in ("direct", "indirect"):
        for file_str, reasons in res[kind].items():
            src = Path(file_str)
            rel = src.relative_to(root_p)  # keep structure so same-named files don't collide
            dest = out_p / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            manifest[str(rel)] = {"type": kind, "reasons": reasons}

    (out_p / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest