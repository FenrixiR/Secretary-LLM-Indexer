import re
import os
from ollama import Client

# Bump PROMPT_VERSION in main.py whenever you edit the prompts below.
# The version is folded into the manifest cache key, so all cached summaries
# are invalidated automatically on the next run — no manual manifest wipe needed.
COMPLEXITY_THRESHOLD = 8  # functions — above this gets the rich 3-point prompt


class SecretaryAI:
    def __init__(self, model=None, host=None, num_gpu=99, num_ctx=4096):
        self.model   = model or os.getenv("AI_MODEL", "qwen2.5-coder:7b")
        self.num_gpu = num_gpu
        self.num_ctx = num_ctx
        host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.client  = Client(host=host)

        # ── One-shot examples anchor the exact output format ──────────────────
        # Without examples, 7b models interpret "domain: keywords" as an
        # invitation to write category headers. Concrete examples prevent that.

        # Simple example: a generic database seeding utility
        _simple_example = (
            "Example input: seed_data.py — populates DB with initial records "
            "from static JSON files, decodes compressed format, runs async\n"
            "Example output: data seeding: static JSON decode, DB init, "
            "async session, record population\n\n"
            "Now index this file:\n"
        )

        # Rich example: a generic game state manager
        _rich_example = (
            "Example:\n"
            "1. session state: player identity, auth token, local player ID, debug flag\n"
            "2. persistence: save/load credentials, file I/O, session restore\n"
            "3. runtime flags: debug mode, feature switches, environment config\n\n"
            "Now index this file:\n"
        )

        # ── Simple prompt — one line, noun-heavy, keyword-dense ───────────────
        self.system_simple = (
            "Write a one-line code index entry. "
            "Format: '<primary domain>: <key concepts, data, systems>' "
            "Flat comma-separated noun phrases only — no subcategories, no slashes, no bullets. "
            "Do NOT write multiple domain lines. ONE line only. "
            "Banned words: handles, manages, defines, contains, extends, implements. "
            "15 words maximum. No engine or language names.\n\n"
            + _simple_example
        )

        # ── Rich prompt — 3 domain lines for complex files ────────────────────
        self.system_rich = (
            "Write a code index entry with exactly 3 lines. "
            "Each line: '<domain>: <flat comma-separated keywords>' "
            "No subcategories. No slashes between domains. No bullets. "
            "Banned words: handles, manages, defines, contains, extends, implements. "
            "Each line max 12 words.\n\n"
            + _rich_example +
            "Format exactly:\n"
            "1. <domain>: <keywords>\n"
            "2. <domain>: <keywords>\n"
            "3. <domain>: <keywords>"
        )

    def summarize_skeleton(self, file_name, skeleton, func_count=0, schema_hint=None):
        """
        Summarize a code file skeleton.
        Returns: (summary: str, _: str)
        """
        if not skeleton.strip():
            return "Empty file.", ""

        is_complex = func_count >= COMPLEXITY_THRESHOLD

        try:
            char_limit   = 2400 if is_complex else 1600
            clean_skel   = skeleton[:char_limit].replace('\n', ' ')
            hint_block   = f"\nAdjacent data schema: {schema_hint}" if schema_hint else ""
            prompt       = f"File: {file_name}{hint_block}\nCode: {clean_skel}\n"
            prompt      += "Responsibilities:" if is_complex else "Sentence:"

            response = self.client.generate(
                model=self.model,
                system=self.system_rich if is_complex else self.system_simple,
                prompt=prompt,
                stream=False,
                options={
                    "temperature": 0.2,
                    "num_predict": 180 if is_complex else 80,
                    "num_ctx":     self.num_ctx,
                    "num_gpu":     self.num_gpu,
                    "num_thread":  8,
                    "stop":        ["File:", "Code:", "---"]
                }
            )

            raw = response['response'].strip()
            return self._parse_rich(raw) if is_complex else self._parse_simple(raw)

        except Exception as e:
            return f"AI error: {e}", ""

    # ── Output parsers ────────────────────────────────────────────────────────

    def _parse_simple(self, raw):
        """Strip filename echoes, bracket tags, leading numbering."""
        clean = raw.strip()
        clean = re.sub(r'^[\w./\\-]+\.\w+\s*:\s*', '', clean)   # strip path echo
        clean = re.sub(r'\s*\[[^\]]+\]\s*', ' ', clean)          # strip [tags]
        clean = re.sub(r'^\d+\.\s*', '', clean.strip())           # strip leading number
        clean = re.sub(r"^[Tt]his file\s+", "", clean)            # strip "This file "
        clean = re.sub(r'\s*/\s*', ' / ', clean)                  # normalise separators
        if len(clean) > 180:
            last_comma = clean[:180].rfind(',')
            clean = clean[:last_comma] if last_comma > 60 else clean[:180]
        return clean.strip(' /.-'), ""

    def _parse_rich(self, raw):
        """Extract numbered responsibilities, strip echoes, join with ' / '."""
        lines  = [l.strip() for l in raw.split('\n') if l.strip()]
        points = []
        for line in lines:
            if re.match(r'^\d+\.', line):
                clean = re.sub(r'^\d+\.\s*', '', line)
                clean = re.sub(r'^[\w./\\-]+\.\w+\s*:\s*', '', clean)
                clean = re.sub(r'\s*\[[^\]]+\]\s*', ' ', clean).strip()
                if len(clean) > 100:
                    last_comma = clean[:100].rfind(',')
                    clean = clean[:last_comma] if last_comma > 30 else clean[:100]
                if clean:
                    points.append(clean.strip())
        if not points:
            return self._parse_simple(raw)
        return " / ".join(points[:3]), ""
