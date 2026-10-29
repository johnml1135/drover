"""Import owner questions."""
import json
import re

from .ledger import ops
from .importer_common import ACTOR, _index, sha

def parse_questions(text):
    """Parse the grilling format: '## Qn. title', '**Body.**', '**Options.**' bullets, '**Recommended.**'."""
    out = []
    for block in re.split(r"(?m)^## ", text)[1:]:
        head, _, rest = block.partition("\n")
        title = re.sub(r"^Q\d+\.\s*", "", head).strip()
        body = re.search(r"\*\*Body\.\*\*\s*(.*?)(?=\n\*\*Options\.\*\*|\Z)", rest, re.S)
        opts = re.search(r"\*\*Options\.\*\*\s*(.*?)(?=\n\*\*Recommended\.\*\*|\Z)", rest, re.S)
        rec = re.search(r"(?:\*\*Recommended\.\*\*|(?m:^)Recommended:)[ \t]*(.*)", rest, re.S)
        options = [{"letter": m.group(1), "label": f"{m.group(1)}. {m.group(2).strip()}"}
                   for m in re.finditer(r"(?m)^- ([A-Z])\.[ \t]+(.*)$", opts.group(1) if opts else "")]
        letters = {o["letter"] for o in options}
        first = re.match(r"\(?([A-Z])(?![A-Za-z])", rec.group(1)) if rec else None  # explicit, first token only
        pick = first.group(1) if first and first.group(1) in letters else None
        for o in options:
            o["recommended"] = o.pop("letter") == pick
        out.append({"title": title, "body": body.group(1).strip() if body else "", "options": options,
                    "recommended_text": rec.group(1).strip() if rec else ""})
    return out


def _upsert_question(conn, idx, tally, key, epic_id, title, body, options, allow_text=True):
    old = idx.get(key)
    if old:
        qid = old["data"]["question"]
        q = ops.get_question(conn, qid)
        opts = [{"label": o["label"], "description": o.get("description", ""), "recommended": bool(o.get("recommended"))}
                for o in options]
        if q["status"] == "open" and (q["title"], q["body"], q["options"]) != (title, body, opts):
            conn.execute("UPDATE questions SET title=?, body=?, options=? WHERE id=?",
                         (title, body, json.dumps(opts, separators=(",", ":"), sort_keys=True), qid))
            ops.event(conn, "import-question", ACTOR, {"source_hash": key, "question": qid, "title": title}, epic_id=epic_id)
            tally.add("question", "updated")
        else:
            tally.add("question", "unchanged")
        return qid
    qid = ops.ask_question(conn, "epic", epic_id, title, body, options, allow_text, ACTOR)
    ops.event(conn, "import-question", ACTOR, {"source_hash": key, "question": qid, "title": title}, epic_id=epic_id)
    tally.add("question", "created")
    return qid


def import_questions(conn, text, import_epic, tally):
    idx = _index(conn, "import-question")
    for q in parse_questions(text):
        if not q["title"]:
            tally.warn("a question has no title; skipped")
            continue
        body = q["body"] + (f"\n\nRecommended: {q['recommended_text']}" if q["recommended_text"] else "")
        _upsert_question(conn, idx, tally, sha("question", q["title"]), import_epic, q["title"], body, q["options"])
