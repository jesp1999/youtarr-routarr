"""Structural accessibility rules (WCAG 2.2 AA) that can be checked without a browser."""
from html.parser import HTMLParser

import pytest

from test_youtarr_routarr import add_video, by_id, cfg, client, conn, file_one  # noqa: F401
from youtarr_routarr.filing import Classification

A, B, C = "AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Page(HTMLParser):
    """Collects elements with their ancestors and text, skipping <template> contents."""

    def __init__(self, markup: str):
        super().__init__()
        self.elements, self.stack = [], []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        element = {"tag": tag, "attrs": dict(attrs), "up": list(self.stack), "text": ""}
        if not any(e["tag"] == "template" for e in self.stack):
            self.elements.append(element)
        if tag not in VOID:
            self.stack.append(element)

    def handle_endtag(self, tag):
        while self.stack and self.stack.pop()["tag"] != tag:
            pass

    def handle_data(self, data):
        for element in self.stack:
            element["text"] += data

    def all(self, tag):
        return [e for e in self.elements if e["tag"] == tag]

    def ids(self):
        return [e["attrs"]["id"] for e in self.elements if "id" in e["attrs"]]


@pytest.fixture
def pages(cfg, conn):
    """Every screen and panel state, as parsed pages."""
    add_video(cfg, "Chan", "One", A)
    add_video(cfg, "Chan", "Two", B)
    gone = add_video(cfg, "Other", "Filed", C)
    file_one(cfg, conn, by_id(cfg)[C], Classification("movie", "Filed", year=2020))
    gone.unlink()
    web = client(cfg)
    paths = ["/", *(f"/?item={A}&type={t}" for t in ("show", "movie", "video", "music")), f"/?ids={A},{B}",
             f"/?filed={C}", "/filed", "/settings"]
    return {path: Page(web.get(path).get_data(as_text=True)) for path in paths}


def test_every_form_control_has_a_label(pages):
    for path, page in pages.items():
        labelled = {e["attrs"]["for"] for e in page.all("label") if "for" in e["attrs"]}
        for control in page.all("input") + page.all("select") + page.all("textarea"):
            attrs = control["attrs"]
            if attrs.get("type") == "hidden":
                continue
            named = ("aria-label" in attrs or attrs.get("id") in labelled
                     or any(e["tag"] == "label" for e in control["up"]))
            assert named, f"{path}: unlabelled {control['tag']} {attrs.get('name')}"
        for target in labelled:
            assert target in page.ids(), f"{path}: label points at missing id {target}"


def test_ids_are_unique(pages):
    for path, page in pages.items():
        ids = page.ids()
        assert len(ids) == len(set(ids)), f"{path}: {sorted(i for i in ids if ids.count(i) > 1)}"


def test_each_page_has_one_main_heading_and_no_skipped_levels(pages):
    for path, page in pages.items():
        levels = [int(e["tag"][1]) for e in page.elements if e["tag"] in ("h1", "h2", "h3", "h4", "h5", "h6")]
        assert levels.count(1) == 1 and levels[0] == 1, f"{path}: {levels}"
        assert all(later - earlier <= 1 for earlier, later in zip(levels, levels[1:])), f"{path}: {levels}"


def test_pages_have_distinct_titles(pages):
    titles = {path: page.all("title")[0]["text"] for path, page in pages.items()}
    assert titles["/"] == "Queue · Routarr" and titles["/filed"] == "History · Routarr"
    assert titles["/settings"] == "Settings & health · Routarr"


def test_skip_links_come_first_and_have_targets(pages):
    for path, page in pages.items():
        first = next(e for e in page.elements if e["tag"] in ("a", "button", "input"))
        assert first["attrs"].get("href") == "#main", f"{path}: first focusable is {first['attrs']}"
        for link in [e for e in page.all("a") if e["attrs"].get("href", "").startswith("#")]:
            assert link["attrs"]["href"][1:] in page.ids(), f"{path}: {link['attrs']['href']}"
    selected = pages[f"/?item={A}&type=show"]
    assert any(e["attrs"].get("href") == "#panel" for e in selected.all("a"))
    assert not any(e["attrs"].get("href") == "#panel" for e in pages["/"].all("a"))


def test_ticking_a_box_is_explained_first(pages):
    page = pages["/"]
    boxes = [e for e in page.all("input") if e["attrs"].get("name") == "ids"]
    assert boxes and all(e["attrs"].get("aria-describedby") == "select-help" for e in boxes)
    assert "Ticking a box" in next(e for e in page.elements if e["attrs"].get("id") == "select-help")["text"]


def test_repeated_controls_say_which_item_they_act_on(pages):
    bulk = pages[f"/?ids={A},{B}"]
    names = [e["attrs"]["aria-label"] for e in bulk.all("input") if e["attrs"].get("name", "").startswith(
        ("episode-", "title-", "special-"))]
    assert len(names) == 6 and len(set(names)) == 6
    # This filing's source is gone, so the reason is read out with its title instead of a hover tip.
    assert "Filed: The source copy is gone" in pages["/filed"].all("td")[-1]["text"]


def test_history_buttons_name_their_item(cfg, conn):
    add_video(cfg, "Chan", "Kept", A)
    file_one(cfg, conn, by_id(cfg)[A], Classification("movie", "Kept", year=2020))
    page = Page(client(cfg).get("/filed").get_data(as_text=True))
    names = [e["attrs"].get("aria-label") for e in page.all("button")]
    assert names == ["Undo Kept", "Reclassify Kept"]


def test_tables_have_headers(pages):
    for path in ("/filed", "/settings", f"/?ids={A},{B}"):
        for header in pages[path].all("th"):
            assert header["text"].strip(), f"{path}: empty header cell"
    assert pages["/settings"].all("th"), "the settings table needs headers"


def test_scrolling_regions_can_be_reached_by_keyboard(pages):
    for path, page in pages.items():
        for element in page.elements:
            classes = element["attrs"].get("class", "").split()
            if "preview" in classes or "scroll" in classes:
                attrs = element["attrs"]
                assert attrs.get("tabindex") == "0" and attrs.get("role") == "region" and attrs.get("aria-label"), \
                    f"{path}: {classes}"


def test_the_message_stays_until_dismissed(pages):
    page = pages[f"/?filed={C}"]
    assert any(e["text"].strip() == "Dismiss" for e in page.all("button"))
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / "build" / "youtarr_routarr" / "static" / "app.js").read_text()
    assert "5000" not in script
