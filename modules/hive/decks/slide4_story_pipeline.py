"""Slide 4 - the story pipeline. One orchestrator, its steps vertical, each
step called out with the skill or agent it delegates to, that file's
parameters and defaults, and a terse statement of what it is told to do."""
import deck
from deck import GOLD, GREY

S = 4
INK, SUB = GOLD, GREY
deck.clear_slide(S)

deck.text(S, 50, 40, "SLIDE 4   -   THE STORY PIPELINE   (as described 2026-10-05, not yet built)", 19, INK)
deck.text(S, 50, 64, "one orchestrator. it holds the dependency graph and the verdicts, delegates everything else, writes no code and judges nothing.", 11, SUB)
deck.text(S, 50, 82, "SOL = gpt-6-sol.   QWEN = qwen35-131k.   every model comes from build-models.json; the orchestrator never picks one.   colour carries nothing.", 11, SUB)

LX, LY, LW, LH = 40, 110, 1760, 760
deck.box(S, LX, LY, LW, LH, INK, 2)
deck.text(S, LX + 150, LY + 30, "THE ORCHESTRATOR    skill: build-orchestrator    params: run directory (default: cwd)", 14, INK)

BX, BW, BH, GAP = 300, 330, 54, 106
CX = BX + BW + 40          # where the callout line ends and text begins
rows = [
    ("0   READ STATE", "internal - no delegation", [
        "reads build-state.json and STORIES.md.",
        "absent state means nothing has run; it starts at step 1.",
        "this is the only thing it reads to decide anything.",
    ]),
    ("1   DECOMPOSE", "skill: build-step-decompose        MODEL: SOL", [
        "params:  brief (default BRIEF.md)   out (default STORIES.md)",
        "told to: split the app into small, composable, non-overlapping stories",
        "in their correct hierarchy. each story declares its prerequisites and its",
        "own deliverables. prerequisites are a graph, not a running order.",
    ]),
    ("2   PLAN EVERY STORY", "skill: build-step-plan              MODEL: SOL", [
        "params:  story (default: all unplanned)   repo (default: cwd)",
        "told to: read the story and the real repo, write",
        "stories/<n>/IMPLEMENTATION.md - what to build, where it goes, what",
        "proves it done. this document is the contract the agent is held to.",
    ]),
    ("3   SELECT THE WAVE", "internal - no delegation", [
        "every story whose prerequisites are all COMPLETE and which is not",
        "itself complete. that set is the wave - it is not one story, and it is",
        "not the file order. an empty wave with stories outstanding means the",
        "graph is blocked, and it stops and says which story blocks it.",
    ]),
    ("4   DISPATCH THE WAVE", "agent: build-agent-story            MODEL: QWEN", [
        "params:  story (REQUIRED, no default)              one agent per story, concurrently",
        "told to: open stories/<n>/.  if CODE-REVIEW.md is there, the code already",
        "exists and is wrong - remedy exactly what the review names.  if it is not",
        "there, implement from IMPLEMENTATION.md.  hand back when done.",
        "the whole prompt is 'work story <n>' - the folder carries everything else.",
    ]),
    ("5   CODE REVIEW", "skill: build-step-code-review       MODEL: SOL", [
        "params:  story (REQUIRED)   rounds (default 3)",
        "told to: run this story's tests AND check its declared deliverables - it is",
        "told what this story owed, so 'the app starts' is a criterion only where that",
        "story owed it. READ THE CODE: hardcoded returns, ignored arguments, a test",
        "asserting the shape of a fake. a green suite is not a pass.",
        "fails -> write stories/<n>/CODE-REVIEW.md naming the defect and prescribing",
        "the fix.   passes -> clear it into stories/<n>/reviews/.",
    ]),
    ("6   RECORD THE VERDICT", "internal - no delegation", [
        "writes pass or fail per story into build-state.json with the model it ran on.",
        "the agent's own report is never evidence - the review is.",
    ]),
]
for i, (name, who, notes) in enumerate(rows):
    y = LY + 60 + i * GAP
    deck.box(S, BX, y, BW, BH, INK, 2)
    deck.text(S, BX + 14, y + 24, name, 13, INK)
    deck.text(S, BX + 14, y + 42, who, 10, INK if "MODEL" in who else SUB)
    if i:
        deck.arrow(S, BX + BW / 2, y - GAP + BH, BX + BW / 2, y - 2, SUB, 1, 7)
    deck.line(S, BX + BW, y + BH / 2, CX - 10, y + BH / 2, SUB, 1)
    for j, n in enumerate(notes):
        deck.text(S, CX, y + 10 + j * 16, n, 11, SUB)

top = LY + 60
y3 = top + 3 * GAP
y4 = top + 4 * GAP
y5 = top + 5 * GAP
y6 = top + 6 * GAP

# 5 does not pass -> 4, same story
r1 = BX - 30
deck.line(S, BX, y5 + BH / 2, r1, y5 + BH / 2, SUB, 1)
deck.line(S, r1, y5 + BH / 2, r1, y4 + BH / 2, SUB, 1)
deck.arrow(S, r1, y4 + BH / 2, BX - 2, y4 + BH / 2, SUB, 1, 7)
deck.text(S, 55, y5 - 6, "review did not pass:", 11, INK)
deck.text(S, 55, y5 + 10, "same story, same", 11, SUB)
deck.text(S, 55, y5 + 26, "agent, carrying the", 11, SUB)
deck.text(S, 55, y5 + 42, "review. three rounds,", 11, SUB)
deck.text(S, 55, y5 + 58, "then it stops for a", 11, SUB)
deck.text(S, 55, y5 + 74, "person. (my call)", 11, SUB)

# 6 -> 3, next wave
r2 = BX - 85
deck.line(S, BX, y6 + BH / 2, r2, y6 + BH / 2, SUB, 1)
deck.line(S, r2, y6 + BH / 2, r2, y3 + BH / 2, SUB, 1)
deck.arrow(S, r2, y3 + BH / 2, BX - 2, y3 + BH / 2, SUB, 1, 7)
deck.text(S, 55, y3 - 24, "stories passed:", 11, INK)
deck.text(S, 55, y3 - 8, "select the next wave", 11, SUB)
deck.text(S, 55, y3 + 8, "from whatever they", 11, SUB)
deck.text(S, 55, y3 + 24, "just unblocked", 11, SUB)

# exit
deck.arrow(S, BX + BW / 2, LY + LH, BX + BW / 2, LY + LH + 40, SUB, 1, 7)
deck.box(S, BX - 30, LY + LH + 42, 390, 56, INK, 2)
deck.text(S, BX - 14, LY + LH + 68, "EVERY STORY COMPLETE", 13, INK)
deck.text(S, BX - 14, LY + LH + 86, "the app exists because every story's deliverables were met.", 10, SUB)

# artifacts
AX, AY = 700, LY + LH + 40
deck.text(S, AX, AY, "WHAT IS ON DISK   -   the artifacts are the interface. an agent gets a number and reads the rest.", 14, INK)
for j, n in enumerate([
    "STORIES.md                       the app as a whole, every story, every prerequisite edge",
    "build-state.json                 the orchestrator's verdicts - the only file it writes itself",
    "stories/<n>/IMPLEMENTATION.md    written by SOL before any agent is dispatched",
    "stories/<n>/CODE-REVIEW.md       written by SOL only when a handback did not pass. its PRESENCE is the instruction.",
    "stories/<n>/reviews/             every review ever written for that story, kept once it is cleared",
]):
    deck.text(S, AX + 10, AY + 26 + j * 18, n, 11, SUB)
