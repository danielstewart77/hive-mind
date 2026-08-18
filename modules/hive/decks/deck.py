"""Push slides onto the hive canvas. Boxes, labels and arrows in one place."""
import json, os, subprocess, urllib.request

TOKEN = subprocess.run(["docker","exec","hive-site","printenv","COMMS_BEARER_TOKEN"],
                       capture_output=True, text=True).stdout.strip()
BASE = "http://127.0.0.1:8710"

GOLD, DIM, TEAL, GREY = "#c9a84c", "#8a7a44", "#2dd4bf", "#7d8590"

def push(msg):
    req = urllib.request.Request(f"{BASE}/canvas/push", method="POST",
        data=json.dumps(msg).encode(),
        headers={"Content-Type":"application/json","Authorization":f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)

def clear_slide(n):
    push({"type":"clear","slide":n})

def text(slide, x, y, s, size=15, color=GOLD):
    push({"type":"text","slide":slide,"x":x,"y":y,"content":s,"size":size,"color":color})

def box(slide, x, y, w, h, color=GOLD, sw=2):
    d = f"M{x},{y} L{x+w},{y} L{x+w},{y+h} L{x},{y+h} L{x},{y}"
    push({"type":"path","slide":slide,"d":d,"color":color,"sw":sw})

def line(slide, x1, y1, x2, y2, color=GREY, sw=2):
    push({"type":"path","slide":slide,"d":f"M{x1},{y1} L{x2},{y2}","color":color,"sw":sw})

def arrow(slide, x1, y1, x2, y2, color=GREY, sw=2, head=9):
    line(slide, x1, y1, x2, y2, color, sw)
    import math
    a = math.atan2(y2-y1, x2-x1)
    for off in (2.6, -2.6):
        hx, hy = x2 + head*math.cos(a+off), y2 + head*math.sin(a+off)
        line(slide, x2, y2, round(hx,1), round(hy,1), color, sw)


def _centered_x(cx, s, size):
    return round(cx - 0.6 * size * len(s) / 2, 1)


def node(slide, cx, cy, label, r=34, color=GOLD, size=13, sub=None):
    """A graph node: a ring with its label centred inside it."""
    import math
    pts = [(cx + r * math.cos(2 * math.pi * i / 20), cy + r * math.sin(2 * math.pi * i / 20))
           for i in range(21)]
    d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    push({"type": "path", "slide": slide, "d": d, "color": color, "sw": 2})
    text(slide, _centered_x(cx, label, size), cy + (0 if sub is None else -3), label, size, color)
    if sub:
        text(slide, _centered_x(cx, sub, 10), cy + 13, sub, 10, GREY)
    return (cx, cy, r)


def edge(slide, a, b, label, color=GREY, size=11):
    """A directed edge between two nodes, trimmed to their rings and labelled."""
    import math
    (ax, ay, ar), (bx, by, br) = a, b
    ang = math.atan2(by - ay, bx - ax)
    x1, y1 = ax + (ar + 3) * math.cos(ang), ay + (ar + 3) * math.sin(ang)
    x2, y2 = bx - (br + 6) * math.cos(ang), by - (br + 6) * math.sin(ang)
    arrow(slide, round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1), color, 1, 7)
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    # Nudge the label off the line, on whichever side keeps it clear.
    off = -8 if abs(math.sin(ang)) < 0.5 else 0
    text(slide, _centered_x(mx, label, size), round(my + off, 1), label, size, color)
