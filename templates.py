from dataclasses import dataclass
from pathlib import Path
import random
import re

@dataclass(frozen=True)
class Template:
    template_id: str
    kind: str
    text: str

def load_templates(path: str):
    templates = []
    gen_n = per_n = 0
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("GEN "):
            gen_n += 1
            templates.append(Template(f"GEN_{gen_n:03d}", "GEN", line[4:].strip()))
        elif line.startswith("PER "):
            per_n += 1
            templates.append(Template(f"PER_{per_n:03d}", "PER", line[4:].strip()))
    return templates

def fill_template(template: Template, first_name=None, city=None, location=None):
    text = template.text
    if "_____" in text:
        if not first_name:
            return None
        text = text.replace("_____", first_name)
    for marker, value in {"{{city}}": city, "{{location}}": location}.items():
        if marker in text:
            if not value:
                return None
            text = text.replace(marker, str(value))
    if re.search(r"\{\{[^}]+\}\}|_____", text):
        return None
    return text

def pick_random_filled(templates, kind, first_name=None, city=None, location=None):
    """Shuffle candidates of `kind` and return the first (template, filled_text) that has
    all its placeholders satisfied, so message wording varies across leads instead of always
    picking the first line in the file. Returns (None, None) if none fit."""
    candidates = [t for t in templates if t.kind == kind]
    random.shuffle(candidates)
    for t in candidates:
        filled = fill_template(t, first_name=first_name, city=city, location=location)
        if filled:
            return t, filled
    return None, None
