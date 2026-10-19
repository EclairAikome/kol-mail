"""First use: the colleague writes their own signature and describes how they negotiate; that description lives as
one section of their playbook file, which the drafts follow."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import onboarding as ob  # noqa: E402

TEMPLATE = "# KOL 谈判手册（模板）\n\n每次都会现读这份文件。\n\n## 一、合作模式\n\n1. 默认先推什么。\n\n## 二、付款\n\n- 发布后才付。\n"


def test_no_preferences_in_a_fresh_template():
    assert ob.get_preferences(TEMPLATE) == ""


def test_preferences_go_before_the_first_section_and_read_back():
    text = ob.set_preferences(TEMPLATE, "先推 CPS，底价 100 美元一条。\n\n不接受预付。")
    assert text.index(ob.PREFS_HEADING) < text.index("## 一、合作模式")
    assert ob.get_preferences(text) == "先推 CPS，底价 100 美元一条。\n\n不接受预付。"
    assert "## 二、付款\n\n- 发布后才付。" in text


def test_editing_preferences_replaces_only_that_section():
    once = ob.set_preferences(TEMPLATE, "旧的打法")
    twice = ob.set_preferences(once, "新的打法")
    assert ob.get_preferences(twice) == "新的打法" and "旧的打法" not in twice
    assert twice.count(ob.PREFS_HEADING) == 1 and twice.endswith("- 发布后才付。\n")


def test_empty_preferences_remove_the_section():
    assert ob.set_preferences(ob.set_preferences(TEMPLATE, "x"), "  ") == TEMPLATE


def test_a_heading_typed_by_the_user_cannot_split_the_playbook():
    text = ob.set_preferences(TEMPLATE, "先推 CPS\n## 六、付款\n随便付")
    assert ob.get_preferences(text) == "先推 CPS\n### 六、付款\n随便付"
    assert text.count("\n## ") == TEMPLATE.count("\n## ") + 1


def test_playbook_without_sections_gets_the_preferences_appended():
    assert ob.get_preferences(ob.set_preferences("只有一段说明", "我的打法")) == "我的打法"


def test_hand_edited_headings_are_still_found():
    spaced = TEMPLATE.replace("## 一、合作模式", "## 我的谈判方式  \n\n旧打法\n\n## 一、合作模式")
    assert ob.get_preferences(spaced) == "旧打法"
    assert ob.set_preferences(spaced, "新打法").count("我的谈判方式") == 1
    assert ob.get_preferences("﻿## 我的谈判方式\n\n带 BOM\n\n## 二、付款\n") == "带 BOM"
    assert ob.get_preferences("# 手册\r\n\r\n## 我的谈判方式\r\n\r\nCRLF\r\n") == "CRLF"


def test_duplicate_sections_collapse_into_one():
    twice = "# 手册\n\n## 我的谈判方式\n\n甲\n\n## 一、A\n\nx\n\n## 我的谈判方式\n\n乙\n"
    assert ob.get_preferences(twice) == "甲\n\n乙"
    once = ob.set_preferences(twice, "丙")
    assert once.count("## 我的谈判方式") == 1 and ob.get_preferences(once) == "丙" and "## 一、A\n\nx" in once


def test_a_top_level_heading_after_the_section_is_kept():
    text = "# 手册\n\n## 我的谈判方式\n\n甲\n\n# 第二部分\n\nkeep me\n"
    assert ob.get_preferences(text) == "甲"
    assert "# 第二部分\n\nkeep me" in ob.set_preferences(text, "乙")


def test_a_similar_heading_is_another_section():
    text = "# 手册\n\n## 我的谈判方式二\n\n别动\n"
    assert ob.get_preferences(text) == "" and "别动" in ob.set_preferences(text, "x")


def test_signature_is_cleaned():
    assert ob.clean_signature("  Eclair \r\n Marketing | LootBar  \n\n\n") == "Eclair\nMarketing | LootBar"
    assert ob.clean_signature("") == ""
