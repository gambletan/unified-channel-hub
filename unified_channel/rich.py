"""RichReply — platform-agnostic rich messages that auto-degrade to plain text."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .types import Button, OutboundMessage


class SectionType(Enum):
    TEXT = "text"
    TABLE = "table"
    BUTTONS = "buttons"
    IMAGE = "image"
    CODE = "code"
    DIVIDER = "divider"


@dataclass
class Section:
    type: SectionType
    # Payload varies by type
    text: str = ""
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    buttons: list[list[Button]] = field(default_factory=list)
    url: str = ""
    alt: str = ""
    code: str = ""
    language: str = ""


class RichReply:
    """Platform-agnostic rich message that auto-degrades to plain text.

    Fluent API — every add_* method returns self for chaining.
    """

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.sections: list[Section] = []
        if text:
            self.sections.append(Section(type=SectionType.TEXT, text=text))

    def add_text(self, text: str) -> RichReply:
        """Append a text section."""
        self.sections.append(Section(type=SectionType.TEXT, text=text))
        return self

    def add_table(
        self, headers: list[str], rows: list[list[str]]
    ) -> RichReply:
        """Append a table section."""
        self.sections.append(
            Section(type=SectionType.TABLE, headers=headers, rows=rows)
        )
        return self

    def add_buttons(self, buttons: list[list[Button]]) -> RichReply:
        """Append a button grid."""
        self.sections.append(
            Section(type=SectionType.BUTTONS, buttons=buttons)
        )
        return self

    def add_image(self, url: str, alt: str = "") -> RichReply:
        """Append an image."""
        self.sections.append(
            Section(type=SectionType.IMAGE, url=url, alt=alt)
        )
        return self

    def add_code(self, code: str, language: str = "") -> RichReply:
        """Append a code block."""
        self.sections.append(
            Section(type=SectionType.CODE, code=code, language=language)
        )
        return self

    def add_divider(self) -> RichReply:
        """Append a visual divider."""
        self.sections.append(Section(type=SectionType.DIVIDER))
        return self

    # ── Renderers ──────────────────────────────────────────────

    def to_plain_text(self) -> str:
        """Render everything as plain text (fallback for any platform)."""
        parts: list[str] = []
        for s in self.sections:
            if s.type == SectionType.TEXT:
                parts.append(s.text)
            elif s.type == SectionType.TABLE:
                parts.append(self._render_table_plain(s.headers, s.rows))
            elif s.type == SectionType.BUTTONS:
                for row in s.buttons:
                    parts.append(
                        " | ".join(
                            f"[{b.label}]({b.url})" if b.url else f"[{b.label}]"
                            for b in row
                        )
                    )
            elif s.type == SectionType.IMAGE:
                parts.append(f"[Image: {s.alt or s.url}]")
            elif s.type == SectionType.CODE:
                lang = s.language
                parts.append(f"```{lang}\n{s.code}\n```")
            elif s.type == SectionType.DIVIDER:
                parts.append("---")
        return "\n\n".join(parts)

    def to_telegram(self) -> dict[str, Any]:
        """Render for Telegram (Markdown + inline_keyboard)."""
        text_parts: list[str] = []
        inline_keyboard: list[list[dict[str, str]]] = []

        for s in self.sections:
            if s.type == SectionType.TEXT:
                text_parts.append(s.text)
            elif s.type == SectionType.TABLE:
                text_parts.append(
                    f"```\n{self._render_table_plain(s.headers, s.rows)}\n```"
                )
            elif s.type == SectionType.BUTTONS:
                for row in s.buttons:
                    kb_row: list[dict[str, str]] = []
                    for b in row:
                        btn: dict[str, str] = {"text": b.label}
                        if b.url:
                            btn["url"] = b.url
                        elif b.callback_data:
                            btn["callback_data"] = b.callback_data
                        kb_row.append(btn)
                    inline_keyboard.append(kb_row)
            elif s.type == SectionType.IMAGE:
                text_parts.append(f"[{s.alt or 'Image'}]({s.url})")
            elif s.type == SectionType.CODE:
                lang = s.language
                text_parts.append(f"```{lang}\n{s.code}\n```")
            elif s.type == SectionType.DIVIDER:
                text_parts.append("---")

        result: dict[str, Any] = {
            "text": "\n\n".join(text_parts),
            "parse_mode": "Markdown",
        }
        if inline_keyboard:
            result["reply_markup"] = {"inline_keyboard": inline_keyboard}
        return result

    def to_discord(self) -> dict[str, Any]:
        """Render for Discord (embeds + components)."""
        description_parts: list[str] = []
        components: list[dict[str, Any]] = []

        for s in self.sections:
            if s.type == SectionType.TEXT:
                description_parts.append(s.text)
            elif s.type == SectionType.TABLE:
                description_parts.append(
                    f"```\n{self._render_table_plain(s.headers, s.rows)}\n```"
                )
            elif s.type == SectionType.BUTTONS:
                for row in s.buttons:
                    action_row: dict[str, Any] = {
                        "type": 1,
                        "components": [],
                    }
                    for b in row:
                        if b.url:
                            action_row["components"].append(
                                {
                                    "type": 2,
                                    "style": 5,
                                    "label": b.label,
                                    "url": b.url,
                                }
                            )
                        else:
                            action_row["components"].append(
                                {
                                    "type": 2,
                                    "style": 1,
                                    "label": b.label,
                                    "custom_id": b.callback_data or b.label,
                                }
                            )
                    components.append(action_row)
            elif s.type == SectionType.IMAGE:
                description_parts.append(s.url)
            elif s.type == SectionType.CODE:
                lang = s.language
                description_parts.append(f"```{lang}\n{s.code}\n```")
            elif s.type == SectionType.DIVIDER:
                description_parts.append("---")

        result: dict[str, Any] = {
            "embeds": [{"description": "\n\n".join(description_parts)}],
        }
        if components:
            result["components"] = components
        return result

    def to_slack(self) -> dict[str, Any]:
        """Render for Slack (blocks)."""
        blocks: list[dict[str, Any]] = []

        for s in self.sections:
            if s.type == SectionType.TEXT:
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": s.text},
                    }
                )
            elif s.type == SectionType.TABLE:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"```{self._render_table_plain(s.headers, s.rows)}```",
                        },
                    }
                )
            elif s.type == SectionType.BUTTONS:
                elements: list[dict[str, Any]] = []
                for row in s.buttons:
                    for b in row:
                        if b.url:
                            elements.append(
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": b.label,
                                    },
                                    "url": b.url,
                                }
                            )
                        else:
                            elements.append(
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": b.label,
                                    },
                                    "action_id": b.callback_data or b.label,
                                }
                            )
                blocks.append({"type": "actions", "elements": elements})
            elif s.type == SectionType.IMAGE:
                blocks.append(
                    {
                        "type": "image",
                        "image_url": s.url,
                        "alt_text": s.alt or "image",
                    }
                )
            elif s.type == SectionType.CODE:
                lang = s.language
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"```{s.code}```",
                        },
                    }
                )
            elif s.type == SectionType.DIVIDER:
                blocks.append({"type": "divider"})

        return {"blocks": blocks}

    def to_outbound(self, channel: str) -> OutboundMessage:
        """Auto-select the best format for the given channel."""
        if channel == "telegram":
            tg = self.to_telegram()
            return OutboundMessage(
                chat_id="",
                text=tg["text"],
                parse_mode=tg.get("parse_mode"),
                buttons=self._extract_buttons(),
                metadata={"_rich": tg},
            )
        elif channel == "discord":
            dc = self.to_discord()
            return OutboundMessage(
                chat_id="",
                text=dc["embeds"][0]["description"] if dc["embeds"] else "",
                metadata={"_rich": dc},
            )
        elif channel == "slack":
            sl = self.to_slack()
            return OutboundMessage(
                chat_id="",
                text=self.to_plain_text(),
                metadata={"_rich": sl},
            )
        else:
            # Fallback to plain text for unknown channels
            return OutboundMessage(
                chat_id="",
                text=self.to_plain_text(),
            )

    # ── Helpers ────────────────────────────────────────────────

    def _extract_buttons(self) -> list[list[Button]] | None:
        """Collect all button sections into a flat button grid."""
        all_buttons: list[list[Button]] = []
        for s in self.sections:
            if s.type == SectionType.BUTTONS:
                all_buttons.extend(s.buttons)
        return all_buttons or None

    @staticmethod
    def _render_table_plain(
        headers: list[str], rows: list[list[str]]
    ) -> str:
        """Render an ASCII table."""
        if not headers and not rows:
            return ""
        all_rows = [headers, *rows] if headers else rows
        col_widths = [
            max(len(str(cell)) for cell in col) for col in zip(*all_rows)
        ]
        lines: list[str] = []
        if headers:
            lines.append(
                " | ".join(
                    str(h).ljust(w) for h, w in zip(headers, col_widths)
                )
            )
            lines.append("-+-".join("-" * w for w in col_widths))
        for row in rows:
            lines.append(
                " | ".join(
                    str(c).ljust(w) for c, w in zip(row, col_widths)
                )
            )
        return "\n".join(lines)
