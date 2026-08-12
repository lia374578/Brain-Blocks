#!/usr/bin/env python3
"""Build Brain Blocks (Navi) game skills as JSON, in the same shape the editor
exports/imports: { skill: { name, version, robot: "navi", description, category,
tags, compatibleRobots, publishTo, blocks: { blocks: <Blockly state>,
variables: [...] } } }.

Only blocks available in the editor palette / vendored Blockly are used, with the
exact serialization shapes the editor produces (verified against
"Guess-the-number-voice.json" and the vendored blockly 9.3.3 sources):
  - statements: inputs.{DO,DO0,..,ELSE} = {"block": <first of stack>}, next-chains
  - values:     inputs.X = {"block": ...}   (default pills are real blocks, not shadows — shadows render pale & aren't draggable)
  - controls_if extra branches: extraState {"elseIfCount": N, "hasElse": true}
  - text_join / lists_create_with: extraState {"itemCount": N}, inputs ADD0..
  - lists_getIndex: input name "VALUE" (list) + "AT" (1-based, FROM_START)
  - lists_indexOf: "VALUE" + "FIND", END=FIRST — with oneBasedIndex: true the
    generator emits `list.indexOf(x) + 1`, so 0 means "not found"
  - lists_split: MODE=SPLIT, inputs "INPUT" (text) + "DELIM" (string)
  - lists_length: input "VALUE"
  - contains-check: logic_compare NEQ(text_indexOf(END=FIRST, VALUE, FIND), 0)
    (editor runs oneBasedIndex: true -> generator emits indexOf+1, so NEQ 0
    correctly means "found", including at position 0; matches the guess game)

Voice matching strategy (games are kid-facing; STT is noisy):
  - Every heard utterance is split into words (lists_split on " ") and each word
    is matched by EXACT equality against curated matcher lists (lists_indexOf),
    so "pigeon" never matches "pig" and "I don't know" never matches "no".
  - Multi-word utterances score every valid word ("horse donkey pony" = 3 points).
  - Plural/irregular forms are explicit matchers that map to one canonical name
    for repeat detection ("MICE"/"MOUSE" both score MOUSE; saying both = repeat).

Usage: python3 tools/build_games.py   (writes the 3 .json files in the repo root)
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Block-builder helpers (mirror Blockly 9.3 serialization JSON)
# ---------------------------------------------------------------------------

class Builder:
    def __init__(self):
        self.vars = []          # list of Var
        self._seen = set()

    def var(self, name):
        """Register (once) and return a variable reference {id} for fields."""
        if name not in self._seen:
            self._seen.add(name)
            self.vars.append({"name": name, "id": "id_" + name.replace(" ", "_")})
        return {"id": "id_" + name.replace(" ", "_")}

    # ---- leaf value blocks -----------------------------------------------
    def num(self, n):
        return {"type": "math_number", "fields": {"NUM": n}}

    def text(self, s):
        return {"type": "text", "fields": {"TEXT": s}}

    def boolean(self, v):
        return {"type": "logic_boolean", "fields": {"BOOL": "TRUE" if v else "FALSE"}}

    def blk(self, btype, fields=None, inputs=None, extra=None):
        b = {"type": btype}
        if fields:
            b["fields"] = fields
        if inputs:
            b["inputs"] = inputs
        if extra:
            b["extraState"] = extra
        return b

    # ---- input wrappers ----------------------------------------------------
    # Default value pills are real blocks, NOT Blockly shadows: shadows render
    # pale and can't be dragged out of their input (the editor also converts
    # any shadow it sees on import). sval is kept for call-site readability
    # (default value vs a user-visible connected block) but emits the same
    # {"block": ...} shape as bval.
    def sval(self, body):
        return {"block": body}

    def bval(self, body):
        return {"block": body}

    def stmt(self, body):
        return {"block": body}

    # ---- variables ----------------------------------------------------------
    def vget(self, name):
        return self.blk("variables_get", fields={"VAR": self.var(name)})

    def vset(self, name, value_body, is_shadow=False):
        return self.blk("variables_set", fields={"VAR": self.var(name)}, inputs={
            "VALUE": self.sval(value_body) if is_shadow else self.bval(value_body),
        })

    # ---- logic / text --------------------------------------------------------
    def contains(self, value_body, find_text):
        """True if value contains find_text (substring, whole-utterance)."""
        return self.contains_v(value_body, self.text(find_text))

    def contains_v(self, value_body, find_body):
        """True if value contains the computed value find_body."""
        return self.blk("logic_compare", fields={"OP": "NEQ"}, inputs={
            "A": self.bval(self.blk("text_indexOf", fields={"END": "FIRST"}, inputs={
                "VALUE": self.bval(value_body),
                "FIND": self.bval(find_body),
            })),
            "B": self.sval(self.num(0)),
        })

    def eq_num(self, a_body, n):
        return self.blk("logic_compare", fields={"OP": "EQ"}, inputs={
            "A": self.bval(a_body), "B": self.sval(self.num(n)),
        })

    def neq_num(self, a_body, n):
        return self.blk("logic_compare", fields={"OP": "NEQ"}, inputs={
            "A": self.bval(a_body), "B": self.sval(self.num(n)),
        })

    def eq_text(self, a_body, s):
        return self.blk("logic_compare", fields={"OP": "EQ"}, inputs={
            "A": self.bval(a_body), "B": self.sval(self.text(s)),
        })

    def lt(self, a_body, b_body):
        return self.blk("logic_compare", fields={"OP": "LT"}, inputs={
            "A": self.bval(a_body), "B": self.bval(b_body),
        })

    def lte(self, a_body, b_body):
        return self.blk("logic_compare", fields={"OP": "LTE"}, inputs={
            "A": self.bval(a_body), "B": self.bval(b_body),
        })

    def gte(self, a_body, b_body):
        return self.blk("logic_compare", fields={"OP": "GTE"}, inputs={
            "A": self.bval(a_body), "B": self.bval(b_body),
        })

    def logic_or(self, items):
        acc = items[0]
        for it in items[1:]:
            acc = self.blk("logic_operation", fields={"OP": "OR"}, inputs={
                "A": self.bval(acc), "B": self.bval(it),
            })
        return acc

    def logic_and(self, items):
        acc = items[0]
        for it in items[1:]:
            acc = self.blk("logic_operation", fields={"OP": "AND"}, inputs={
                "A": self.bval(acc), "B": self.bval(it),
            })
        return acc

    def logic_not(self, body):
        return self.blk("logic_negate", inputs={"BOOL": self.bval(body)})

    def uppercase(self, body):
        return self.blk("text_changeCase", fields={"CASE": "UPPERCASE"}, inputs={
            "TEXT": self.bval(body),
        })

    def join(self, items):
        """text_join of value bodies (each already a block dict)."""
        inputs = {}
        for i, it in enumerate(items):
            is_shadow = isinstance(it, dict) and it.get("type") and it["type"] in ("text", "math_number", "logic_boolean")
            inputs["ADD%d" % i] = self.sval(it) if is_shadow else self.bval(it)
        return self.blk("text_join", extra={"itemCount": len(items)}, inputs=inputs)

    def add(self, a_body, b_body, b_is_shadow=False):
        return self.blk("math_arithmetic", fields={"OP": "ADD"}, inputs={
            "A": self.bval(a_body),
            "B": self.sval(b_body) if b_is_shadow else self.bval(b_body),
        })

    # ---- lists ---------------------------------------------------------------
    def list_of(self, items):
        return self.blk("lists_create_with", extra={"itemCount": len(items)}, inputs={
            "ADD%d" % i: self.sval(self.text(s)) for i, s in enumerate(items)
        })

    def list_get(self, list_body, at_body):
        return self.blk("lists_getIndex", fields={"MODE": "GET", "WHERE": "FROM_START"}, inputs={
            "VALUE": self.bval(list_body), "AT": self.bval(at_body),
        })

    def split(self, body, delim):
        """Split a text value into a list of words on delim."""
        return self.blk("lists_split", fields={"MODE": "SPLIT"}, inputs={
            "INPUT": self.bval(body),
            "DELIM": self.sval(self.text(delim)),
        })

    def length_of(self, body):
        return self.blk("lists_length", inputs={"VALUE": self.bval(body)})

    def index_of(self, list_body, find_body):
        """1-based index of find_body in list_body; 0 when absent (oneBasedIndex)."""
        return self.blk("lists_indexOf", fields={"END": "FIRST"}, inputs={
            "VALUE": self.bval(list_body),
            "FIND": self.bval(find_body),
        })

    # ---- zsibot blocks -------------------------------------------------------
    def speak(self, text_body):
        return self.blk("zsibot_speak", fields={"VOICE": ""}, inputs={
            "TEXT": self.bval(text_body),
        })

    def speak_text(self, s):
        return self.speak(self.text(s))

    def kick(self):
        """do action 516 (Kick) — the win/loss reaction on the dog."""
        return self.blk("zsibot_action", fields={"ACTION": "516"})

    def hear(self, pause=0.6, max_sec=8):
        return self.blk("zsibot_hear", fields={"PAUSE": pause, "MAX": max_sec})

    def sound(self, name):
        return self.blk("zsibot_sound", fields={"SOUND": name})

    def prnt(self, value_body):
        return self.blk("zsibot_print", inputs={"VALUE": self.bval(value_body)})

    def wait(self, secs):
        return self.blk("zsibot_wait", fields={"SECONDS": secs})

    # ---- control flow ---------------------------------------------------------
    def while_(self, cond_body, body_blocks):
        return self.blk("controls_whileUntil", fields={"MODE": "WHILE"}, inputs={
            "BOOL": self.bval(cond_body), "DO": self.stmt(self.chain(body_blocks)),
        })

    def repeat(self, times, body_blocks):
        return self.blk("zsibot_repeat", fields={"TIMES": times}, inputs={
            "DO": self.stmt(self.chain(body_blocks)),
        })

    def if_(self, branches, else_blocks=None):
        """branches: list of (cond_body, [stmt blocks]). else_blocks: list or None."""
        inputs = {}
        extra = {}
        for i, (cond, body) in enumerate(branches):
            inputs["IF%d" % i] = self.bval(cond)
            inputs["DO%d" % i] = self.stmt(self.chain(body))
        if else_blocks:
            inputs["ELSE"] = self.stmt(self.chain(else_blocks))
            extra["hasElse"] = True
        if len(branches) > 1:
            extra["elseIfCount"] = len(branches) - 1
        return self.blk("controls_if", inputs=inputs, extra=extra or None)

    def chain(self, blocks):
        """Wire `next` through a list of statement blocks; return the first."""
        if not blocks:
            raise ValueError("empty chain")
        for i in range(len(blocks) - 1):
            blocks[i]["next"] = {"block": blocks[i + 1]}
        return blocks[0]

    def hat(self, blocks):
        """Wrap a statement chain under a zsibot_start hat (the runnable entry)."""
        return {"type": "zsibot_start", "x": 0, "y": 0, "next": {"block": self.chain(blocks)}}

    # ---- random ----------------------------------------------------------------
    def random_int(self, lo, hi):
        return self.blk("math_random_int", inputs={
            "FROM": self.sval(self.num(lo)), "TO": self.sval(self.num(hi)),
        })

    # ---------------------------------------------------------------------------
    def skill(self, name, version, description, tags, top_block):
        assert top_block["type"] == "zsibot_start", "top block must be a zsibot_start hat"
        return {"skill": {
            "name": name,
            "version": version,
            "robot": "navi",
            "description": description,
            "category": "Interaction",
            "tags": tags,
            "compatibleRobots": ["navi"],
            "publishTo": ["Teen store"],
            "blocks": {
                "blocks": {"languageVersion": 0, "blocks": [top_block]},
                "variables": self.vars,
            },
        }}


def word_lists(items):
    """Expand [(canonical, [forms...]), ...] into parallel (matchers, canons)
    lists for lists_indexOf-based matching. Matchers must be unique (each heard
    word may match at most one canonical)."""
    mats, cans = [], []
    seen = set()
    for canon, forms in items:
        for f in forms:
            if f in seen:
                raise ValueError("duplicate matcher %r (canonical %s)" % (f, canon))
            seen.add(f)
            mats.append(f)
            cans.append(canon)
    return mats, cans


# ---------------------------------------------------------------------------
# Game 1 — 20 Questions (Navi thinks of an animal, asks 6 yes/no questions)
# ---------------------------------------------------------------------------

# Q1 mammal  Q2 wings  Q3 bigger than dog  Q4 water  Q5 legs  Q6 eats meat
QUESTIONS = [
    "Is it a mammal?",
    "Does it have wings?",
    "Is it bigger than a dog?",
    "Does it spend most of its time in water?",
    "Does it have legs?",
    "Does it eat meat?",
]
ANIMALS = [
    ("lion",     "YNYNYY"),
    ("elephant", "YNYNYN"),
    ("dolphin",  "YNYYNY"),
    ("rabbit",   "YNNNYN"),
    ("pigeon",   "NYNNYN"),
    ("owl",      "NYNNYY"),
    ("snake",    "NNNNNY"),
    ("frog",     "NNNYYY"),
    ("dog",      "YNNNYY"),
    ("shark",    "NNYYNY"),
    ("penguin",  "NYNYYY"),
    ("duck",     "NYNYYN"),
    ("turtle",   "NNNYYN"),
]

# Single-word yes/no answers, matched word-by-word so "don't" never counts as
# "no". Multi-word phrases like "uh huh" are handled separately (whole-utterance
# contains) because STT usually returns them with a space.
YES_WORDS = ["YES", "YEAH", "YUP", "YEP", "YEA", "SURE", "OK", "OKAY",
             "RIGHT", "CORRECT", "TRUE", "Y", "AHA", "MMHM", "MMHMM", "UHHUH"]
NO_WORDS = ["NO", "NOPE", "NAH", "NUP", "NUH"]


def build_twenty_questions():
    b = Builder()

    def ask_question():
        return b.while_(
            b.logic_not(b.vget("got")),
            [
                b.speak(b.join([
                    b.text("Question "), b.vget("q"), b.text(": "),
                    b.list_get(b.vget("questions"), b.vget("q")),
                ])),
                b.wait(1.2),
                b.vset("up", b.uppercase(b.hear(0.6, 8))),
                b.if_([(b.eq_text(b.vget("up"), ""),
                        [b.speak_text("I didn't hear you. Please say yes or no.")])],
                      else_blocks=[
                          # phrase answers ("uh huh") before the word loop
                          b.if_([(b.contains(b.vget("up"), "UH HUH"),
                                  [b.vset("a", b.text("Y")),
                                   b.vset("got", b.boolean(True), is_shadow=True)])]),
                          b.vset("words", b.split(b.vget("up"), " ")),
                          b.vset("i", b.num(1)),
                          b.while_(b.lte(b.vget("i"), b.length_of(b.vget("words"))), [
                              b.if_([(b.logic_not(b.vget("got")), [
                                  b.if_([
                                      (b.neq_num(b.index_of(b.vget("yesWords"),
                                                            b.list_get(b.vget("words"), b.vget("i"))), 0),
                                       [b.vset("a", b.text("Y")),
                                        b.vset("got", b.boolean(True), is_shadow=True)]),
                                      (b.neq_num(b.index_of(b.vget("noWords"),
                                                            b.list_get(b.vget("words"), b.vget("i"))), 0),
                                       [b.vset("a", b.text("N")),
                                        b.vset("got", b.boolean(True), is_shadow=True)]),
                                  ]),
                              ])]),
                              b.vset("i", b.add(b.vget("i"), b.num(1), b_is_shadow=True)),
                          ]),
                          b.if_([(b.logic_not(b.vget("got")), [
                              # check the try budget BEFORE counting: up to 4
                              # attempts per question (kid-friendly)
                              b.if_([(b.gte(b.vget("tries"), b.num(3)),
                                      [b.vset("a", b.text("?")),
                                       b.vset("got", b.boolean(True), is_shadow=True)])],
                                    else_blocks=[
                                        b.vset("tries", b.add(b.vget("tries"), b.num(1), b_is_shadow=True)),
                                        b.speak_text("I did not catch that. Please say yes or no."),
                                    ]),
                          ])]),
                      ]),
            ],
        )

    match_branches = []
    for animal, code in ANIMALS:
        article = "an" if animal[0] in "aeiou" else "a"
        match_branches.append((
            b.eq_text(b.vget("answers"), code),
            [
                b.speak_text("It is %s %s! I win!" % (article, animal)),
                b.kick(),
                b.sound("chime"),
                b.vset("won", b.boolean(True), is_shadow=True),
            ],
        ))

    def round_blocks():
        return [
            b.vset("answers", b.text("")),
            b.vset("q", b.num(1)),
            b.repeat(6, [
                b.vset("got", b.boolean(False), is_shadow=True),
                b.vset("tries", b.num(0)),
                ask_question(),
                b.vset("answers", b.join([b.vget("answers"), b.vget("a")])),
                b.vset("q", b.add(b.vget("q"), b.num(1), b_is_shadow=True)),
            ]),
            b.if_(match_branches, else_blocks=[
                b.speak_text("Hmm, that does not match anything I know. Let me try again!"),
            ]),
        ]

    animal_list = ", ".join(a for a, _ in ANIMALS)
    intro = ("Think of one of these: %s. I will ask six questions. "
             "Answer yes or no!" % animal_list)

    top = b.hat([
        b.speak_text(intro),
        b.prnt(b.text(intro)),
        b.sound("chime"),
        b.vset("questions", b.list_of(QUESTIONS)),
        b.vset("yesWords", b.list_of(YES_WORDS)),
        b.vset("noWords", b.list_of(NO_WORDS)),
        b.vset("won", b.boolean(False), is_shadow=True),
        b.repeat(2, [
            b.if_([(b.logic_not(b.vget("won")), round_blocks())]),
        ]),
        b.if_([(b.logic_not(b.vget("won")),
                [b.speak_text("I give up! You win this time. Good game!"), b.kick()])]),
        b.speak_text("Thanks for playing twenty questions!"),
    ])

    # sanity: unique codes, right length, no intra-question contradictions
    codes = [c for _, c in ANIMALS]
    assert len(set(codes)) == len(codes), "animal codes must be unique"
    for _, code in ANIMALS:
        assert len(code) == len(QUESTIONS)
        assert set(code) <= set("YN")

    return b.skill(
        "20 Questions (voice)", "0.2.0",
        "Navi thinks of an animal and asks up to six yes/no questions to guess it. Say yes or no!",
        ["game", "voice", "questions"],
        top,
    )


# ---------------------------------------------------------------------------
# Game 2 — Taboo (Navi deals cards, listens, dings on taboo words)
# ---------------------------------------------------------------------------

# Each card: (target, [[taboo forms...], [taboo forms...], [taboo forms...]]).
# Forms are matched per WORD (exact equality), so "yellowish" does not trip
# "YELLOW" but "strings" still trips "STRING" and "bananas" still wins.
TABOO_CARDS = [
    ("BANANA",    [["MONKEY", "MONKEYS"], ["YELLOW"], ["PEEL", "PEELS"]]),
    ("GUITAR",    [["MUSIC"], ["STRING", "STRINGS"], ["BAND", "BANDS"]]),
    ("PIRATE",    [["SHIP", "SHIPS"], ["TREASURE", "TREASURES"], ["HOOK", "HOOKS"]]),
    ("TELESCOPE", [["STAR", "STARS"], ["MOON", "MOONS"], ["ZOOM"]]),
    ("UNICORN",   [["HORN", "HORNS"], ["RAINBOW", "RAINBOWS"], ["MAGIC"]]),
]
TARGET_FORMS = {
    "BANANA":    ["BANANA", "BANANAS"],
    "GUITAR":    ["GUITAR", "GUITARS"],
    "PIRATE":    ["PIRATE", "PIRATES"],
    "TELESCOPE": ["TELESCOPE", "TELESCOPES"],
    "UNICORN":   ["UNICORN", "UNICORNS"],
}


def build_taboo():
    b = Builder()

    def is_form(w_body, forms):
        return b.logic_or([b.eq_text(w_body, f) for f in forms])

    def not_taboo_yet():
        # first taboo said wins; a taboo also overrides an earlier target hit
        return b.logic_or([b.eq_text(b.vget("hit"), ""), b.eq_text(b.vget("hit"), "WIN")])

    def round_blocks(target, taboos, round_no, total):
        t1, t2, t3 = taboos
        return [
            b.speak_text("Round %d of %d. Your word is %s. Don't say: %s, %s, or %s!" %
                         (round_no, total, target, t1[0], t2[0], t3[0])),
            b.sound("chime"),
            b.wait(1.0),
            b.vset("up", b.uppercase(b.hear(0.9, 25))),
            b.vset("words", b.split(b.vget("up"), " ")),
            b.vset("hit", b.text("")),
            b.vset("i", b.num(1)),
            b.while_(b.lte(b.vget("i"), b.length_of(b.vget("words"))), [
                b.vset("w", b.list_get(b.vget("words"), b.vget("i"))),
                # taboo always wins, even if the target was said earlier in the
                # same utterance; the target only scores if nothing taboo yet
                b.if_([
                    (b.logic_and([is_form(b.vget("w"), t1), not_taboo_yet()]),
                     [b.vset("hit", b.text("T1"))]),
                    (b.logic_and([is_form(b.vget("w"), t2), not_taboo_yet()]),
                     [b.vset("hit", b.text("T2"))]),
                    (b.logic_and([is_form(b.vget("w"), t3), not_taboo_yet()]),
                     [b.vset("hit", b.text("T3"))]),
                    (b.logic_and([is_form(b.vget("w"), TARGET_FORMS[target]),
                                  b.eq_text(b.vget("hit"), "")]),
                     [b.vset("hit", b.text("WIN"))]),
                ]),
                b.vset("i", b.add(b.vget("i"), b.num(1), b_is_shadow=True)),
            ]),
            b.if_([
                (b.eq_text(b.vget("hit"), "T1"),
                 [b.sound("alarm"),
                  b.speak_text("Taboo! You said %s. No point." % t1[0]),
                  b.kick()]),
                (b.eq_text(b.vget("hit"), "T2"),
                 [b.sound("alarm"),
                  b.speak_text("Taboo! You said %s. No point." % t2[0]),
                  b.kick()]),
                (b.eq_text(b.vget("hit"), "T3"),
                 [b.sound("alarm"),
                  b.speak_text("Taboo! You said %s. No point." % t3[0]),
                  b.kick()]),
                (b.eq_text(b.vget("hit"), "WIN"),
                 [b.sound("chime"),
                  b.vset("score", b.add(b.vget("score"), b.num(1), b_is_shadow=True)),
                  b.speak(b.join([b.text("You got it! The word was "), b.text(target),
                                  b.text(". Your score is "), b.vget("score"), b.text(".")])),
                  b.kick()]),
            ], else_blocks=[
                b.speak_text("Time's up! The word was %s." % target),
            ]),
        ]

    total = len(TABOO_CARDS)
    all_blocks = [
        b.speak_text("Taboo! I will give you a word. Describe it to your friend "
                     "without saying the forbidden words. I am listening!"),
        b.prnt(b.text("Taboo! Describe the word without saying the taboo words.")),
        b.sound("chime"),
        b.vset("score", b.num(0)),
    ]
    for i, (target, taboos) in enumerate(TABOO_CARDS, start=1):
        all_blocks.extend(round_blocks(target, taboos, i, total))
    all_blocks.extend([
        b.speak(b.join([b.text("Game over! You got "), b.vget("score"),
                        b.text(" out of %d." % total)])),
        b.if_([(b.gte(b.vget("score"), b.num(3)),
                [b.speak_text("Amazing! You are a Taboo champion!")])],
              else_blocks=[
                  b.speak_text("Good try! Run it again to beat your score."),
              ]),
    ])
    top = b.hat(all_blocks)

    # sanity: forms are unique within each round and never collide with target
    for target, taboos in TABOO_CARDS:
        seen = set()
        for forms in taboos:
            for f in forms:
                assert f not in seen, "duplicate taboo form %r" % f
                seen.add(f)
        assert target not in seen, "target %s doubles as a taboo form" % target
        for f in seen:
            assert f != target, (target, f)

    return b.skill(
        "Taboo (voice)", "0.2.0",
        "Navi deals Taboo cards: describe the word without saying the forbidden words. Navi listens and dings when you slip!",
        ["game", "voice", "party"],
        top,
    )


# ---------------------------------------------------------------------------
# Game 3 — Categories (Navi picks a category, you name 5 things, no repeats)
# ---------------------------------------------------------------------------

# (canonical, [accepted word forms]). Canonical is what repeat-detection tracks;
# every form (plural/irregular) maps to it, so "mouse" + "mice" = a repeat.
CATEGORIES = {
    "ANIMALS": [
        ("DOG",        ["DOG", "DOGS"]),
        ("CAT",        ["CAT", "CATS"]),
        ("FISH",       ["FISH", "FISHES"]),
        ("BIRD",       ["BIRD", "BIRDS"]),
        ("LION",       ["LION", "LIONS"]),
        ("TIGER",      ["TIGER", "TIGERS"]),
        ("BEAR",       ["BEAR", "BEARS"]),
        ("PANDA",      ["PANDA", "PANDAS"]),
        ("WOLF",       ["WOLF", "WOLVES"]),
        ("FOX",        ["FOX", "FOXES"]),
        ("COYOTE",     ["COYOTE", "COYOTES"]),
        ("CHEETAH",    ["CHEETAH", "CHEETAHS"]),
        ("LEOPARD",    ["LEOPARD", "LEOPARDS"]),
        ("ELEPHANT",   ["ELEPHANT", "ELEPHANTS"]),
        ("GIRAFFE",    ["GIRAFFE", "GIRAFFES"]),
        ("ZEBRA",      ["ZEBRA", "ZEBRAS"]),
        ("RHINO",      ["RHINO", "RHINOS"]),
        ("HIPPO",      ["HIPPO", "HIPPOS"]),
        ("HORSE",      ["HORSE", "HORSES"]),
        ("PONY",       ["PONY", "PONIES"]),
        ("DONKEY",     ["DONKEY", "DONKEYS"]),
        ("MULE",       ["MULE", "MULES"]),
        ("COW",        ["COW", "COWS"]),
        ("BULL",       ["BULL", "BULLS"]),
        ("PIG",        ["PIG", "PIGS"]),
        ("SHEEP",      ["SHEEP", "SHEEPS"]),
        ("GOAT",       ["GOAT", "GOATS"]),
        ("CHICKEN",    ["CHICKEN", "CHICKENS"]),
        ("ROOSTER",    ["ROOSTER", "ROOSTERS"]),
        ("HEN",        ["HEN", "HENS"]),
        ("DUCK",       ["DUCK", "DUCKS"]),
        ("GOOSE",      ["GOOSE", "GEESE", "GOOSES"]),
        ("TURKEY",     ["TURKEY", "TURKEYS"]),
        ("RABBIT",     ["RABBIT", "RABBITS"]),
        ("BUNNY",      ["BUNNY", "BUNNIES"]),
        ("HAMSTER",    ["HAMSTER", "HAMSTERS"]),
        ("MOUSE",      ["MOUSE", "MICE", "MOUSES"]),
        ("RAT",        ["RAT", "RATS"]),
        ("SQUIRREL",   ["SQUIRREL", "SQUIRRELS"]),
        ("BEAVER",     ["BEAVER", "BEAVERS"]),
        ("SKUNK",      ["SKUNK", "SKUNKS"]),
        ("BADGER",     ["BADGER", "BADGERS"]),
        ("DEER",       ["DEER", "DEERS"]),
        ("MOOSE",      ["MOOSE", "MOOSES"]),
        ("ELK",        ["ELK", "ELKS"]),
        ("KANGAROO",   ["KANGAROO", "KANGAROOS"]),
        ("KOALA",      ["KOALA", "KOALAS"]),
        ("WOMBAT",     ["WOMBAT", "WOMBATS"]),
        ("MONKEY",     ["MONKEY", "MONKEYS"]),
        ("GORILLA",    ["GORILLA", "GORILLAS"]),
        ("CHIMPANZEE", ["CHIMPANZEE", "CHIMPANZEES"]),
        ("APE",        ["APE", "APES"]),
        ("WHALE",      ["WHALE", "WHALES"]),
        ("DOLPHIN",    ["DOLPHIN", "DOLPHINS"]),
        ("SHARK",      ["SHARK", "SHARKS"]),
        ("SEAL",       ["SEAL", "SEALS"]),
        ("WALRUS",     ["WALRUS", "WALRUSES"]),
        ("OCTOPUS",    ["OCTOPUS", "OCTOPUSES", "OCTOPI"]),
        ("SQUID",      ["SQUID", "SQUIDS"]),
        ("CRAB",       ["CRAB", "CRABS"]),
        ("LOBSTER",    ["LOBSTER", "LOBSTERS"]),
        ("SHRIMP",     ["SHRIMP", "SHRIMPS"]),
        ("TURTLE",     ["TURTLE", "TURTLES"]),
        ("TORTOISE",   ["TORTOISE", "TORTOISES"]),
        ("FROG",       ["FROG", "FROGS"]),
        ("TOAD",       ["TOAD", "TOADS"]),
        ("SNAKE",      ["SNAKE", "SNAKES"]),
        ("LIZARD",     ["LIZARD", "LIZARDS"]),
        ("CROCODILE",  ["CROCODILE", "CROCODILES"]),
        ("ALLIGATOR",  ["ALLIGATOR", "ALLIGATORS"]),
        ("IGUANA",     ["IGUANA", "IGUANAS"]),
        ("CAMEL",      ["CAMEL", "CAMELS"]),
        ("LLAMA",      ["LLAMA", "LLAMAS"]),
        ("RACCOON",    ["RACCOON", "RACCOONS"]),
        ("PORCUPINE",  ["PORCUPINE", "PORCUPINES"]),
        ("HEDGEHOG",   ["HEDGEHOG", "HEDGEHOGS"]),
        ("EAGLE",      ["EAGLE", "EAGLES"]),
        ("HAWK",       ["HAWK", "HAWKS"]),
        ("FALCON",     ["FALCON", "FALCONS"]),
        ("OWL",        ["OWL", "OWLS"]),
        ("PARROT",     ["PARROT", "PARROTS"]),
        ("PENGUIN",    ["PENGUIN", "PENGUINS"]),
        ("OSTRICH",    ["OSTRICH", "OSTRICHES"]),
        ("PEACOCK",    ["PEACOCK", "PEACOCKS"]),
        ("SWAN",       ["SWAN", "SWANS"]),
        ("PIGEON",     ["PIGEON", "PIGEONS"]),
        ("ROBIN",      ["ROBIN", "ROBINS"]),
        ("SPARROW",    ["SPARROW", "SPARROWS"]),
        ("CROW",       ["CROW", "CROWS"]),
        ("RAVEN",      ["RAVEN", "RAVENS"]),
        ("SEAGULL",    ["SEAGULL", "SEAGULLS"]),
        ("BAT",        ["BAT", "BATS"]),
        ("BEE",        ["BEE", "BEES"]),
        ("WASP",       ["WASP", "WASPS"]),
        ("ANT",        ["ANT", "ANTS"]),
        ("SPIDER",     ["SPIDER", "SPIDERS"]),
        ("BUTTERFLY",  ["BUTTERFLY", "BUTTERFLIES"]),
        ("MOTH",       ["MOTH", "MOTHS"]),
        ("DRAGONFLY",  ["DRAGONFLY", "DRAGONFLIES"]),
        ("LADYBUG",    ["LADYBUG", "LADYBUGS"]),
        ("WORM",       ["WORM", "WORMS"]),
        ("SNAIL",      ["SNAIL", "SNAILS"]),
        ("CATERPILLAR", ["CATERPILLAR", "CATERPILLARS"]),
        ("GRASSHOPPER", ["GRASSHOPPER", "GRASSHOPPERS"]),
        ("SCORPION",   ["SCORPION", "SCORPIONS"]),
        ("DINOSAUR",   ["DINOSAUR", "DINOSAURS"]),
        ("DRAGON",     ["DRAGON", "DRAGONS"]),
        ("UNICORN",    ["UNICORN", "UNICORNS"]),
        ("HUMAN",      ["HUMAN", "HUMANS"]),
        ("KITTEN",     ["KITTEN", "KITTENS"]),
        ("PUPPY",      ["PUPPY", "PUPPIES"]),
        ("MEGALODON",  ["MEGALODON", "MEGALODONS"]),
    ],
    "FOODS": [
        ("APPLE",       ["APPLE", "APPLES"]),
        ("BANANA",      ["BANANA", "BANANAS"]),
        ("ORANGE",      ["ORANGE", "ORANGES"]),
        ("PEAR",        ["PEAR", "PEARS"]),
        ("PEACH",       ["PEACH", "PEACHES"]),
        ("PLUM",        ["PLUM", "PLUMS"]),
        ("CHERRY",      ["CHERRY", "CHERRIES"]),
        ("GRAPE",       ["GRAPE", "GRAPES"]),
        ("STRAWBERRY",  ["STRAWBERRY", "STRAWBERRIES"]),
        ("BLUEBERRY",   ["BLUEBERRY", "BLUEBERRIES"]),
        ("RASPBERRY",   ["RASPBERRY", "RASPBERRIES"]),
        ("WATERMELON",  ["WATERMELON", "WATERMELONS"]),
        ("MELON",       ["MELON", "MELONS"]),
        ("PINEAPPLE",   ["PINEAPPLE", "PINEAPPLES"]),
        ("MANGO",       ["MANGO", "MANGOES"]),
        ("LEMON",       ["LEMON", "LEMONS"]),
        ("LIME",        ["LIME", "LIMES"]),
        ("KIWI",        ["KIWI", "KIWIS"]),
        ("COCONUT",     ["COCONUT", "COCONUTS"]),
        ("AVOCADO",     ["AVOCADO", "AVOCADOS"]),
        ("BREAD",       ["BREAD"]),
        ("TOAST",       ["TOAST"]),
        ("BAGEL",       ["BAGEL", "BAGELS"]),
        ("MUFFIN",      ["MUFFIN", "MUFFINS"]),
        ("PANCAKE",     ["PANCAKE", "PANCAKES"]),
        ("WAFFLE",      ["WAFFLE", "WAFFLES"]),
        ("CEREAL",      ["CEREAL"]),
        ("OATMEAL",     ["OATMEAL"]),
        ("EGG",         ["EGG", "EGGS"]),
        ("CHEESE",      ["CHEESE"]),
        ("MILK",        ["MILK"]),
        ("YOGURT",      ["YOGURT"]),
        ("BUTTER",      ["BUTTER"]),
        ("HONEY",       ["HONEY"]),
        ("JAM",         ["JAM"]),
        ("JELLY",       ["JELLY"]),
        ("PIZZA",       ["PIZZA", "PIZZAS"]),
        ("PASTA",       ["PASTA"]),
        ("SPAGHETTI",   ["SPAGHETTI"]),
        ("NOODLE",      ["NOODLE", "NOODLES"]),
        ("RICE",        ["RICE"]),
        ("BEAN",        ["BEAN", "BEANS"]),
        ("SOUP",        ["SOUP", "SOUPS"]),
        ("SALAD",       ["SALAD", "SALADS"]),
        ("SANDWICH",    ["SANDWICH", "SANDWICHES"]),
        ("BURGER",      ["BURGER", "BURGERS"]),
        ("CHEESEBURGER", ["CHEESEBURGER", "CHEESEBURGERS"]),
        ("HOTDOG",      ["HOTDOG", "HOTDOGS", "HOT", "DOG"]),
        ("FRIES",       ["FRIES"]),
        ("CHIPS",       ["CHIPS"]),
        ("POPCORN",     ["POPCORN"]),
        ("PRETZEL",     ["PRETZEL", "PRETZELS"]),
        ("CRACKER",     ["CRACKER", "CRACKERS"]),
        ("COOKIE",      ["COOKIE", "COOKIES"]),
        ("CAKE",        ["CAKE", "CAKES"]),
        ("CUPCAKE",     ["CUPCAKE", "CUPCAKES"]),
        ("PIE",         ["PIE", "PIES"]),
        ("DONUT",       ["DONUT", "DONUTS"]),
        ("CANDY",       ["CANDY", "CANDIES"]),
        ("CHOCOLATE",   ["CHOCOLATE", "CHOCOLATES"]),
        ("ICECREAM",    ["ICECREAM", "ICE"]),
        ("PEANUTBUTTER", ["PEANUTBUTTER", "PEANUT"]),
        ("CARROT",      ["CARROT", "CARROTS"]),
        ("POTATO",      ["POTATO", "POTATOES"]),
        ("TOMATO",      ["TOMATO", "TOMATOES"]),
        ("CUCUMBER",    ["CUCUMBER", "CUCUMBERS"]),
        ("LETTUCE",     ["LETTUCE"]),
        ("BROCCOLI",    ["BROCCOLI"]),
        ("CAULIFLOWER", ["CAULIFLOWER"]),
        ("CELERY",      ["CELERY"]),
        ("ONION",       ["ONION", "ONIONS"]),
        ("GARLIC",      ["GARLIC"]),
        ("PEPPER",      ["PEPPER", "PEPPERS"]),
        ("MUSHROOM",    ["MUSHROOM", "MUSHROOMS"]),
        ("PEA",         ["PEA", "PEAS"]),
        ("SPINACH",     ["SPINACH"]),
        ("CABBAGE",     ["CABBAGE"]),
        ("SQUASH",      ["SQUASH"]),
        ("PUMPKIN",     ["PUMPKIN", "PUMPKINS"]),
        ("OLIVE",       ["OLIVE", "OLIVES"]),
        ("PICKLE",      ["PICKLE", "PICKLES"]),
        ("KETCHUP",     ["KETCHUP"]),
        ("MUSTARD",     ["MUSTARD"]),
        ("MAYO",        ["MAYO"]),
        ("SALT",        ["SALT"]),
        ("SUGAR",       ["SUGAR"]),
        ("BACON",       ["BACON"]),
        ("SAUSAGE",     ["SAUSAGE", "SAUSAGES"]),
        ("HAM",         ["HAM"]),
        ("STEAK",       ["STEAK", "STEAKS"]),
        ("CHICKEN",     ["CHICKEN"]),
        ("TURKEY",      ["TURKEY"]),
        ("FISH",        ["FISH"]),
        ("SHRIMP",      ["SHRIMP", "SHRIMPS"]),
        ("LOBSTER",     ["LOBSTER"]),
        ("SUSHI",       ["SUSHI"]),
        ("TACO",        ["TACO", "TACOS"]),
        ("BURRITO",     ["BURRITO", "BURRITOS"]),
        ("NACHOS",      ["NACHOS"]),
        ("CORN",        ["CORN"]),
        ("PUDDING",     ["PUDDING"]),
        ("JELLO",       ["JELLO"]),
        ("POPSICLE",    ["POPSICLE", "POPSICLES"]),
        ("LOLLIPOP",    ["LOLLIPOP", "LOLLIPOPS"]),
        ("GUM",         ["GUM"]),
        ("SODA",        ["SODA"]),
        ("JUICE",       ["JUICE"]),
        ("WATER",       ["WATER"]),
        ("TEA",         ["TEA"]),
        ("COFFEE",      ["COFFEE"]),
        ("MILKSHAKE",   ["MILKSHAKE", "MILKSHAKES"]),
        ("SMOOTHIE",    ["SMOOTHIE", "SMOOTHIES"]),
        ("SYRUP",       ["SYRUP"]),
    ],
    "COLORS": [
        ("RED",        ["RED"]),
        ("BLUE",       ["BLUE"]),
        ("GREEN",      ["GREEN"]),
        ("YELLOW",     ["YELLOW"]),
        ("PURPLE",     ["PURPLE"]),
        ("ORANGE",     ["ORANGE"]),
        ("PINK",       ["PINK"]),
        ("BLACK",      ["BLACK"]),
        ("WHITE",      ["WHITE"]),
        ("BROWN",      ["BROWN"]),
        ("GRAY",       ["GRAY"]),
        ("GREY",       ["GREY"]),
        ("GOLD",       ["GOLD"]),
        ("SILVER",     ["SILVER"]),
        ("BRONZE",     ["BRONZE"]),
        ("TAN",        ["TAN"]),
        ("BEIGE",      ["BEIGE"]),
        ("CREAM",      ["CREAM"]),
        ("TURQUOISE",  ["TURQUOISE"]),
        ("TEAL",       ["TEAL"]),
        ("NAVY",       ["NAVY"]),
        ("MAROON",     ["MAROON"]),
        ("VIOLET",     ["VIOLET"]),
        ("MAGENTA",    ["MAGENTA"]),
        ("LIME",       ["LIME"]),
        ("LAVENDER",   ["LAVENDER"]),
        ("INDIGO",     ["INDIGO"]),
        ("CYAN",       ["CYAN"]),
        ("AQUA",       ["AQUA"]),
        ("CORAL",      ["CORAL"]),
        ("PEACH",      ["PEACH"]),
        ("OLIVE",      ["OLIVE"]),
        ("MINT",       ["MINT"]),
        ("SALMON",     ["SALMON"]),
        ("ROSE",       ["ROSE"]),
        ("RUBY",       ["RUBY"]),
        ("EMERALD",    ["EMERALD"]),
        ("SAPPHIRE",   ["SAPPHIRE"]),
        ("IVORY",      ["IVORY"]),
        ("PEARL",      ["PEARL"]),
        ("CHARCOAL",   ["CHARCOAL"]),
        ("SLATE",      ["SLATE"]),
        ("KHAKI",      ["KHAKI"]),
        ("PLUM",       ["PLUM"]),
        ("BURGUNDY",   ["BURGUNDY"]),
        ("CHAMPAGNE",  ["CHAMPAGNE"]),
        ("RAINBOW",    ["RAINBOW"]),
        ("MULTICOLOR", ["MULTICOLOR"]),
        ("SPARKLY",    ["SPARKLY"]),
        ("SHINY",      ["SHINY"]),
        ("NEON",       ["NEON"]),
        ("PASTEL",     ["PASTEL"]),
        ("COLORFUL",   ["COLORFUL"]),
    ],
    "SPORTS": [
        ("SOCCER",        ["SOCCER"]),
        ("FOOTBALL",      ["FOOTBALL"]),
        ("BASEBALL",      ["BASEBALL"]),
        ("BASKETBALL",    ["BASKETBALL"]),
        ("VOLLEYBALL",    ["VOLLEYBALL"]),
        ("TENNIS",        ["TENNIS"]),
        ("BADMINTON",     ["BADMINTON"]),
        ("HOCKEY",        ["HOCKEY"]),
        ("GOLF",          ["GOLF"]),
        ("CRICKET",       ["CRICKET"]),
        ("RUGBY",         ["RUGBY"]),
        ("LACROSSE",      ["LACROSSE"]),
        ("SWIMMING",      ["SWIMMING", "SWIM"]),
        ("DIVING",        ["DIVING", "DIVE"]),
        ("SURFING",       ["SURFING", "SURF"]),
        ("SNORKELING",    ["SNORKELING", "SNORKEL"]),
        ("RUNNING",       ["RUNNING", "RUN"]),
        ("JOGGING",       ["JOGGING", "JOG"]),
        ("WALKING",       ["WALKING", "WALK"]),
        ("HIKING",        ["HIKING", "HIKE"]),
        ("CLIMBING",      ["CLIMBING", "CLIMB"]),
        ("CYCLING",       ["CYCLING", "CYCLE"]),
        ("BIKING",        ["BIKING", "BIKE"]),
        ("SKATING",       ["SKATING", "SKATE"]),
        ("SKIING",        ["SKIING", "SKI"]),
        ("SNOWBOARDING",  ["SNOWBOARDING", "SNOWBOARD"]),
        ("SLEDDING",      ["SLEDDING", "SLED"]),
        ("GYMNASTICS",    ["GYMNASTICS"]),
        ("CHEERLEADING",  ["CHEERLEADING", "CHEER"]),
        ("DANCING",       ["DANCING", "DANCE"]),
        ("BALLET",        ["BALLET"]),
        ("KARATE",        ["KARATE"]),
        ("JUDO",          ["JUDO"]),
        ("TAEKWONDO",     ["TAEKWONDO"]),
        ("BOXING",        ["BOXING", "BOX"]),
        ("WRESTLING",     ["WRESTLING", "WRESTLE"]),
        ("FENCING",       ["FENCING", "FENCE"]),
        ("ARCHERY",       ["ARCHERY"]),
        ("SHOOTING",      ["SHOOTING", "SHOOT"]),
        ("JUMPING",       ["JUMPING", "JUMP"]),
        ("SKIPPING",      ["SKIPPING", "SKIP"]),
        ("YOGA",          ["YOGA"]),
        ("ROWING",        ["ROWING", "ROW"]),
        ("SAILING",       ["SAILING", "SAIL"]),
        ("CANOEING",      ["CANOEING", "CANOE"]),
        ("KAYAKING",      ["KAYAKING", "KAYAK"]),
        ("FISHING",       ["FISHING", "FISH"]),
        ("BOWLING",       ["BOWLING", "BOWL"]),
        ("DARTS",         ["DARTS"]),
        ("BILLIARDS",     ["BILLIARDS"]),
        ("POOL",          ["POOL"]),
        ("PINGPONG",      ["PINGPONG", "PING", "PONG"]),
        ("FRISBEE",       ["FRISBEE"]),
        ("SKATEBOARDING", ["SKATEBOARDING", "SKATEBOARD"]),
        ("CHESS",         ["CHESS"]),
        ("CHECKERS",      ["CHECKERS"]),
        ("CARDS",         ["CARDS"]),
        ("TAG",           ["TAG"]),
        ("HOPSCOTCH",     ["HOPSCOTCH"]),
        ("TRAMPOLINE",    ["TRAMPOLINE", "TRAMPOLINING"]),
        ("MARATHON",      ["MARATHON"]),
        ("TRIATHLON",     ["TRIATHLON"]),
        ("OLYMPICS",      ["OLYMPICS"]),
        ("WORKOUT",       ["WORKOUT", "WORKOUTS"]),
        ("EXERCISE",      ["EXERCISE", "EXERCISES"]),
        ("PUSHUPS",       ["PUSHUPS"]),
        ("SITUPS",        ["SITUPS", "SIT", "UPS"]),
        ("PULLUPS",       ["PULLUPS"]),
        ("HULA",          ["HULA"]),
        ("ZUMBA",         ["ZUMBA"]),
        ("AEROBICS",      ["AEROBICS"]),
        ("PILATES",       ["PILATES"]),
    ],
}
TARGET_COUNT = 5


def build_categories():
    b = Builder()
    cat_names = list(CATEGORIES.keys())

    # per-category parallel (matchers, canons) lists — matchers[j] scores canons[j]
    matcher_lists, canon_lists = {}, {}
    for name, items in CATEGORIES.items():
        mats, cans = word_lists(items)
        matcher_lists[name] = mats
        canon_lists[name] = cans

    def match_word():
        """Score one word (variable `w`) against the current category's lists."""
        already_said = b.if_([
            (b.contains_v(b.join([b.text(" "), b.vget("said"), b.text(" ")]),
                          b.join([b.text(" "), b.vget("c"), b.text(" ")])),
             [b.speak(b.join([b.text("You already said "), b.vget("c"),
                              b.text("! Say a new one.")]))]),
        ], else_blocks=[
            b.vset("said", b.join([b.vget("said"), b.vget("c"), b.text(" ")])),
            b.vset("score", b.add(b.vget("score"), b.num(1), b_is_shadow=True)),
            b.sound("chime"),
            b.speak(b.join([b.text("Good! That's "), b.vget("score"),
                            b.text(" of %d." % TARGET_COUNT)])),
        ])
        return [
            b.vset("idx", b.index_of(b.vget("matchers"), b.vget("w"))),
            b.if_([
                (b.neq_num(b.vget("idx"), 0), [
                    b.vset("c", b.list_get(b.vget("canons"), b.vget("idx"))),
                    b.vset("found", b.boolean(True), is_shadow=True),
                    already_said,
                ]),
            ]),
        ]

    def listen_cycle():
        return [
            b.vset("up", b.uppercase(b.hear(0.8, 12))),
            b.vset("found", b.boolean(False), is_shadow=True),
            b.if_([(b.eq_text(b.vget("up"), ""),
                    [b.speak_text("I didn't hear you. Say something!")])],
                  else_blocks=[
                      b.vset("words", b.split(b.vget("up"), " ")),
                      b.vset("i", b.num(1)),
                      b.while_(b.lte(b.vget("i"), b.length_of(b.vget("words"))), [
                          b.vset("w", b.list_get(b.vget("words"), b.vget("i"))),
                          b.if_([(b.logic_and([
                              b.logic_not(b.eq_text(b.vget("w"), "")),
                              b.lt(b.vget("score"), b.num(TARGET_COUNT)),
                          ]), match_word())]),
                          b.vset("i", b.add(b.vget("i"), b.num(1), b_is_shadow=True)),
                      ]),
                      b.if_([(b.logic_not(b.vget("found")),
                              [b.speak_text("Hmm, that's not one I know in this category. Try another!")])]),
                  ]),
        ]

    # pick the category's matcher/canon lists once per game
    cat_setup_branches = []
    for i, name in enumerate(cat_names, start=1):
        cat_setup_branches.append((
            b.eq_num(b.vget("cat"), i),
            [
                b.vset("matchers", b.vget("m_" + name)),
                b.vset("canons", b.vget("c_" + name)),
            ],
        ))

    top = b.hat([
        b.speak_text("Categories! I will pick a category. Name %d things in it "
                     "without repeating yourself. Go!" % TARGET_COUNT),
        b.prnt(b.text("Categories — name %d things, no repeats." % TARGET_COUNT)),
        b.sound("chime"),
        b.vset("catNames", b.list_of(cat_names)),
        b.vset("cat", b.random_int(1, len(cat_names))),
        b.vset("catName", b.list_get(b.vget("catNames"), b.vget("cat"))),
        b.speak(b.join([b.text("Your category is "), b.vget("catName"),
                        b.text(". Name %d things! Go!" % TARGET_COUNT)])),
        b.vset("score", b.num(0)),
        b.vset("said", b.text(" ")),
    ] + [
        b.vset("m_" + name, b.list_of(matcher_lists[name]))
        for name in cat_names
    ] + [
        b.vset("c_" + name, b.list_of(canon_lists[name]))
        for name in cat_names
    ] + [
        b.if_(cat_setup_branches),
        b.while_(b.lt(b.vget("score"), b.num(TARGET_COUNT)), listen_cycle()),
        b.sound("chime"),
        b.speak(b.join([b.text("You did it! Five "), b.vget("catName"),
                        b.text("! Awesome work!")])),
        b.kick(),
        b.speak_text("Thanks for playing categories!"),
    ])

    return b.skill(
        "Categories (voice)", "0.2.0",
        "Navi picks a category and you name five things in it — no repeats! Navi keeps score and catches repeats.",
        ["game", "voice", "words"],
        top,
    )


# ---------------------------------------------------------------------------
def main():
    games = [
        ("20-Questions-voice.json", build_twenty_questions()),
        ("Taboo-voice.json", build_taboo()),
        ("Categories-voice.json", build_categories()),
    ]
    for fname, data in games:
        path = os.path.join(ROOT, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print("wrote %s (%d bytes)" % (path, os.path.getsize(path)))


if __name__ == "__main__":
    main()
