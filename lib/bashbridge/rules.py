"""Find the path-scoped rules and nested CLAUDE.md files Claude Code would load
for a file it touched with Read/Edit/Write, so bash-bridge can inject them for
files that were changed (or read) through Bash instead."""
from __future__ import annotations

import json
import os
import re

from .util import claude_config_dir, debug, is_within, read_json, truncate

_GLOB_CACHE: dict = {}


def expand_braces(pat: str, budget: int = 1000):
    """Expand {a,b} groups (nested allowed). Bounded like Claude Code's budget."""
    out = [pat]
    for _ in range(20):
        changed = False
        nxt = []
        for p in out:
            m = _find_brace(p)
            if m is None:
                nxt.append(p)
                continue
            start, end, parts = m
            changed = True
            for part in parts:
                nxt.append(p[:start] + part + p[end + 1:])
            if len(nxt) > budget:
                return [pat]
        out = nxt
        if not changed:
            break
    return out


def _find_brace(p: str):
    depth = 0
    start = -1
    i = 0
    while i < len(p):
        c = p[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                inner = p[start + 1:i]
                parts, buf, d = [], "", 0
                for ch in inner:
                    if ch == "," and d == 0:
                        parts.append(buf)
                        buf = ""
                        continue
                    if ch == "{":
                        d += 1
                    elif ch == "}":
                        d -= 1
                    buf += ch
                parts.append(buf)
                if len(parts) > 1:
                    return start, i, parts
                start = -1
        i += 1
    return None


def glob_to_regex(pat: str):
    """picomatch-style glob -> regex. `*` stays inside one path segment, `**`
    spans segments. Returns None for an invalid pattern (unclosed `[`)."""
    i, n, out = 0, len(pat), ""
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i:i + 2] == "**":
                j = i + 2
                at_seg_start = i == 0 or pat[i - 1] == "/"
                if at_seg_start and j < n and pat[j] == "/":
                    out += "(?:[^/]*/)*"
                    i = j + 1
                    continue
                out += ".*"
                i = j
                continue
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in "!^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                return None
            body = pat[i + 1:j]
            if body[:1] in ("!", "^"):
                body = "^" + body[1:]
            out += "[" + body.replace("\\", "\\\\") + "]"
            i = j
        elif c == "\\" and i + 1 < n:
            out += re.escape(pat[i + 1])
            i += 1
        else:
            out += re.escape(c)
        i += 1
    return "^" + out + "$"


def glob_match(pattern: str, rel: str) -> bool:
    pattern = pattern.strip()
    if pattern.startswith("./"):
        pattern = pattern[2:]
    pattern = pattern.lstrip("/")
    key = pattern
    rx = _GLOB_CACHE.get(key)
    if rx is None:
        rxs = []
        for p in expand_braces(pattern):
            r = glob_to_regex(p)
            if r is not None:
                rxs.append(re.compile(r))
        rx = rxs
        _GLOB_CACHE[key] = rx
    return any(r.match(rel) for r in rx)


def parse_frontmatter(text: str):
    """Return (paths or None, body). Minimal YAML: `paths:` as list, inline list or CSV string."""
    if not text.startswith("---"):
        return None, text
    lines = text.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, text
    fm = lines[1:end]
    body = "\n".join(lines[end + 1:])
    paths = None
    i = 0
    while i < len(fm):
        m = re.match(r"^paths\s*:\s*(.*)$", fm[i])
        if m:
            val = m.group(1).strip()
            items = []
            if val.startswith("["):
                try:
                    items = json.loads(val)
                except ValueError:
                    items = [x.strip().strip("'\"") for x in val.strip("[]").split(",")]
            elif val:
                items = [x.strip().strip("'\"") for x in val.split(",")]
            else:
                j = i + 1
                while j < len(fm) and re.match(r"^\s*-\s*", fm[j]):
                    items.append(re.sub(r"^\s*-\s*", "", fm[j]).strip().strip("'\""))
                    j += 1
                i = j - 1
            paths = [str(x) for x in items if str(x).strip()]
        i += 1
    return paths, body


def strip_html_comments(text: str) -> str:
    out, in_code = [], False
    buf = text.split("\n")
    i = 0
    while i < len(buf):
        line = buf[i]
        if line.strip().startswith("```"):
            in_code = not in_code
            out.append(line)
            i += 1
            continue
        if not in_code and line.strip().startswith("<!--"):
            j = i
            while j < len(buf) and "-->" not in buf[j]:
                j += 1
            i = j + 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _walk_md(root: str, limit: int = 500):
    res, seen = [], set()
    stack = [root]
    while stack and len(res) < limit:
        d = stack.pop()
        try:
            real = os.path.realpath(d)
            if real in seen:
                continue
            seen.add(real)
            with os.scandir(d) as it:
                for ent in sorted(it, key=lambda e: e.name):
                    if ent.is_dir():
                        stack.append(ent.path)
                    elif ent.name.endswith(".md") and ent.is_file():
                        res.append(ent.path)
        except OSError:
            continue
    return sorted(res)


class RuleSet:
    def __init__(self, project_dir: str, cfg: dict, excludes=None):
        self.project = os.path.realpath(project_dir)
        self.cfg = cfg
        self.excludes = list(excludes or [])
        self._rules = None

    def _load(self):
        if self._rules is not None:
            return self._rules
        rules = []
        for base, scope in ((os.path.join(claude_config_dir(), "rules"), "user rule"),
                            (os.path.join(self.project, ".claude", "rules"), "project rule")):
            if not os.path.isdir(base):
                continue
            for path in _walk_md(base):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read(400000)
                except OSError:
                    continue
                globs, body = parse_frontmatter(text)
                if globs:  # rules without paths load at launch already
                    rules.append({"path": path, "globs": globs, "body": body, "scope": scope, "base": self.project})
        self._rules = rules
        return rules

    def excluded(self, path: str) -> bool:
        return any(glob_match(p.lstrip("/"), path.lstrip("/")) for p in self.excludes)

    def matches_for(self, file_path: str):
        """Instruction files that apply to file_path: [(instruction_path, reason, body)]."""
        file_path = os.path.abspath(file_path)
        real = os.path.realpath(file_path)
        candidates = []
        for fp in {file_path, real}:
            if is_within(fp, self.project) and fp != self.project:
                candidates.append(fp)
        if not candidates:
            return []
        fp = candidates[0]
        rel = os.path.relpath(fp, self.project).replace(os.sep, "/")
        found = []
        for r in self._load():
            if any(glob_match(g, rel) for g in r["globs"]):
                found.append((r["path"], "%s, paths: %s" % (r["scope"], ", ".join(r["globs"])), r["body"]))
        # nested CLAUDE.md / CLAUDE.local.md / .claude/rules between project root and the file
        parts = rel.split("/")[:-1]
        d = self.project
        for part in parts:
            d = os.path.join(d, part)
            for name in ("CLAUDE.md", "CLAUDE.local.md"):
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    found.append((p, "nested %s for %s/" % (name, os.path.relpath(d, self.project)), None))
            nested_rules = os.path.join(d, ".claude", "rules")
            if os.path.isdir(nested_rules):
                sub_rel = os.path.relpath(fp, d).replace(os.sep, "/")
                for path in _walk_md(nested_rules, 100):
                    try:
                        with open(path, "r", encoding="utf-8", errors="replace") as fh:
                            globs, body = parse_frontmatter(fh.read(400000))
                    except OSError:
                        continue
                    if not globs or any(glob_match(g, sub_rel) for g in globs):
                        found.append((path, "nested rule in %s/.claude/rules" % os.path.relpath(d, self.project),
                                      body))
        return [f for f in found if not self.excluded(f[0])]


def loaded_in_transcript(transcript_path: str, max_bytes: int = 64 * 1024 * 1024):
    """Instruction files Claude Code itself already loaded (nested_memory attachments)."""
    res = set()
    if not transcript_path:
        return res
    try:
        if os.path.getsize(transcript_path) > max_bytes:
            return res
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"nested_memory"' not in line and '"compact_boundary"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("subtype") == "compact_boundary" or d.get("type") == "compact_boundary":
                    res = set()
                    continue
                att = d.get("attachment") or {}
                if att.get("type") == "nested_memory" and att.get("path"):
                    res.add(os.path.realpath(att["path"]))
    except OSError:
        pass
    return res


def build_context(matches, already, cfg, header_files):
    """matches: ordered [(instr_path, reason, body_or_None, trigger_file)]. Returns (text, injected_paths)."""
    budget = int(cfg.get("context_chars", 9000))
    per = int(cfg.get("rule_chars", 4000))
    parts, injected, skipped = [], [], []
    used = 0
    for instr, reason, body, trigger in matches:
        key = os.path.realpath(instr)
        if key in already or key in injected:
            continue
        if body is None:
            try:
                with open(instr, "r", encoding="utf-8", errors="replace") as fh:
                    body = fh.read(400000)
            except OSError:
                continue
        body = strip_html_comments(body).strip()
        block = "Contents of %s (%s; applies to %s):\n\n%s" % (instr, reason, trigger, truncate(body, per))
        if used + len(block) > budget:
            skipped.append(instr)
            continue
        parts.append(block)
        injected.append(key)
        used += len(block)
    if not parts and not skipped:
        return "", []
    head = ("bash-bridge: the last Bash command %s %s. Claude Code loads the project instructions below only "
            "for files touched with Read/Edit/Write, so they were not in context yet. They apply to those files; "
            "the change has already happened, so check it against them." % header_files)
    text = head + "\n\n" + "\n\n---\n\n".join(parts)
    if skipped:
        text += "\n\nMore instruction files apply but did not fit (read them with the Read tool): " + ", ".join(skipped)
        debug("rules over budget: %s" % skipped)
    return text, injected


def claude_md_excludes(sources):
    res = []
    for path in sources:
        data = read_json(path, None)
        if isinstance(data, dict) and isinstance(data.get("claudeMdExcludes"), list):
            res += [str(x) for x in data["claudeMdExcludes"]]
    return res
