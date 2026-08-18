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
