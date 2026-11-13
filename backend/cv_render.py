import html
from typing import Dict, Any

# Single-column, ATS-friendly layout: real text (no images/tables), standard headings, one font
CSS = """
@page { size: A4; margin: 14mm 16mm 14mm 16mm; }
* { box-sizing: border-box; }
body { font-family: "Carlito", "Liberation Sans", "DejaVu Sans", Arial, sans-serif; font-size: 10.5pt;
       line-height: 1.35; color: #1f2328; margin: 0; }
h1 { font-size: 22pt; margin: 0; letter-spacing: 0.3px; color: #111; }
.headline { font-size: 11.5pt; color: #2b5797; margin-top: 2px; font-weight: bold; }
.contact { font-size: 9.5pt; color: #444; margin-top: 5px; }
.contact span + span::before { content: "  ·  "; color: #999; }
h2 { font-size: 10.5pt; text-transform: uppercase; letter-spacing: 1.2px; color: #2b5797;
     border-bottom: 1px solid #c9d3e3; padding-bottom: 2px; margin: 13px 0 6px; }
.summary { margin: 0; }
.entry { margin-bottom: 7px; page-break-inside: avoid; }
.row { display: flex; justify-content: space-between; gap: 10px; }
.etitle { font-weight: bold; }
.dates { color: #555; white-space: nowrap; font-size: 9.5pt; }
.org { font-style: italic; color: #333; }
.loc { color: #555; font-size: 9.5pt; white-space: nowrap; }
/* Bullets as inline text (not list markers) so ATS text extraction keeps them next to each line */
ul { margin: 3px 0 0; padding-left: 13px; list-style: none; }
li { margin-bottom: 1.5px; }
li::before { content: "• "; margin-left: -10px; }
.skills p { margin: 0 0 2px; }
.skills b { color: #111; }
"""


def _e(v: Any) -> str:
    return html.escape(str(v or ""))


def render_html(cv: Dict[str, Any]) -> str:
    c = cv.get("contact", {}) or {}
    contact = "".join(f"<span>{_e(c.get(k))}</span>" for k in ("email", "phone", "location", "linkedin", "github", "website") if c.get(k))

    out = [f"<h1>{_e(cv.get('name'))}</h1>"]
    if cv.get("headline"):
        out.append(f"<div class='headline'>{_e(cv['headline'])}</div>")
    if contact:
        out.append(f"<div class='contact'>{contact}</div>")
    if cv.get("summary"):
        out.append(f"<h2>Summary</h2><p class='summary'>{_e(cv['summary'])}</p>")

    for s in cv.get("sections", []):
        stype = s.get("type")
        body = []
        if stype == "entries":
            for en in s.get("entries", []):
                bullets = "".join(f"<li>{_e(b)}</li>" for b in en.get("bullets", []) if b)
                body.append(
                    "<div class='entry'>"
                    f"<div class='row'><span class='etitle'>{_e(en.get('title'))}</span><span class='dates'>{_e(en.get('dates'))}</span></div>"
                    + (f"<div class='row'><span class='org'>{_e(en.get('organization'))}</span><span class='loc'>{_e(en.get('location'))}</span></div>"
                       if en.get("organization") or en.get("location") else "")
                    + (f"<ul>{bullets}</ul>" if bullets else "")
                    + "</div>"
                )
        elif stype == "skills":
            rows = "".join(
                f"<p><b>{_e(g.get('name'))}:</b> {_e(', '.join(map(str, g.get('items', []))))}</p>" if g.get("name")
                else f"<p>{_e(', '.join(map(str, g.get('items', []))))}</p>"
                for g in s.get("groups", []) if g.get("items")
            )
            body.append(f"<div class='skills'>{rows}</div>")
        else:
            items = "".join(f"<li>{_e(i)}</li>" for i in s.get("items", []) if i)
            if items:
                body.append(f"<ul>{items}</ul>")
        if body:
            out.append(f"<h2>{_e(s.get('title'))}</h2>" + "".join(body))

    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{''.join(out)}</body></html>"


def render_pdf(cv: Dict[str, Any], path: str) -> str:
    from weasyprint import HTML  # Imported lazily: needs Pango, which is installed in the Docker image
    HTML(string=render_html(cv)).write_pdf(path)
    return path
