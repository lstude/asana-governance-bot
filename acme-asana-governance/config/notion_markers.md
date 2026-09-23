# Notion markers — paste once, never again

The sync script edits the governance page by finding **marker blocks** and
replacing everything between them. It locates sections by these exact markers,
**not** by header text — so the markers must exist in the page before the first
sync, and you must not edit the text of the marker lines themselves.

## Why these markers look the way they do

We use curly-brace tokens like `{{PD-SET:BEGIN}}` rather than HTML comments.
Notion auto-converts `-->` into an arrow (→), which silently corrupted the old
`<!-- ... -->` style. `{{...}}` is left alone by Notion's autoformatting.

The sync **recurses into toggles**, so it's fine (expected, even) for these
markers to live inside the "Custom field naming standards" toggle.

## How to add them

1. Open the **Custom field naming standards** toggle so its contents show.
2. Click where you want the PD- table — below the PD- intro prose.
3. Press **Enter** for a fresh empty line *inside the toggle*.
4. **Copy–paste** (don't hand-type) this exact line:

   ```
   {{PD-SET:BEGIN}}
   ```

5. Press **Enter twice** (one blank line — that's where the table lands).
6. Paste this exact line:

   ```
   {{PD-SET:END}}
   ```

7. Press **Escape** so Notion stops formatting the next thing you type.

> The lines render as plain gray-ish text. That's expected — they mark the
> machine-managed zone. Everything outside them is never touched.

### Gotchas

- **Plain text only.** If a pasted line becomes a code block / callout / heading,
  use the `⋮⋮` handle → **Turn into → Text**. The script matches paragraph blocks.
- **No autoreplace damage.** After pasting, confirm the lines read exactly
  `{{PD-SET:BEGIN}}` and `{{PD-SET:END}}` — no arrows, no em-dashes.
- Markers can be inside the toggle (recommended) or at page top level; both work.

---

## Later sprints — add these when you get there (optional now)

Team libraries (Sprint 2), in the same "Custom field naming standards" toggle,
below the PD-SET pair:

```
{{TEAM-LIBRARIES:BEGIN}}
{{TEAM-LIBRARIES:END}}
```

Templates (Sprint 2), in the "Template naming standards" toggle:

```
{{TEMPLATES-LIVE:BEGIN}}
{{TEMPLATES-LIVE:END}}
```

Portfolios (Sprint 2), in the "Portfolio naming standards" toggle:

```
{{PORTFOLIOS-LIVE:BEGIN}}
{{PORTFOLIOS-LIVE:END}}
```

If a marker pair is absent when its sync runs, the script logs a warning and
skips that section — it never guesses where to put a table.
