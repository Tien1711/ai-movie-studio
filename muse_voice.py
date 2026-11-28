"""Voice profiles for speech generated in Muse videos."""

import json
from dataclasses import dataclass

from pydantic import BaseModel, Field

from consistency import DIALOGUE_SOURCES, resolve_scene_cast
NARRATOR_ROLE = "Off-screen narrator"
NARRATOR_ALIASES = {"narrator", "off-screen narrator", "người kể", "người kể chuyện", "thuyết minh"}


class MuseVoiceProfile(BaseModel):
    voice_id: str = Field(pattern=r"^avocado_v2:[A-Za-z0-9_-]+$", max_length=160)
    language: str = Field(default="vi", pattern=r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$", max_length=24)
    speed: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    delivery: str = Field(default="on_screen", pattern=r"^(?:" + "|".join(DIALOGUE_SOURCES) + r")$")


class MuseNarratorProfile(MuseVoiceProfile):
    description: str = Field(default="", max_length=600)


def narrator_text(scene, profile):
    return (scene.get("voiceover") or "").strip() if profile else ""


REMOTE_DELIVERY = {
    "offscreen": "off-screen voice from outside the frame",
    "radio": "two-way radio",
    "phone": "phone earpiece",
    "speaker": "public-address speaker",
}


def dialogue_turns(scene, registry):
    if not (scene.get("dialogue") or "").strip():
        return [
            {
                "character": (line.get("character") or line.get("speaker") or "").strip(),
                "text": (line.get("text") or "").strip(),
                "delivery": line.get("delivery"),
            }
            for line in scene.get("voice_segments") or [] if (line.get("text") or "").strip()
        ]
    cast, lines = resolve_scene_cast(scene, registry, require_tts_voice=False)
    defaults = {
        entry["name"]: (entry.get("muse_voice") or {}).get("delivery", "on_screen")
        for entry in cast
    }
    return [{**line, "delivery": line.get("delivery") or defaults[line["character"]]} for line in lines]


def remote_speaker_names(registry, scene):
    return set(plan_muse_speech(scene, registry, native=False).remote)


@dataclass(frozen=True)
class MuseSpeech:
    direction: str
    remote: frozenset[str]
    offscreen: bool

    def append_to(self, visual_prompt):
        return append_muse_speech(visual_prompt, self.direction)


def append_muse_speech(visual_prompt, direction):
    return f"{visual_prompt}\n\n{direction}" if direction else visual_prompt


def plan_muse_speech(scene, registry, narrator=None, *, native=True, strict=False, freeform=False):
    """One source of audio, speaking-order and lip-sync rules for every Muse video request."""
    registry = registry or []
    by_name = {
        entry["name"].casefold(): entry for entry in registry
        if entry.get("name") and entry.get("kind", "character") == "character"
    }
    by_alias = {
        alias.strip().casefold(): entry
        for entry in by_name.values()
        for alias in (entry["name"], entry.get("id"), *(entry.get("aliases") or []))
        if isinstance(alias, str) and alias.strip()
    }
    lines = []
    for line in dialogue_turns(scene, registry):
        person = by_alias.get(line["character"].casefold(), {})
        name = person.get("name") or line["character"] or "Unseen speaker"
        default_source = "offscreen" if scene.get("offscreen_narration") and not scene.get("dialogue") else "on_screen"
        delivery = line.get("delivery") or (person.get("muse_voice") or {}).get("delivery") or default_source
        if not line["character"]:
            delivery = "offscreen"
        if not person and name.casefold() in NARRATOR_ALIASES:
            name, delivery = NARRATOR_ROLE, "offscreen"
        if delivery not in DIALOGUE_SOURCES:
            raise ValueError(f"Nguồn thoại [{delivery}] không hợp lệ.")
        lines.append({**line, "character": name, "delivery": delivery})
    narration = "" if lines and not scene.get("dialogue") else (scene.get("voiceover") or "").strip()
    if not lines and narration and not scene.get("offscreen_narration"):
        speaker = scene.get("character") or (scene.get("speaker") if scene.get("speak_onscreen") else "")
        if isinstance(speaker, str) and speaker.strip() and speaker.casefold() not in NARRATOR_ALIASES:
            person = by_alias.get(speaker.strip().casefold(), {})
            delivery = (person.get("muse_voice") or {}).get("delivery", "on_screen")
            lines = [{"character": person.get("name", speaker), "delivery": delivery, "text": narration}]
            narration = ""
    if strict and native and not narrator:
        narration = ""
    remote = frozenset(line["character"] for line in lines if line["delivery"] in REMOTE_DELIVERY)
    visible = {line["character"] for line in lines if line["delivery"] not in REMOTE_DELIVERY}
    offscreen = not any(line["delivery"] == "on_screen" for line in lines)
    if not narration and not lines:
        if not freeform:
            return MuseSpeech("", remote, offscreen)
        guidance = [
            "MUSE SPEECH AND LIP SYNC — Speech is allowed only when requested in the prompt. "
            "Keep each line with its original speaker, wording, language and source, in order and without overlap. "
            "Only the visible person who is speaking moves their lips. "
            "During narration, thoughts, off-camera speech, or voices from devices, visible people react silently. "
            "If there is no requested speech, use location ambience and Foley only."
        ]
        for person in by_name.values():
            if saved := person.get("muse_voice"):
                voice = MuseVoiceProfile.model_validate(saved)
                guidance.append(_catalog_voice(person["name"], voice) + f" | default source: {voice.delivery}")
        return MuseSpeech("\n".join(guidance), remote, False)
    instructions = (
        "MUSE VOICE PROFILES — use exact catalog voice IDs when supplied. Keep each speaker's voice, "
        "language and base speed consistent across shots. "
        if native else "DUBBED PERFORMANCE — Follow the speech cues for visual acting. External dubbing provides the final voices. "
    )
    instructions += (
        "Keep footsteps, movement and location ambience under the voices where appropriate. "
        "Do not add intelligible background speech."
    )
    profiles, speech = [], []
    if native and (narration or any(line["character"] == NARRATOR_ROLE for line in lines)):
        voice = MuseNarratorProfile.model_validate(narrator) if narrator else None
        if voice:
            profiles.append(_catalog_voice(NARRATOR_ROLE, voice))
        description = (voice.description.strip() if voice else "") or "Calm, clear third-person delivery with stable timbre, accent and pauses."
        profiles.append(f"Narrator voice description: {description}")
    if narration:
        speech.extend([
            "THIRD-PERSON NARRATION — use a separate off-screen narrator. "
            "The narrator is never visible. On-screen characters never move their lips to the narration.",
        ])
        if native:
            if lines:
                speech.append("Read the narration before the dialogue below.")
            speech.append(f"Off-screen narrator: {json.dumps(narration, ensure_ascii=False)}")
    if lines:
        if native:
            for name in dict.fromkeys(line["character"] for line in lines):
                if name == NARRATOR_ROLE:
                    continue
                saved = by_name.get(name.casefold(), {}).get("muse_voice")
                if saved:
                    profiles.append(_catalog_voice(name, MuseVoiceProfile.model_validate(saved)))
                elif strict:
                    raise ValueError(f'Nhân vật {name} chưa khóa Voice ID trong mục “Ảnh & giọng Muse”.')
        speech.append(
            "SPOKEN DIALOGUE — preserve the exact words and speaking order. Do not overlap lines. "
            "After the scripted lines, continue with natural ambience and silent reaction. "
            "During an off-screen line, every visible person keeps their lips closed and only listens. "
            "Keep the speaker off camera. If needed, show the device or the listener from behind. "
            "On an on-screen line, only the named speaker moves their lips. "
            "When the speaker or sound source changes, use a cut to the new speaker, the device or a silent reaction. "
            "Follow these instructions if earlier visual notes disagree about who is speaking."
        )
        for line in lines:
            name = line["character"]
            channel = REMOTE_DELIVERY.get(line["delivery"])
            words = (": " + json.dumps(line["text"], ensure_ascii=False)) if native or line["delivery"] == "on_screen" else ""
            if channel:
                speech.append(f"[OFF-SCREEN · {channel} ONLY · ALL VISIBLE LIPS CLOSED] {name}{words}")
            elif line["delivery"] == "thought":
                speech.append(f"[INNER MONOLOGUE · ALL VISIBLE LIPS CLOSED] {name}{words}")
            else:
                speech.append(f"[ON-SCREEN · ONLY THIS SPEAKER MOVES THEIR LIPS] {name}{words}")
    return MuseSpeech("\n".join([instructions, *profiles, *speech]), remote - visible, offscreen)


def _catalog_voice(name, voice):
    return f"{name} | voice_id: {voice.voice_id} | language: {voice.language} | speed: {voice.speed}"


def scene_voice_prompt(scene, registry, narrator=None):
    return plan_muse_speech(scene, registry, narrator, strict=True).direction


def voice_turn_blocks(scene, registry, narrator=None):
    """Group consecutive lines that have the same speaker and source."""
    narration = narrator_text(scene, narrator)
    blocks = [{"voiceover": narration, "dialogue": [], "delivery": "narrator", "speaker": ""}] if narration else []
    for line in dialogue_turns({**scene, "voice_segments": []}, registry):
        key = (line["character"], line["delivery"])
        if not blocks or (blocks[-1]["speaker"], blocks[-1]["delivery"]) != key:
            blocks.append({"voiceover": "", "dialogue": [], "delivery": key[1], "speaker": key[0]})
        blocks[-1]["dialogue"].append(f"{key[0]} [{key[1]}]: {line['text']}")
    return blocks
