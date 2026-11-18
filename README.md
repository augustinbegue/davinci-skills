# DaVinci Resolve skills

[Claude Code](https://claude.com/claude-code) skills that take a car shoot from raw rushes to a first cut in DaVinci Resolve. Nothing gets rendered or deleted: the results are markers, metadata and timelines you keep editing.

| Skill | What it does |
|---|---|
| [`derush`](skills/derush/SKILL.md) | Finds the usable sequences in the rushes, labels and rates them, flags the junk. Writes source range markers, clip metadata, and `Selects` / `Rejects` timelines back into Resolve. |
| [`reel-edit`](skills/reel-edit/SKILL.md) | Builds a short vertical reel from the derush selects and a music track. It finds the beats, picks which select plays where, and builds a timeline with the music on A1 and every cut on the beat. |

Run `derush` first: `reel-edit` reads its `segments.json` and uses its sheet renderer.

## Requirements

- DaVinci Resolve with *Preferences > System > General > External scripting using* set to *Local*
- `ffmpeg` on PATH
- Python 3 (no packages needed)

## Install

As a plugin:

```
/plugin marketplace add augustinbegue/davinci-skills
/plugin install davinci-skills@davinci-skills
```

Or symlink the skills into your personal skills folder:

```bash
git clone https://github.com/augustinbegue/davinci-skills
ln -s "$PWD/davinci-skills/skills/derush" ~/.claude/skills/derush
ln -s "$PWD/davinci-skills/skills/reel-edit" ~/.claude/skills/reel-edit
```

Then ask Claude to "derush the rushes in Resolve", and afterwards to "cut a reel from the selects to this track".

`skills/reel-edit/references/playbook.json` holds the editing rules and shot plan, drawn from nine reference reels.
