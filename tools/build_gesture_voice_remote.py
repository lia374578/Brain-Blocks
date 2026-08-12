#!/usr/bin/env python3
"""Build sum_remote.json — the combined skill:
  the `when` block IS the voice listener (voice takes priority), the main loop
  does gesture control + facing the user. Coordination between the two hats:
    - main loop sets `listen = true` at the top of every cycle
    - the when block's condition is `listen`; it fires at the next boundary,
      hears a command, dispatches it at max speed, then sets `listen = false`
    - a heard command sets `voiced = true` so the main loop skips gestures that
      cycle (voice overrides gestures); no command sets `voiced = false`
  All motion uses max speeds: VX/VY 2 m/s, YAW 5 rad/s. No `do action`.

  Editor convention (oneBasedIndex: true): text_indexOf emits indexOf(x)+1,
  so "found" checks are NEQ 0 (not-found = -1+1 = 0).
"""
import json

SKILL = {
    "name": "Gesture + voice remote",
    "version": "0.4.0",
    "robot": "navi",
    "description": ("Remote control by voice (priority) and gesture. A 'when listen' block hears "
                    "voice commands (forward / back / left / right / strafe left|right / sit / stand / "
                    "come) and they override gestures. Gestures when nobody speaks: palm=forward, "
                    "thumb-up=stand, thumb-down=back, point-left/right=turn, victory=strafe left, "
                    "love=strafe right, fist=sit, wave=come. Faces the user when idle."),
    "category": "Testing",
    "tags": ["voice", "gesture", "vision", "remote"],
    "compatibleRobots": ["navi"],
    "publishTo": ["Teen store"],
}

VARS = [
    {"id": "id_cmd", "name": "cmd"},
    {"id": "id_cmdUp", "name": "cmdUp"},
    {"id": "id_g", "name": "g"},
    {"id": "id_voiced", "name": "voiced"},
]

# ---- block helpers ---------------------------------------------------------
def block(type_, fields=None, inputs=None, next_=None, extra_state=None):
    b = {"type": type_}
    if extra_state is not None:
        b["extraState"] = extra_state
    if fields:
        b["fields"] = fields
    if inputs:
        b["inputs"] = inputs
    if next_ is not None:
        b["next"] = {"block": next_}
    return b


def controls_if(branch_count, has_else, inputs):
    """controls_if with extraState so the loaded block matches the serialized
    IF/DO branches: branch_count = number of if-branches (elseifCount = n-1)."""
    return block("controls_if", None, inputs,
                 extra_state={"elseIfCount": max(branch_count - 1, 0), "hasElse": has_else})


def text_shadow(t):
    return {"shadow": {"type": "text", "fields": {"TEXT": t}}}


def num_shadow(n):
    return {"shadow": {"type": "math_number", "fields": {"NUM": n}}}


def chain(blocks):
    """Link blocks in order via `next`; return the first block."""
    for i in range(len(blocks) - 1):
        blocks[i]["next"] = {"block": blocks[i + 1]}
    return blocks[0]


def var_get(var_id):
    return block("variables_get", {"VAR": {"id": var_id}})


def var_set(var_id, value_block):
    return block("variables_set", {"VAR": {"id": var_id}},
                 {"VALUE": {"block": value_block}})


def set_bool(var_id, value):
    return var_set(var_id, block("logic_boolean", {"BOOL": "TRUE" if value else "FALSE"}))


def speak(text):
    return block("zsibot_speak", {"VOICE": ""}, {"TEXT": text_shadow(text)})


def find_ne_0(source_block, needle):
    """(find <needle> in <source>) ≠ 0 — 1-based indexOf means 0 = not found."""
    return block("logic_compare", {"OP": "NEQ"}, {
        "A": {"block": block("text_indexOf", {"END": "FIRST"}, {
            "VALUE": {"block": source_block},
            "FIND": text_shadow(needle),
        })},
        "B": num_shadow(0),
    })


def logic_or(a, b):
    return block("logic_operation", {"OP": "OR"},
                 {"A": {"block": a}, "B": {"block": b}})


# ---- motion (max speeds) ---------------------------------------------------
def move_forward():
    return block("zsibot_forward", {"VX": 2, "DURATION": 1.5})


def move_backward():
    return block("zsibot_backward", {"VX": 2, "DURATION": 1.5})


def turn_left():
    return block("zsibot_turn_left", {"YAW": 5, "DURATION": 0.5})


def turn_right():
    return block("zsibot_turn_right", {"YAW": 5, "DURATION": 0.5})


def strafe_left():
    return block("zsibot_strafe_left", {"VY": 2, "DURATION": 1})


def strafe_right():
    return block("zsibot_strafe_right", {"VY": 2, "DURATION": 1})


def face_person():
    return block("zsibot_face")


# ---- gesture fallback (main loop; runs when no voice command this cycle) ----
def gesture_branch(label, do_blocks):
    return {"IF": {"block": block("logic_compare", {"OP": "EQ"}, {
        "A": {"block": var_get("id_g")},
        "B": text_shadow(label),
    })}, "DO": {"block": chain(do_blocks)}}


def gesture_fallback():
    inputs = {}
    br0 = gesture_branch("palm", [speak("Forward!"), move_forward()])
    inputs["IF0"] = br0["IF"]
    inputs["DO0"] = br0["DO"]
    for i, (label, blocks) in enumerate([
        ("thumbup", [speak("Standing up!"), block("zsibot_stand")]),
        ("thumbdown", [speak("Backing up!"), move_backward()]),
        ("point-left", [speak("Turning left!"), turn_left()]),
        ("point-right", [speak("Turning right!"), turn_right()]),
        ("victory", [speak("Strafing left!"), strafe_left()]),
        ("love", [speak("Strafing right!"), strafe_right()]),
        ("fist", [speak("Sitting down."), block("zsibot_lie")]),
        ("wave", [speak("Here I come!"), face_person()]),
    ], start=1):
        br = gesture_branch(label, blocks)
        inputs[f"IF{i}"] = br["IF"]
        inputs[f"DO{i}"] = br["DO"]
    # no gesture within the window → face the user briefly
    inputs["ELSE"] = {"block": chain([block("zsibot_toward", {"SECONDS": 2})])}
    gif = controls_if(9, True, inputs)
    set_g = var_set("id_g", block("zsibot_gesture", {"MAX": 4}))
    return chain([set_g, gif])


# ---- the when block: the voice listener ------------------------------------
def voice_branch(needles, do_blocks):
    """A controls_if branch: OR of find-ne-0 conditions on cmdUp."""
    cond = find_ne_0(var_get("id_cmdUp"), needles[0])
    for n in needles[1:]:
        cond = logic_or(cond, find_ne_0(var_get("id_cmdUp"), n))
    return {"IF": {"block": cond}, "DO": {"block": chain(do_blocks)}}


def voice_chain():
    """The chain under the when block: read the latest transcript, dispatch."""
    set_cmd = var_set("id_cmd", block("zsibot_lastHear"))
    set_cmd_up = var_set("id_cmdUp", block("text_changeCase", {"CASE": "UPPERCASE"},
                                           {"TEXT": {"block": var_get("id_cmd")}}))
    # strafe first (LEFT/RIGHT alone means turn)
    strafe_inner = controls_if(1, True, {
        "IF0": {"block": find_ne_0(var_get("id_cmdUp"), "LEFT")},
        "DO0": {"block": chain([speak("Strafing left!"), strafe_left(), set_bool("id_voiced", True)])},
        "ELSE": {"block": chain([speak("Strafing right!"), strafe_right(), set_bool("id_voiced", True)])},
    })
    branches = [
        voice_branch(["STRAFE", "SIDE"], [speak("Strafing!"), strafe_inner, set_bool("id_voiced", True)]),
        voice_branch(["FORWARD"], [speak("Going forward!"), move_forward(), set_bool("id_voiced", True)]),
        voice_branch(["BACK"], [speak("Backing up!"), move_backward(), set_bool("id_voiced", True)]),
        voice_branch(["LEFT"], [speak("Turning left!"), turn_left(), set_bool("id_voiced", True)]),
        voice_branch(["RIGHT"], [speak("Turning right!"), turn_right(), set_bool("id_voiced", True)]),
        voice_branch(["SIT", "DOWN", "STOP"], [speak("Sitting down."), block("zsibot_lie"), set_bool("id_voiced", True)]),
        voice_branch(["STAND", "UP"], [speak("Standing up!"), block("zsibot_stand"), set_bool("id_voiced", True)]),
        voice_branch(["COME", "LOOK", "FACE"], [speak("Here I come!"), face_person(), set_bool("id_voiced", True)]),
    ]
    inputs = {}
    for i, br in enumerate(branches):
        inputs[f"IF{i}"] = br["IF"]
        inputs[f"DO{i}"] = br["DO"]
    # no command matched → nothing (main loop's per-cycle voiced=false lets gestures run)
    big_if = controls_if(8, False, inputs)
    return chain([set_cmd, set_cmd_up, big_if])


# ---- main hat: gesture control + facing (gated by `voiced`) -----------------
def main_loop_body():
    # if no voice command ran this cycle → gesture control + face the user
    not_voiced = block("logic_compare", {"OP": "EQ"}, {
        "A": {"block": var_get("id_voiced")},
        "B": {"block": block("logic_boolean", {"BOOL": "FALSE"})},
    })
    gated = controls_if(1, False, {
        "IF0": {"block": not_voiced},
        "DO0": {"block": gesture_fallback()},
    })
    # reset the override flag at the END of the cycle so the when block (which
    # fires at boundaries, e.g. right before the `if` above) has had its say
    return chain([gated, set_bool("id_voiced", False)])


def build():
    # start hat: camera, intro, then the gesture/facing loop
    start = chain([
        block("zsibot_start"),
        block("zsibot_camera", {"CAMERA": "front"}),
        speak("I'm ready! Show a gesture or tell me what to do."),
        block("zsibot_forever", {}, {"DO": {"block": main_loop_body()}}),
    ])
    # when hat: fires when background STT hears NEW speech (consuming flag —
    # `new speech?` is true once per utterance), then dispatches the command.
    when = chain([
        block("zsibot_when", {}, {
            "COND": {"block": block("zsibot_newHear")},
        }),
        voice_chain(),
    ])
    skill = {**SKILL, "blocks": {"blocks": {"languageVersion": 0,
                                            "blocks": [start, when],
                                            "variables": VARS}}}
    return {"skill": skill}


if __name__ == "__main__":
    out = "sum_remote.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(build(), f, indent=2, ensure_ascii=False)
    print("wrote", out)
