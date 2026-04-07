"""Tests for HOOK.md frontmatter parser."""

from nanobot.agent.hooks.frontmatter import parse_hook_frontmatter


def test_parse_basic():
    content = '---\nname: my-hook\ndescription: "Does a thing"\n---\n\n# My Hook\n'
    result = parse_hook_frontmatter(content)
    assert result["name"] == "my-hook"
    assert result["description"] == "Does a thing"


def test_parse_with_metadata_json():
    content = '---\nname: test\nmetadata: {"events": ["agent:bootstrap"], "export": "handler"}\n---\n'
    result = parse_hook_frontmatter(content)
    assert result["metadata"]["events"] == ["agent:bootstrap"]
    assert result["metadata"]["export"] == "handler"


def test_parse_with_single_quoted_description():
    content = "---\nname: test\ndescription: 'single quoted'\n---\n"
    result = parse_hook_frontmatter(content)
    assert result["description"] == "single quoted"


def test_parse_empty_frontmatter():
    content = "---\n---\n# No frontmatter fields"
    result = parse_hook_frontmatter(content)
    assert result == {}


def test_parse_no_frontmatter():
    content = "# Just a regular markdown file\nNo frontmatter here."
    result = parse_hook_frontmatter(content)
    assert result == {}


def test_parse_invalid_json_metadata():
    content = '---\nname: test\nmetadata: {not json}\n---\n'
    result = parse_hook_frontmatter(content)
    assert result["metadata"] == {}


def test_parse_comments_ignored():
    content = "---\n# comment\nname: test\n---\n"
    result = parse_hook_frontmatter(content)
    assert result["name"] == "test"
    assert len(result) == 1


def test_parse_openclaw_self_improvement():
    """Real HOOK.md from self-improving-agent."""
    content = (
        '---\n'
        'name: self-improvement\n'
        'description: "Injects self-improvement reminder during agent bootstrap"\n'
        'metadata: {"openclaw":{"emoji":"🧠","events":["agent:bootstrap"]}}\n'
        '---\n'
    )
    result = parse_hook_frontmatter(content)
    assert result["name"] == "self-improvement"
    assert result["description"] == "Injects self-improvement reminder during agent bootstrap"
    meta = result["metadata"]
    assert "openclaw" in meta
    assert meta["openclaw"]["events"] == ["agent:bootstrap"]
