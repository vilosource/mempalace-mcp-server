# Git hooks

Version-controlled hooks for this repository.

## What's enforced

- **commit-msg** — rejects commit messages containing AI authorship attributions (`Co-Authored-By: Claude`, `Generated with Claude Code`, `🤖`, etc.) or AI-tool domain references.
- **pre-commit** — scans staged files for the same patterns, so AI attribution in source/docs is rejected before it reaches a commit.

## Enabling

Git doesn't run hooks from a versioned directory by default. One-time setup per clone:

```bash
git config core.hooksPath .githooks
```

Verify:

```bash
git config --get core.hooksPath   # should print: .githooks
```

If you add a new hook file, make it executable (`chmod +x .githooks/<name>`) and commit the mode bit.

## Rationale

Project policy: commits and file contents in this repository should stand on their own — no AI tool attribution, no "generated with" markers. These hooks catch the common patterns mechanically rather than relying on reviewer vigilance.

## Bypassing

`git commit --no-verify` bypasses the hooks. Don't use it for AI-attribution content. If a hook false-positives on legitimate prose (e.g. documenting one of these patterns verbatim, as this file does), adjust the hook, not the commit.
