#!/usr/bin/env python3
"""The hive intro deck. Re-runnable: each slide clears itself before drawing.

    python3 decks/hive_intro.py [slide ...]
"""
import sys
from deck import DIM, GOLD, GREY, TEAL, arrow, box, clear_slide, line, text


def slide1():
    S = 1
    clear_slide(S)

    text(S, 60, 62, "Hive Mind: a coding harness, wrapped in an API", 30, GOLD)
    text(S, 60, 92, "the CLI agent stops being a terminal app and becomes a service "
                    "you can reach from anywhere", 15, DIM)

    # Surfaces — the front doors.
    text(S, 60, 150, "SURFACES", 13, GREY)
    surfaces = [("Telegram", "phone, on the couch", 175),
                ("Web terminal", "xterm.js in the browser", 275),
                ("Discord", "team channel", 375)]
    for label, sub, y in surfaces:
        box(S, 60, y, 250, 70, GOLD)
        text(S, 80, y + 30, label, 20, GOLD)
        text(S, 80, y + 52, sub, 13, DIM)

    # Gateway — one conversation behind all of them.
    text(S, 400, 150, "GATEWAY", 13, GREY)
    box(S, 400, 175, 250, 270, GOLD)
    text(S, 420, 210, "hive-comms", 22, GOLD)
    text(S, 420, 245, "one conversation,", 14, DIM)
    text(S, 420, 265, "many front doors", 14, DIM)
    text(S, 420, 305, "· session + identity", 13, DIM)
    text(S, 420, 328, "· composes the prompt", 13, DIM)
    text(S, 420, 351, "· routes to the right mind", 13, DIM)
    text(S, 420, 374, "· one live process per chat", 13, DIM)
    for _, _, y in surfaces:
        arrow(S, 310, y + 35, 398, 310)
    text(S, 300, 480, "bot API / websocket", 12, GREY)

    # The mind — the wrapper and the harness it wraps.
    text(S, 740, 150, "THE MIND (one python process)", 13, GREY)
    box(S, 740, 175, 500, 270, TEAL)
    box(S, 765, 205, 450, 80, TEAL, 1)
    text(S, 785, 240, "mind_server  —  FastAPI", 20, TEAL)
    text(S, 785, 265, "HTTP + websocket. spawn, stream, kill.", 13, DIM)
    box(S, 765, 315, 450, 100, GOLD, 1)
    text(S, 785, 350, "claude  /  codex   subprocess", 20, GOLD)
    text(S, 785, 375, "the same CLI you run by hand —", 13, DIM)
    text(S, 785, 396, "now spoken to over a socket instead of a keyboard", 13, DIM)
    arrow(S, 650, 310, 738, 310)
    text(S, 658, 296, "dispatch", 12, GREY)
    arrow(S, 990, 285, 990, 313, GREY, 1, 7)

    # Memory — what outlives the process.
    box(S, 740, 500, 500, 80, GOLD)
    text(S, 765, 535, "hive-lucent  —  memory", 20, GOLD)
    text(S, 765, 560, "knowledge graph + vectors. what survives the session.", 13, DIM)
    arrow(S, 990, 445, 990, 498)
    text(S, 1000, 470, "every turn", 12, GREY)

    text(S, 60, 560, "The harness never changes.", 18, GOLD)
    text(S, 60, 588, "Everything around it —", 14, DIM)
    text(S, 60, 610, "surfaces, sessions, memory —", 14, DIM)
    text(S, 60, 632, "is the hive.", 14, DIM)


SLIDES = {1: slide1}

if __name__ == "__main__":
    wanted = [int(a) for a in sys.argv[1:]] or sorted(SLIDES)
    for n in wanted:
        SLIDES[n]()
        print(f"slide {n} drawn")
