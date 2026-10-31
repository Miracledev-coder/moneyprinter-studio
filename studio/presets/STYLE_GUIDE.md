# MoneyPrinter Studio — Long-form Explainer Style Guide
Built 2026-10-31 from 3 references. Measurements: ffmpeg scene detect (gt(scene,0.3)), yt-dlp auto-subs (en-orig), volumedetect.
Files: sheet-skin-colors.jpg, sheet-ink-allday.jpg, sheet-ottoman-daughter.jpg (1 frame / 20s); cuts-*.txt (cut timestamps); sub-*.txt (timed transcripts).

## Key numbers
| Ref | Duration | Cuts | Avg shot | Median shot | Sentences | Shots/sentence | WPM* |
|---|---|---|---|---|---|---|---|
| Joe – Skin Colors (os0KUojqFXo) | 12:04 | 184 | 3.9s | 3.3s | 142 | 1.3 | ~130-150 |
| Ink – Ancient Humans All Day (49_Ph2q6uIM, 17M views) | 11:12 | 227 | 2.95s | 2.3s | 200 | 1.1 | ~200-225 |
| Ottoman Days – Sultan's Daughter (CTCTAW5o5Zk, 534K) | 19:40 | 394 | 3.0s | 2.4s | 314 | 1.25 | ~210-220 |
*WPM = deduped auto-caption words / duration; Joe narration is noticeably slower and has pauses. Ink hook is fastest: 34 cuts in first 60s (~1.8s/shot), then settles to ~3s.

## 1. Joe — "Why do Humans Have Different Skin Colors?"
- **Art**: 2D flat digital illustration, clean black outlines, soft cel shading. Two registers: (a) white-background stick figures (round heads, dot eyes) for concept beats, (b) fully painted scenes (savanna, ice age, forests, migration lines) with consistent cartoon cavemen in fur tunics. Warm earthy palette (ochre, sand, sky blue, greens). Infographic elements: hand-lettered all-caps labels ("PREHISTORIC MAN / MODERN EUROPEAN MAN"), maps (Africa, Beringia, Europe), family trees, DNA helices, side-by-side split panels, crowd "lineup" panels.
- **Motion**: still images with slow zoom/pan (Ken Burns) + hard cuts; small channel bug bottom-right.
- **Narration**: calm, curious, second-person time travel. Hook: "It's roughly 200,000 years ago. You're a human, not a king, not a farmer…" → surprising claim ("you probably wouldn't be white…") → question ("where did all the other colors come from?") → "Let's start at the beginning." Chaptered with spoken headers ("The Asian shortcut."). Analogies ("two students taking the same math test").
- **Audio**: mean -19.5 dB, quiet soft ambient bed under voice.

## 2. Ink Explainer — "What Did Ancient Humans Actually Do All Day?"
- **Art**: hand-drawn ink/pen sketch with flat watercolor-ish fill, slightly wobbly lines. Recurring caveman: messy brown hair, big round eyes, brown fur tunic, stick limbs — highly consistent. Modern "you" = bald white stick figure. Backgrounds: desaturated beige/sand, teal sky, night scenes with campfire glow. Many white-background beats with one object + handwritten caps text ("NOBODY TELLS YOU THIS", "CAVEMAN?", "BANK ACCOUNT" tag), red X-outs over modern things (office desk, fridge), small doodle props (alarm clock, money, skeletons).
- **Motion**: stills, gentle push-ins, very fast cuts in hook. Literal visuals: nearly every clause gets a picture (alarm clock on "machine screamed", washing machine on "washing machines").
- **Narration**: fast (~220 wpm), punchy, "you vs. him" contrast. Hook: modern pain ("a machine screamed at you until you woke up") → "Now, rewind 50,000 years" → contrarian ("your brain is already fighting this… that was the official story. But the proof… was buried in the ground") → mystery question. Evidence-reveal (skeleton comparison, "it's the opposite").
- **Audio**: mean -14.5 dB (louder, compressed voice), light music bed.
- **Titles (channel top)**: "What Did Ancient Humans Actually Do All Day?" (17M), "…Do When It Rained All Week?" (1.9M), "Why Are We the Only Human Species Left?" (1.6M), "The Disturbing Ways Ancient Humans Survived Winter" (1.3M), "When Did Ancient Humans Start Drinking Alcohol?" (934K). Pattern: "What/How/When Did Ancient Humans [everyday activity]?" + "Actually"; 6-11 min.

## 3. Ottoman Days — "Why It Sucked to Be the Sultan's Daughter"
- **Art**: same cute stick-figure DNA as Ink (round heads, dot eyes) but richly painted Ottoman settings: Iznik-tile walls, latticed windows, carpets, Istanbul skyline with domes/minarets, warm sunset palette (teal, terracotta, gold, crimson). Costume = identity: tall white börk/kavuk turbans for officials, crowns/veils for women, colored kaftans. White-background caption cards in dark serif-ish caps ("EVERY RULE IS REAL", "50,000+ GOLD COINS", "8 YEARS LATER"), name tags in boxes ("SULTAN MURAD", "ISMIHAN SULTAN"), portrait medallions with role label ("PIYALE PASHA – ADMIRAL"), simple bar charts and family-tree diagrams.
- **Motion**: stills + Ken Burns, hard cuts ~3s.
- **Narration**: ~220 wpm, second person "you're going to live her life from the very first day", composite character disclaimer ("You're built from the lives of several women… every rule is real"). Hook: modern relatable scene (filming a bride at a wedding) → twist ("nobody ever sees the bride") → hard fact (1562, 50,000 gold coins) → title question ("So why did it suck to be…?") + secondary question.
- **Titles**: "Why It Sucked to Be the [Role] (in the Ottoman Empire)" (534K top, 69K, 41K), "What Did the [Role] Actually Do All Day?", "What Actually Happened When You Became [Role]?". 12-22 min. Note: channel's older uploads are ancient-human explainers — it copied the Ink formula, then niched down.

## Shared recipe
1. **Format**: 2D illustrated stills, cute minimalist stick-figure characters in detailed painted environments; alternating with white "concept cards" (one object/text). No 3D, no photoreal.
2. **Cadence**: avg 3s shots (2s in first minute), ~1.2 images per sentence; a new image whenever the noun changes. Visuals are literal to the line.
3. **Motion**: Ken Burns (zoom 1.00→1.08, slow pan), hard cuts, no fancy transitions; occasional split-screen for comparisons.
4. **Hook (0-40s)**: modern relatable "you" scene → time jump → myth/assumption → twist or hard number → title question → promise ("to find out, we start…").
5. **Body**: chapters with spoken headers, second-person immersion, evidence reveals, analogies, a "but it's the opposite" twist every 60-90s, end loop back to the modern you.
6. **Text on screen**: hand-lettered all-caps for numbers, names, and key phrases; red X for "not this".
7. **Audio**: voice dominant (-14 to -16 LUFS-ish), soft ambient/lo-fi or cinematic-folk bed at -25 to -30 dB, light SFX (whoosh on text cards, fire crackle).
See director_style_presets.json for machine-usable presets.
