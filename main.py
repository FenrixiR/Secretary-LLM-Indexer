import os
import sys
import json
import re
import yaml
import hashlib
from collections import defaultdict, Counter
from datetime import datetime
from itertools import groupby

from parsers import CodeScanner, FLAG_TYPES
from verifier import run_sync_verification, find_sync_candidates, reconstruct_db_schema
from ollama_client import SecretaryAI

# ── Load config ───────────────────────────────────────────────────────────────

def load_config():
    """
    Load secretary.config.yml if present, then apply environment variable
    overrides. Environment variables always take precedence.
    """
    config = {
        'ai_model':          'qwen2.5-coder:7b',
        'ollama_host':       'http://localhost:11434',
        'num_gpu':           99,
        'num_ctx':           4096,
        'ws_server_files':   [],
        'ws_client_files':   [],
        'ignore_dirs':       [
            '.git', '.godot', '__pycache__', 'node_modules',
            'venv', 'build', 'dist'
        ],
        'ignore_extensions': ['.pyc', '.uid', '.import'],
    }

    config_path = os.path.join(os.path.dirname(__file__), 'secretary.config.yml')
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            file_cfg = yaml.safe_load(f) or {}
        config.update(file_cfg)

    # Environment variable overrides
    if os.getenv('AI_MODEL'):       config['ai_model']    = os.getenv('AI_MODEL')
    if os.getenv('OLLAMA_HOST'):    config['ollama_host'] = os.getenv('OLLAMA_HOST')

    return config

CFG = load_config()

# ── Paths (env vars override config) ─────────────────────────────────────────

PROJECT_ROOT  = os.getenv('PROJECT_ROOT',  '/project')
OUTPUT_DIR    = os.getenv('OUTPUT_DIR',    '/app/data/output')
MANIFEST_PATH = os.getenv('MANIFEST_PATH', '/app/data/manifest.json')

IGNORE_DIRS       = set(CFG['ignore_dirs'])
IGNORE_EXTENSIONS = set(CFG['ignore_extensions'])
CODE_EXTENSIONS   = {'.gd', '.py', '.js', '.ts'}
DATA_EXTENSIONS   = {'.json', '.csv'}
TSCN_EXTENSION    = '.tscn'

WS_SERVER_FILES = set(CFG.get('ws_server_files', []))
WS_CLIENT_FILES = set(CFG.get('ws_client_files', []))

# Bump when prompts change — automatically invalidates all cached summaries.
# Format: YYYYMMDD-N
PROMPT_VERSION = "20260315-1"

# ── Manifest ──────────────────────────────────────────────────────────────────

def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, 'r') as f:
            return json.load(f)
    return {}

def save_manifest(manifest):
    with open(MANIFEST_PATH, 'w') as f:
        json.dump(manifest, f, indent=4)

def file_hash(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        h.update(f.read())
    return h.hexdigest()

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_numbered_folder_pattern(dirname):
    m = re.match(r'^([a-zA-Z][a-zA-Z0-9_]*)_(\d+)$', dirname)
    if m:
        return m.group(1), f"{m.group(1)}_X"
    return None, None

def get_numbered_file_pattern(filename):
    m = re.match(r'^([a-zA-Z][a-zA-Z0-9_]*)_(\d+)(\.[^.]+)$', filename)
    if m:
        return f"{m.group(1)}_X{m.group(3)}"
    return None

def extract_json_schema(full_path):
    try:
        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
            raw = f.read(4000)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            for end in [raw.rfind('}'), raw.rfind(']')]:
                if end == -1: continue
                try:
                    data = json.loads(raw[:end+1]); break
                except: continue
            else:
                return "Complex/truncated JSON — could not parse schema", ""
        return describe_json_structure(data, 0, 3), describe_json_structure(data, 0, 1)
    except Exception as e:
        return f"Could not read: {e}", ""

def describe_json_structure(data, depth=0, max_depth=3):
    indent = "  " * depth
    if depth >= max_depth:
        return f"{indent}..."
    if isinstance(data, dict):
        lines = [f"{indent}{{"]
        for k, v in list(data.items())[:20]:
            lines.append(f'{indent}  "{k}": {describe_json_structure(v, depth+1, max_depth)}')
        if len(data) > 20:
            lines.append(f'{indent}  ... ({len(data)} keys total)')
        lines.append(f"{indent}}}")
        return "\n".join(lines)
    elif isinstance(data, list):
        if not data: return "[]"
        return f"[ ({len(data)} items)\n{describe_json_structure(data[0], depth+1, max_depth)}\n{indent}]"
    else:
        return type(data).__name__

def extract_csv_schema(full_path):
    try:
        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
            header = f.readline().strip()
        return f"Columns: {header}", header[:120]
    except Exception as e:
        return f"Could not read: {e}", ""

def compute_dep_hash(imports, project_root):
    h = hashlib.md5()
    for imp in sorted(imports):
        candidates = [imp] if '.' in os.path.basename(imp) else [
            imp + ext for ext in ('.gd', '.py', '.js', '.ts')
        ]
        for candidate in candidates:
            full = os.path.join(project_root, candidate)
            if os.path.exists(full):
                try: h.update(file_hash(full).encode())
                except: pass
                break
    return h.hexdigest()

# ── Main ──────────────────────────────────────────────────────────────────────

def run_secretary(session_end=False):
    scanner = CodeScanner()
    ai      = SecretaryAI(
        model   = CFG['ai_model'],
        host    = CFG['ollama_host'],
        num_gpu = CFG['num_gpu'],
        num_ctx = CFG['num_ctx'],
    )
    manifest = load_manifest()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── Output accumulators ──
    tree_lines     = []
    schema_entries = []
    devlog_entries = []
    dep_map        = {}
    rev_dep_map    = defaultdict(list)
    all_flags      = []
    all_code_files = set()
    all_constants  = {}
    ws_messages    = {}
    migration_ops  = []
    schema_hints   = {}

    seen_folder_patterns = {}
    seen_file_patterns   = {}

    print(f"🔍 Scanning: {PROJECT_ROOT}", flush=True)

    # ── Per-file code processor ───────────────────────────────────────────────

    def process_code_file(full_path, rel_path, file, file_indent, is_pattern):
        skeleton, imports, func_count = scanner.scan_file(full_path)

        flags = scanner.scan_flags(full_path)
        for fl in flags:
            fl['file'] = rel_path
        all_flags.extend(flags)

        if imports:
            dep_map[rel_path] = imports
            for imp in imports:
                rev_dep_map[imp].append(rel_path)

        constants = scanner.harvest_constants(full_path)
        if constants:
            all_constants[rel_path] = constants

        if file in WS_SERVER_FILES or file in WS_CLIENT_FILES:
            msgs = scanner.extract_ws_messages(full_path)
            if msgs:
                ws_messages[rel_path] = msgs

        if 'versions' in rel_path.replace('\\', '/').split('/') and rel_path.endswith('.py'):
            ops = scanner.parse_alembic_migration(full_path)
            migration_ops.append((rel_path, ops))

        try:
            f_hash    = file_hash(full_path)
            d_hash    = compute_dep_hash(imports, PROJECT_ROOT)
            cache_key = f"{PROMPT_VERSION}:{f_hash}:{d_hash}"
        except:
            cache_key = ""

        cached_key     = manifest.get(f"{rel_path}__cache_key")
        cached_summary = manifest.get(f"{rel_path}__summary", "(cached)")

        if cache_key and cached_key == cache_key:
            tree_lines.append(f"{file_indent}📝 {file} — {cached_summary}")
            return

        print(f"🧠 Summarizing {rel_path} ({func_count} funcs)...", flush=True)

        previous_summary = manifest.get(f"{rel_path}__summary", None)

        schema_hint = None
        rel_dir = os.path.dirname(rel_path)
        for hint_path, hint_val in schema_hints.items():
            if os.path.dirname(hint_path) == rel_dir:
                schema_hint = hint_val
                break

        summary, _ = ai.summarize_skeleton(
            rel_path, skeleton,
            func_count=func_count,
            schema_hint=schema_hint,
        )

        manifest[f"{rel_path}__cache_key"] = cache_key
        manifest[f"{rel_path}__summary"]   = summary
        save_manifest(manifest)

        devlog_entries.append({
            'file':     rel_path,
            'summary':  summary,
            'previous': previous_summary,
            'time':     datetime.now().strftime('%Y-%m-%d %H:%M'),
            'is_new':   previous_summary is None,
        })
        tree_lines.append(f"{file_indent}📝 {file} — {summary}")

    def process_tscn_file(full_path, rel_path, file, file_indent):
        tscn_data   = scanner.scan_tscn(full_path)
        det_summary = scanner.tscn_summary_line(tscn_data)
        try:
            cache_key = f"{PROMPT_VERSION}:tscn:{file_hash(full_path)}"
        except:
            cache_key = ""

        cached_key     = manifest.get(f"{rel_path}__cache_key")
        cached_summary = manifest.get(f"{rel_path}__summary", "")

        if cache_key and cached_key == cache_key and cached_summary:
            tree_lines.append(f"{file_indent}🎬 {file} — {cached_summary}")
            return

        manifest[f"{rel_path}__cache_key"] = cache_key
        manifest[f"{rel_path}__summary"]   = det_summary
        save_manifest(manifest)
        tree_lines.append(f"{file_indent}🎬 {file} — {det_summary}")

    # ── Walk ──────────────────────────────────────────────────────────────────

    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = sorted([d for d in dirs
                          if d not in IGNORE_DIRS and not d.startswith('.')])

        rel_root    = os.path.relpath(root, PROJECT_ROOT)
        depth       = 0 if rel_root == '.' else rel_root.count(os.sep) + 1
        indent      = "  " * depth
        file_indent = "  " * (depth + 1)
        folder_name = os.path.basename(root) if rel_root != '.' else os.path.basename(PROJECT_ROOT)

        prefix, folder_pattern_key = get_numbered_folder_pattern(folder_name)
        if folder_pattern_key:
            parent_rel = os.path.relpath(os.path.dirname(root), PROJECT_ROOT)
            global_key = os.path.join(parent_rel, folder_pattern_key)
            if global_key not in seen_folder_patterns:
                seen_folder_patterns[global_key] = {"count": 1}
                tree_lines.append(
                    f"{indent}📁 {folder_pattern_key}/"
                    f" (×N numbered folders — contents from first instance)"
                )
            else:
                seen_folder_patterns[global_key]["count"] += 1
                continue
        else:
            tree_lines.append(f"{indent}📁 {folder_name}/")

        sorted_files = sorted(files)
        code_pending = []

        for file in sorted_files:
            full_path = os.path.join(root, file)
            rel_path  = os.path.relpath(full_path, PROJECT_ROOT)
            ext       = os.path.splitext(file)[1].lower()

            if ext in IGNORE_EXTENSIONS:
                continue

            file_pattern_key = get_numbered_file_pattern(file)
            if file_pattern_key:
                parent_key = os.path.join(rel_root, file_pattern_key)
                if parent_key not in seen_file_patterns:
                    seen_file_patterns[parent_key] = {"count": 0, "ext": ext}
                seen_file_patterns[parent_key]["count"] += 1
                if seen_file_patterns[parent_key]["count"] > 1:
                    continue
                is_pattern = True
            else:
                is_pattern = False

            # Pass A — data files (build schema hints)
            if ext in DATA_EXTENSIONS:
                if ext == '.json':
                    full_schema, hint = extract_json_schema(full_path)
                    label = "JSON"
                else:
                    full_schema, hint = extract_csv_schema(full_path)
                    label = "CSV"

                display_name = os.path.join(rel_root, file_pattern_key) if is_pattern else rel_path
                count_note   = " (×N — schema from first instance)" if is_pattern else ""
                schema_hints[display_name] = hint
                hint_flat = hint.replace('\n', ' ').replace('  ', ' ').strip()
                inline    = f" — {hint_flat[:100]}" if hint_flat else ""
                tree_lines.append(f"{file_indent}📊 {os.path.basename(display_name)}{count_note}{inline}")
                schema_entries.append(
                    f"### `{display_name}`{count_note}\n- Type: {label}\n"
                    f"- Schema:\n```\n{full_schema}\n```\n"
                )

            elif ext == TSCN_EXTENSION:
                process_tscn_file(full_path, rel_path, file, file_indent)

            elif ext in CODE_EXTENSIONS:
                all_code_files.add(rel_path)
                code_pending.append((full_path, rel_path, file, is_pattern))

            else:
                tree_lines.append(f"{file_indent}📄 {file}")

        # Pass B — code files
        for full_path, rel_path, file, is_pattern in code_pending:
            process_code_file(full_path, rel_path, file, file_indent, is_pattern)

    # ── Post-walk analysis ────────────────────────────────────────────────────

    # Build reverse dep map
    for src, imports in dep_map.items():
        for imp in imports:
            imp_clean  = imp.lstrip('/')
            candidates = [imp_clean] if '.' in os.path.basename(imp_clean) else [
                imp_clean + ext for ext in CODE_EXTENSIONS
            ]
            for code_file in all_code_files:
                norm = code_file.replace('\\', '/')
                for candidate in candidates:
                    if norm.endswith(candidate.replace('\\', '/')):
                        if src not in rev_dep_map[code_file]:
                            rev_dep_map[code_file].append(src)
                        break

    # Dead file detection — source 1: import targets
    referenced = set()
    for imports in dep_map.values():
        for imp in imports:
            imp_clean  = imp.lstrip('/')
            candidates = [imp_clean] if '.' in os.path.basename(imp_clean) else [
                imp_clean + ext for ext in CODE_EXTENSIONS
            ]
            for code_file in all_code_files:
                norm = code_file.replace('\\', '/')
                for candidate in candidates:
                    if norm.endswith(candidate.replace('\\', '/')):
                        referenced.add(code_file)
                        break

    # Dead file detection — source 2: tscn scene node attachments
    def extract_tscn_script_refs(tscn_path):
        refs = []
        try:
            with open(tscn_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    m = re.search(r'type="Script"[^>]*path="res://([^"]+)"', line)
                    if not m:
                        m = re.search(r'path="res://([^"]+\.gd)"', line)
                    if m:
                        refs.append(m.group(1))
        except Exception:
            pass
        return refs

    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith('.')]
        for file in files:
            if file.endswith('.tscn'):
                tscn_path   = os.path.join(root, file)
                tscn_rel    = os.path.relpath(tscn_path, PROJECT_ROOT)
                client_root = tscn_rel.split(os.sep)[0]
                for script_path in extract_tscn_script_refs(tscn_path):
                    full_ref = os.path.join(client_root, script_path).replace('\\', '/')
                    for code_file in all_code_files:
                        if code_file.replace('\\', '/') == full_ref:
                            referenced.add(code_file)
                            break

    # Dead file detection — source 3: files that import things are alive
    referenced.update(dep_map.keys())

    # Directories that are legitimately unreferenced by design
    EXEMPT_DIRS = {'tools', 'alembic', 'versions', 'autoloads'}
    unreferenced = sorted([
        f for f in all_code_files
        if f not in referenced
        and not any(part in EXEMPT_DIRS for part in f.replace('\\', '/').split('/'))
    ])

    sync_flags      = [f for f in all_flags if f['flag'] == 'SYNC']
    sync_results    = run_sync_verification(sync_flags, PROJECT_ROOT)
    sync_candidates = find_sync_candidates(all_constants)
    db_schema       = reconstruct_db_schema(migration_ops)

    server_msgs = set()
    client_msgs = set()
    for rel_path, msgs in ws_messages.items():
        fname = os.path.basename(rel_path)
        if fname in WS_SERVER_FILES:  server_msgs.update(msgs)
        elif fname in WS_CLIENT_FILES: client_msgs.update(msgs)

    # ── Write INDEX.md ────────────────────────────────────────────────────────

    with open(os.path.join(OUTPUT_DIR, "INDEX.md"), "w") as f:
        f.write(f"# Project Tree\n_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n")
        f.write("```\n")
        f.write("\n".join(tree_lines))
        f.write("\n```\n")

        if dep_map or rev_dep_map:
            f.write("\n\n## Dependency Map\n\n")
            f.write("| File | Imports | Used By |\n|---|---|---|\n")
            all_mentioned = set(dep_map.keys()) | set(rev_dep_map.keys())
            for src in sorted(all_mentioned):
                imports = ", ".join(f"`{d}`" for d in dep_map.get(src, []))
                used_by = ", ".join(f"`{d}`" for d in rev_dep_map.get(src, []))
                if imports or used_by:
                    f.write(f"| `{src}` | {imports or '—'} | {used_by or '—'} |\n")

    # ── Write ISSUES.md ───────────────────────────────────────────────────────

    with open(os.path.join(OUTPUT_DIR, "ISSUES.md"), "w") as f:
        f.write(f"# Issues & Flags\n_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n")

        if all_flags:
            sorted_flags = sorted(all_flags, key=lambda x: (x['severity'], x['file'], x['line']))
            for (severity, label), group in groupby(sorted_flags, key=lambda x: (x['severity'], x['label'])):
                entries = list(group)
                f.write(f"## {label} ({len(entries)})\n\n")
                if label.startswith('🤖'):
                    for e in entries:
                        f.write(f"- `{e['file']}` line {e['line']}")
                        if e['message']: f.write(f" — {e['message']}")
                        f.write("\n")
                else:
                    f.write("| File | Line | Note |\n|---|---|---|\n")
                    for e in entries:
                        f.write(f"| `{e['file']}` | {e['line']} | {e['message'] or '—'} |\n")
                f.write("\n")
            f.write("---\n\n## Summary\n\n| Type | Count |\n|---|---|\n")
            counts = Counter(fl['flag'] for fl in all_flags)
            for flag_key, (label, _) in sorted(FLAG_TYPES.items(), key=lambda x: x[1][1]):
                if flag_key in counts:
                    f.write(f"| {label} | {counts[flag_key]} |\n")
            f.write(f"| **Total** | **{len(all_flags)}** |\n")
        else:
            f.write("_No flags found._\n")

        f.write("\n---\n\n## 🗑️ Unreferenced Files\n\n")
        f.write("_Files with no known imports. May be dead code, autoloads, or entry points._\n\n")
        if unreferenced:
            f.write("| File | Note |\n|---|---|\n")
            for uf in unreferenced:
                parts = uf.replace('\\', '/').split('/')
                if 'autoloads' in parts:               note = "Autoload — globally available"
                elif parts[-1] in ('main.py', 'main.gd'): note = "Entry point"
                else:                                  note = "⚠️ Verify not dead code"
                f.write(f"| `{uf}` | {note} |\n")
        else:
            f.write("_None found._\n")

        f.write("\n---\n\n## 🔵 Sync Contracts\n\n")
        if not sync_results:
            f.write("_No `# SYNC` annotations found. Add them to constants that must match across files._\n")
        else:
            drift = [r for r in sync_results if r['status'] == 'drift']
            ok    = [r for r in sync_results if r['status'] == 'ok']
            unres = [r for r in sync_results if r['status'] == 'unresolved']
            if drift:
                f.write("### ⚠️ Drift Detected\n\n")
                for r in drift:
                    f.write(f"**`{r['constant']}`** — {r['note']}\n\n")
                    for p in r['parties']:
                        f.write(f"- `{p['file']}`: `{p['value']}`\n")
                    f.write("\n")
            if ok:
                f.write("### ✅ In Sync\n\n| Constant | Files | Value |\n|---|---|---|\n")
                for r in ok:
                    files = ", ".join(f"`{p['file']}`" for p in r['parties'])
                    value = r['parties'][0]['value'][:80] if r['parties'] else '—'
                    f.write(f"| `{r['constant']}` | {files} | `{value}` |\n")
                f.write("\n")
            if unres:
                f.write("### ❓ Unresolved\n\n| Constant | Note |\n|---|---|\n")
                for r in unres:
                    f.write(f"| `{r['constant']}` | {r['note']} |\n")

        if sync_candidates:
            f.write("\n---\n\n## 🔍 SYNC Candidates\n\n")
            f.write("_Constants appearing in multiple files — consider `# SYNC` annotations._\n\n")
            f.write("| Constant | Status | Files |\n|---|---|---|\n")
            for c in sync_candidates:
                files  = ", ".join(f"`{e['file']}`" for e in c['files'])
                status = "✅ identical" if c['match'] else "⚠️ differ"
                f.write(f"| `{c['constant']}` | {status} | {files} |\n")

    # ── Write SCHEMA.md ───────────────────────────────────────────────────────

    parsed_schemas = [
        e for e in schema_entries
        if 'Complex/truncated' not in e and 'Could not read' not in e
    ]

    with open(os.path.join(OUTPUT_DIR, "SCHEMA.md"), "w") as f:
        f.write(f"# Data & Schema Reference\n_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n")

        if db_schema:
            f.write("## Database Schema\n\n")
            f.write("_Reconstructed from Alembic migrations. May not reflect manual changes._\n\n")
            for table, cols in sorted(db_schema.items()):
                f.write(f"### `{table}`\n\n")
                if cols:
                    f.write("| Column | Type |\n|---|---|\n")
                    for col, col_type in sorted(cols.items()):
                        f.write(f"| `{col}` | {col_type} |\n")
                else:
                    f.write("_No columns tracked_\n")
                f.write("\n")

        if parsed_schemas:
            f.write("## JSON / CSV Schemas\n\n")
            f.write("_Only successfully parsed files shown. Large files appear inline in INDEX.md._\n\n")
            f.write("\n".join(parsed_schemas))

        claude_flags = [fl for fl in all_flags if fl['flag'] == 'CLAUDE']
        if claude_flags:
            f.write("\n\n---\n\n## 🤖 CLAUDE Notes\n\n")
            f.write("_Direct annotations from the codebase written for AI context._\n\n")
            for fl in sorted(claude_flags, key=lambda x: (x['file'], x['line'])):
                f.write(f"- **`{fl['file']}`** line {fl['line']}")
                if fl['message']: f.write(f" — {fl['message']}")
                f.write("\n")

    # ── Write PROTOCOL.md ─────────────────────────────────────────────────────

    with open(os.path.join(OUTPUT_DIR, "PROTOCOL.md"), "w") as f:
        f.write(f"# WebSocket Message Protocol\n_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n")
        f.write("_Extracted from source files. Manual review recommended._\n\n")
        all_types = server_msgs | client_msgs
        if not all_types:
            f.write("_No WebSocket message types detected._\n\n")
            if WS_SERVER_FILES or WS_CLIENT_FILES:
                f.write(f"_Searched in: {', '.join(WS_SERVER_FILES | WS_CLIENT_FILES)}_\n")
            else:
                f.write("_No WS scan targets configured. Set `ws_server_files` and `ws_client_files` in secretary.config.yml._\n")
        else:
            f.write("| Message Type | Server | Client | Notes |\n|---|---|---|---|\n")
            for msg in sorted(all_types):
                s = "✅ sends/handles" if msg in server_msgs else "—"
                c = "✅ sends/handles" if msg in client_msgs else "—"
                f.write(f"| `{msg}` | {s} | {c} | |\n")
        f.write("\n\n## Source Files Scanned\n\n")
        for rel_path in sorted(ws_messages.keys()):
            f.write(f"- `{rel_path}`: {len(ws_messages[rel_path])} message types found\n")

    # ── Write CHANGELOG.md ────────────────────────────────────────────────────

    if devlog_entries:
        run_time  = datetime.now().strftime('%Y-%m-%d %H:%M')
        new_files = [e for e in devlog_entries if e['is_new']]
        changed   = [e for e in devlog_entries if not e['is_new']]
        with open(os.path.join(OUTPUT_DIR, "CHANGELOG.md"), "a") as f:
            total = len(devlog_entries)
            f.write(f"\n---\n\n## Run: {run_time} — {total} file{'s' if total != 1 else ''} changed\n\n")
            if new_files:
                f.write(f"### 🆕 New Files ({len(new_files)})\n\n| File | Summary |\n|---|---|\n")
                for e in new_files:
                    f.write(f"| `{e['file']}` | {e['summary']} |\n")
                f.write("\n")
            if changed:
                f.write(f"### ✏️ Changed Files ({len(changed)})\n\n| File | Before | After |\n|---|---|---|\n")
                for e in changed:
                    before = (e['previous'] or '—')[:120]
                    after  = e['summary'][:120]
                    f.write(f"| `{e['file']}` | {before} | {after} |\n")
                f.write("\n")

    # ── Session-end mode ──────────────────────────────────────────────────────

    if session_end:
        _write_session_end_template(devlog_entries, all_flags)

    print(f"✅ Done. Output in {OUTPUT_DIR}", flush=True)
    print(f"   Files: INDEX.md, ISSUES.md, SCHEMA.md, PROTOCOL.md, CHANGELOG.md", flush=True)


def _write_session_end_template(devlog_entries, all_flags):
    """Write a pre-filled session summary template."""
    today         = datetime.now().strftime('%Y-%m-%d')
    changed_files = [e['file'] for e in devlog_entries]
    new_flags     = [f for f in all_flags if f['flag'] in ('FIXME', 'STUB', 'TODO')]

    template = f"""# Session Summary — {today}

## Files Changed This Session
{chr(10).join(f'- `{f}`' for f in changed_files) if changed_files else '- (none detected)'}

## Key Outcomes
- 

## Decisions Made
- 

## New Known Issues
{chr(10).join(f"- `{f['file']}` line {f['line']}: {f['message']}" for f in new_flags) if new_flags else '- (none flagged)'}

## Next Session
- 
"""
    path = os.path.join(OUTPUT_DIR, f"SESSION_{today}.md")
    with open(path, 'w') as f:
        f.write(template)
    print(f"📋 Session template written: {path}", flush=True)


if __name__ == "__main__":
    session_end = "--session-end" in sys.argv
    run_secretary(session_end=session_end)
