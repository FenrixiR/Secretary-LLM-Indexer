import re
import os
from collections import defaultdict


def _extract_value(file_path, constant_name):
    """
    Find the definition of `constant_name` in a file and return its
    normalised value string. Returns None if not found.
    """
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception:
        return None

    pattern = re.compile(
        r'(?:const|var)?\s*' + re.escape(constant_name) + r'\s*(?:[:=])\s*(.+)',
        re.MULTILINE
    )
    m = pattern.search(content)
    if not m:
        return None

    normalised = re.sub(r'\s+', ' ', m.group(1)).strip()
    normalised = re.sub(r'\s*#.*$',  '', normalised).strip()
    normalised = re.sub(r'\s*//.*$', '', normalised).strip()
    return normalised


def _resolve_ref(ref, project_root):
    """
    Resolve a bare filename like "config.py" to its relative path within
    project_root. Returns None if not found.
    """
    ref_basename = os.path.basename(ref)
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
        for file in files:
            if file == ref_basename:
                return os.path.relpath(os.path.join(root, file), project_root)
    return None


def run_sync_verification(sync_flags, project_root):
    """
    Given SYNC flag dicts from scanner.scan_flags(), verify that annotated
    constants actually match across files.

    Returns list of result dicts:
        {'constant': str, 'status': 'ok'|'drift'|'unresolved',
         'parties': [{'file': str, 'value': str}], 'note': str}
    """
    if not sync_flags:
        return []

    contracts = {}  # constant_name -> set of rel_paths

    for flag in sync_flags:
        parts = flag.get('message', '').strip().split()
        if len(parts) < 2:
            continue
        other_ref, const_name = parts[0], parts[1]
        if const_name not in contracts:
            contracts[const_name] = set()
        contracts[const_name].add(flag['file'])
        resolved = _resolve_ref(other_ref, project_root)
        if resolved:
            contracts[const_name].add(resolved)

    results = []
    for const_name, file_set in contracts.items():
        parties = []
        for rel_path in sorted(file_set):
            value = _extract_value(os.path.join(project_root, rel_path), const_name)
            parties.append({
                'file':  rel_path,
                'value': value if value is not None else '⚠️ not found'
            })

        values  = [p['value'] for p in parties if p['value'] != '⚠️ not found']
        missing = [p for p in parties if p['value'] == '⚠️ not found']

        if not values:
            status, note = 'unresolved', 'Could not extract value from any participant'
        elif len(set(values)) == 1 and not missing:
            status, note = 'ok', 'Values match'
        elif missing:
            status = 'drift'
            note   = f"Could not find `{const_name}` in: " + \
                     ', '.join(p['file'] for p in missing)
        else:
            status, note = 'drift', 'Values differ between files'

        results.append({
            'constant': const_name,
            'status':   status,
            'parties':  parties,
            'note':     note,
        })

    return sorted(results, key=lambda x: (x['status'] != 'drift', x['constant']))


def find_sync_candidates(all_constants):
    """
    Given {rel_path: {const_name: value}}, find constants that appear in 2+
    files with dict or list values. These are candidates for # SYNC annotations.

    Returns list of dicts:
        {'constant': str, 'files': [{'file': str, 'value': str}],
         'match': bool, 'note': str}
    """
    by_name = defaultdict(list)
    for rel_path, constants in all_constants.items():
        for name, value in constants.items():
            by_name[name].append({'file': rel_path, 'value': value})

    candidates = []
    for name, entries in by_name.items():
        if len(entries) < 2:
            continue
        def value_type(v):
            v = v.strip()
            if v.startswith('{'): return 'dict'
            if v.startswith('['): return 'list'
            return 'other'
        types  = set(value_type(e['value']) for e in entries)
        values = set(e['value'] for e in entries)
        if 'dict' not in types and 'list' not in types:
            continue
        match = len(values) == 1
        note  = 'Identical values — safe to annotate' if match \
                else '⚠️ Values differ — review before annotating'
        candidates.append({
            'constant': name,
            'files':    entries,
            'match':    match,
            'note':     note,
        })

    return sorted(candidates, key=lambda x: (x['match'], x['constant']))


def reconstruct_db_schema(migration_ops_list):
    """
    Given a list of (file_path, ops_dict) tuples in migration order,
    reconstruct the current inferred DB schema.

    Returns dict: {table_name: {col_name: type_str}}
    """
    schema = {}

    for _path, ops in migration_ops_list:
        for (table,) in ops.get('creates', []):
            if table not in schema:
                schema[table] = {}
        for (table,) in ops.get('drops', []):
            schema.pop(table, None)
        for (table, col, col_type) in ops.get('adds', []):
            if table not in schema:
                schema[table] = {}
            schema[table][col] = col_type
        for (table, col) in ops.get('removes', []):
            if table in schema:
                schema[table].pop(col, None)
        for (table, col) in ops.get('alters', []):
            if table in schema and col in schema[table]:
                schema[table][col] = schema[table][col] + ' (altered)'

    return schema
