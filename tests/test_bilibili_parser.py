from yysls_news.collectors.bilibili.parser import (
    content_to_text,
    extract_new_items,
    matches_filters,
    parse_dynamic,
    to_normalized_content,
)


def _item(dynamic_id: str, dynamic_type: str, text: str, pinned: bool = False) -> dict:
    modules = {
        "module_author": {
            "name": "测试UP",
            "face": "https://i.example/avatar.png",
            "pub_ts": 1720000000,
        },
        "module_dynamic": {"desc": {"text": text}, "major": {}},
    }
    if pinned:
        modules["module_tag"] = {"text": "置顶"}
    return {
        "id_str": dynamic_id,
        "type": dynamic_type,
        "modules": modules,
    }


def test_extract_new_items_stops_at_known_id_and_skips_pinned() -> None:
    raw = {
        "items": [
            _item("new-2", "DYNAMIC_TYPE_WORD", "第二条"),
            _item("pinned", "DYNAMIC_TYPE_WORD", "置顶", pinned=True),
            _item("new-1", "DYNAMIC_TYPE_WORD", "第一条"),
            _item("old", "DYNAMIC_TYPE_WORD", "历史"),
        ]
    }

    items = extract_new_items(raw, last_dynamic_id="old")

    assert [item["id_str"] for item in items] == ["new-2", "new-1"]


def test_parse_video_dynamic_to_normalized_content() -> None:
    item = _item("123", "DYNAMIC_TYPE_AV", "视频动态")
    item["modules"]["module_dynamic"]["major"] = {
        "archive": {
            "title": "新视频",
            "desc": "视频简介",
            "cover": "https://i.example/cover.jpg",
            "jump_url": "https://www.bilibili.com/video/BV1xx",
        }
    }

    model = parse_dynamic(item, 42)
    assert model is not None
    normalized = to_normalized_content(model)

    assert normalized.external_id == "123"
    assert normalized.title == "新视频"
    assert normalized.category == "视频"
    assert normalized.source_url == "https://t.bilibili.com/123"
    assert matches_filters(model, [], [])
    assert not matches_filters(model, ["视频"], [])


def test_parse_structured_desc_text_without_rendering_the_api_dict() -> None:
    item = _item("structured", "DYNAMIC_TYPE_DRAW", "")
    structured_text = {
        "text": "12312",
        "rich_text_nodes": [{"text": "12312", "type": "RICH_TEXT_NODE_TYPE_TEXT"}],
        "paragraphs": [],
        "has_more": False,
    }
    item["modules"]["module_dynamic"]["desc"] = {
        "text": structured_text,
        "rich_text_nodes": [],
        "paragraphs": [],
        "has_more": False,
    }
    item["modules"]["module_dynamic"]["major"] = {
        "draw": {"items": [{"src": "//i.example/legacy-draw.png"}]}
    }

    model = parse_dynamic(item, 42)

    assert model is not None
    assert model.content == "12312"
    assert "rich_text_nodes" not in model.content
    paragraphs = {"paragraphs": [{"text": "第一段"}, {"text": "第二段"}]}
    assert content_to_text(paragraphs) == "第一段\n第二段"
    assert content_to_text({"text": "", "rich_text_nodes": [{"text": "补充正文"}]}) == "补充正文"
    legacy_text = (
        "{'text': '12312', 'rich_text_nodes': [], 'paragraphs': [], 'has_more': False}"
    )
    assert content_to_text(legacy_text) == "12312"


def test_parse_opus_summary_for_filter() -> None:
    item = _item("opus", "DYNAMIC_TYPE_DRAW", "")
    item["modules"]["module_dynamic"].update(
        {
            "major": {
                "opus": {
                    "jump_url": "//www.bilibili.com/opus/opus",
                    "title": "今日分享",
                    "summary": {"text": "[doge] 看看这个"},
                }
            },
        }
    )

    model = parse_dynamic(item, 42)

    assert model is not None
    assert model.title == "今日分享"
    assert model.content == "[doge] 看看这个"
    assert model.source_url == "https://www.bilibili.com/opus/opus"


def test_parse_word_and_article_use_their_type_specific_fallback_fields() -> None:
    word = _item("word", "DYNAMIC_TYPE_WORD", "")
    word["modules"]["module_dynamic"]["major"] = {
        "opus": {
            "summary": {"text": "文字动态正文", "rich_text_nodes": []},
        }
    }
    article = _item("article", "DYNAMIC_TYPE_ARTICLE", "")
    article["modules"]["module_dynamic"]["major"] = {
        "article": {"title": "专栏标题", "desc": "专栏正文"}
    }

    word_model = parse_dynamic(word, 42)
    article_model = parse_dynamic(article, 42)

    assert word_model is not None
    assert word_model.content == "文字动态正文"
    assert article_model is not None
    assert article_model.title == "专栏标题"
    assert article_model.content == "专栏正文"


def test_parse_forward_keeps_outer_comment_and_recursively_parses_original() -> None:
    original = _item("original", "DYNAMIC_TYPE_DRAW", "")
    original["modules"]["module_dynamic"]["major"] = {
        "opus": {
            "title": "原动态标题",
            "summary": {"text": "原动态正文", "rich_text_nodes": []},
            "pics": [{"url": "https://i.example/original.png"}],
        }
    }
    forwarded = _item("forward", "DYNAMIC_TYPE_FORWARD", "转发时写的评论")
    forwarded["orig"] = original

    model = parse_dynamic(forwarded, 42)

    assert model is not None
    assert model.content == "转发时写的评论\n原动态标题\n原动态正文"


def test_parse_unknown_dynamic_type_without_generating_blank_content() -> None:
    assert parse_dynamic(_item("live", "DYNAMIC_TYPE_LIVE_RCMD", ""), 42) is None
