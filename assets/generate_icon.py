#!/usr/bin/env python3
"""
Generates icon.png for Shack Services: a dark panel (matching DN-AUTO
Monitor's style) with a grid of status-light dots -- mostly green, one
red -- evoking a server rack / status board at a glance. Run once to
(re)produce assets/icon.png.

    python3 assets/generate_icon.py
"""

import os

from PIL import Image, ImageDraw, ImageFilter

SIZE = 256
BEZEL = (40, 46, 52, 255)
BG = (18, 22, 26, 255)
GREEN = (57, 255, 106, 255)
GREEN_DIM = (24, 90, 48, 255)
RED = (255, 77, 77, 255)
TEAL = (40, 224, 224, 255)


def main():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = 8
    draw.rounded_rectangle((margin, margin, SIZE - margin, SIZE - margin), radius=40, fill=BEZEL)
    inset = 20
    draw.rounded_rectangle((inset, inset, SIZE - inset, SIZE - inset), radius=32, fill=BG)

    # top accent bar, like a small title bar on a status panel
    draw.rounded_rectangle((40, 44, SIZE - 40, 56), radius=4, fill=TEAL)

    # 3x3 grid of status dots -- 8 green, 1 red (bottom-right), like a
    # dashboard mostly healthy with one thing needing attention
    rows, cols = 3, 3
    grid_top = 76
    grid_bottom = 208
    grid_left = 56
    grid_right = 200
    cell_w = (grid_right - grid_left) / (cols - 1)
    cell_h = (grid_bottom - grid_top) / (rows - 1)
    r = 16

    red_cell = (2, 2)  # bottom-right

    for row in range(rows):
        for col in range(cols):
            cx = grid_left + col * cell_w
            cy = grid_top + row * cell_h
            is_red = (row, col) == red_cell
            color = RED if is_red else GREEN
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)

    # soft glow behind the red dot to draw the eye, same treatment as the
    # amp monitor icon's needle glow
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    rcx = grid_left + red_cell[1] * cell_w
    rcy = grid_top + red_cell[0] * cell_h
    gdraw.ellipse((rcx - 22, rcy - 22, rcx + 22, rcy + 22), fill=(*RED[:3], 130))
    glow = glow.filter(ImageFilter.GaussianBlur(10))
    img = Image.alpha_composite(glow, img)

    # redraw the dots on top of the glow so they stay crisp
    draw = ImageDraw.Draw(img)
    for row in range(rows):
        for col in range(cols):
            cx = grid_left + col * cell_w
            cy = grid_top + row * cell_h
            is_red = (row, col) == red_cell
            color = RED if is_red else GREEN
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "icon.png")
    img.save(out_path)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
