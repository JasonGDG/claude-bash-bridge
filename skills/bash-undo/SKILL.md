---
name: bash-undo
description: Undo the file changes made by the last Bash command(s), which /rewind does not cover. Arguments - [N] number of Bash calls (default 1), --dry-run to preview, list to show the log, --force to overwrite files changed again since.
argument-hint: "[N] [--dry-run] [--force] | list"
disable-model-invocation: true
---

bash-bridge ran this for the user (`/bash-undo $ARGUMENTS`):

```
!`"${CLAUDE_PLUGIN_ROOT}/bin/bash-bridge" slash $ARGUMENTS`
```

Report the result above to the user in a few lines: which files were restored, recreated or removed, and any that were skipped. If it was a dry run or a list, say that nothing changed yet and how to apply it (`/bash-undo N` without `--dry-run`). Files that were restored now differ from what you saw earlier in this conversation, so read them again before editing them. Do not run the undo again yourself.
