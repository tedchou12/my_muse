from __future__ import annotations

from app.agent import Agent
from app.uploads import save_upload, user_content


def test_text_document_is_extracted_for_the_model(settings):
    attachment = save_upload(
        settings.uploads_dir,
        "notes.txt",
        b"Project notes: ship the upload picker.",
    )
    assert attachment["kind"] == "document"
    content = user_content("Please summarise this.", [attachment], settings.uploads_dir)
    assert "Please summarise this." in content
    assert "Project notes: ship the upload picker." in content
    assert "[Attached document: notes.txt]" in content


def test_image_attachment_becomes_a_vision_block(settings):
    png = b"\x89PNG\r\n\x1a\n" + b"fake-image-body"
    attachment = save_upload(settings.uploads_dir, "photo.png", png)
    assert attachment["kind"] == "image"
    content = user_content("What is this?", [attachment], settings.uploads_dir)
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "What is this?"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_agent_history_rebuilds_attachment_content(app):
    services = app.state.services
    attachment = save_upload(
        services.settings.uploads_dir,
        "brief.txt",
        b"The attached brief says: test the upload flow.",
    )
    agent = Agent(services)
    agent.record_user(
        "attachment-history",
        "Summarise this.",
        [attachment],
    )
    history = agent.history("attachment-history")
    assert history
    content = history[-1]["content"]
    assert "The attached brief says: test the upload flow." in content
