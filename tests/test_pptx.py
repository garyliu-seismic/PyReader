import io
import pytest

pptx = pytest.importorskip("pptx")
from pptx import Presentation
from pptx.util import Inches

from pyreader.pptx import load_pptx

NOTE = "SECRET-SPEAKER-NOTE"


def _png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 6), (255, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


def build(path):
    prs = Presentation()
    # 1: 标题 + 项目符号（含二级）+ 备注
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Intro"
    tf = s.placeholders[1].text_frame
    tf.text = "first point"
    p = tf.add_paragraph()
    p.text = "nested point"
    p.level = 1
    s.notes_slide.notes_text_frame.text = NOTE
    # 2: 表格
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Data"
    tbl = s.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    for r, row in enumerate([["a", "b"], ["c", "d"]]):
        for c, v in enumerate(row):
            tbl.cell(r, c).text = v
    # 3: 空幻灯片（无标题）
    prs.slides.add_slide(prs.slide_layouts[6])
    # 4: 分组形状 + 阅读顺序（下方的先加入）+ 图片
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Shapes"
    s.shapes.add_textbox(Inches(1), Inches(5), Inches(2), Inches(1)).text_frame.text = "bottom"
    g = s.shapes.add_group_shape()
    g.shapes.add_textbox(Inches(1), Inches(3), Inches(2), Inches(1)).text_frame.text = "in group"
    s.shapes.add_textbox(Inches(1), Inches(2), Inches(2), Inches(1)).text_frame.text = "top"
    s.shapes.add_textbox(Inches(1), Inches(6), Inches(2), Inches(1)).text_frame.text = ""
    s.shapes.add_picture(io.BytesIO(_png()), Inches(1), Inches(1.5))
    prs.save(str(path))
    return str(path)


@pytest.fixture
def deck(tmp_path):
    return load_pptx(build(tmp_path / "t.pptx"))


def test_chapters(deck):
    text, chapters, title, extra = deck
    assert [t for _, t in chapters] == ["Intro", "Data", "Slide 3", "Shapes"]
    for off, t in chapters:
        assert text[off:off + len(t)] == t


def test_block_starts_match_text(deck):
    text, _, _, extra = deck
    for b in extra["blocks"]:
        if b["type"] in ("heading", "para", "list"):
            s = "".join(p[0] for p in b["pieces"])
            assert text[b["start"]:b["start"] + len(s)] == s
            for t, _bits, a, e in b["pieces"]:
                assert text[a:e] == t
        elif b["type"] == "table":
            for row in b["cells"]:
                for c, off in row:
                    assert text[off:off + len(c)] == c


def test_bullets_and_levels(deck):
    lists = [b for b in deck[3]["blocks"] if b["type"] == "list"]
    assert [(b["pieces"][0][0], b["depth"]) for b in lists] == [
        ("first point", 0), ("nested point", 1)]


def test_table(deck):
    tbls = [b for b in deck[3]["blocks"] if b["type"] == "table"]
    assert len(tbls) == 1
    assert [[c for c, _ in r] for r in tbls[0]["cells"]] == [["a", "b"], ["c", "d"]]


def test_empty_slide_has_fallback_heading_only(deck):
    blocks = deck[3]["blocks"]
    i = next(i for i, b in enumerate(blocks)
             if b["type"] == "heading" and b["pieces"][0][0] == "Slide 3")
    assert blocks[i + 1]["type"] == "heading"   # 下一块直接是下一张的标题


def test_reading_order_group_and_empty_skipped(deck):
    text = deck[0]
    # 标题 → 顶部图片区域 → top → 组内 → bottom（空文本框被跳过）
    assert text.index("Shapes") < text.index("top") < text.index("in group") < text.index("bottom")
    shapes_blocks = deck[3]["blocks"][-5:]
    assert [b["type"] for b in shapes_blocks] == ["heading", "image", "para", "para", "para"]


def test_image(deck):
    imgs = [b for b in deck[3]["blocks"] if b["type"] == "image"]
    assert len(imgs) == 1
    b = imgs[0]
    assert deck[3]["images"][b["img_id"]][:4] == b"\x89PNG"
    assert (b["img_w"], b["img_h"]) == (8, 6)


def test_notes_not_extracted(deck):
    assert NOTE not in deck[0]
    assert NOTE not in str(deck[3]["blocks"])


def test_loader_dispatch(tmp_path):
    pytest.importorskip("PySide6")
    from pyreader.loader import BookLoader
    assert ".pptx" in BookLoader._FULL_PARSE_EXTS
    got = {}
    bl = BookLoader(build(tmp_path / "d.pptx"))
    bl.loaded.connect(lambda *a: got.update(r=a))
    bl.failed.connect(lambda m: got.update(err=m))
    bl.run()
    assert "err" not in got, got.get("err")
    text, chapters, _title, extra = got["r"]
    assert [t for _, t in chapters][0] == "Intro"
    assert extra["blocks"] and NOTE not in text
