"""The console's few custom components. Colours and fonts live in .streamlit/config.toml; this CSS only covers what
Streamlit has no native element for (the lifecycle stepper, status tiles, outcome banner), using its own class names."""
from __future__ import annotations

from dataclasses import dataclass
from html import escape

import streamlit as st

from .activity import STATE_LABELS, Outcome, Stage

CSS = """
<style>
.stMainBlockContainer { padding-top: 1.75rem; padding-bottom: 3rem; }

/* Readable active states on the dark surface. If Streamlit renames these, the theme's primary colour still applies. */
[data-testid="stTab"][aria-selected="true"], [data-testid="stTab"][aria-selected="true"] * { color: #7FD3CF; }
button[data-variant="pills"][data-selected="true"], button[data-variant="pills"][data-selected="true"] * { color: #7FD3CF; }

/* Activity feed: the event's tone colours its icon. */
[class*="-tone-success-"] [data-testid="stIconMaterial"] { color: #3FB58F; }
[class*="-tone-warning-"] [data-testid="stIconMaterial"] { color: #D9A441; }
[class*="-tone-danger-"] [data-testid="stIconMaterial"] { color: #E5645B; }
[class*="-tone-info-"] [data-testid="stIconMaterial"] { color: #5EC4C0; }

.ah-hero { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: .75rem 1.5rem; margin-bottom: .25rem; }
.ah-hero h1 { font-size: 1.75rem; font-weight: 650; letter-spacing: -.015em; margin: 0; padding: 0; line-height: 1.2; color: #E6EBF2; }
.ah-hero p { margin: .25rem 0 0; color: #A9B5C6; font-size: .95rem; }
.ah-tag { display: inline-block; font-size: .75rem; font-weight: 600; letter-spacing: .02em; padding: .25rem .6rem; border-radius: 6px; border: 1px solid #2E5E66; color: #7FD3CF; background: #10232C; white-space: nowrap; }

.ah-tiles { display: grid; gap: .625rem; margin: .25rem 0 .375rem; }
.ah-tiles.primary { grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); }
.ah-tiles.secondary { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
.ah-tile { background: #131C2E; border: 1px solid #25324A; border-radius: 8px; padding: .75rem .9rem; min-width: 0; border-left: 3px solid #25324A; }
.ah-tile .l { font-size: .75rem; font-weight: 500; color: #A9B5C6; }
.ah-tile .v { font-size: 1.15rem; font-weight: 600; color: #E6EBF2; margin-top: .15rem; overflow-wrap: anywhere; line-height: 1.3; }
.ah-tiles.secondary .v { font-size: 1.15rem; }
.ah-tiles.secondary .ah-tile { padding: .5rem .9rem; }
.ah-tile .c { font-size: .78rem; color: #A9B5C6; margin-top: .15rem; overflow-wrap: anywhere; }
.ah-tile.success { border-left-color: #3FB58F; }
.ah-tile.warning { border-left-color: #D9A441; }
.ah-tile.danger { border-left-color: #E5645B; }
.ah-tile.info { border-left-color: #17A398; }

.ah-stepper { display: flex; flex-wrap: wrap; gap: 0; margin: .5rem 0; background: #131C2E; border: 1px solid #25324A; border-radius: 8px; padding: .9rem .4rem .75rem; }
.ah-step { flex: 1 1 130px; min-width: 120px; position: relative; padding: 0 .55rem; text-align: left; }
.ah-step .dot { width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 700; border: 2px solid #3A4963; color: #A9B5C6; background: #0B1220; position: relative; z-index: 1; }
.ah-step:not(:last-child)::after { content: ""; position: absolute; top: 10px; left: calc(.55rem + 26px); right: 4px; height: 2px; background: #25324A; }
.ah-step .n { font-size: .86rem; font-weight: 600; color: #E6EBF2; margin-top: .4rem; }
.ah-step .s { font-size: .75rem; font-weight: 500; color: #A9B5C6; }
.ah-step .d { font-size: .74rem; color: #A9B5C6; margin-top: .1rem; overflow-wrap: anywhere; }
.ah-step.completed .dot { background: #17A398; border-color: #17A398; color: #06201F; }
.ah-step.completed .s { color: #5EC4C0; }
.ah-step.active .dot { border-color: #5EC4C0; color: #5EC4C0; }
.ah-step.active .s { color: #5EC4C0; }
.ah-step.failed .dot { background: #E5645B; border-color: #E5645B; color: #2A0B09; }
.ah-step.failed .s { color: #F0918A; }
.ah-step.blocked .dot, .ah-step.escalated .dot { background: #D9A441; border-color: #D9A441; color: #2A1E05; }
.ah-step.blocked .s, .ah-step.escalated .s { color: #E5BE6E; }
.ah-step.skipped .n, .ah-step.pending .n { color: #A9B5C6; font-weight: 500; }

.ah-banner { border: 1px solid #25324A; border-left-width: 4px; background: #131C2E; border-radius: 8px; padding: .8rem 1rem; margin: .5rem 0; }
.ah-banner .t { font-size: 1.05rem; font-weight: 650; color: #E6EBF2; }
.ah-banner .e { font-size: .88rem; color: #C7D0DD; margin-top: .15rem; overflow-wrap: anywhere; }
.ah-banner.success { border-left-color: #3FB58F; }
.ah-banner.info { border-left-color: #17A398; }
.ah-banner.warning { border-left-color: #D9A441; }
.ah-banner.danger { border-left-color: #E5645B; }

.ah-empty { padding: .25rem .25rem .5rem; }
.ah-empty .t { font-size: 1.05rem; font-weight: 600; color: #E6EBF2; }
.ah-empty p { font-size: .9rem; color: #A9B5C6; margin: .25rem 0 .75rem; }
.ah-empty ol { margin: 0; padding-left: 1.2rem; color: #C7D0DD; font-size: .9rem; }
.ah-empty li { margin-bottom: .3rem; }
</style>
"""

_STATE_GLYPH = {"completed": "✓", "active": "•", "failed": "×", "blocked": "!", "escalated": "!",
                "skipped": "–", "pending": ""}


@dataclass(frozen=True)
class Tile:
    label: str
    value: str
    tone: str = ""
    caption: str = ""


def inject_css() -> None:
    st.html(CSS)


def hero(title: str, subtitle: str, tag: str = "Simulation Environment") -> None:
    st.html(f'<div class="ah-hero"><div><h1>{escape(title)}</h1><p>{escape(subtitle)}</p></div>'
            f'<span class="ah-tag">{escape(tag)}</span></div>')


def tiles(items: list[Tile], variant: str = "primary") -> None:
    cards = "".join(f'<div class="ah-tile {escape(t.tone)}"><div class="l">{escape(t.label)}</div>'
                    f'<div class="v">{escape(str(t.value))}</div>'
                    + (f'<div class="c">{escape(t.caption)}</div>' if t.caption else "") + "</div>" for t in items)
    st.html(f'<div class="ah-tiles {escape(variant)}">{cards}</div>')


def stepper(stages: list[Stage]) -> None:
    parts = []
    for s in stages:
        detail = f'<div class="d">{escape(s.detail)}</div>' if s.detail else ""
        parts.append(f'<div class="ah-step {s.state}" role="listitem"><div class="dot" aria-hidden="true">'
                     f'{_STATE_GLYPH[s.state]}</div><div class="n">{escape(s.name)}</div>'
                     f'<div class="s">{STATE_LABELS[s.state]}</div>{detail}</div>')
    st.html(f'<div class="ah-stepper" role="list" aria-label="Incident lifecycle">{"".join(parts)}</div>')


def banner(outcome: Outcome) -> None:
    st.html(f'<div class="ah-banner {outcome.tone}" role="status"><div class="t">{escape(outcome.label)}</div>'
            f'<div class="e">{escape(outcome.explanation)}</div></div>')


def empty_state(title: str, body: str, steps: list[str] | None = None) -> None:
    items = "".join(f"<li>{escape(s)}</li>" for s in steps or [])
    ordered = f"<ol>{items}</ol>" if items else ""
    st.html(f'<div class="ah-empty"><div class="t">{escape(title)}</div><p>{escape(body)}</p>{ordered}</div>')
