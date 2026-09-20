from karaoke.lyrics_video import build_ass, build_visual_plan, higgsfield_commands


SYNTHETIC_LRC = """[00:05.00]<00:05.00>Hello <00:05.50>bright <00:06.00>world
[00:10.00]I feel happy today
[00:30.00]A tender return
"""


def test_visual_plan_includes_intro_vocals_and_instrumental():
    episodes = build_visual_plan(SYNTHETIC_LRC, 45.0, energy=0.8, brightness=0.2)
    kinds = {episode.kind for episode in episodes}
    assert {"intro", "vocals", "instrumental", "outro"} <= kinds
    assert all(0 < episode.duration_s <= 12.0 for episode in episodes)
    assert all(episode.bass_drive > episode.treble_drive for episode in episodes)
    assert all("no typography" in episode.prompt for episode in episodes)


def test_higgsfield_commands_deduplicate_reusable_visual_plates(tmp_path):
    episodes = build_visual_plan(SYNTHETIC_LRC, 45.0, energy=0.5, brightness=0.5)
    commands = higgsfield_commands(episodes, tmp_path)
    assert len(commands) < len(episodes)
    assert all(command[:4] == ["higgsfield", "generate", "create", "seedance_2_0"] for command in commands)
    assert all("--generate_audio" in command and "false" in command for command in commands)
    assert all("--resolution" in command and "720p" in command for command in commands)


def test_ass_uses_word_karaoke_tags_and_line_times():
    ass = build_ass(SYNTHETIC_LRC, duration_s=45.0)
    assert "Style: Karaoke" in ass
    assert "Dialogue: 0,0:00:05.00,0:00:10.00,Karaoke" in ass
    assert "{\\k50}Hello" in ass
    assert "{\\k50}bright" in ass
    assert "{\\k400}world" in ass
