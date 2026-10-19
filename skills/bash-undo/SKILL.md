---
name: bash-undo
description: Undo the file changes made by the last Bash command(s), which /rewind does not cover. Arguments - [N] how many of the most recent Bash calls to undo (default 1), #ID one specific call from the list, --dry-run to preview, --force to overwrite files changed again since, or list.
argument-hint: "[N | #ID] [--dry-run] [--force] | list"
disable-model-invocation: true
allowed-tools:
  - Bash("${CLAUDE_PLUGIN_ROOT}/bin/bash-bridge" slash *)
  - Bash("${CLAUDE_PLUGIN_ROOT}/bin/bash-bridge" slash)
---

bash-bridge ran this for the user (`/bash-undo $ARGUMENTS`):

```
!`"${CLAUDE_PLUGIN_ROOT}/bin/bash-bridge" slash $ARGUMENTS`
```

The command above has already run; its output is final. Report it to the user in a few lines: which files were restored, recreated or removed, and any that were skipped. If the output says DRY RUN or is a list, say that nothing changed yet and repeat the apply command it prints. Files that were restored now differ from what you saw earlier in this conversation, so read them again before editing them. Do not run the undo again yourself.
