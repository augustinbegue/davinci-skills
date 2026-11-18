---
name: derush
description: Derush raw car-shoot clips sitting in the open DaVinci Resolve project - find the usable sequences, label and rate them, mark the junk - and write the result back into Resolve as source range markers plus Selects and Rejects timelines. Use when the user asks to derush, log, sort, pre-select or clean up rushes / raw clips / dailies in Resolve.
---

# Derush rushes into DaVinci Resolve

You watch the rushes through thumbnail sheets, decide what each stretch of footage is worth, and
hand the editor a Resolve project where that work is already done. Nothing is rendered or deleted:
the output is markers, metadata and two timelines.

Division of labour:

| Job | Tool | Why |
|---|---|---|
| A first reading list | `ffmpeg` keyframe pass (`signals`) | 20x realtime on 6K HEVC; flags the long junk stretches |
| What it is, is it usable | You, reading sheets Resolve renders (`sheet`, `spot`) | Only looking tells a trunk lining from a dusk shot |
| Frame-accurate cuts | Resolve zoom sheets, labelled with frame numbers | Colour-managed, 30 frames of 6K in about 2 s |
| The deliverable | Resolve scripting API (`apply`) | The editor keeps working where the result lives |

All commands go through one script. Define a shell function once per shell call (a plain variable
does not word-split in zsh):

```
D() { python3 <skill dir>/scripts/derush.py --work <absolute work dir> "$@"; }
```

Pick a work dir outside the media folder (for example `<cwd>/derush-work/<project>`) and reuse it
for the whole job.

Positions are always **0-based source frame offsets** (`f1380`), and times in notes are **seconds
from the start of the clip**; both are printed on every sheet cell, so read them, never estimate.

**Judge the picture as shot; let possible fixes rescue a shot, never upgrade one.** The frame of
reference is the full source frame, exactly as the sheets show it (the black bars around it are not
picture). The delivery format is not known here, so a `keep` and its stars are earned on that full
frame with no help: a flaw at the very edge still counts against it. In the other direction be
generous: a flawed stretch that a reframe (crop, punch-in) or stabilisation could plausibly rescue
is included as a `maybe`, not thrown away. Its note says what the flaw is, where it sits, and what
would rescue it.

**Cell size matters.** A sheet is one image of fixed size, so more cells means smaller cells. At 30
cells you can judge content; you cannot see a faint foreign object at a frame edge. Use 16 cells or
fewer whenever the question is "is this frame clean".

## Before you start

- Resolve must be open with the project loaded and the rushes in the media pool. If `inventory`
  cannot connect, ask the user to open Resolve and set *Preferences > System > General > External
  scripting using* to *Local*.
- `ffmpeg` on PATH. No Python packages are needed; do not install any.
- Do not transcode proxies. A full decode of 6K long-GOP runs slower than realtime; the keyframe
  pass and Resolve's own thumbnails make it unnecessary.

## Step 1 - Inventory

```
D inventory            # add --bin NAME to restrict to one bin
```

If it reports markers made by the user on a clip, leave them alone (the tool only replaces its
own) and mention them in your report.

Done when: every rush is listed with its frame count.

## Step 2 - Signals

```
D signals              # --clip NAME for one clip
```

Prints suspect intervals per clip (`soft`, `flat`, `dark`, `bright`, `jolt`). Long multi-flag
intervals are nearly always junk. Single-flag blips are usually fine footage, and short problems
(a one-second camera dip, an object at the frame edge) are often not flagged at all. It tells you
where to look first, never where you can skip looking.

Done when: you have the list of suspect intervals for every clip.

## Step 3 - Overview

```
D sheet CLIP           # one frame every 2 s, 30 per sheet
```

Read every sheet. Write down a rough shot list: where the picture changes character (framing,
place, event, problem) and roughly at which frame. These rushes are usually long continuous takes,
so the cut points are changes of *content*, not camera cuts.

## Step 4 - Zoom on every boundary

```
D sheet CLIP --ranges "1340-1440:10,1660-1760:10,3040-3130:10"
```

Each window gets a letter (`A f1340`, `B f1660`) so you can tell them apart; 16 cells per sheet.
Start with a step of 10 frames over about 100 frames. If the change falls between two cells and
matters (focus coming back, an obstruction leaving), zoom again at step 5.

Placing a cut between a clean stretch and a dirty one:
- The boundary frame number is the **last clean cell before the dirt** (end of a keep) or the
  **first clean cell after it** (start of a keep). Write that number as `out` of one segment and
  `in` of the next. Segments are `[in, out)`, so this gives up one clean frame at the end of a
  keep, on purpose.
- Unsure whether a cell is clean? Treat it as dirty and use the next cell further into the clean
  stretch. Five lost frames cost nothing; one junk frame at the head of a select does.
- Content boundaries with nothing dirty (forest to fields, car drifting from wide to medium) need
  no precision: any frame in the transition will do.

## Step 5 - Write `<work dir>/segments.json`

Every frame of every clip belongs to exactly one segment: no gaps, no overlaps.

```json
{
  "descriptions": { "CLIP.MOV": "One sentence on what this rush is." },
  "clips": {
    "CLIP.MOV": [
      { "in": 0, "out": 240, "verdict": "keep", "rating": 2, "scale": "medium",
        "label": "Pull-out at junction, turns onto road",
        "note": "Blue hatchback follows from 4s.", "tags": ["junction", "turn", "traffic"] },
      { "in": 240, "out": 285, "verdict": "maybe", "rating": 1, "scale": "wide",
        "label": "Front tracking, forest, case in bottom-left",
        "note": "Blurred case fills the bottom-left corner.", "tags": ["forest", "edge-intrusion"] },
      { "in": 285, "out": 618, "verdict": "reject",
        "label": "Camera in trunk", "reason": "view blocked by trunk lining" }
    ]
  }
}
```

A `keep` and a `maybe` take the same fields. A `reject` needs only `label` and `reason`.

### Verdict

| Situation | Verdict |
|---|---|
| Car is the subject, in focus, framing settled, nothing foreign anywhere in the frame, 1.5 s or longer | `keep` |
| Foreign object (lead car, case, strap) at an edge or corner, thin or large, lasting more than about 1 s, while the car stays whole, in focus and clear of it | split it out as `maybe`, tag `edge-intrusion`; the note says which part of the frame is clean |
| Same, but flickering in for under about 1 s inside a longer clean run | stays in the `keep`; note it with its time, tag `edge-intrusion` |
| Car cut by the frame edge for part of the shot | `maybe` if it is most of the segment, otherwise a note |
| Framing drifting or shaky, but the car stays whole and in focus | `maybe`, tag `unsteady`; the note says stabilisation or a reframe could hold it |
| The car itself hidden by the object, cut by the frame throughout, or out of frame | `reject` |
| Camera swung, lowered or put down so the car is not held in frame | `reject` |
| Focus lost, lens blocked, empty frame | `reject` |
| A usable window shorter than 1.5 s between two rejects | part of the `reject`; mention the window in the reason |

When a stretch sits between `maybe` and `reject`, choose `maybe`: the editor can drop it in a
second, but cannot find what you threw away without re-watching the rush.

A reject's `reason` says in plain words what happened to the camera or the picture. The editor
reads it to decide whether to trust you without opening the clip.

### Scale (also the clip colour on the Selects timeline)

Measure the car against the picture, not the black bars. Use the value that holds for most of the
segment.

| Value | Car occupies | Colour |
|---|---|---|
| `wide` | under a quarter of the picture width | Blue |
| `medium` | a quarter to half | Teal |
| `close` | more than half, or cropped by the frame | Orange |
| `detail` | a part of the car only (wheel, light, badge, interior) | Pink |

### Rating

| Stars | Meaning |
|---|---|
| 3 | You would open the edit with it: strong framing or light, or a real event (corner, pass-by, pull-out) |
| 2 | Solid, clean, usable coverage |
| 1 | Usable but weak: car tiny, dark, distractions in frame, long and uneventful |

Expect only a few 3s per rush. If everything is a 3, nothing is.

### When to split a continuous take

Split when one of these starts or ends:
- a problem (see the verdict table);
- an event of the hero car: junction, corner, pull-out, pass-by (the hero car passing the camera);
- the setting (forest to fields), if both sides run 2 s or more;
- the scale, if the new scale holds for 2 s or more. A car that swells and shrinks within one move
  is one segment.

Do not split for other traffic, bystanders or parked cars: those go in the note with their time and
a tag, and may cost a star. Do not split just to shorten: a 35 s steady run is one segment. A
remnant under 2 s after a split joins its neighbour.

### Label, note, tags

- **Label**, under 60 characters, written so two segments are told apart at a glance. Keeps and
  maybes: `<angle or move>, <setting>, <what happens>`. Rejects: what happened to the camera
  (`Camera in trunk`, `Camera jolt`, `Empty frame`). Resolve refuses `/ \ : * ? " < > | [ ]` in
  clip names; the script swaps them out, so avoid them.
- **Note**: what the label cannot hold, with times in seconds from clip start: other vehicles,
  bystanders, a brief flaw.
- **Tags**: a few lowercase words reused across segments, e.g. `forest fields junction tree-tunnel
  dusk traffic oncoming turn pass-by indicator flare bystander edge-intrusion unsteady`.

Then:

```
D check
```

Done when: `check: OK`. Fix every ERROR it prints; it names the segment.

## Step 6 - Spot-check the selects

```
D spot                 # in, out and one frame every 4 s of every keep and maybe, 15 per sheet
```

This is where problems that were never a "boundary" get caught: the overview cells are too small
to show a case or the lead car's edge creeping into a corner. Read every sheet and look at the
**edges and corners** of each cell, then at focus, then at whether the subject is whole.

For anything you find, zoom on it with `sheet --ranges` to see when it starts and ends, apply the
verdict table, fix `segments.json`, and run `check` again. Re-run `spot` only if you moved many
boundaries.

Done when: every in and out cell is clean and every interior flaw is either split out or noted.

## Step 7 - Apply to Resolve

```
D apply                # add --order rating to put the best shots first
```

This writes, without touching anything the user made:
- a range marker per segment on each source clip (green keep, yellow maybe, red reject) with label
  and note;
- Description, Comments and Keywords metadata on each source clip;
- a `Derush` bin with `<project> - Selects` (keeps and maybes, named, coloured by scale, one
  timeline marker each) and `<project> - Rejects` (so the user can audit what was thrown away).

If those timelines already exist, new ones are created with a `v2` suffix. Use `--replace` only to
redo your own previous apply in the same job, or when the user says the old ones can go.

## Step 8 - Verify

```
D verify
```

Compares every select and marker in Resolve with `segments.json`.

Done when: `verify: OK`. On a mismatch, fix the cause and `apply --replace`.

## Report to the user

Tell them, briefly: where to look in Resolve (timeline names, the `Derush` bin, markers on the
source clips), how much was kept / maybe / rejected, the 3-star shots by name, and the judgement
calls they may want to overrule (maybes, long 1-star segments). A change of mind is an edit to `segments.json` followed by `apply --replace`.
