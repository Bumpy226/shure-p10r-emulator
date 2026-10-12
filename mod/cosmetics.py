"""Cosmetic changes shared by the three images: boot logo and menu title."""
import sys

# The boot logo is a 73 x 14 bitmap, one row after the other, 10 bytes per
# row, bit 0 of each byte on the left.
LOGO_W, LOGO_H, LOGO_ROW_BYTES = 73, 14, 10
STOCK_LOGO_ROW0 = bytes.fromhex("f87f7fffbfdffff1ff01")

GLYPHS = {
    "B": ["###########..",
          "############.",
          "############.",
          "####....####.",
          "####....####.",
          "###########..",
          "###########..",
          "############.",
          "####....#####",
          "####.....####",
          "####....#####",
          "#############",
          "############.",
          "###########.."],
    "U": ["####.....####"] * 10 + [
          "#####...#####",
          "#############",
          ".###########.",
          "..#########.."],
    "M": ["####.....####",
          "#####...#####",
          "######.######",
          "#############",
          "#############",
          "####.###.####",
          "####..#..####"] + ["####.....####"] * 7,
    "P": ["###########..",
          "############.",
          "#############",
          "####.....####",
          "####.....####",
          "####.....####",
          "#############",
          "############.",
          "###########.."] + ["####........."] * 5,
    "Y": ["####.....####",
          "####.....####",
          "#####...#####",
          ".####...####.",
          ".#####.#####.",
          "..#########..",
          "...#######...",
          "....#####...."] + ["....#####...."] * 6,
}
LOGO_TEXT = "BUMPY"
GLYPH_GAP = 2

MAIN_MENU_OLD = b"MAIN MENU\0\0\0"
MAIN_MENU_NEW = b"MOD LOADED\0\0"


def logo_bitmap():
    rows = []
    for y in range(LOGO_H):
        line = ("." * GLYPH_GAP).join(GLYPHS[c][y] for c in LOGO_TEXT)
        assert len(line) <= LOGO_W, len(line)
        line = line.ljust(LOGO_ROW_BYTES * 8, ".")
        rows.append(bytes(sum((line[i + b] == "#") << b for b in range(8))
                          for i in range(0, len(line), 8)))
    return b"".join(rows)


def patch_logo(img, offset):
    if bytes(img[offset:offset + LOGO_ROW_BYTES]) != STOCK_LOGO_ROW0:
        sys.exit("boot logo not found at 0x%x" % offset)
    img[offset:offset + LOGO_H * LOGO_ROW_BYTES] = logo_bitmap()


def patch_main_menu_title(img, offset):
    """In place: the string has two bytes of padding behind it."""
    if bytes(img[offset:offset + len(MAIN_MENU_OLD)]) != MAIN_MENU_OLD:
        sys.exit("menu title not found at 0x%x" % offset)
    img[offset:offset + len(MAIN_MENU_NEW)] = MAIN_MENU_NEW
