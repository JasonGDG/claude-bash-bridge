# bash-bridge

A Claude Code plugin for the times Claude changes files through **Bash** (`sed -i`, heredocs, `python -c`, `mv`, `rm`) instead of the Edit/Write tools. When that happens, bash-bridge:

1. **runs your `Write|Edit` hooks** (formatters, linters, guards, loggers) on the files the Bash command changed,
2. **loads the path-scoped rules and nested `CLAUDE.md` files** that Claude Code would have loaded if Edit/Write had been used,
3. **keeps an undo log**, so `/bash-undo` can roll back Bash changes. `/rewind` does not cover them.

Python 3 standard library only. Runs on macOS and Linux.

## The problem

In Auto Mode and bypass mode, Claude Code's system prompt tells the model to do file work through Bash. Several features only notice the dedicated file tools, so with Bash edits they stop working, and nothing tells you:

| What breaks | Issue |
| --- | --- |
| `PostToolUse` / `PreToolUse` hooks with matcher `Write\|Edit` never fire. Formatters, lint gates, change logs and write guards are skipped. | [#87971](https://github.com/anthropics/claude-code/issues/87971), [#89251](https://github.com/anthropics/claude-code/issues/89251) (a guard denying edits to one file, routed around by `sed -i`), [#87575 comment](https://github.com/anthropics/claude-code/issues/87575) (a CI battery "exits green" because it saw zero files) |
| Path-scoped rules (`.claude/rules/*.md` with `paths:`) and nested `CLAUDE.md` files don't load for files touched through Bash. 2.1.288 and 2.1.293 fixed Write/Edit and plain single-file `cat`/`head`. A 2.1.296 measurement shows pipes, `cd … &&`, redirects and **every Bash write** still load nothing. | [#90450](https://github.com/anthropics/claude-code/issues/90450), [#88565](https://github.com/anthropics/claude-code/issues/88565) |
| `/rewind` says it succeeded, but files Bash changed stay changed. A `sed` range delete with a mistyped end pattern cuts a file down to its first lines, and there's nothing to restore from. | [#87575](https://github.com/anthropics/claude-code/issues/87575), [#87971](https://github.com/anthropics/claude-code/issues/87971) |

`CLAUDE_CODE_THRIFTY_SONIC=0` in `settings.json` removes the Bash-first instruction (see the issues). It doesn't help when a model picks Bash anyway: bulk renames, `sed` one-liners, or a prompt that asks for Bash. bash-bridge works either way.

## What it does

```
PreToolUse(Bash)   -> skip commands that are clearly read-only; otherwise snapshot the work tree
the command runs
PostToolUse(Bash)  -> snapshot again, diff: created / modified / deleted
                   -> PreToolUse  Write|Edit hooks, after the fact: a deny rolls that file back
                   -> PostToolUse Write|Edit hooks with a synthesized Edit/Write event
                   -> rules + nested CLAUDE.md for the changed files, once per session
                   -> undo log entry
```

* **Snapshots** go into a private shadow git repository per project, `~/.claude-bash-bridge/projects/<hash>/shadow.git`. Everything under `~/.claude-bash-bridge` is created `0700`/`0600`. bash-bridge refuses to use a state folder that is a symlink or belongs to another user. Its work tree is your project. bash-bridge never writes to your repository's `.git`, not even the index. For git projects the shadow repo borrows your object store (`alternates`). When it's safe, it also starts from a copy of your index. It skips the copy when the index is locked or is a split or sparse index, when fsmonitor or the untracked cache is on, when entries are skip-worktree or assume-unchanged, or when attributes rewrite content. Then a snapshot costs about as much as `git status`: `ls-files -m -d -o` lists what changed, and only those files are hashed. `.gitignore` is respected. Without git, the defaults leave out `node_modules/`, `.venv/`, `__pycache__/` and similar folders.
* **Synthesized events.** A modified text file becomes `Edit` with the smallest changed line range as `old_string`/`new_string`. A new file becomes `Write` with `content` (text up to 256 KB). The top-level fields, `tool_input` and `tool_response` have the same keys as real Write/Edit events captured from Claude Code 2.1.303 (`test/fixtures/native_posttooluse.json`), including `originalFile` and a `structuredPatch` built with difflib. `session_id`, `transcript_path`, `cwd`, `permission_mode`, `prompt_id`, `effort` and `duration_ms` come from the real Bash event. One extra field, `bash_bridge: {"synthetic": true, "command": …}`, marks the event as synthesized; a hook that rejects unknown fields would have to allow it. Deleted files have no Edit/Write equivalent, so they don't fire hooks, but undo still covers them.
* **Which hooks run.** Command hooks from managed settings, `~/.claude/settings.json`, enabled plugins (`hooks/hooks.json` and `plugin.json` `hooks`), and, in trusted projects only (see [Security](#security)), `.claude/settings.json` and `.claude/settings.local.json`. They run when the matcher matches `Write` or `Edit` but not `Bash`. Hooks matching `*`, `""` or `Bash` already ran for the real Bash call. Shell-form commands get `${CLAUDE_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_DATA}` and `${CLAUDE_PROJECT_DIR}` as environment variables, never pasted into the command text. Exec-form `args` get them as plain strings. `timeout` and `async` are honoured. `if` is matched approximately: `Edit(glob)` / `Write(glob)` against the file path; any other form makes the hook skip, with a debug note. `disableAllHooks` and `allowManagedHooksOnly` are honoured at every level they appear in. bash-bridge never runs its own hooks: nested calls see `BASH_BRIDGE_ACTIVE=1` and exit at once.
* **What Claude is told.** An exit-2 / `decision: "block"` result from a bridged hook goes back to Claude as this Bash call's `decision: "block"` + `reason`. `additionalContext` and `systemMessage` are passed through as well. Rules arrive as `additionalContext` with a short header, kept under Claude Code's 10,000-character cap. Files Claude Code already loaded itself, found as `nested_memory` attachments in the transcript, are not sent again. A compaction resets this record.
* **Read-only commands** (`cat … | head`, `cd src && cat x`, `grep`, `git log` …) get no snapshot. bash-bridge still sends the rules for files they name, which Claude Code skips for those forms. Plain single-file `cat`/`head` is left to Claude Code, which handles it since 2.1.293.

## Evidence

### Unit and integration tests

`test/run.sh` simulates Claude Code calling the hooks around real shell commands. The suite has 106 tests and all pass, on Python 3.14 and on macOS's `/usr/bin/python3` 3.9. It covers:

* **Detection:** create, modify, delete, rename, `sed -i`, heredoc, `python` writes, names with spaces and Unicode, binary files, `chmod`, commits inside the command, `git checkout`, failing commands, ignored files, folders without git, and the cap for huge repos (bails out in under 3 s with a one-time note).
* **Hooks:** the synthesized Edit/Write payload, every settings source and plugins, exit-2 and JSON block relayed, `additionalContext`, matcher rules, `if`, timeouts, the recursion guard, formatter output included in undo, and the #89251 guard (revert and report modes).
* **Rules:** globs (`**`, braces, brackets, root-only `*.md`), frontmatter forms, nested `CLAUDE.md`, nested `.claude/rules`, user rules, `claudeMdExcludes`, HTML comment stripping, per-session dedupe, skipping what the transcript shows as already loaded, re-injection after a compaction, piped and `cd &&` reads, and size caps.
* **Undo:** restore, recreate and remove, dry run, the last N calls in order, conflicts that need `--force`, pre-images of dirty and untracked files, binary files, file modes, and folders without git.
* **Review fixes** (`test/test_review.py`):
  * a guard rollback that would overwrite a later change is refused
  * interleaved parallel calls
  * a file changed between undo planning and writing is kept
  * a restore replaces a symlink rather than writing through it
  * untrusted projects and repos that try to opt themselves in
  * `disableAllHooks` and managed-only settings
  * http/prompt hooks next to command hooks
  * undo still works after `git gc --prune=now` in the project and in the shadow repo
  * expired entries are reported as such
  * age-based expiry takes pre-images along
  * `0700`/`0600` modes, and the user's hooks keep their own umask
  * symlinked state folders are refused
  * index seeding refused for `index.lock`, skip-worktree, assume-unchanged, untracked cache, sparse checkout and fsmonitor
  * linked worktrees and nested repositories
  * a plugin path containing `$()`, quotes and backticks
  * `if` forms
  * key-for-key comparison with captured native events
  * rule symlinks that leave the rules folder, and symlink loops

  Three of these tests were checked by mutation: undoing the fix makes them fail.
* **Safety:** garbage on stdin, corrupt settings, no `git` on `PATH`, an unwritable state dir, a corrupt shadow repo, and no Python. In every case: exit 0 and nothing on stdout.

The project's own `.git/index` is checked byte for byte: it stays untouched.

Hook overhead in a small warm repo: about 340–360 ms for pre and post together with Homebrew Python 3.14, about 500 ms with macOS's `/usr/bin/python3`, start-up included. Inside real sessions the pre hook took 38–48 ms of its own work, plus about 370 ms the first time while it creates the shadow repo.

### End-to-end against real Claude Code 2.1.303

`test/e2e.py` builds a fresh git project for every run. Each project has a `.claude/settings.json` with:

* a `PostToolUse` `Write|Edit` logging hook,
* a `PreToolUse` `Write|Edit|NotebookEdit` guard that denies `protected.txt`,
* an `InstructionsLoaded` logger, which records what Claude Code loaded by itself.

The project also contains `.claude/rules/api.md` (`paths: ["src/api/**"]`, which requires `// RULE-MARKER-7731`) and `src/api/CLAUDE.md` (which requires `// NESTED-MARKER-4412`). Each run is `claude -p --setting-sources project,local --permission-mode auto`, with and without `--plugin-dir`. All numbers come from files on disk and hook logs, not from the model's own report.

| Scenario (model, runs) | Without bash-bridge | With bash-bridge |
| --- | --- | --- |
| **Edit with one `sed -i`**, no reading first (haiku, 3 + 5) | Write\|Edit hook fired **0/3**; rule marker **0/3**; nested marker **0/3**; Claude said "I hadn't been given any project instruction for that file" | hook fired **5/5** (2 synthesized events each); rule marker **5/5**; nested marker **5/5**; undo restored the file **5/5** |
| **Edit asked to go through Bash**, exploring allowed (haiku, 3 + 4) | hook **0/3**; rule marker **0/3**; nested marker 3/3, because the model ran `find … -exec cat CLAUDE.md` itself | hook **4/4**; rule marker **4/4**; nested **4/4**; undo **4/4** |
| **Bulk rename, natural prompt** (sonnet chose Bash for every step on its own, 1 + 1) | hook **0**; Claude Code loaded nested `CLAUDE.md` **no**; nested marker **missing** | hook fired for **4** synthesized events; nested marker **present**; undo restored the files |
| **#89251 guard**: `echo 'hacked' >> protected.txt` (haiku, 3 + 4) | guard called **0/3**; protected.txt **modified 3/3** | guard called **4/4**; protected.txt **restored 4/4**; Claude replied "a project guard hook denies changes to protected.txt, and bash-bridge restored the file" |
| **#87575 sed truncation**: `sed -i '' '/BEGIN generated/,/END generatd/d' doc.md` on an uncommitted file (haiku, 3 + 4) | doc.md cut from 43 to **10 lines, 3/3**; nothing to restore from (not committed, and `/rewind` doesn't cover Bash) | truncated 3/4 (in one run the model refused to run the command); `bash-bridge undo` restored all **43 lines, 3/3** |
| **Opus 5.5, auto mode, natural multi-file prompt** ("Rename the identifier `users` to `userCount` everywhere under src/api"; 1 + 1) | Opus edited with the **Write tool** (2 Writes), so hooks fired natively (2) and Claude Code loaded the rule and nested `CLAUDE.md` itself | Opus edited **both files with Bash heredocs** (`cat > src/api/users.ts <<'EOF'`, 0 Edit/Write calls); 2 synthesized Write\|Edit hook events and 2 guard checks; `bash-bridge undo` restored both files. Opus had read `src/api/CLAUDE.md` with `cat` before writing, so here the hooks and undo made the difference, not the rule injection |
| `/bash-bridge:bash-undo --dry-run`, then `/bash-bridge:bash-undo` inside `claude -p` (haiku) | – | the dry run listed the file without changing it; the undo restored README.md (2 of 2 runs after `allowed-tools` was added; one earlier run without it had the inline command blocked) |

When the model used Edit/Write on its own, both sides behaved the same and bash-bridge stayed quiet. That was haiku on the natural and bulk prompts, and sonnet on the natural prompt: hooks fired natively and Claude Code loaded the rules itself. With these models in `claude -p --permission-mode auto`, the Bash-first instruction didn't seem to be active. Haiku used Bash only when the prompt asked for it, while sonnet chose Bash by itself for the bulk rename. Opus 5.5 chose Bash heredocs for its edits in 1 of 2 runs of the same natural prompt, and used Write in the other.

After the review fixes, the `sed -i` (rule + nested + hooks), #89251 guard and #87575 undo arms were run again with haiku, 2 times each, with the same results: hooks 2/2, both markers 2/2, protected.txt restored 2/2, truncated doc.md restored 2/2.

Cost of all end-to-end runs: about **$0.68** (51 harness runs, including 2 with Opus at $0.12–0.15 each, plus 13 short `/bash-undo` runs; haiku about $0.002–0.005 per run, sonnet $0.05–0.07).

## Install

```bash
claude plugin marketplace add JasonGDG/claude-bash-bridge
claude plugin install bash-bridge@claude-bash-bridge
```

Restart Claude Code. To try it without installing: `claude --plugin-dir /path/to/claude-bash-bridge`.

## Use

Nothing to do: it runs on every Bash call. To undo:

```
/bash-bridge:bash-undo --dry-run     # what the last Bash call would undo
/bash-bridge:bash-undo               # undo the last Bash call
/bash-bridge:bash-undo 3             # undo the last three, newest first
/bash-bridge:bash-undo list
```

The same from a terminal, run inside the project. The plugin's `bin/` is on Claude's `PATH`. In your own shell, use the full path, for example `~/.claude/plugins/cache/claude-bash-bridge/bash-bridge/<version>/bin/bash-bridge`:

```bash
bash-bridge list                 # recorded Bash calls and the files they changed
bash-bridge undo 2 --dry-run
bash-bridge undo --id 17         # one specific call
bash-bridge undo --force         # also overwrite files that changed again since
bash-bridge status
```

Undo restores modified files with their content and mode, recreates deleted files and removes created ones. If a file changed again after that Bash call, through Edit, you or another command, undo skips it unless you pass `--force`.

## Configuration and opt-out

`~/.claude/bash-bridge.json`, or `<project>/.claude/bash-bridge.json`, which takes precedence. A project file can't change `project_hooks`, `sources`, `plugin_dirs`, `include_match_all` or `follow_rule_symlinks`, so a cloned repository can't widen what bash-bridge runs or reads:

```json
{
  "enabled": true,
  "hooks": true,
  "guard": "revert",
  "rules": true,
  "rules_on_read": "auto",
  "undo": true,
  "include_match_all": false,
  "max_files": 100000,
  "max_file_bytes": 20971520,
  "undo_days": 30,
  "project_hooks": "trusted",
  "follow_rule_symlinks": false
}
```

* `guard`: `"revert"` rolls back a change your PreToolUse Write/Edit hook denies; `"report"` only tells Claude; `"off"` skips it.
* `rules_on_read`: `"auto"`, `"all"` or `"off"`.
* `project_hooks`: `"trusted"` (the default), `true` or `false`.
* `follow_rule_symlinks`: `true` also reads `.claude/rules` symlinks that point outside the rules folder. Claude Code itself asks before loading those.

Environment variables override the files, and you can set them in `settings.json` under `env`:

* `BASH_BRIDGE_DISABLE=1` turns everything off.
* `BASH_BRIDGE_HOOKS`, `BASH_BRIDGE_RULES` and `BASH_BRIDGE_UNDO` take `0`/`1`.
* `BASH_BRIDGE_GUARD` takes `revert`, `report` or `off`.
* `BASH_BRIDGE_SOURCES=user,project,local,plugins,managed` picks the settings sources hooks are collected from.
* `BASH_BRIDGE_PROJECT_HOOKS=1|0|trusted` decides whether project hooks run. Use `1` for `claude -p` runs in folders that were never trusted interactively.
* `BASH_BRIDGE_PLUGIN_DIRS` adds plugins you load with `--plugin-dir`.
* `BASH_BRIDGE_MAX_FILES` sets the file cap.
* `BASH_BRIDGE_STATE_DIR` defaults to `~/.claude-bash-bridge`.
* `BASH_BRIDGE_DEBUG=/path/log` writes a debug log; that's the only place errors go.
* `BASH_BRIDGE_PYTHON` picks the interpreter.

## Limits

* **Hooks run after the fact.** A guard can't stop a Bash command before it runs, because nobody knows the target files in advance. bash-bridge rolls the file back right after the command. Side effects outside the files remain, and so does anything the command did with the content in between.
* **Only the project's work tree is watched.** Files outside it are not snapshotted: `~/.zshrc`, `/tmp`, other repositories. Neither are git-ignored files, files in submodules or nested repositories, untracked files over 20 MB, or files that `assume-unchanged` / skip-worktree hides. A symlink is tracked as a link, so writes through it to a target outside the project are not seen.
* **Big projects.** Over `max_files` (100,000 tracked files), or when the first snapshot takes longer than 8 s, bash-bridge switches itself off for that project and says so once per session. A folder without git gets a 400 ms file-count scan first. A home directory, for example, gets switched off right away.
* **Attribution.** Changes from parallel Bash calls, background processes or other programs during a command can land in that command's entry. `run_in_background` commands are skipped.
* **Command hooks only.** `http`, `mcp_tool`, `prompt` and `agent` hooks, hooks from skill or agent frontmatter, and PowerShell hooks are not bridged. Plugins loaded with `--plugin-dir` are invisible unless you list them in `BASH_BRIDGE_PLUGIN_DIRS`. Runtime choices are invisible to a hook: `--setting-sources`, `--settings '{"disableAllHooks": true}'`. Mirror them with `BASH_BRIDGE_SOURCES` / `BASH_BRIDGE_DISABLE`.
* **Rules arrive one step late:** after the Bash write, not before. Claude then fixes the file, which is what the end-to-end runs show. The glob matcher mirrors Claude Code's documented syntax but isn't its code. For nested `.claude/rules`, globs are relative to the folder that contains the `.claude` folder; that's an assumption.
* **Synthesized `Edit` payloads** carry the changed line range, not Claude's exact `old_string`. Hooks that rebuild a patch from it see a line-level diff.
* **Undo covers Bash changes only.** Edit/Write changes are still for `/rewind`. Undo does not depend on git objects. The previous content of every changed file is copied into `blobs/` (zlib, `0600`, at most 200 MB per call) and deleted together with its entry. An entry is dropped after 30 days or when the log grows past 300 calls. Entries whose content is no longer stored show as `[expired]` in `list` and as `EXPIRED` in `undo`, and nothing is touched for them. Old snapshot trees in the shadow repo are pruned by a background `git gc` (14 days, at most every 3 days); detection only needs the current trees.
* **Guard rollback is conservative.** A denied file is rolled back only if it still holds exactly what the Bash command wrote. Undo checks each file again, under the lock, right before writing it. Both write through a temp file plus rename and never follow a symlink out of the project.

## Security

bash-bridge executes hook commands. That makes it a security boundary, so it is stricter than "whatever is in the files":

* Hooks from `.claude/settings.json` and `.claude/settings.local.json` are code from the repository. They run only if Claude Code has recorded the workspace trust dialog as accepted for the project or a parent folder (`hasTrustDialogAccepted` in `~/.claude.json`), or if you set `project_hooks: true` / `BASH_BRIDGE_PROJECT_HOOKS=1` yourself. `claude -p` skips the trust dialog, so throwaway folders used with `-p` need that opt-in. A repository can't opt itself in. Plugins enabled only in an untrusted project's settings are ignored too.
* `disableAllHooks` (any level; managed = everything) and `allowManagedHooksOnly` are honoured. Runtime flags are not visible to hooks, see Limits.
* Bridged hooks inherit the Claude Code environment, as native hooks do, plus `BASH_BRIDGE_ACTIVE=1`, `CLAUDE_PROJECT_DIR` and the plugin variables.
* The undo store holds old file contents, which can include secrets you deleted. It is private (`0700`/`0600`), owner-checked and never a symlink. Deleting `~/.claude-bash-bridge` clears it.
* Rule files reached through symlinks that leave `.claude/rules` are not injected unless you allow it.

## Uninstall

```bash
claude plugin uninstall bash-bridge@claude-bash-bridge
claude plugin marketplace remove claude-bash-bridge
rm -rf ~/.claude-bash-bridge        # snapshots and undo logs
```

## Development

```bash
test/run.sh                            # unit + integration tests
PYTHON=/usr/bin/python3 BASH_BRIDGE_PYTHON=/usr/bin/python3 test/run.sh
python3 test/e2e.py /tmp/bb-e2e --model haiku    # real claude -p runs, costs a few cents
claude plugin validate .
```

MIT licensed.
