#!/usr/bin/env python3
"""The hive intro deck. Re-runnable: each slide clears itself before drawing.

    python3 decks/hive_intro.py [slide ...]
"""
import pathlib
import sys
HERE = pathlib.Path(__file__).resolve().parent

from deck import (DIM, GOLD, GREY, TEAL, arrow, box, clear_slide, dot, edge,
                  image, line, node, ring, text)


def slide1():
    """The mind alone: a CLI agent with an HTTP server bolted to it."""
    S = 1
    clear_slide(S)

    text(S, 60, 62, "First: wrap the harness in a web API", 30, GOLD)
    text(S, 60, 92, "one python process — an HTTP server that spawns the coding CLI "
                    "and streams it back", 15, DIM)

    # Any client at all. The point is that it is no longer a keyboard.
    box(S, 60, 250, 190, 90, GREY, 1)
    text(S, 80, 285, "any client", 20, GREY)
    text(S, 80, 308, "phone, browser,", 13, DIM)
    text(S, 80, 328, "another mind", 13, DIM)
    arrow(S, 250, 295, 328, 295)

    text(S, 330, 158, "THE MIND — one python process", 13, GREY)
    box(S, 330, 175, 640, 320, TEAL)

    box(S, 360, 205, 580, 130, TEAL, 1)
    text(S, 380, 240, "mind_server  —  FastAPI", 22, TEAL)
    # Route and gloss are separate elements: SVG collapses runs of spaces, so
    # a padded single string does not stay in columns.
    for y, route, gloss in ((268, "POST /sessions", "spawn a conversation"),
                            (291, "WS  /attach-pty", "a real terminal, in a browser tab"),
                            (314, "DELETE /sessions/{id}", "end it")):
        text(S, 380, y, route, 13, GOLD)
        text(S, 610, y, gloss, 13, DIM)

    arrow(S, 650, 335, 650, 363, GREY, 1, 7)

    box(S, 360, 365, 580, 105, GOLD, 1)
    text(S, 380, 400, "claude  /  codex   subprocess", 22, GOLD)
    text(S, 380, 427, "the unmodified CLI — stream-json for chat,", 13, DIM)
    text(S, 380, 449, "a pty when you want to watch it work", 13, DIM)

    text(S, 60, 560, "No fork of the agent. No re-implementation of its API.", 18, GOLD)
    text(S, 60, 590, "The same binary you type into by hand — just reachable over a socket.", 14, DIM)


def slide2():
    S = 2
    clear_slide(S)

    text(S, 60, 62, "Then: hang surfaces off it", 30, GOLD)
    text(S, 60, 92, "one conversation, reachable from a phone, a browser tab or a "
                    "team channel — and remembering across all of them", 15, DIM)

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
    box(S, 400, 175, 250, 270, GOLD)
    text(S, 420, 208, "the nervous system", 19, GOLD)
    text(S, 420, 243, "one conversation,", 14, DIM)
    text(S, 420, 264, "many front doors", 14, DIM)
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
    text(S, 765, 535, "long-term memory", 20, GOLD)
    text(S, 765, 560, "knowledge graph + vectors. what survives the session.", 13, DIM)
    arrow(S, 990, 445, 990, 498)
    text(S, 1000, 470, "every turn", 12, GREY)

    text(S, 60, 560, "The harness never changes.", 18, GOLD)
    text(S, 60, 588, "Everything around it —", 14, DIM)
    text(S, 60, 610, "surfaces, sessions, memory —", 14, DIM)
    text(S, 60, 632, "is the hive.", 14, DIM)


def slide3():
    """What survives the session: three kinds of memory, used differently."""
    S = 3
    clear_slide(S)

    text(S, 60, 62, "Memory is what survives the session", 30, GOLD)
    text(S, 60, 92, "the conversation is disposable — three stores underneath it are not", 15, DIM)

    # ── The graph ─────────────────────────────────────────────────────────
    box(S, 50, 145, 400, 330, GOLD)
    text(S, 72, 182, "the graph", 22, GOLD)
    text(S, 72, 206, "things, and how they relate", 13, GREY)

    daniel = node(S, 150, 268, "Daniel", 36, TEAL, 13, "person")
    aiden  = node(S, 370, 268, "Aiden", 34, TEAL, 13, "person")
    skippy = node(S, 130, 390, "Skippy", 34, GOLD, 13, "mind")
    zack   = node(S, 380, 390, "Zack", 32, GOLD, 13, "mind")

    edge(S, daniel, aiden, "PARENT_OF")
    edge(S, daniel, skippy, "OPERATES")
    edge(S, aiden, zack, "OPERATES")
    edge(S, skippy, zack, "TALKS_TO")

    text(S, 72, 445, "you traverse it: one hop, exactly, no guessing", 13, DIM)

    # ── Semantic memory: a pull ───────────────────────────────────────────
    box(S, 470, 145, 410, 330, TEAL)
    text(S, 492, 182, "semantic memory", 22, TEAL)
    text(S, 492, 206, "a pull — distance decides", 13, GREY)

    cx, cy = 675, 330
    ring(S, cx, cy, 96, GREY, 1)           # the relevance threshold
    node(S, cx, cy, "this turn", 26, TEAL, 11)

    # Inside the ring: carried into the prompt, unasked for.
    for dx, dy, label, lx in ((-56, -50, "prefers a PR", -96),
                              (56, -50, "green & silver", 2),
                              (0, 62, "the backlog path", -46)):
        dot(S, cx + dx, cy + dy, 5, TEAL)
        text(S, cx + lx, cy + dy + 20, label, 11, TEAL)

    # Outside it: everything else, still there, not relevant now.
    for dx, dy in ((-146, -66), (138, 60), (152, -26), (-132, 62),
                   (-50, -114), (100, -108), (-162, 4), (30, 116)):
        dot(S, cx + dx, cy + dy, 4, GREY)

    text(S, 492, 458, "thousands more sit outside it, unread", 12, GREY)

    # ── Standing rules: a push ────────────────────────────────────────────
    box(S, 900, 145, 310, 330, GOLD)
    text(S, 922, 182, "standing rules", 22, GOLD)
    text(S, 922, 206, "a push — no turn is exempt", 13, GREY)

    box(S, 922, 222, 262, 96, GOLD, 1)
    text(S, 938, 249, "· defer the restart", 12, GOLD)
    text(S, 938, 272, "· one question per turn", 12, GOLD)
    text(S, 938, 295, "· never speak markdown", 12, GOLD)

    arrow(S, 1053, 322, 1053, 344, GOLD, 1, 6)
    text(S, 1070, 338, "stamped onto", 11, GREY)

    for i, label in enumerate(("turn 1", "turn 400", "turn 9,001")):
        y = 350 + i * 34
        box(S, 922, y, 140, 26, GREY, 1)
        text(S, 936, y + 18, label, 12, GREY)

    text(S, 922, 470, "identical, every time", 13, GOLD)

    # ── Where it comes from ───────────────────────────────────────────────
    text(S, 60, 540, "The session writes into all three as it goes.", 18, GOLD)
    text(S, 60, 572, "A hook fires on every completed turn: worth keeping goes in, the "
                     "rest is dropped. When the", 14, DIM)
    text(S, 60, 594, "conversation is rotated out, nothing is lost that mattered — "
                     "which is what makes a", 14, DIM)
    text(S, 60, 616, "disposable session survivable.", 14, DIM)


def slide4():
    """Agent-to-agent: my representative talks to yours."""
    S = 4
    clear_slide(S)

    text(S, 60, 62, "My agent talks to your agent", 30, GOLD)
    text(S, 60, 92, "two people who never opened a chat window — the conversation happens "
                    "between the agents that represent them", 15, DIM)

    # ── Daniel's bubble ───────────────────────────────────────────────────
    box(S, 50, 150, 490, 330, TEAL)
    text(S, 75, 185, "DANIEL", 13, GREY)
    text(S, 75, 220, "the human", 20, TEAL)
    text(S, 75, 243, "asks in plain language, walks away", 13, DIM)

    box(S, 72, 265, 445, 185, TEAL, 1)
    text(S, 95, 302, "Skippy", 24, TEAL)
    text(S, 95, 332, "· knows his projects, his history, his preferences", 13, DIM)
    text(S, 95, 355, "· speaks with his authority, inside limits he set", 13, DIM)
    text(S, 95, 378, "· holds his memory — which never leaves this bubble", 13, DIM)
    text(S, 95, 415, "represents Daniel", 14, TEAL)
    text(S, 95, 437, "when Daniel is not in the room", 13, GREY)

    # ── The other party's bubble ──────────────────────────────────────────
    box(S, 720, 150, 490, 330, GOLD)
    text(S, 735, 185, "SOMEONE ELSE  —  a son, a household, a business", 13, GREY)
    text(S, 735, 220, "Aiden", 20, GOLD)
    text(S, 735, 243, "his own machine, his own rules", 13, DIM)

    box(S, 742, 265, 445, 185, GOLD, 1)
    text(S, 755, 302, "Zack", 24, GOLD)
    text(S, 755, 332, "· knows Aiden's box, his schedule, his voice", 13, DIM)
    text(S, 755, 355, "· answers for him when he is not there", 13, DIM)
    text(S, 755, 378, "· holds his memory — which never leaves that bubble", 13, DIM)
    text(S, 755, 415, "represents Aiden", 14, GOLD)
    text(S, 755, 437, "and answers to Aiden, not to me", 13, GREY)

    # ── The seam is the protocol, so draw the protocol ────────────────────
    box(S, 560, 150, 140, 330, GOLD, 1)
    text(S, 578, 182, "THE", 15, GOLD)
    text(S, 578, 203, "NERVOUS", 15, GOLD)
    text(S, 578, 224, "SYSTEM", 15, GOLD)
    text(S, 578, 252, "one protocol,", 12, GREY)
    text(S, 578, 270, "spoken by both", 12, GREY)

    arrow(S, 552, 320, 708, 320)
    text(S, 578, 308, "a request", 13, GREY)
    arrow(S, 708, 380, 552, 380)
    text(S, 578, 403, "an answer", 13, GREY)

    text(S, 578, 440, "neither agent", 12, GREY)
    text(S, 578, 458, "reaches past it", 12, GREY)

    # ── The point ─────────────────────────────────────────────────────────
    text(S, 60, 545, "What crosses the seam is a message. Nothing else.", 18, GOLD)
    text(S, 60, 578, "Not my memory, not my credentials, not my transcript — his agent "
                     "never reads my context,", 14, DIM)
    text(S, 60, 600, "and mine never reads his. Two representatives, each accountable "
                     "to a different person.", 14, DIM)


def slide5():
    """The photo, and nothing else. It does not need a caption."""
    S = 5
    clear_slide(S)
    image(S, 400, 20, 450, 600, str(HERE / "assets" / "666.jpg"), max_px=900)


def slide6():
    """The security dashboard, live off the sensor host. Same rule as slide 5:
    the picture is the argument, so nothing is written over it."""
    S = 6
    clear_slide(S)
    image(S, 60, 40, 1080, 750, str(HERE / "assets" / "grafana-dashboard.png"),
          max_px=1600, quality=80)


def slide7():
    """The agent is inside the site — and the slide proves it."""
    S = 7
    clear_slide(S)

    text(S, 60, 62, "The agent lives inside the site", 30, GOLD)
    text(S, 60, 92, "not a chat bubble bolted onto a page — a mind whose workspace "
                    "is the page itself", 15, DIM)

    # ── A browser window ──────────────────────────────────────────────────
    box(S, 60, 145, 700, 400, GOLD)
    box(S, 60, 145, 700, 36, GOLD, 1)
    text(S, 80, 169, "hive.sparktobloom.com", 14, DIM)

    # Its own navigation.
    for i, (label, x) in enumerate((("graph", 90), ("memory", 200), ("rules", 320),
                                    ("canvas", 420), ("settings", 545), ("minds", 680))):
        text(S, x, 212, label, 13, GOLD if label == "canvas" else GREY)
    line(S, 415, 222, 490, 222, GOLD, 2)

    # The page currently open is this one.
    box(S, 90, 245, 640, 275, TEAL, 1)
    text(S, 110, 275, "the canvas — currently showing slide 7 of 7", 15, TEAL)

    # A little deck inside the deck.
    box(S, 130, 300, 170, 90, GREY, 1)
    box(S, 330, 300, 170, 90, GREY, 1)
    box(S, 530, 300, 170, 90, GOLD, 1)
    text(S, 150, 335, "the mind", 13, GREY)
    text(S, 350, 335, "the surfaces", 13, GREY)
    text(S, 550, 335, "you are here", 13, GOLD)
    text(S, 130, 425, "\u25c0", 20, GOLD)
    text(S, 690, 425, "\u25b6", 20, GOLD)
    text(S, 300, 425, "clicked by a human, drawn by an agent", 13, DIM)

    text(S, 110, 495, "the agent writes here through the site's own API — "
                      "same door a person uses", 12, GREY)

    # ── What that buys ────────────────────────────────────────────────────
    text(S, 800, 175, "SO IT CAN", 13, GREY)
    items = [
        ("generate the content", "draws into the page instead of describing what to draw", 215),
        ("change the site itself", "this slideshow did not exist three hours ago", 300),
        ("operate the pages", "reads and writes the graph, the memory, the settings", 385),
        ("hold a conversation here", "the page is a surface, like a phone or a chat channel", 470),
    ]
    for headline, detail, y in items:
        text(S, 800, y, headline, 17, GOLD)
        text(S, 800, y + 24, detail, 13, DIM)
        arrow(S, 790, y - 6, 764, y - 6, GREY, 1, 6)

    # ── The closer ────────────────────────────────────────────────────────
    text(S, 60, 590, "You are not looking at a diagram of this. "
                     "You are looking at it.", 18, GOLD)
    text(S, 60, 618, "This slide was drawn by the agent, on the page it is drawn on, "
                     "while we were talking.", 14, DIM)


SLIDES = {1: slide1, 2: slide2, 3: slide3, 4: slide4, 5: slide5,
          6: slide6, 7: slide7}

if __name__ == "__main__":
    wanted = [int(a) for a in sys.argv[1:]] or sorted(SLIDES)
    for n in wanted:
        SLIDES[n]()
        print(f"slide {n} drawn")
