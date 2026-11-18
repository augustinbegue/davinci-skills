#!/usr/bin/env python3
"""derush.py - helper for the derush skill. Stdlib only; needs ffmpeg and a running DaVinci Resolve.

All positions are 0-based SOURCE FRAME OFFSETS from the start of a clip ("f1380").
Segments are [in, out) : `out` is the first frame NOT included.

Commands (run `derush.py <cmd> -h` for options):
  inventory   list the clips of the current Resolve project -> <work>/inventory.json
  signals     fast keyframe-only measurement of every clip   -> <work>/signals/<clip>.json + suspects
  sheet       labelled thumbnail grid(s) rendered by Resolve -> <work>/sheets/*.jpg
  check       validate <work>/segments.json (coverage, overlaps, labels)
  spot        large-cell sheets of the in, out and interior frames of every keep/maybe
  apply       write markers + Selects/Rejects timelines into Resolve
  verify      read the result back from Resolve and compare it with segments.json
"""
import argparse, json, math, os, re, statistics, subprocess, sys, time

API_DIR = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
LIB_CANDIDATES = [
    "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so",
    "/Applications/DaVinci Resolve Studio/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so",
]
MARKER_COLOR = {"keep": "Green", "maybe": "Yellow", "reject": "Red"}
SCALE_COLOR = {"wide": "Blue", "medium": "Teal", "close": "Orange", "detail": "Pink"}
VERDICTS = ("keep", "maybe", "reject")
TAG = "derush:"  # customData prefix of every marker this tool owns


def die(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- Resolve
def connect():
    os.environ.setdefault("RESOLVE_SCRIPT_API", API_DIR)
    if "RESOLVE_SCRIPT_LIB" not in os.environ:
        lib = next((c for c in LIB_CANDIDATES if os.path.exists(c)), None)
        if not lib:  # app launched from an unusual place: ask the running process
            ps = subprocess.run(["pgrep", "-fl", "MacOS/Resolve"], capture_output=True, text=True).stdout
            m = re.search(r"(/.*?DaVinci Resolve\.app)/Contents/MacOS/Resolve", ps)
            if m:
                lib = m.group(1) + "/Contents/Libraries/Fusion/fusionscript.so"
        if not lib:
            die("fusionscript.so not found. Is DaVinci Resolve installed?")
        os.environ["RESOLVE_SCRIPT_LIB"] = lib
    sys.path.append(os.path.join(os.environ["RESOLVE_SCRIPT_API"], "Modules"))
    import DaVinciResolveScript as dvr
    resolve = dvr.scriptapp("Resolve")
    if not resolve:
        die("cannot reach DaVinci Resolve. Open it, and set Preferences > System > General > "
            "External scripting using = Local.")
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        die("no project is open in Resolve.")
    return resolve, project, project.GetMediaPool()


def walk(folder, path=""):
    here = path + "/" + folder.GetName()
    for c in folder.GetClipList():
        yield here, c
    for s in folder.GetSubFolderList():
        yield from walk(s, here)


def media_clips(mp, bin_name=None):
    """name -> MediaPoolItem for real video files (optionally only inside a bin named bin_name)."""
    out = {}
    for path, c in walk(mp.GetRootFolder()):
        p = c.GetClipProperty()
        if not p.get("File Path") or "Video" not in (p.get("Type") or ""):
            continue
        if bin_name and bin_name not in path.split("/"):
            continue
        out[c.GetName()] = c
    return out


def work_dir(args, project):
    w = args.work or os.path.join(os.getcwd(), "derush-work", re.sub(r"[^\w.-]+", "_", project.GetName()))
    w = os.path.abspath(w)  # Resolve resolves relative paths against its own cwd
    for sub in ("", "signals", "sheets"):
        os.makedirs(os.path.join(w, sub), exist_ok=True)
    return w


def load_inventory(w):
    p = os.path.join(w, "inventory.json")
    if not os.path.exists(p):
        die("run `inventory` first (no %s)" % p)
    return json.load(open(p))


# ---------------------------------------------------------------- inventory
def cmd_inventory(args):
    resolve, project, mp = connect()
    w = work_dir(args, project)
    inv = {"project": project.GetName(), "resolve": resolve.GetVersionString(),
           "timelineFps": project.GetSetting("timelineFrameRate"), "clips": {}}
    for name, c in media_clips(mp, args.bin).items():
        p = c.GetClipProperty()
        frames, fps = int(p["Frames"]), float(p["FPS"])
        marks = c.GetMarkers() or {}
        own = sum(1 for m in marks.values() if (m.get("customData") or "").startswith(TAG))
        inv["clips"][name] = {"path": p["File Path"], "fps": fps, "frames": frames,
                              "seconds": round(frames / fps, 2), "startTC": p.get("Start TC"),
                              "resolution": p.get("Resolution"), "codec": p.get("Video Codec"),
                              "audioCh": p.get("Audio Ch"),
                              "derushMarkers": own, "otherMarkers": len(marks) - own}
    if not inv["clips"]:
        die("no video clips found in the media pool" + (" bin '%s'" % args.bin if args.bin else ""))
    json.dump(inv, open(os.path.join(w, "inventory.json"), "w"), indent=1)
    print("project: %s   work dir: %s" % (inv["project"], w))
    for n, c in inv["clips"].items():
        print("  %-22s %6d frames  %7.1fs  %s fps  %s" % (n, c["frames"], c["seconds"], c["fps"], c["resolution"]))
        if c["derushMarkers"]:
            print("      has %d markers from an earlier derush (apply replaces them)" % c["derushMarkers"])
        if c["otherMarkers"]:
            print("      has %d markers made by the user (never touched; tell the user they are there)" % c["otherMarkers"])


# ---------------------------------------------------------------- signals
def parse_metadata(path):
    rows, cur = [], None
    for line in open(path):
        if line.startswith("frame:"):
            cur = {"t": float(line.rsplit("pts_time:", 1)[1])}
            rows.append(cur)
        elif cur is not None and "=" in line:
            k, v = line.strip().split("=", 1)
            try:
                cur[k] = float(v)
            except ValueError:
                pass
    return rows


def measure(clip, w, name):
    """Keyframe-only decode (about 20x realtime on 6K HEVC). One row per keyframe."""
    vtxt, atxt = os.path.join(w, "signals", name + ".v.txt"), os.path.join(w, "signals", name + ".a.txt")
    vf = ("scale=480:-2:flags=fast_bilinear,signalstats,blurdetect=block_width=32:block_height=32,"
          "siti,metadata=print:file='%s'" % vtxt)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-skip_frame", "nokey", "-hwaccel", "videotoolbox",
                    "-i", clip["path"], "-map", "0:v:0", "-fps_mode", "passthrough", "-vf", vf,
                    "-f", "null", "-"], check=True)
    af = "aresample=16000,astats=metadata=1:reset=1:length=0.5,ametadata=print:key=lavfi.astats.Overall.RMS_level:file='%s'" % atxt
    a = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", clip["path"], "-map", "0:a:0", "-af", af,
                        "-f", "null", "-"])
    rms = {}
    if a.returncode == 0 and os.path.exists(atxt):
        for r in parse_metadata(atxt):
            x = r.get("lavfi.astats.Overall.RMS_level")
            if x is not None and x == x and x > -200:
                rms.setdefault(int(r["t"] * 2), []).append(x)
    rows = []
    for r in parse_metadata(vtxt):
        a_bin = rms.get(int(r["t"] * 2))
        rows.append({"f": int(round(r["t"] * clip["fps"])), "t": round(r["t"], 2),
                     "luma": round(r.get("lavfi.signalstats.YAVG", 0)),
                     "diff": round(r.get("lavfi.signalstats.YDIF", 0), 1),
                     "blur": round(r.get("lavfi.blur", 0), 2),
                     "si": round(r.get("lavfi.siti.si", 0), 1),
                     "rms": round(statistics.mean(a_bin)) if a_bin else None})
    for p in (vtxt, atxt):
        if os.path.exists(p):
            os.remove(p)
    return rows


def suspects(rows, fps):
    """Flag keyframes that look unlike the clip's normal picture, then merge them into intervals.
    Signals only say WHERE to look; the verdict always comes from looking at the frames."""
    if len(rows) < 8:
        return []
    med = lambda k: statistics.median(r[k] for r in rows)
    mb, ms, ml = med("blur"), med("si"), med("luma")
    # typical frame-to-frame change spread of this clip; a local spread far above it = camera handled
    calm = max(1.0, statistics.median(statistics.pstdev(x["diff"] for x in rows[k:k + 5])
                                      for k in range(0, len(rows) - 4)))
    flags = []
    for i, r in enumerate(rows):
        why = []
        if r["blur"] > mb * 1.35: why.append("soft")          # defocus / object against the lens
        if r["si"] < ms * 0.55: why.append("flat")            # no detail: covered lens, floor, sky
        if r["luma"] < ml * 0.72: why.append("dark")
        if r["luma"] > ml * 1.45: why.append("bright")
        j = rows[max(0, i - 2):i + 3]
        if len(j) >= 3 and statistics.pstdev(x["diff"] for x in j) > 2.2 * calm:
            why.append("jolt")                                 # erratic change: camera being handled
        flags.append(why)
    out, cur = [], None
    gap = int(1.5 * fps)
    for r, why in zip(rows, flags):
        if not why:
            continue
        if cur and r["f"] - cur["to"] <= gap:
            cur["to"] = r["f"]; cur["why"].update(why); cur["n"] += 1
        else:
            cur = {"from": r["f"], "to": r["f"], "why": set(why), "n": 1}; out.append(cur)
    return [{"from": c["from"], "to": c["to"], "why": sorted(c["why"]), "keyframes": c["n"]}
            for c in out if c["n"] >= 2]


def cmd_signals(args):
    _, project, _ = connect()
    w = work_dir(args, project)
    inv = load_inventory(w)
    for name, clip in inv["clips"].items():
        if args.clip and name != args.clip:
            continue
        t = time.time()
        rows = measure(clip, w, name)
        sus = suspects(rows, clip["fps"])
        json.dump({"clip": name, "rows": rows, "suspects": sus},
                  open(os.path.join(w, "signals", name + ".json"), "w"))
        print("\n%s  (%d keyframes measured in %.0fs)" % (name, len(rows), time.time() - t))
        if args.table:
            print("   frame   sec  luma  diff  blur    si  rms")
            for r in rows[::max(1, args.table)]:
                print("  %6d %5.1f %5d %5.1f %5.2f %5.1f %4s" % (r["f"], r["t"], r["luma"], r["diff"], r["blur"], r["si"], r["rms"]))
        print("  suspect intervals (look at these first; they are hints, not verdicts):")
        for s in sus:
            print("    f%-6d - f%-6d  %5.1fs-%5.1fs  %s" % (s["from"], s["to"], s["from"] / clip["fps"], s["to"] / clip["fps"], ",".join(s["why"])))
        if not sus:
            print("    none")


# ---------------------------------------------------------------- sheets
def export_sheets(item, frames, labels, out_prefix, per_sheet, max_dim):
    paths = []
    for n in range(0, len(frames), per_sheet):
        chunk = [{"frameOffset": f, "label": l} for f, l in zip(frames[n:n + per_sheet], labels[n:n + per_sheet])]
        path = "%s_%02d.jpg" % (out_prefix, n // per_sheet + 1)
        r = item.ExportThumbnails(chunk, {"outputType": "file", "filePath": path, "maxDimension": max_dim, "quality": 80})
        if not r or not r.get("success"):
            die("Resolve could not render %s: %s" % (path, (r or {}).get("error")))
        paths.append(path)
        print("%s   %d cells" % (path, len(chunk)))
    return paths


def cmd_sheet(args):
    _, project, mp = connect()
    w = work_dir(args, project)
    inv = load_inventory(w)
    if args.clip not in inv["clips"]:
        die("unknown clip %s. Known: %s" % (args.clip, ", ".join(inv["clips"])))
    clip = inv["clips"][args.clip]
    last = clip["frames"] - 1
    if args.ranges:
        parts = []
        for part in args.ranges.split(","):
            m = re.fullmatch(r"(\d+)-(\d+)(?::(\d+))?", part.strip())
            if not m:
                die("bad range '%s'. Use FROM-TO:STEP, e.g. 1340-1440:10,1660-1760:10" % part)
            parts.append((int(m.group(1)), int(m.group(2)), int(m.group(3) or 10)))
        frames, labels = [], []
        for n, (a, b, st) in enumerate(sorted(parts)):  # a letter per window, so a jump between windows is obvious
            for f in range(a, min(b, last) + 1, st):
                frames.append(f)
                labels.append("%s  f%d  %.1fs" % (chr(65 + n % 26), f, f / clip["fps"]))
        stem = os.path.splitext(args.clip)[0]
        used = [int(m.group(1)) for x in os.listdir(os.path.join(w, "sheets"))
                for m in [re.match(re.escape(stem) + r"_zoom(\d+)_", x)] if m]
        prefix = os.path.join(w, "sheets", "%s_zoom%02d" % (stem, max(used, default=0) + 1))
        export_sheets(media_clips(mp)[args.clip], frames, labels, prefix, args.per_sheet or 16, args.max_dim)
        return
    if args.around is not None:
        lo, hi = args.around - args.radius, args.around + args.radius
        step = args.step or max(1, args.radius // 14)
        kind = "zoom_f%d_s%d" % (args.around, step)
    else:
        lo, hi = args.start or 0, last if args.end is None else args.end
        step = args.step or int(round(2 * clip["fps"]))
        kind = "overview_s%d_f%d" % (step, lo) if (args.start or args.end is not None) else "overview_s%d" % step
    frames = [f for f in range(max(0, lo), min(last, hi) + 1, step)]
    labels = ["f%d  %.1fs" % (f, f / clip["fps"]) for f in frames]
    item = media_clips(mp)[args.clip]
    prefix = os.path.join(w, "sheets", "%s_%s" % (os.path.splitext(args.clip)[0], kind))
    export_sheets(item, frames, labels, prefix, args.per_sheet or 30, args.max_dim)


# ---------------------------------------------------------------- segments
def load_segments(w, inv):
    p = os.path.join(w, "segments.json")
    if not os.path.exists(p):
        die("no %s. Write it first (see SKILL.md step 5)." % p)
    data = json.load(open(p))
    errors, warns = [], []
    for name, segs in data.get("clips", {}).items():
        if name not in inv["clips"]:
            errors.append("%s: not in inventory" % name); continue
        frames, fps = inv["clips"][name]["frames"], inv["clips"][name]["fps"]
        segs.sort(key=lambda s: s["in"])
        pos = 0
        for i, s in enumerate(segs):
            sid = "%s #%d" % (name, i + 1)
            if s.get("verdict") not in VERDICTS: errors.append("%s: verdict must be one of %s" % (sid, VERDICTS))
            if not s.get("label"): errors.append("%s: label is empty" % sid)
            if s["out"] <= s["in"]: errors.append("%s: out <= in" % sid)
            if s["in"] < pos: errors.append("%s: overlaps previous segment (in f%d < f%d)" % (sid, s["in"], pos))
            if s["in"] > pos: errors.append("%s: gap f%d..f%d is not covered by any segment" % (sid, pos, s["in"]))
            if s["out"] > frames: errors.append("%s: out f%d is past the clip end f%d" % (sid, s["out"], frames))
            if s.get("verdict") != "reject":
                if s.get("scale") not in SCALE_COLOR: errors.append("%s: scale must be one of %s" % (sid, list(SCALE_COLOR)))
                if s.get("rating") not in (1, 2, 3): errors.append("%s: rating must be 1, 2 or 3" % sid)
                if (s["out"] - s["in"]) / fps < 1.5: warns.append("%s: shorter than 1.5 s, hard to use" % sid)
            elif not s.get("reason"): errors.append("%s: a reject needs a reason" % sid)
            pos = max(pos, s["out"])
        if pos < frames: errors.append("%s: tail f%d..f%d is not covered" % (name, pos, frames))
    for name in inv["clips"]:
        if name not in data.get("clips", {}): warns.append("%s: has no segments at all" % name)
    return data, errors, warns


def clean(text):
    """Resolve refuses clip names containing any of / \\ : * ? " < > | [ ] (SetName returns False)."""
    text = text.replace("3/4", "\u00be")
    return re.sub(r'[/\\:*?"<>|\[\]]', "-", text)


def seg_name(clip_name, i, s):
    stem = os.path.splitext(clip_name)[0][-4:]
    if s["verdict"] == "reject":
        return clean("%s-%02d \u2715 %s" % (stem, i, s["label"]))
    stars = "\u2605" * s["rating"] + "\u2606" * (3 - s["rating"])
    return clean("%s-%02d %s %s \u00b7 %s" % (stem, i, stars, s["scale"].upper(), s["label"]))


def seg_note(s, fps):
    bits = []
    if s.get("note"): bits.append(s["note"])
    if s.get("reason"): bits.append("reason: " + s["reason"])
    if s.get("tags"): bits.append("tags: " + ", ".join(s["tags"]))
    bits.append("%.1fs" % ((s["out"] - s["in"]) / fps))
    return " | ".join(bits)


def cmd_check(args):
    _, project, _ = connect()
    w = work_dir(args, project)
    inv = load_inventory(w)
    data, errors, warns = load_segments(w, inv)
    for name, segs in data["clips"].items():
        fps = inv["clips"][name]["fps"]
        tot = {v: sum(s["out"] - s["in"] for s in segs if s.get("verdict") == v) / fps for v in VERDICTS}
        print("%s: %d segments   keep %.0fs  maybe %.0fs  reject %.0fs" % (name, len(segs), tot["keep"], tot["maybe"], tot["reject"]))
    for x in warns: print("WARNING: " + x)
    for x in errors: print("ERROR: " + x)
    print("check: " + ("FAILED, fix segments.json" if errors else "OK"))
    sys.exit(1 if errors else 0)


# ---------------------------------------------------------------- spot
def cmd_spot(args):
    """Large cells of the in, out and interior frames of every select, straight from the source clips.
    Small overview cells hide faint things at the frame edges; this is where they get caught."""
    _, project, mp = connect()
    w = work_dir(args, project)
    inv = load_inventory(w)
    data, errors, _ = load_segments(w, inv)
    if errors:
        die("segments.json is not valid; run `check` first.")
    items = media_clips(mp)
    for old in os.listdir(os.path.join(w, "sheets")):
        if "_spot_" in old:
            os.remove(os.path.join(w, "sheets", old))
    for name, segs in data["clips"].items():
        if args.clip and name != args.clip:
            continue
        fps = inv["clips"][name]["fps"]
        step = max(1, int(round(args.every * fps)))
        frames, labels = [], []
        for i, s in enumerate(segs, 1):
            if s["verdict"] == "reject":
                continue
            sid = "%s-%02d" % (os.path.splitext(name)[0][-4:], i)
            inner = list(range(s["in"] + step, s["out"] - step // 2, step))
            for f, tag in [(s["in"], "IN ")] + [(f, "") for f in inner] + [(s["out"] - 1, "OUT ")]:
                frames.append(f); labels.append("%s %sf%d" % (sid, tag, f))
        if frames:
            export_sheets(items[name], frames, labels, os.path.join(w, "sheets", "%s_spot" % os.path.splitext(name)[0]),
                          args.per_sheet, args.max_dim)


# ---------------------------------------------------------------- apply
def find_or_make_bin(mp, name):
    root = mp.GetRootFolder()
    for s in root.GetSubFolderList():
        if s.GetName() == name:
            return s
    return mp.AddSubFolder(root, name)


def unique_timeline_name(project, base):
    names = {project.GetTimelineByIndex(i).GetName() for i in range(1, project.GetTimelineCount() + 1)}
    if base not in names:
        return base
    n = 2
    while "%s v%d" % (base, n) in names:
        n += 1
    return "%s v%d" % (base, n)


def build_timeline(project, mp, name, entries, by_scale):
    """entries: list of (MediaPoolItem, clip_name, index, segment, fps). Never touches existing timelines."""
    tl = mp.CreateEmptyTimeline(name)
    if not tl:
        die("could not create timeline %s" % name)
    project.SetCurrentTimeline(tl)
    infos = [{"mediaPoolItem": it, "startFrame": s["in"], "endFrame": s["out"]} for it, _, _, s, _ in entries]
    items = mp.AppendToTimeline(infos) or []
    video = [i for i in items if i and i.GetType() == "video"] if items else []
    if len(video) != len(entries):
        video = tl.GetItemListInTrack("video", 1)
    if len(video) != len(entries):
        die("timeline %s: expected %d clips, Resolve placed %d" % (name, len(entries), len(video)))
    start = tl.GetStartFrame()
    for ti, (_, clip_name, idx, s, fps) in zip(video, entries):
        nm = seg_name(clip_name, idx, s)
        if not ti.SetName(nm):
            die("Resolve refused the clip name %r (forbidden character?)" % nm)
        ti.SetClipColor(SCALE_COLOR[s["scale"]] if by_scale and s["verdict"] != "reject" else
                        {"keep": "Green", "maybe": "Yellow", "reject": "Brown"}[s["verdict"]])
        tl.AddMarker(int(ti.GetStart() - start), MARKER_COLOR[s["verdict"]], nm, seg_note(s, fps), 1, TAG + nm)
    return tl, video


def cmd_apply(args):
    _, project, mp = connect()
    w = work_dir(args, project)
    inv = load_inventory(w)
    data, errors, warns = load_segments(w, inv)
    if errors:
        for x in errors: print("ERROR: " + x)
        die("segments.json is not valid; run `check`.")
    items = media_clips(mp)
    selects, rejects = [], []
    for name, segs in data["clips"].items():
        it, fps = items[name], inv["clips"][name]["fps"]
        # 1. range markers on the source clip (only markers owned by this tool are replaced)
        for frame, m in (it.GetMarkers() or {}).items():
            if (m.get("customData") or "").startswith(TAG):
                it.DeleteMarkerAtFrame(frame)
        n_ok = 0
        for i, s in enumerate(segs, 1):
            ok = it.AddMarker(s["in"], MARKER_COLOR[s["verdict"]], seg_name(name, i, s), seg_note(s, fps),
                              s["out"] - s["in"], TAG + "%s-%02d" % (name, i))
            n_ok += bool(ok)
            (rejects if s["verdict"] == "reject" else selects).append((it, name, i, s, fps))
        # 2. clip-level metadata
        kept = [s for s in segs if s["verdict"] != "reject"]
        tags = sorted({t for s in kept for t in s.get("tags", [])})
        meta = {"Description": data.get("descriptions", {}).get(name, ""),
                "Comments": "derush: %d selects, %d rejects" % (len(kept), len(segs) - len(kept))}
        if tags: meta["Keywords"] = ",".join(tags)
        it.SetMetadata({k: v for k, v in meta.items() if v})
        print("%s: %d/%d source markers written" % (name, n_ok, len(segs)))
    # 3. timelines, in a Derush bin; existing timelines are never modified or deleted
    prev = os.path.join(w, "applied.json")
    if args.replace and os.path.exists(prev):
        old = set(json.load(open(prev)).get("timelines", {}).values())
        doomed = [project.GetTimelineByIndex(i) for i in range(1, project.GetTimelineCount() + 1)
                  if project.GetTimelineByIndex(i).GetName() in old]
        if doomed:
            print("replacing: %s" % ", ".join(t.GetName() for t in doomed))
            mp.DeleteTimelines(doomed)
    folder = find_or_make_bin(mp, "Derush")
    mp.SetCurrentFolder(folder)
    prefix = args.prefix or project.GetName()
    made = {}
    if args.order == "rating":
        selects.sort(key=lambda e: (-e[3]["rating"], e[1], e[3]["in"]))
    for label, entries, by_scale in (("Selects", selects, True), ("Rejects", rejects, False)):
        if not entries:
            continue
        name = unique_timeline_name(project, "%s - %s" % (prefix, label))
        tl, video = build_timeline(project, mp, name, entries, by_scale)
        made[label] = name
        print("timeline '%s': %d clips, %.1fs" % (name, len(video), sum(e[3]["out"] - e[3]["in"] for e in entries) / entries[0][4]))
    if "Selects" in made:  # leave the user looking at the selects
        for i in range(1, project.GetTimelineCount() + 1):
            if project.GetTimelineByIndex(i).GetName() == made["Selects"]:
                project.SetCurrentTimeline(project.GetTimelineByIndex(i))
    json.dump({"timelines": made, "appliedAt": time.strftime("%Y-%m-%d %H:%M:%S")},
              open(os.path.join(w, "applied.json"), "w"), indent=1)
    print("apply: OK. Next: `verify`.")


# ---------------------------------------------------------------- verify
def cmd_verify(args):
    _, project, mp = connect()
    w = work_dir(args, project)
    inv = load_inventory(w)
    data, errors, _ = load_segments(w, inv)
    applied = json.load(open(os.path.join(w, "applied.json")))
    tl = None
    for i in range(1, project.GetTimelineCount() + 1):
        if project.GetTimelineByIndex(i).GetName() == applied["timelines"].get("Selects"):
            tl = project.GetTimelineByIndex(i)
    if not tl:
        die("Selects timeline %s not found" % applied["timelines"].get("Selects"))
    expected = [(n, i, s) for n, segs in data["clips"].items() for i, s in enumerate(segs, 1) if s["verdict"] != "reject"]
    got = tl.GetItemListInTrack("video", 1)
    bad = 0
    by_name = {seg_name(n, i, s): (n, s) for n, i, s in expected}
    frames, labels = [], []
    start = tl.GetStartFrame()
    for ti in got:
        nm = ti.GetName()
        if nm not in by_name:
            print("MISMATCH: unexpected timeline clip '%s'" % nm); bad += 1; continue
        n, s = by_name.pop(nm)
        src_in, dur = int(ti.GetLeftOffset()), int(ti.GetDuration())  # GetSourceStartFrame is off by one on later items
        if src_in != s["in"] or dur != s["out"] - s["in"]:
            print("MISMATCH: %s is f%d +%d, expected f%d +%d" % (nm, src_in, dur, s["in"], s["out"] - s["in"])); bad += 1
        a, b = int(ti.GetStart() - start), int(ti.GetEnd() - start) - 1
        frames += [a, b]; labels += [nm.split(" ")[0] + " IN", nm.split(" ")[0] + " OUT"]
    for nm in by_name:
        print("MISMATCH: missing from timeline: %s" % nm); bad += 1
    m_bad = 0
    items = media_clips(mp)
    for n, segs in data["clips"].items():
        mk = {f: m for f, m in (items[n].GetMarkers() or {}).items() if (m.get("customData") or "").startswith(TAG)}
        for s in segs:
            m = mk.get(s["in"]) or mk.get(float(s["in"]))
            if not m or int(m["duration"]) != s["out"] - s["in"]:
                print("MISMATCH: source marker for %s f%d" % (n, s["in"])); m_bad += 1
    print("timeline '%s': %d clips checked, %d mismatches; source markers: %d mismatches" % (tl.GetName(), len(got), bad, m_bad))
    if frames and args.sheet:
        print("first and last frame of every select (look for junk at the edges):")
        export_sheets(tl, frames, labels, os.path.join(w, "sheets", "verify_inout"), args.per_sheet, args.max_dim)
    print("verify: " + ("FAILED" if bad or m_bad else "OK"))
    sys.exit(1 if bad or m_bad else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", help="work directory (default ./derush-work/<project>)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inventory"); p.add_argument("--bin", help="only clips inside this bin"); p.set_defaults(fn=cmd_inventory)
    p = sub.add_parser("signals"); p.add_argument("--clip"); p.add_argument("--table", type=int, nargs="?", const=1, default=0, help="print every Nth keyframe row"); p.set_defaults(fn=cmd_signals)
    p = sub.add_parser("sheet"); p.add_argument("clip")
    p.add_argument("--start", type=int); p.add_argument("--end", type=int); p.add_argument("--step", type=int)
    p.add_argument("--ranges", help="zoom on several windows at once: FROM-TO:STEP,FROM-TO:STEP,...")
    p.add_argument("--around", type=int, help="zoom: centre frame"); p.add_argument("--radius", type=int, default=45)
    p.add_argument("--per-sheet", type=int, help="cells per sheet (default 30 for the overview, 16 for zooms; fewer = larger cells)")
    p.add_argument("--max-dim", type=int, default=2300); p.set_defaults(fn=cmd_sheet)
    p = sub.add_parser("check"); p.set_defaults(fn=cmd_check)
    p = sub.add_parser("spot"); p.add_argument("--clip"); p.add_argument("--every", type=float, default=4.0, help="seconds between interior samples")
    p.add_argument("--per-sheet", type=int, default=15); p.add_argument("--max-dim", type=int, default=2300); p.set_defaults(fn=cmd_spot)
    p = sub.add_parser("apply"); p.add_argument("--prefix", help="timeline name prefix (default project name)")
    p.add_argument("--order", choices=("source", "rating"), default="source")
    p.add_argument("--replace", action="store_true", help="delete the Selects/Rejects timelines made by the previous apply (only if the user has not edited them)")
    p.set_defaults(fn=cmd_apply)
    p = sub.add_parser("verify"); p.add_argument("--sheet", action="store_true", help="also render in/out frames from the timeline")
    p.add_argument("--per-sheet", type=int, default=30); p.add_argument("--max-dim", type=int, default=2300); p.set_defaults(fn=cmd_verify)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
