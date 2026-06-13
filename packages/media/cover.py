"""Cover image domain logic: frame extraction selection + AI cover prompt build.

The frame-based cover (extract a representative thumbnail) is the default,
free, non-fabricated cover. The AI cover prompt builder here is a faithful port
of the origin ``AICoverService._build_cover_prompt`` (digital-human-Cutagent
``app/services/ai_cover_service.py``): it renders the seeded ``prompt.cover.ai_cover``
template (the calibration) with the same style/source-frame guidance branches.

This module is provider-agnostic and side-effect free: it only assembles the
text prompt. The actual paid image-generation call lives in the
``openai.image`` provider plugin and is gated in the cover node.
"""

from __future__ import annotations

from dataclasses import dataclass

# Default size mirrors the origin publish-cover target (vertical 3:4-ish 2:3).
DEFAULT_COVER_SIZE = "1024x1536"
_TITLE_MAX_CHARS = 32
_SUBTITLE_MAX_CHARS = 32
_DESCRIPTION_MAX_CHARS = 900
_TAG_MAX = 6

# Faithful copy of the origin DEFAULT_AI_COVER_PROMPT_TEMPLATE, used only when the
# seeded ``prompt.cover.ai_cover`` template is unavailable.
DEFAULT_AI_COVER_PROMPT_TEMPLATE = """Create one polished vertical Chinese short-video cover image at {size}.
{style_reference}
{source_frame_reference}

Main headline text must be readable Chinese text exactly:
"{title}"
{subtitle_instruction}

Video/case context:
- Case/brand: {case_name}
- Publish copy summary: {description}
- Tags: {tags}

Design requirements:
- Commercial short-video cover for automotive paint repair / local service publishing.
- Strong first-glance hierarchy, clear headline, high contrast, mobile-thumbnail readability.
- Use the selected video frame as the image2/source subject: keep the real person, car, shop, product, or scene recognizable when present, while improving composition and cover polish.
- The final saved cover will be center-cropped to 3:4, so keep the main subject and all text inside the central safe area.
- Keep all visible Chinese text short and legible. Do not add watermarks, QR codes, platform logos, fake account names, or real brand logos.
- Avoid cluttered paragraphs; use at most one supporting phrase if useful.
- The final image must be a single cover, not a collage, not a UI screenshot, not a mockup sheet, and not a split reference board.{prompt_extra}"""


@dataclass(frozen=True)
class CoverPromptInputs:
    title: str
    description: str = ""
    tags: tuple[str, ...] = ()
    case_name: str | None = None
    subtitle: str | None = None
    prompt_extra: str | None = None
    has_source_frame: bool = False
    has_template: bool = False
    style_guidance: str | None = None
    size: str = DEFAULT_COVER_SIZE


def build_cover_prompt(inputs: CoverPromptInputs, *, template: str | None = None) -> str:
    """Render the AI cover prompt. ``template`` is the seeded prompt content;
    falls back to the origin default template when omitted."""
    clean_title = (inputs.title or "").strip()[:_TITLE_MAX_CHARS] or "精彩案例"
    clean_subtitle = (inputs.subtitle or "").strip()[:_SUBTITLE_MAX_CHARS]
    clean_description = (inputs.description or "").strip()
    if len(clean_description) > _DESCRIPTION_MAX_CHARS:
        clean_description = clean_description[:_DESCRIPTION_MAX_CHARS] + "..."
    tag_line = "、".join([tag.strip("# ") for tag in inputs.tags if tag.strip("# ")][:_TAG_MAX])

    variables = {
        "size": inputs.size,
        "style_reference": _style_reference(inputs),
        "source_frame_reference": _source_frame_reference(inputs),
        "title": clean_title,
        "subtitle": clean_subtitle,
        "subtitle_instruction": _subtitle_instruction(clean_subtitle),
        "case_name": inputs.case_name or "未指定",
        "description": clean_description or "无",
        "tags": tag_line or "无",
        "prompt_extra": _prompt_extra(inputs.prompt_extra),
    }
    return _render_template(template or DEFAULT_AI_COVER_PROMPT_TEMPLATE, variables)


def _style_reference(inputs: CoverPromptInputs) -> str:
    if inputs.style_guidance:
        return (
            "Use the selected reference image as a style guide. Apply its layout logic, "
            "typography hierarchy, color mood, spacing, and commercial cover feel, but do "
            "not copy old wording or logos.\nReference image style guide:\n"
            f"{inputs.style_guidance}"
        )
    if inputs.has_template:
        return (
            "Use the provided reference image as a style and layout template: preserve its "
            "overall composition, typography hierarchy, color mood, spacing, and commercial "
            "cover feel, but do not copy any old text verbatim."
        )
    return "Create a clean, high-conversion Chinese short-video cover layout from scratch."


def _source_frame_reference(inputs: CoverPromptInputs) -> str:
    if inputs.has_source_frame and inputs.has_template:
        return (
            "The combined edit reference image is a two-panel board: left panel is the style "
            "reference cover, right panel is the selected video frame/image2. Use the right "
            "panel as the primary subject and scene basis, and use the left panel only for "
            "visual style."
        )
    if inputs.has_source_frame:
        return (
            "The provided edit reference image is the selected video frame/image2. Use it as "
            "the primary subject and scene basis, not merely as style."
        )
    return "No video frame source was provided; infer a suitable automotive local-service cover subject from the context."


def _subtitle_instruction(clean_subtitle: str) -> str:
    if clean_subtitle:
        return f'Supporting text, if used, must be readable Chinese text exactly:\n"{clean_subtitle}"'
    return "Do not invent extra Chinese copy unless it clearly improves the cover."


def _prompt_extra(prompt_extra: str | None) -> str:
    if prompt_extra and prompt_extra.strip():
        return f"\nAdditional direction: {prompt_extra.strip()}"
    return ""


def _render_template(template: str, variables: dict[str, str]) -> str:
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace("{" + key + "}", str(value))
        rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered
