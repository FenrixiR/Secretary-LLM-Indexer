import re
import os

# ── Flag convention ───────────────────────────────────────────────────────────
# Add these comments anywhere in your code to surface issues in ISSUES.md.
# Both "# FLAG message" and "# FLAG: message" formats are accepted.
# Uppercase only — lowercase won't trigger the scanner.
#
# Examples:
#   # FIXME calculation is wrong for edge case X
#   # STUB replace with real implementation when Y is designed
#   # SYNC other_file.py CONSTANT_NAME
#   # DEBT this works but needs rework before launch
#   # TODO add validation here
#   # CLAUDE this runs before the seeding step — order matters

FLAG_TYPES = {
    # key: (display_label, severity_rank)  lower rank = shown first in ISSUES.md
    'FIXME':  ('🔴 FIXME',  1),
    'STUB':   ('🟡 STUB',   2),
    'SYNC':   ('🔵 SYNC',   3),
    'DEBT':   ('🟠 DEBT',   4),
    'TODO':   ('⬜ TODO',   5),
    'CLAUDE': ('🤖 CLAUDE', 6),
}

# ── WebSocket message dispatch patterns ───────────────────────────────────────
# Matches "type" field values in WS message dicts and dispatch blocks.
# Works for Python servers and GDScript clients.
_WS_PATTERNS = [
    # "type": "message_name"  — dict literal (Python and GDScript outgoing sends)
    re.compile(r'"type"\s*:\s*"([a-z][a-z0-9_]+)"'),
    # Python: data["type"] == "x"  or  payload["type"] == "x"
    re.compile(r'(?:data|payload|msg|message)\[.type.\]\s*==\s*"([a-z][a-z0-9_]+)"'),
    # GDScript: msg.type == "x"  or  data.type == "x"
    re.compile(r'(?:msg|data|message|payload)\.type\s*==\s*"([a-z][a-z0-9_]+)"'),
    # Python/GDScript: .get("type") == "x"
    re.compile(r'\.get\s*\(\s*"type"\s*\)\s*==\s*"([a-z][a-z0-9_]+)"'),
    # GDScript match arms: "message_name": inside a match type block
    re.compile(r'^\s+"([a-z][a-z0-9_]+)"\s*:', re.MULTILINE),
]

# GDScript match-arm context detector
# Only extract string arms that appear inside a match block on a type variable
_GD_MATCH_TYPE_BLOCK = re.compile(
    r'match\s+\w+(?:_type|\.type|\.get\s*\("type"\))[^\n]*\n((?:.*\n)*?)(?=\nfunc|\Z)',
    re.MULTILINE
)

# Alembic migration operation patterns
_ALEMBIC_CREATE     = re.compile(r'op\.create_table\(\s*["\'](\w+)["\']')
_ALEMBIC_DROP       = re.compile(r'op\.drop_table\(\s*["\'](\w+)["\']')
_ALEMBIC_ADD_COL    = re.compile(
    r'op\.add_column\(\s*["\'](\w+)["\'],\s*sa\.Column\(\s*["\'](\w+)["\'],\s*sa\.([\w\(\), ]+)'
)
_ALEMBIC_DROP_COL   = re.compile(r'op\.drop_column\(\s*["\'](\w+)["\'],\s*["\'](\w+)["\']')
_ALEMBIC_ALTER      = re.compile(r'op\.alter_column\(\s*["\'](\w+)["\'],\s*["\'](\w+)["\']')
_ALEMBIC_INLINE_COL = re.compile(r"sa\.Column\(\s*['\"](\w+)['\"]\s*,\s*sa\.([\w]+)")


class CodeScanner:

    # ── Public API ────────────────────────────────────────────────────────────

    def scan_file(self, file_path):
        """Return (skeleton: str, imports: list[str], func_count: int)."""
        ext = file_path.rsplit('.', 1)[-1].lower()
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except Exception as e:
            return f"# Could not read: {e}", [], 0

        if ext == 'gd':
            return (self.scan_gdscript(content),
                    self.extract_imports_gdscript(content),
                    self.count_functions_gdscript(content))
        elif ext == 'py':
            return (self.scan_python(content),
                    self.extract_imports_python(content),
                    self.count_functions_python(content))
        elif ext in ('js', 'ts'):
            return (self.scan_js(content),
                    self.extract_imports_js(content),
                    self.count_functions_js(content))
        else:
            return content[:800], [], 0

    def scan_flags(self, file_path):
        """
        Return list of flag annotation dicts for all FLAG_TYPES found in file.
        Uppercase only. Both '# FLAG msg' and '# FLAG: msg' accepted.
        """
        results = []
        flag_re = re.compile(
            r'(?:#|//)\s*(' + '|'.join(FLAG_TYPES.keys()) + r')\b\s*(.*)'
        )
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line_no, line in enumerate(f, start=1):
                    m = flag_re.search(line)
                    if m:
                        keyword = m.group(1).upper()
                        message = m.group(2).strip().lstrip(':').strip()
                        label, severity = FLAG_TYPES[keyword]
                        results.append({
                            'flag':     keyword,
                            'label':    label,
                            'severity': severity,
                            'line':     line_no,
                            'message':  message,
                            'file':     file_path,
                        })
        except Exception:
            pass
        return results

    def scan_tscn(self, file_path):
        """
        Parse a Godot .tscn file. Returns structural summary dict or None.
        """
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except Exception:
            return None

        result = {
            'root_type':   'Unknown',
            'root_name':   '',
            'script':      None,
            'child_count': 0,
            'node_types':  [],
            'script_refs': [],
        }
        node_types = []
        root_found = False

        for line in content.split('\n'):
            node_m = re.match(r'\[node\s+name="([^"]+)"(?:[^]]*type="([^"]+)")?([^]]*)\]', line)
            if node_m:
                name      = node_m.group(1)
                node_type = node_m.group(2) or ''
                rest      = node_m.group(3)
                is_root   = 'parent' not in rest
                if is_root and not root_found:
                    result['root_name'] = name
                    result['root_type'] = node_type or 'Node'
                    root_found = True
                else:
                    result['child_count'] += 1
                if node_type:
                    node_types.append(node_type)

            script_m = re.search(r'type="Script"[^\]]*path="res://([^"]+)"', line)
            if not script_m:
                script_m = re.search(r'path="res://([^"]+\.gd)"', line)
            if script_m:
                path = script_m.group(1)
                if path not in result['script_refs']:
                    result['script_refs'].append(path)
                if result['script'] is None:
                    result['script'] = path

        result['node_types'] = sorted(set(node_types))
        return result

    def tscn_summary_line(self, tscn_data):
        """Build a deterministic one-line description from scan_tscn output."""
        if not tscn_data:
            return "Scene file"
        root   = tscn_data['root_type']
        kids   = tscn_data['child_count']
        script = os.path.basename(tscn_data['script']) if tscn_data['script'] else None
        NOTABLE = {
            'CharacterBody3D', 'RigidBody3D', 'StaticBody3D', 'Area3D',
            'MeshInstance3D', 'CollisionShape3D', 'Camera3D',
            'AnimationPlayer', 'AudioStreamPlayer3D', 'GPUParticles3D',
            'NavigationAgent3D', 'PathFollow3D', 'MultiMeshInstance3D',
            'SubViewport', 'Control', 'CanvasLayer',
        }
        notable = [t for t in tscn_data['node_types'] if t in NOTABLE][:3]
        parts   = [f"{root} root"]
        if kids:    parts.append(f"{kids} children")
        if notable: parts.append(", ".join(notable))
        if script:  parts.append(f"→ {script}")
        return " · ".join(parts)

    def extract_ws_messages(self, file_path):
        """
        Extract WebSocket message type strings from a file.
        Handles Python server dispatch and GDScript client match blocks.
        Returns sorted list of unique message type names.
        """
        # Generic field names that appear in message payloads but are not message types
        FIELD_NOISE = {
            'type', 'error', 'ok', 'true', 'false', 'null', 'none',
            'id', 'uid', 'name', 'data', 'status', 'message', 'result',
            'code', 'detail', 'state',
        }
        found = set()
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except Exception:
            return []

        ext = file_path.rsplit('.', 1)[-1].lower()

        if ext == 'gd':
            # Apply non-arm patterns to full content
            for pattern in _WS_PATTERNS[:-1]:
                for m in pattern.finditer(content):
                    if m.lastindex and m.lastindex >= 1:
                        msg = m.group(1)
                        if len(msg) > 2 and msg not in FIELD_NOISE:
                            found.add(msg)
            # Apply arm pattern only within match type blocks
            for block_m in _GD_MATCH_TYPE_BLOCK.finditer(content):
                for m in _WS_PATTERNS[-1].finditer(block_m.group(1)):
                    msg = m.group(1)
                    if len(msg) > 2 and msg not in FIELD_NOISE:
                        found.add(msg)
        else:
            for pattern in _WS_PATTERNS[:-1]:
                for m in pattern.finditer(content):
                    if m.lastindex and m.lastindex >= 1:
                        msg = m.group(1)
                        if len(msg) > 2 and msg not in FIELD_NOISE:
                            found.add(msg)

        return sorted(found)

    def harvest_constants(self, file_path):
        """
        Extract UPPER_CASE named constants from Python and GDScript files.
        Returns {name: value_str}. Used for SYNC candidate detection.
        """
        constants = {}
        ext = file_path.rsplit('.', 1)[-1].lower()
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except Exception:
            return {}

        if ext == 'py':
            for m in re.finditer(r'^([A-Z][A-Z0-9_]{2,})\s*=\s*(.+)$', content, re.MULTILINE):
                val = re.sub(r'\s*#.*$', '', m.group(2)).strip()
                constants[m.group(1)] = val
        elif ext == 'gd':
            for m in re.finditer(
                r'^(?:const|var)\s+([A-Z][A-Z0-9_]{2,})\s*(?:[:=])[^=]\s*(.+)$',
                content, re.MULTILINE
            ):
                val = re.sub(r'\s*#.*$', '', m.group(2)).strip()
                constants[m.group(1)] = val

        return constants

    def parse_alembic_migration(self, file_path):
        """
        Parse a single Alembic migration file and return schema operations.
        Returns dict with keys: creates, drops, adds, removes, alters.
        """
        ops = {'creates': [], 'drops': [], 'adds': [], 'removes': [], 'alters': []}
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            upgrade_m = re.search(r'def upgrade\(\)[^{]*?:(.*?)(?=\ndef |\Z)', content, re.DOTALL)
            if not upgrade_m:
                return ops
            body = upgrade_m.group(1)

            # Parse create_table blocks including inline column definitions
            create_block_re = re.compile(
                r"op\.create_table\(\s*['\"](\w+)['\"](.+?)(?=op\.|\Z)", re.DOTALL
            )
            for block_m in create_block_re.finditer(body):
                table_name = block_m.group(1)
                ops['creates'].append((table_name,))
                for col_m in _ALEMBIC_INLINE_COL.finditer(block_m.group(2)):
                    col_name = col_m.group(1)
                    if not col_name.startswith('fk_'):
                        ops['adds'].append((table_name, col_name, col_m.group(2)))

            for m in _ALEMBIC_DROP.finditer(body):
                ops['drops'].append((m.group(1),))
            for m in _ALEMBIC_ADD_COL.finditer(body):
                ops['adds'].append((m.group(1), m.group(2), m.group(3).split('(')[0].strip()))
            for m in _ALEMBIC_DROP_COL.finditer(body):
                ops['removes'].append((m.group(1), m.group(2)))
            for m in _ALEMBIC_ALTER.finditer(body):
                ops['alters'].append((m.group(1), m.group(2)))

        except Exception:
            pass
        return ops

    # ── Skeleton extractors ───────────────────────────────────────────────────

    def scan_gdscript(self, content):
        """Extract structural skeleton of a GDScript file."""
        lines    = content.split('\n')
        skeleton = []
        in_func  = False
        func_indent = 0

        # Extract header docblock fenced by # ===...=== lines
        docblock   = []
        in_docblock = False
        for line in lines[:40]:
            stripped = line.strip()
            if re.match(r'^#\s*={4,}', stripped):
                in_docblock = not in_docblock
                continue
            if in_docblock and stripped.startswith('#'):
                text = re.sub(r'^#\s*', '', stripped).strip()
                if text:
                    docblock.append(text)
            elif not in_docblock and docblock:
                break
        if docblock:
            doc_text = ' | '.join(docblock[:3])[:120]
            skeleton.append('# DOC: ' + doc_text)

        for line in lines:
            stripped = line.strip()
            if re.match(r'^func\s+\w+', stripped):
                skeleton.append(line.rstrip())
                in_func     = True
                func_indent = len(line) - len(line.lstrip())
                continue
            if in_func:
                current_indent = len(line) - len(line.lstrip()) if line.strip() else func_indent + 1
                if line.strip() == '' or current_indent > func_indent:
                    continue
                else:
                    in_func = False
            if stripped.startswith((
                'extends ', 'class_name ', 'signal ', 'const ',
                '@export', '@onready', 'var ', 'enum '
            )):
                skeleton.append(line.rstrip())

        return "\n".join(skeleton) if skeleton else content[:800]

    def scan_python(self, content):
        """Extract structural skeleton of a Python file."""
        lines             = content.split('\n')
        skeleton          = []
        skip_until_indent = None

        for line in lines:
            stripped = line.strip()
            if skip_until_indent is not None:
                current_indent = len(line) - len(line.lstrip()) if stripped else skip_until_indent + 1
                if stripped == '' or current_indent > skip_until_indent:
                    continue
                else:
                    skip_until_indent = None
            if stripped.startswith(('import ', 'from ')):
                skeleton.append(line.rstrip())
            elif stripped.startswith('@'):
                skeleton.append(line.rstrip())
            elif re.match(r'^(class|def|async def)\s+\w+', stripped):
                skeleton.append(line.rstrip())
                skip_until_indent = len(line) - len(line.lstrip())

        return "\n".join(skeleton) if skeleton else content[:800]

    def scan_js(self, content):
        """Extract structural skeleton of a JS/TS file."""
        lines    = content.split('\n')
        skeleton = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(('import ', 'export ', 'const ', 'let ', 'var ',
                                     'function ', 'class ', 'async function')):
                skeleton.append(line.rstrip())
            elif re.match(r'^\w[\w\s]*\(.*\)\s*[:{]', stripped):
                skeleton.append(line.rstrip())
        return "\n".join(skeleton) if skeleton else content[:800]

    # ── Import extractors ─────────────────────────────────────────────────────

    def extract_imports_gdscript(self, content):
        """Return local script paths from preload/load calls."""
        deps = []
        for m in re.finditer(r'(?:preload|load)\s*\(\s*["\']res://([^"\']+)["\']', content):
            path = m.group(1)
            if not path.startswith('addons/'):
                deps.append(path)
        return sorted(set(deps))

    def extract_imports_python(self, content):
        """Return local module imports, filtering out stdlib and common third-party."""
        SKIP = {
            '__future__', '__annotations__',
            'os', 'sys', 're', 'json', 'math', 'time', 'datetime', 'pathlib',
            'typing', 'collections', 'itertools', 'functools', 'hashlib',
            'logging', 'traceback', 'abc', 'copy', 'enum', 'dataclasses',
            'asyncio', 'uuid', 'random', 'string', 'io', 'struct',
            'contextlib', 'subprocess', 'shutil', 'tempfile', 'socket',
            'fastapi', 'sqlalchemy', 'pydantic', 'pydantic_settings',
            'alembic', 'uvicorn', 'starlette', 'jose', 'passlib', 'dotenv',
            'aiohttp', 'requests', 'numpy', 'pandas', 'pytest',
        }
        deps = []
        for line in content.split('\n'):
            stripped = line.strip()
            m = re.match(r'^from\s+([\w.]+)\s+import', stripped)
            if m:
                module = m.group(1)
                if module.split('.')[0] not in SKIP:
                    deps.append(module.replace('.', '/'))
                continue
            m = re.match(r'^import\s+([\w.]+)', stripped)
            if m:
                module = m.group(1)
                if module.split('.')[0] not in SKIP:
                    deps.append(module.replace('.', '/'))
        return sorted(set(deps))

    def extract_imports_js(self, content):
        """Return local relative imports from JS/TS import statements."""
        deps = []
        for m in re.finditer(r'import\s+.*?\s+from\s+["\'](\.[^"\']+)["\']', content):
            deps.append(m.group(1))
        return sorted(set(deps))

    # ── Complexity counters ───────────────────────────────────────────────────

    def count_functions_gdscript(self, content):
        return len(re.findall(r'^\s*func\s+\w+', content, re.MULTILINE))

    def count_functions_python(self, content):
        return len(re.findall(r'^\s*(?:async\s+)?def\s+\w+', content, re.MULTILINE))

    def count_functions_js(self, content):
        return len(re.findall(
            r'(?:function\s+\w+|=>\s*\{|^\s*\w+\s*\(.*\)\s*\{)',
            content, re.MULTILINE
        ))
