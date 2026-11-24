"""Render the walkthrough onto an independent copy of the output framebuffer."""
import os
from functools import lru_cache

from PIL import ImageDraw, ImageFont

from live_screen import fit, jpeg


@lru_cache
def font(size):
    path = os.environ.get('WINDOWS_CONSOLE_DEMO_FONT',
                          '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        raise RuntimeError('Install fonts-noto-cjk or set WINDOWS_CONSOLE_DEMO_FONT') from None


def wrap(text, face, width, max_lines):
    lines = []
    for paragraph in text.split('\n'):
        line = ''
        for char in paragraph:
            if line and face.getlength(line + char) > width:
                lines.append(line)
                if len(lines) == max_lines:
                    return lines[:-1] + [lines[-1][:-1] + '…']
                line = ''
            line += char
        lines.append(line)
        if len(lines) == max_lines:
            break
    return lines


def demo_jpeg(canvas, max_width, step, remaining):
    if not step or remaining <= 0:
        return jpeg(canvas, max_width)
    image = fit(canvas, max_width).copy()
    draw = ImageDraw.Draw(image)
    w, h = image.size
    size = min(20, max(11, w // 64))
    padding = max(5, size // 2)
    line_height = size + 6

    def position(x, y):
        return round((w - 1) * x / 32767), round((h - 1) * y / 32767)

    if step['text'] and w > 2 * padding and h >= line_height + 2 * padding:
        face = font(size)
        lines = wrap(step['text'], face, min(34 * size, w - 2 * padding),
                     (h - 2 * padding) // line_height)
        width = min(w, round(max(face.getlength(line) for line in lines)) + 2 * padding)
        height = len(lines) * line_height + 2 * padding
        x, y = position(step['x'], step['y'])
        x, y = min(x, w - width), min(y, h - height)
        draw.rounded_rectangle((x, y, x + width - 1, y + height - 1), radius=6,
                               fill='#17221f', outline='#708f85')
        for index, line in enumerate(lines):
            draw.text((x + padding, y + padding + index * line_height), line,
                      font=face, fill='#f3f8f6', anchor='lt')
        track_width = max(0, width - 6)
        length = round(track_width * min(1, remaining / (step['duration_ms'] / 1000)))
        track_y = y + height - 3
        if track_width:
            draw.line((x + 3, track_y, x + 3 + track_width, track_y), fill='#394b45', width=2)
        if length:
            draw.line((x + 3, track_y, x + 3 + length, track_y), fill='#a4dfce', width=2)

    px, py = position(*step['pointer'])
    radius = max(6, size * 3 // 4)
    draw.ellipse((px-radius, py-radius, px+radius, py+radius), outline='#bbf3df', width=2)
    sx = -1 if w - px < radius else 1
    sy = -1 if h - py < radius else 1
    draw.polygon([(px + sx * x * radius // 12, py + sy * y * radius // 12)
                  for x, y in [(0, 0), (12, 7), (7, 8), (10, 12), (8, 14), (5, 9), (2, 12)]],
                 fill='#defff4')
    return jpeg(image, max_width)
